"""New directed planning capability, with independent structural and SQLite checks."""

import socket
import sqlite3
from itertools import permutations
from pathlib import Path
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine

from openchatbi.semantic.join import JoinConflictError, JoinGraph, merge_edges, to_sql_clause
from openchatbi.semantic.models import JoinEdge, JoinKey, JoinPlan
from openchatbi.semantic.sources import SemanticSourceError
from openchatbi.semantic.sources.catalog_fk import ForeignKeyJoinSource
from openchatbi.semantic.sources.yaml_file import YamlJoinSource
from scripts.validate_semantic_data import read_text, read_yaml

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "example/semantic"


def edge(child, parent, *, join_type="inner", source="declared", left="id", right="id", **kwargs):
    return JoinEdge(
        child=child,
        parent=parent,
        keys=(JoinKey(left=left, right=right),),
        join_type=join_type,
        source=source,
        **kwargs,
    )


@pytest.fixture
def demo_graph():
    edges = YamlJoinSource(DATA / "joins.yaml").load_edges()
    return JoinGraph(edges, tables=("finance_expenses",))


def test_twelve_hand_labelled_join_structures_execute(demo_graph):
    fixture = read_yaml(Path(__file__).parent / "fixtures/join_expectations.yaml")
    assert len(fixture["cases"]) == 12
    with sqlite3.connect(":memory:") as db:
        db.executescript(read_text(DATA / "catalog/schema.sql"))
        for case in fixture["cases"]:
            plan = demo_graph.plan_join(case["anchor"], case["targets"])
            actual = [[e.child, e.parent, e.join_type, [[k.left, k.right] for k in e.keys]] for e in plan.steps]
            expected = [fixture["edge_definitions"][key] for key in case["expected"]]
            assert actual == expected, case["question"]
            assert plan.unreachable == ()
            db.execute("SELECT COUNT(*) " + to_sql_clause(plan)).fetchone()
            assert demo_graph.plan_join(case["anchor"], case["targets"]).model_dump_json() == plan.model_dump_json()


def test_composite_on_matches_handwritten_sql(demo_graph):
    plan = demo_graph.plan_join("sales_orders", ["exchange_rates"])
    assert to_sql_clause(plan) == (
        "FROM sales_orders\nLEFT JOIN exchange_rates ON sales_orders.order_date = exchange_rates.rate_date\n"
        "    AND sales_orders.currency = exchange_rates.currency"
    )
    with sqlite3.connect(":memory:") as db:
        db.executescript(read_text(DATA / "catalog/schema.sql"))
        db.execute("INSERT INTO exchange_rates VALUES ('2026-05-15', 'CNY', 1)")
        assert db.execute("SELECT COUNT(*) " + to_sql_clause(plan)).fetchone() == (10,)


def test_left_join_preserves_unmatched_anchor_rows(demo_graph):
    with sqlite3.connect(":memory:") as db:
        db.executescript(read_text(DATA / "catalog/schema.sql"))
        db.execute("INSERT INTO dim_customers VALUES (99, 'unmatched', 'test', 'test', 'test', 'test')")
        plan = demo_graph.plan_join("dim_customers", ["exchange_rates"])
        assert db.execute(
            "SELECT COUNT(*) " + to_sql_clause(plan) + " WHERE dim_customers.customer_id = 99"
        ).fetchone() == (1,)


def test_anchor_and_output_ignore_input_permutations():
    edges = [
        edge("fact", "dim", relationship="many_to_one"),
        edge("dim", "fact", join_type="left", relationship="one_to_many"),
        edge("fact", "rates", join_type="left"),
    ]
    expected = None
    for ordering in permutations(edges):
        graph = JoinGraph(ordering)
        assert graph.select_anchor(["rates", "dim", "fact"]) == "fact"
        for targets in permutations(["rates", "dim", "fact"]):
            plan = graph.plan_join("", targets)
            assert plan.anchor == "fact"
            if expected is None:
                expected = plan.model_dump_json()
            assert plan.model_dump_json() == expected


def test_inner_branches_are_planned_before_left_and_bridges_are_added():
    graph = JoinGraph([edge("root", "optional", join_type="left"), edge("root", "bridge"), edge("bridge", "target")])
    plan = graph.plan_join("root", ["optional", "target"])
    assert [e.parent for e in plan.steps] == ["bridge", "target", "optional"]
    assert "JOIN bridge" in to_sql_clause(plan)


