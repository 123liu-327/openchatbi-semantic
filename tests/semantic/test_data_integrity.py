"""Offline migration contracts, corrupt-data rejection and real catalog compatibility."""

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.import_chatbi_mvp_kb import dump_yaml, literal_constant, migrate
from scripts.validate_semantic_data import SemanticDataError, load_catalog, read_text, read_yaml, validate_directory

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "example/semantic"


@pytest.fixture
def data(tmp_path):
    out = tmp_path / "semantic"
    shutil.copytree(DATA, out)
    return out


def test_checked_in_data_passes_strict_validation():
    report = validate_directory(DATA, strict=True)
    assert report.counts == {"metrics": 13, "rules": 8, "edges": 6}
    assert report.warnings == []
    assert read_yaml(DATA / "_unresolved.yaml")["unresolved"] == []


@pytest.mark.parametrize("file", ["metrics.yaml", "rules.yaml", "joins.yaml", "_unresolved.yaml", "migration.yaml"])
def test_data_is_utf8_without_bom(file):
    assert read_yaml(DATA / file)["version"] == 1


@pytest.mark.parametrize("content", [b"\xef\xbb\xbfversion: 1", b"\xff\xfe", b"\x80"])
def test_invalid_encoding_reports_path(tmp_path, content):
    path = tmp_path / "bad.yaml"
    path.write_bytes(content)
    with pytest.raises(SemanticDataError, match="bad.yaml.*encoding"):
        read_yaml(path)


@pytest.mark.parametrize(
    ("filename", "section", "index", "field", "value", "error"),
    [
        ("metrics.yaml", "metrics", 0, "name", "Not snake case", "name"),
        ("metrics.yaml", "metrics", 1, "name", "revenue", "duplicate name"),
        ("metrics.yaml", "metrics", 1, "aliases", ["销售成本", "收入"], "conflicts"),
        ("metrics.yaml", "metrics", 0, "aliases", ["revenue"], "include"),
        ("metrics.yaml", "metrics", 0, "aliases", "收入", "list"),
        ("metrics.yaml", "metrics", 0, "level", "invented", "level"),
        ("metrics.yaml", "metrics", 0, "requires_multi_step", "true", "boolean"),
        ("metrics.yaml", "metrics", 0, "sql_template", None, "expression"),
        ("metrics.yaml", "metrics", 0, "formula", "SUM(net_amount)", "choose"),
        ("metrics.yaml", "metrics", 0, "source_tables", ["missing"], "unknown table"),
        ("metrics.yaml", "metrics", 0, "depends_on", ["missing"], "unknown metric"),
        ("metrics.yaml", "metrics", 0, "depends_on", ["revenue"], "cycle"),
        ("metrics.yaml", "metrics", 0, "time_field", "sales_orders.missing", "column"),
        ("rules.yaml", "rules", 0, "trigger_any", [], "trigger"),
        ("rules.yaml", "rules", 0, "require_tables", ["missing"], "table"),
        ("rules.yaml", "rules", 0, "scope_tables", ["missing"], "table"),
        ("rules.yaml", "rules", 0, "require_columns", ["sales_orders.missing"], "column"),
        ("rules.yaml", "rules", 0, "forbid_columns", ["sales_orders.net_amount"], "required and forbidden"),
        ("joins.yaml", "edges", 0, "parent", "missing", "table"),
        ("joins.yaml", "edges", 0, "keys", [], "key pair"),
        ("joins.yaml", "edges", 0, "keys", [{"left": "missing", "right": "customer_id"}], "column"),
        ("joins.yaml", "edges", 0, "join_type", "cross", "join_type"),
        ("joins.yaml", "edges", 0, "relationship", "many_to_many", "relationship"),
        ("joins.yaml", "edges", 0, "source", "invented", "source"),
        ("metrics.yaml", "metrics", 0, "sql_template", "SELECT missing FROM sales_orders", "sql_template"),
        ("metrics.yaml", "metrics", 0, "sql_template", "DELETE FROM sales_orders", "SELECT"),
    ],
)
def test_invalid_record_has_file_and_record_context(data, filename, section, index, field, value, error):
    path = data / filename
    document = read_yaml(path)
    document[section][index][field] = value
    dump_yaml(path, document)
    with pytest.raises(SemanticDataError, match=error) as caught:
        validate_directory(data, strict=True)
    assert filename in str(caught.value)
    assert f"[{index}]" in str(caught.value) or "[revenue]" in str(caught.value)


