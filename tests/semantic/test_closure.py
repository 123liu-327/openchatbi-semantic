"""Dependency expansion, including shared-subgraph depth and real migration data."""

from pathlib import Path

import pytest

from openchatbi.semantic.closure import MetricCycleError, MetricDepthError, UnknownMetricError, expand_dependencies
from openchatbi.semantic.models import MetricSpec
from scripts.validate_semantic_data import read_yaml


def metric(name, dependencies=()):
    return MetricSpec(name=name, label=name, level="derived", formula="1", depends_on=dependencies)


def test_ten_manually_labelled_closures():
    root = Path(__file__).resolve().parents[2]
    rows = read_yaml(root / "example/semantic/metrics.yaml")["metrics"]
    metrics = {row["name"]: MetricSpec.model_validate(row) for row in rows}
    cases = read_yaml(Path(__file__).parent / "fixtures/closure_expectations.yaml")["cases"]
    assert len(cases) == 10
    for name, expected in cases.items():
        actual = [m.name for m in expand_dependencies(metrics, [name])]
        assert actual == expected, name


def test_multiple_roots_and_diamond_preserve_order_without_duplicates():
    metrics = {
        m.name: m
        for m in (
            metric("first", ("second", "third")),
            metric("second", ("last",)),
            metric("third", ("last",)),
            metric("last"),
        )
    }
    result = expand_dependencies(metrics, ["first", "second", "first"])
    assert [m.name for m in result] == ["first", "second", "last", "third"]
    assert expand_dependencies(metrics, ["first", "second", "first"]) == result


@pytest.mark.parametrize("dependencies", [("first",), ("second",)])
def test_cycle_includes_dependency_path(dependencies):
    metrics = {"first": metric("first", dependencies), "second": metric("second", ("first",))}
    with pytest.raises(MetricCycleError) as caught:
        expand_dependencies(metrics, ["first"])
    assert caught.value.path[0] == caught.value.path[-1] == "first"


def test_depth_counts_edges_and_checks_previously_emitted_subgraphs():
    metrics = {m.name: m for m in (metric("first", ("second",)), metric("second", ("last",)), metric("last"))}
    assert len(expand_dependencies(metrics, ["first"], max_depth=2)) == 3
    with pytest.raises(MetricDepthError) as caught:
        expand_dependencies(metrics, ["second", "first"], max_depth=1)
    assert caught.value.path == ("first", "second", "last")
    assert caught.value.max_depth == 1


def test_root_only_and_empty_closures():
    assert expand_dependencies({}, []) == ()
    assert expand_dependencies({"first": metric("first")}, ["first"], max_depth=0)[0].name == "first"
    with pytest.raises(ValueError, match="nonnegative"):
        expand_dependencies({}, [], max_depth=-1)


@pytest.mark.parametrize("roots", [["missing"], ["first"]])
def test_unknown_references_are_not_silently_dropped(roots):
    with pytest.raises(UnknownMetricError, match="missing") as caught:
        expand_dependencies({"first": metric("first", ("missing",))}, roots)
    assert caught.value.path[-1] == "missing"