def test_unreachable_is_explained_without_cross_join(demo_graph):
    plan = demo_graph.plan_join("sales_orders", ["finance_expenses", "absent", "dim_products"])
    assert plan.unreachable == ("absent", "finance_expenses")
    assert "finance_expenses" in plan.reason
    assert "finance_expenses" not in to_sql_clause(plan)
    assert "CROSS" not in to_sql_clause(plan)


def test_left_direction_is_not_invented_and_inner_after_left_is_not_reordered():
    graph = JoinGraph([edge("root", "optional", join_type="left"), edge("optional", "target")])
    plan = graph.plan_join("root", ["target", "optional"])
    assert plan.unreachable == ("target",)
    assert [e.parent for e in plan.steps] == ["optional"]
    assert graph.plan_join("optional", ["root"]).unreachable == ("root",)


def test_cycle_and_diamond_do_not_repeat_joined_tables():
    graph = JoinGraph([edge("a", "b"), edge("a", "c"), edge("b", "d"), edge("c", "d"), edge("d", "a")])
    plan = graph.plan_join("a", ["d", "c"])
    assert len({e.parent for e in plan.steps}) == len(plan.steps) == 3
    to_sql_clause(plan)


def test_empty_unknown_and_isolated_anchors():
    graph = JoinGraph([], tables=["isolated"])
    assert graph.plan_join("isolated", ["isolated"]).unreachable == ()
    assert to_sql_clause(graph.plan_join("", [])) == ""
    assert graph.plan_join("missing", ["isolated"]).unreachable == ("isolated", "missing")
    assert to_sql_clause(graph.plan_join("missing", ["isolated"])) == ""
    assert graph.plan_join("", ["missing"]).unreachable == ("missing",)


def test_declared_composite_edge_overrides_fk_hint():
    fk = edge("a", "b", source="foreign_key", join_type="left")
    declared = JoinEdge(child="a", parent="b", keys=(JoinKey(left="id", right="id"), JoinKey(left="day", right="day")))
    assert merge_edges([fk], [declared]) == (declared,)
    assert merge_edges([declared], [fk, fk]) == (declared,)


def test_ambiguous_roles_and_self_join_are_observable():
    with pytest.raises(JoinConflictError, match="first.yaml.*second.yaml"):
        merge_edges(
            [
                edge("a", "b", left="owner_id", provenance="first.yaml"),
                edge("a", "b", left="reviewer_id", provenance="second.yaml"),
            ]
        )
    with pytest.raises(JoinConflictError, match="aliases"):
        JoinGraph([edge("a", "a")])


@pytest.mark.parametrize(
    "steps",
    [
        (edge("not_joined", "new"),),
        (edge("root", "root"),),
        (edge("root", "optional", join_type="left"), edge("optional", "new")),
    ],
)
def test_invalid_external_plans_cannot_render_invalid_sql(steps):
    with pytest.raises(ValueError):
        to_sql_clause(JoinPlan(anchor="root", steps=steps))


def test_unsupported_identifier_fails_instead_of_interpolating_sql():
    with pytest.raises(ValueError, match="identifier"):
        to_sql_clause(JoinPlan(anchor="unsafe; SELECT 1", steps=()))