def test_non_strict_reference_failure_is_observable(data):
    path = data / "metrics.yaml"
    document = read_yaml(path)
    document["metrics"][0]["depends_on"] = ["missing"]
    document["metrics"][0].pop("provenance")
    dump_yaml(path, document)
    report = validate_directory(data, strict=False)
    assert len(report.warnings) == 2
    assert any("unknown metric" in warning for warning in report.warnings)
    assert any("provenance" in warning for warning in report.warnings)


def test_demo_schema_rows_and_metric_results():
    metrics = read_yaml(DATA / "metrics.yaml")["metrics"]
    with sqlite3.connect(":memory:") as db:
        db.executescript(read_text(DATA / "catalog/schema.sql"))
        catalog = load_catalog(DATA / "catalog")
        assert len(catalog) == 5
        assert sum(map(len, catalog.values())) == 40
        assert sum(db.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] for table in catalog) == 30
        results = {row["name"]: db.execute(row["sql_template"]).fetchall() for row in metrics if row["sql_template"]}
        assert len(results) == 10
        assert results["order_count"] == [(8,)]
        assert results["total_quantity"] == [(98,)]
        # Independently sum completed-order amounts by date/currency from the seed.
        expected_revenue = 2_820_000 * 7.18 + 1_230_000 * 7.82 + 4_240_000 + 1_500_000 * 7.16 + 2_820_000 * 7.79
        assert results["revenue"][0][0] == pytest.approx(expected_revenue)
        assert results["gross_profit"][0][0] == pytest.approx(results["revenue"][0][0] - results["cogs"][0][0])
        # Reapplying demo data is idempotent.
        db.executescript(read_text(DATA / "catalog/schema.sql"))
        assert db.execute("SELECT COUNT(*) FROM sales_orders").fetchone() == (10,)


def test_catalog_works_with_existing_store():
    from openchatbi.catalog.store.file_system import FileSystemCatalogStore

    store = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    assert len(store.get_table_list()) == 5
    assert sum(len(store.get_column_list(table)) for table in store.get_table_list()) == 40
    expected_names = set().union(*load_catalog(DATA / "catalog").values())
    assert {column["column_name"] for column in store.get_column_list()} == expected_names
    assert {column["category"] for column in store.get_column_list()} == {"dimension", "metric"}
    assert all(store.get_table_information(table)["description"] for table in store.get_table_list())
    assert store.get_sql_examples() == []


def test_composite_keys_and_direction_are_preserved():
    edges = read_yaml(DATA / "joins.yaml")["edges"]
    exchange = next(row for row in edges if row["child"] == "sales_orders" and row["parent"] == "exchange_rates")
    assert exchange["keys"] == [{"left": "order_date", "right": "rate_date"}, {"left": "currency", "right": "currency"}]
    assert exchange["join_type"] == "left"
    assert exchange["relationship"] == "many_to_one"
    reverse = next(row for row in edges if row["child"] == "exchange_rates")
    assert reverse["relationship"] == "one_to_many"


def test_explicit_multi_step_and_preserved_legacy_conflicts():
    rows = read_yaml(DATA / "metrics.yaml")["metrics"]
    assert {row["name"] for row in rows if row["requires_multi_step"]} == {
        "profit",
        "rd_expense_ratio",
        "selling_expense_ratio",
    }
    assert all(not row["sql_template"] for row in rows if row["requires_multi_step"])
    report = read_yaml(DATA / "_unresolved.yaml")
    assert len(report["legacy_conflicts"]) == 5
    assert len(report["conversions"]) == 13
    assert set(report["excluded_tables"]).isdisjoint(load_catalog(DATA / "catalog"))


def test_literal_extraction_does_not_execute_source(tmp_path):
    file = tmp_path / "source.py"
    file.write_text("raise RuntimeError('must not execute')\nVALUE = {'answer': [1, 2]}\n", encoding="utf-8")
    assert literal_constant(file, "VALUE") == {"answer": [1, 2]}
    file.write_text("VALUE = __import__('os').getcwd()", encoding="utf-8")
    with pytest.raises(SemanticDataError, match="AST literal"):
        literal_constant(file, "VALUE")


