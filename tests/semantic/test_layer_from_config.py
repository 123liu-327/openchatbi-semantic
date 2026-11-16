import builtins
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from openchatbi.catalog.store.file_system import FileSystemCatalogStore
from openchatbi.semantic.config import SemanticConfig, SemanticSourceConfig
from openchatbi.semantic.layer import SemanticLayer
from openchatbi.semantic.loader import SemanticDataError
from openchatbi.semantic.sources import SemanticSourceError
from scripts.import_chatbi_mvp_kb import dump_yaml
from scripts.validate_semantic_data import read_yaml

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "example/semantic"


def demo_layer():
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    cfg = {
        "semantic": {
            "enabled": True,
            "strict": True,
            "sources": [
                {"type": "yaml", "path": str(DATA / name)} for name in ("metrics.yaml", "rules.yaml", "joins.yaml")
            ],
        }
    }
    return SemanticLayer.from_config(cfg, catalog)


def test_disabled_layer_performs_no_io(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("disabled provider performed I/O")

    monkeypatch.setattr(Path, "read_text", fail)
    monkeypatch.setattr(builtins, "open", fail)
    catalog = Mock()
    catalog.get_table_list.side_effect = fail
    layer = SemanticLayer.from_config({"semantic": {"enabled": False, "sources": [{"path": "missing"}]}}, catalog)
    assert layer.render(question="anything", selected_tables=[]) == ""
    assert layer.metric("anything") is None
    assert catalog.mock_calls == []


def test_demo_provider_closure_render_and_longest_alias():
    layer = demo_layer()
    assert layer.warnings == ()
    assert [m.name for m in layer.search_metrics("毛利率")] == ["gross_margin_rate"]
    assert {m.name for m in layer.closure(["gross_margin_rate"])} == {
        "gross_margin_rate",
        "gross_profit",
        "revenue",
        "cogs",
    }
    block = layer.render(question="收入", selected_tables=["sales_orders", "dim_products", "exchange_rates"])
    assert "## Metrics" in block and "## Table relationships" in block and "## Business rules" in block
    assert block == layer.render(question="收入", selected_tables=["sales_orders", "dim_products", "exchange_rates"])
    assert layer.search_metrics("unrecognized") == ()
    assert layer.search_metrics("收入", k=0) == ()


def record():
    return {
        "name": "sample",
        "label": "Sample",
        "aliases": ["Sample"],
        "level": "atomic",
        "formula": "SUM(value)",
        "source_tables": ["events"],
        "provenance": "fixture.yaml#sample",
    }


@pytest.fixture
def catalog():
    result = Mock()
    result.get_table_list.return_value = ["events"]
    result.get_column_list.return_value = [{"column_name": "value"}]
    return result


def test_priority_resolution_and_equal_priority_rejection(tmp_path, catalog):
    first, second = tmp_path / "first.yaml", tmp_path / "second.yaml"
    low = record()
    high = {**record(), "formula": "COUNT(*)"}
    dump_yaml(first, {"version": 1, "metrics": [low]})
    dump_yaml(second, {"version": 1, "metrics": [high]})
    cfg = {
        "semantic": {
            "enabled": True,
            "sources": [{"path": str(first), "priority": 100}, {"path": str(second), "priority": 10}],
        }
    }
    assert SemanticLayer.from_config(cfg, catalog).metric("sample").formula == "COUNT(*)"
    cfg["semantic"]["sources"][0]["priority"] = 10
    with pytest.raises(SemanticDataError, match="equal-priority"):
        SemanticLayer.from_config(cfg, catalog)


@pytest.mark.parametrize(
    "change", [{"source_tables": ["missing"]}, {"depends_on": ["missing"]}, {"time_field": "missing"}]
)
def test_reference_failures_warn_or_raise(tmp_path, catalog, change):
    path = tmp_path / "metrics.yaml"
    dump_yaml(path, {"version": 1, "metrics": [{**record(), **change}]})
    cfg = {"semantic": {"enabled": True, "sources": [{"path": str(path)}]}}
    assert SemanticLayer.from_config(cfg, catalog).warnings
    cfg["semantic"]["strict"] = True
    with pytest.raises(SemanticDataError, match="fixture.yaml.*unknown"):
        SemanticLayer.from_config(cfg, catalog)


def test_config_relative_paths_and_missing_source(tmp_path, monkeypatch, catalog):
    dump_yaml(tmp_path / "metrics.yaml", {"version": 1, "metrics": [record()]})
    cfg = SemanticConfig(enabled=True, sources=[SemanticSourceConfig(path="metrics.yaml")])
    cfg._config_path = tmp_path / "config.yaml"
    monkeypatch.chdir(ROOT.parent)
    assert SemanticLayer.from_config(SimpleNamespace(semantic=cfg), catalog).metric("sample")
    cfg.sources = [SemanticSourceConfig(path="absent.yaml")]
    with pytest.raises(SemanticSourceError, match="absent.yaml"):
        SemanticLayer.from_config(SimpleNamespace(semantic=cfg), catalog)


def test_json_adapter_and_alias_collision(tmp_path, catalog):
    path = tmp_path / "metrics.json"
    path.write_text(json.dumps({"metrics": [record()]}), encoding="utf-8")
    cfg = {"semantic": {"enabled": True, "sources": [{"type": "json", "path": str(path)}]}}
    assert SemanticLayer.from_config(cfg, catalog).metric("sample")
    path.write_text(json.dumps({"metrics": [record(), {**record(), "name": "other"}]}), encoding="utf-8")
    with pytest.raises(SemanticDataError, match="alias.*conflicts"):
        SemanticLayer.from_config(cfg, catalog)


def test_config_defaults_do_not_share_source_lists():
    first, second = SemanticConfig(), SemanticConfig()
    first.sources.append(SemanticSourceConfig(type="catalog"))
    assert second.sources == []


def test_no_business_data_is_changed_by_loading():
    before = read_yaml(DATA / "metrics.yaml")
    demo_layer()
    assert read_yaml(DATA / "metrics.yaml") == before


def test_legacy_json_requires_explicit_machine_key_mapping(tmp_path, catalog):
    from openchatbi.semantic.sources.json_file import JsonMetricSource

    path = tmp_path / "indicators.json"
    path.write_text(
        json.dumps(
            {
                "indicators": [
                    {
                        "name": "Count",
                        "level": "Base",
                        "definition": "All events",
                        "sql_template": "SELECT COUNT(*) FROM events",
                        "data_source": "events",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SemanticSourceError, match="mapping_path"):
        JsonMetricSource(path).load_metrics()
    mapping = tmp_path / "mapping.yaml"
    dump_yaml(
        mapping,
        {
            "version": 1,
            "metric_names": {"Count": "event_count"},
            "levels": {"Base": "atomic"},
            "multi_step_metrics": [],
        },
    )
    assert JsonMetricSource(path, mapping_path=mapping).load_metrics()[0].name == "event_count"


def test_app_config_records_its_real_location(tmp_path, catalog):
    from openchatbi.config_loader import ConfigLoader
    from openchatbi.config_loader import SemanticSourceConfig as ReexportedSource

    file = tmp_path / "config.yaml"
    dump_yaml(
        file,
        {
            "version": 1,
            "data_warehouse_config": {"uri": "sqlite:///:memory:"},
            "catalog_store": {"store_type": "file_system", "data_path": str(DATA / "catalog"), "auto_load": False},
            "default_llm": {
                "class": "langchain_core.language_models.FakeListChatModel",
                "params": {"responses": ["SELECT 1"]},
            },
        },
    )
    loader = ConfigLoader()
    loader.load(str(file))
    assert loader.get().semantic._config_path == file
    assert ReexportedSource is SemanticSourceConfig