def test_yaml_paths_are_relative_to_config_or_repo(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert len(YamlJoinSource("example/semantic/joins.yaml").load_edges()) == 6
    source = tmp_path / "joins.yaml"
    source.write_text("version: 1\nedges: []\n", encoding="utf-8")
    assert YamlJoinSource("joins.yaml", config_path=tmp_path / "config.yaml").load_edges() == ()


@pytest.mark.parametrize(
    "content",
    [
        b"\xef\xbb\xbfversion: 1",
        b"\xff",
        b"version: [",
        b"version: 2\nedges: []",
        b"version: 1\nedges: {}",
        b"version: 1\nedges: [{}]",
    ],
)
def test_yaml_errors_include_file_and_location(tmp_path, content):
    path = tmp_path / "bad.yaml"
    path.write_bytes(content)
    with pytest.raises(SemanticSourceError, match=r"bad.yaml \["):
        YamlJoinSource(path).load_edges()


def test_source_constructors_do_not_read_catalog_or_file(tmp_path):
    catalog = Mock()
    ForeignKeyJoinSource(catalog)
    catalog.get_table_list.assert_not_called()
    catalog.get_sql_engine.assert_not_called()
    source = YamlJoinSource(tmp_path / "absent.yaml")
    with pytest.raises(SemanticSourceError, match="absent.yaml"):
        source.load_edges()


def test_fk_source_preserves_composite_schema_and_error_context():
    catalog, reader = Mock(), Mock()
    catalog.get_table_list.return_value = ["main.child"]
    reader.get_foreign_keys.return_value = [
        {
            "name": "fk_test",
            "referred_schema": "other",
            "referred_table": "parent",
            "constrained_columns": ["x", "y"],
            "referred_columns": ["a", "b"],
        }
    ]
    result = ForeignKeyJoinSource(catalog, reader=reader).load_edges()
    reader.get_foreign_keys.assert_called_once_with("child")
    assert result[0].parent == "other.parent"
    assert [(k.left, k.right) for k in result[0].keys] == [("x", "a"), ("y", "b")]
    assert result[0].source == "foreign_key"
    catalog.get_sql_engine.assert_not_called()
    reader.get_foreign_keys.return_value[0]["referred_columns"] = ["a"]
    with pytest.raises(SemanticSourceError, match=r"main.child.*foreign_keys:0.*lengths"):
        ForeignKeyJoinSource(catalog, reader=reader).load_edges()


def test_fk_reader_failure_is_not_treated_as_no_edges():
    catalog, reader = Mock(), Mock()
    catalog.get_table_list.return_value = ["child"]
    reader.get_foreign_keys.side_effect = RuntimeError("metadata unavailable")
    with pytest.raises(SemanticSourceError, match="child.*metadata unavailable"):
        ForeignKeyJoinSource(catalog, reader=reader).load_edges()


def test_real_composite_foreign_key_is_not_truncated():
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.begin() as conn:
            conn.exec_driver_sql("CREATE TABLE parent (id INT, day INT, PRIMARY KEY(id, day))")
            conn.exec_driver_sql(
                "CREATE TABLE child (parent_id INT, day INT, FOREIGN KEY(parent_id, day) REFERENCES parent(id, day))"
            )
        catalog = Mock()
        catalog.get_table_list.return_value = ["child", "parent"]
        catalog.get_sql_engine.return_value = engine
        edges = ForeignKeyJoinSource(catalog).load_edges()
        assert len(edges) == 1
        assert [(key.left, key.right) for key in edges[0].keys] == [("parent_id", "id"), ("day", "day")]
    finally:
        engine.dispose()


def test_upstream_reflection_failure_remains_visible_in_log(caplog):
    engine = create_engine("sqlite:///:memory:")
    try:
        catalog = Mock()
        catalog.get_table_list.return_value = ["absent"]
        catalog.get_sql_engine.return_value = engine
        assert ForeignKeyJoinSource(catalog).load_edges() == ()
        assert "Failed to get foreign keys for table absent" in caplog.text
    finally:
        engine.dispose()


def test_yaml_typo_does_not_silently_select_default_join_type(tmp_path):
    file = tmp_path / "typo.yaml"
    file.write_text(
        "version: 1\nedges:\n- child: a\n  parent: b\n  keys: [{left: id, right: id}]\n  join_typ: inner\n",
        encoding="utf-8",
    )
    with pytest.raises(SemanticSourceError, match="edges:0.*join_typ"):
        YamlJoinSource(file).load_edges()


def test_real_upstream_fk_loader_merges_with_yaml_without_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("unexpected network")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    engine = create_engine("sqlite:///:memory:")
    try:
        with engine.connect() as connection:
            raw = connection.connection.driver_connection
            raw.executescript(read_text(DATA / "catalog/schema.sql"))
        catalog = Mock()
        catalog.get_sql_engine.return_value = engine
        catalog.get_table_list.return_value = [
            "sales_orders",
            "dim_customers",
            "dim_products",
            "exchange_rates",
            "finance_expenses",
        ]
        reflected = ForeignKeyJoinSource(catalog).load_edges()
        assert len(reflected) == 2
        assert all(e.source == "foreign_key" for e in reflected)
        declared = YamlJoinSource(DATA / "joins.yaml").load_edges()
        merged = merge_edges(reflected, declared)
        assert len(merged) == 6
        plan = JoinGraph(merged).plan_join("sales_orders", ["dim_products", "exchange_rates"])
        assert [e.join_type for e in plan.steps] == ["inner", "left"]
        with engine.connect() as connection:
            assert connection.exec_driver_sql("SELECT COUNT(*) " + to_sql_clause(plan)).scalar() == 10
    finally:
        engine.dispose()
