"""Domain contracts, deep immutability and clean-process import boundaries."""

import copy
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from openchatbi.semantic.layer import SemanticLayer
from openchatbi.semantic.models import BusinessRule, JoinEdge, JoinKey, MetricSpec, SemanticBundle
from openchatbi.semantic.ports import SemanticProvider
from scripts.validate_semantic_data import load_catalog, read_yaml

if TYPE_CHECKING:

    def _provider_contract(layer: SemanticLayer) -> SemanticProvider:
        return layer


def test_migrated_records_fit_domain_models():
    data = Path(__file__).resolve().parents[2] / "example/semantic"
    metrics = [MetricSpec.model_validate(row) for row in read_yaml(data / "metrics.yaml")["metrics"]]
    rules = [BusinessRule.model_validate(row) for row in read_yaml(data / "rules.yaml")["rules"]]
    edges = [JoinEdge.model_validate(row) for row in read_yaml(data / "joins.yaml")["edges"]]
    bundle = SemanticBundle(
        metrics={m.name: m for m in metrics},
        rules=tuple(rules),
        edges=tuple(edges),
        tables=frozenset(load_catalog(data / "catalog")),
    )
    assert (len(bundle.metrics), len(bundle.rules), len(bundle.edges)) == (13, 8, 6)
    assert all(row.provenance for row in (*metrics, *rules, *edges))
    assert SemanticBundle.model_validate_json(bundle.model_dump_json()) == bundle
    assert bundle.model_copy(deep=True) == bundle


@pytest.mark.parametrize("level", ["atomic", "derived", "composite"])
def test_missing_expression_rejected(level):
    with pytest.raises(ValidationError, match="needs formula"):
        MetricSpec(name="sample", label="Sample", level=level)


def test_multistep_is_explicit_and_cannot_replace_atomic_expression():
    assert MetricSpec(name="sample", label="Sample", level="composite", requires_multi_step=True).requires_multi_step
    with pytest.raises(ValidationError, match="atomic metric"):
        MetricSpec(name="sample", label="Sample", level="atomic", requires_multi_step=True)
    with pytest.raises(ValidationError, match="choose"):
        MetricSpec(name="sample", label="Sample", level="atomic", formula="1", sql_template="SELECT 1")


@pytest.mark.parametrize("operation", ["set", "delete", "update", "clear", "pop", "popitem", "setdefault", "ior"])
def test_bundle_metrics_cannot_be_mutated(operation):
    metric = MetricSpec(name="sample", label="Sample", level="atomic", formula="1")
    original = {metric.name: metric}
    bundle = SemanticBundle(metrics=original, rules=(), edges=(), tables=frozenset())
    original.clear()
    assert bundle.metrics == {"sample": metric}
    with pytest.raises(TypeError, match="read-only"):
        if operation == "set":
            bundle.metrics["other"] = metric
        elif operation == "delete":
            del bundle.metrics["sample"]
        elif operation == "ior":
            bundle.metrics |= {"other": metric}
        elif operation == "update":
            bundle.metrics.update({"other": metric})
        elif operation == "pop":
            bundle.metrics.pop("sample")
        elif operation == "setdefault":
            bundle.metrics.setdefault("other", metric)
        else:
            getattr(bundle.metrics, operation)()
    assert copy.deepcopy(bundle).metrics == {"sample": metric}
    with pytest.raises(ValidationError, match="frozen"):
        metric.label = "changed"
    with pytest.raises(ValidationError, match="frozen"):
        bundle.rules = ()


def test_empty_join_keys_rejected():
    with pytest.raises(ValidationError, match="keys"):
        JoinEdge(child="first", parent="second", keys=())
    key = JoinKey(left="id", right="id")
    with pytest.raises(ValidationError, match="frozen"):
        key.left = "changed"


@pytest.mark.parametrize(
    "module",
    [
        "openchatbi.semantic",
        "openchatbi.semantic.models",
        "openchatbi.semantic.ports",
        "openchatbi.semantic.layer",
        "openchatbi.semantic.closure",
        "openchatbi.semantic.render",
        "openchatbi.semantic.join",
        "openchatbi.semantic.sources",
        "openchatbi.semantic.sources.catalog_fk",
        "openchatbi.semantic.sources.yaml_file",
        "openchatbi.semantic.config",
        "openchatbi.semantic.loader",
        "openchatbi.semantic.rules",
        "openchatbi.semantic.sources.json_file",
        "openchatbi.semantic.integration",
    ],
)
def test_import_has_no_application_io_or_service_imports(module, tmp_path):
    root = str(Path(__file__).resolve().parents[2])
    code = f"""
import sys
sys.path.insert(0, {root!r})
import builtins
import pathlib
import socket
import pydantic
def forbidden(*args, **kwargs):
    raise AssertionError('application I/O during semantic import')
builtins.open = forbidden
pathlib.Path.read_text = forbidden
pathlib.Path.read_bytes = forbidden
socket.socket = forbidden
__import__({module!r})
for name in sys.modules:
    assert not name.startswith(('openchatbi.catalog', 'openchatbi.config_loader', 'openchatbi.llm',
                                'chromadb', 'langchain', 'openai')), name
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