@pytest.fixture
def synthetic_source(tmp_path):
    source = tmp_path / "source"
    (source / "database").mkdir(parents=True)
    (source / "database/init.sql").write_text(
        "CREATE TABLE IF NOT EXISTS events (\n    id INT NOT NULL,\n    PRIMARY KEY (id)\n) ENGINE=InnoDB;\n"
        "INSERT INTO events (id) VALUES (1) ON DUPLICATE KEY UPDATE id=VALUES(id);\n",
        encoding="utf-8",
    )
    metric = {
        "name": "计数",
        "level": "原子",
        "aliases": [],
        "definition": "事件数量",
        "formula": "COUNT(*)",
        "sql_template": "SELECT COUNT(*) FROM events",
        "data_source": "events",
    }
    (source / "indicators_full.json").write_text(json.dumps({"indicators": [metric]}), encoding="utf-8")
    (source / "field_matcher.py").write_text(
        "raise RuntimeError('not imported')\nBUSINESS_RULES = []", encoding="utf-8"
    )
    (source / "join_resolver.py").write_text(
        "raise RuntimeError('not imported')\nTABLE_RELATIONSHIPS = {}", encoding="utf-8"
    )
    mapping = tmp_path / "mapping.yaml"
    dump_yaml(
        mapping,
        {
            "version": 1,
            "levels": {"原子": "atomic"},
            "metric_names": {"计数": "event_count"},
            "multi_step_metrics": [],
            "rule_names": [],
            "include_tables": ["events"],
            "exclude_tables": [],
        },
    )
    return source, mapping


def test_migration_is_static_deterministic_and_portable(synthetic_source, tmp_path, monkeypatch):
    source, mapping = synthetic_source
    out = tmp_path / "out"
    monkeypatch.chdir(tmp_path)
    assert migrate(source, out, mapping) == {"metrics": 1, "rules": 0, "edges": 0}
    before = {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}
    migrate(source, out, mapping)
    assert before == {p.relative_to(out): p.read_bytes() for p in out.rglob("*") if p.is_file()}


def test_unmapped_metric_is_preserved_in_report(synthetic_source, tmp_path):
    source, mapping = synthetic_source
    document = read_yaml(mapping)
    document["metric_names"] = {}
    dump_yaml(mapping, document)
    out = tmp_path / "out"
    with pytest.raises(SemanticDataError, match="unresolved"):
        migrate(source, out, mapping)
    assert read_yaml(out / "_unresolved.yaml")["unresolved"][0]["record"]["name"] == "计数"


def test_cli_resolves_default_data_from_outside_repo(tmp_path):
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/validate_semantic_data.py"), "--strict"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "0 errors" in result.stdout


def test_validation_needs_no_network_or_api_keys(monkeypatch):
    def reject_connection(*args, **kwargs):
        raise AssertionError("network access during offline validation")

    monkeypatch.setattr(socket.socket, "connect", reject_connection)
    for key in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    assert validate_directory(DATA, strict=True).warnings == []


def test_missing_source_field_has_record_context(synthetic_source, tmp_path):
    source, mapping = synthetic_source
    file = source / "indicators_full.json"
    document = json.loads(read_text(file))
    del document["indicators"][0]["sql_template"]
    file.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(SemanticDataError, match=r"indicators_full.json \[0\] sql_template"):
        migrate(source, tmp_path / "out", mapping)


def test_all_unknown_references_are_written_to_report(synthetic_source, tmp_path):
    source, mapping = synthetic_source
    file = source / "indicators_full.json"
    document = json.loads(read_text(file))
    document["indicators"][0]["depends_on"] = ["unknown_one", "unknown_two"]
    file.write_text(json.dumps(document), encoding="utf-8")
    out = tmp_path / "out"
    with pytest.raises(SemanticDataError, match="unresolved"):
        migrate(source, out, mapping)
    report = read_yaml(out / "_unresolved.yaml")
    reasons = "\n".join(row["reason"] for row in report["unresolved"])
    assert "unknown_one" in reasons and "unknown_two" in reasons
