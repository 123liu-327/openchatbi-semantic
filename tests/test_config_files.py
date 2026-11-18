"""Configuration composition and startup selection without network services."""

from unittest.mock import patch

import pytest
import yaml

from openchatbi.config_files import read_config_file
from scripts.workbench_config import select_config_file


def write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def test_composition_separates_sources_and_resolves_declaring_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_DATABASE_URI", "sqlite:///:memory:")
    write(tmp_path / "infra/db.yaml", {"data_warehouse_config": {"uri": "${TEST_DATABASE_URI}"}})
    write(
        tmp_path / "business/package.yaml",
        {
            "catalog_store": {"store_type": "file_system", "data_path": "catalog"},
            "semantic": {"sources": [{"type": "yaml", "path": "metrics.yaml"}]},
            "workflow": {"retrieval_path": "index", "max_concurrency": 1},
        },
    )
    write(
        tmp_path / "app.yaml",
        {"includes": ["infra/db.yaml", "business/package.yaml"], "workflow": {"max_concurrency": 3}},
    )
    data = read_config_file(tmp_path / "app.yaml")
    assert data["data_warehouse_config"]["uri"] == "sqlite:///:memory:"
    assert data["catalog_store"]["data_path"] == str(tmp_path / "business/catalog")
    assert data["semantic"]["sources"][0]["path"] == str(tmp_path / "business/metrics.yaml")
    assert data["workflow"] == {"retrieval_path": str(tmp_path / "business/index"), "max_concurrency": 3}


def test_legacy_single_file_keeps_relative_path_convention(tmp_path):
    write(tmp_path / "app.yaml", {"report_directory": "data/reports"})
    assert read_config_file(tmp_path / "app.yaml")["report_directory"] == "data/reports"


def test_environment_values_do_not_become_yaml(tmp_path, monkeypatch):
    secret = "key: [secret] # literal"
    monkeypatch.setenv("TEST_SECRET", secret)
    write(tmp_path / "app.yaml", {"key": "${TEST_SECRET}"})
    assert read_config_file(tmp_path / "app.yaml")["key"] == secret
    monkeypatch.delenv("TEST_SECRET")
    with pytest.raises(ValueError, match="TEST_SECRET"):
        read_config_file(tmp_path / "app.yaml")


@pytest.mark.parametrize("includes", ["db.yaml", [123], ["missing.yaml"]])
def test_bad_include_is_explicit(tmp_path, includes):
    write(tmp_path / "app.yaml", {"includes": includes})
    with pytest.raises(ValueError):
        read_config_file(tmp_path / "app.yaml")


def test_include_cycle_is_rejected(tmp_path):
    write(tmp_path / "a.yaml", {"includes": ["b.yaml"]})
    write(tmp_path / "b.yaml", {"includes": ["a.yaml"]})
    with pytest.raises(ValueError, match="cycle"):
        read_config_file(tmp_path / "a.yaml")


def test_lists_replace_and_root_overrides_fragments(tmp_path):
    write(tmp_path / "base.yaml", {"semantic": {"sources": [{"type": "catalog"}]}})
    write(tmp_path / "app.yaml", {"includes": ["base.yaml"], "semantic": {"sources": []}})
    assert read_config_file(tmp_path / "app.yaml")["semantic"]["sources"] == []


def test_explicit_config_never_prepares_demo(tmp_path, monkeypatch):
    write(tmp_path / "app.yaml", {})
    monkeypatch.setenv("CONFIG_FILE", "app.yaml")
    with patch("scripts.run_semantic_demo.prepare") as prepare:
        assert select_config_file(tmp_path) == tmp_path / "app.yaml"
        prepare.assert_not_called()
    monkeypatch.setenv("CONFIG_FILE", "missing.yaml")
    with pytest.raises(ValueError, match="CONFIG_FILE"):
        select_config_file(tmp_path)


def test_launcher_honors_custom_config_without_demo_credentials(tmp_path, monkeypatch):
    import run_workbench

    write(tmp_path / "app.yaml", {})
    monkeypatch.setenv("CONFIG_FILE", str(tmp_path / "app.yaml"))
    with patch.object(run_workbench, "load_dotenv"), patch.object(run_workbench.subprocess, "run") as run:
        assert run_workbench.main() == 0
    assert run.call_args.kwargs["env"]["CONFIG_FILE"] == str(tmp_path / "app.yaml")


def test_deployment_example_loads_canonical_business_independently(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from langchain_core.language_models import BaseChatModel

    from openchatbi.config_loader import ConfigLoader
    from openchatbi.semantic.layer import SemanticLayer
    from scripts.run_semantic_demo import ROOT, prepare

    _, database, _ = prepare(tmp_path / "demo")
    monkeypatch.setenv("CHATBI_MODEL", "offline-test")
    monkeypatch.setenv("CHATBI_DATABASE_URI", "sqlite:///" + database.as_posix())
    entry = tmp_path / "app.yaml"
    write(
        entry,
        {
            "includes": [str(ROOT / "example/deployment/config.yaml")],
            "catalog_store": {"data_path": str(tmp_path / "demo/catalog")},
        },
    )
    loader = ConfigLoader()
    with patch("langchain_openai.ChatOpenAI", return_value=MagicMock(spec=BaseChatModel)):
        loader.load(str(entry))
    cfg = loader.get()
    layer = SemanticLayer.from_config(cfg, cfg.catalog_store)
    assert len(layer.list_metrics()) == 13
    assert len(layer.list_rules()) == 8
    assert cfg.data_warehouse_config["uri"] == "sqlite:///" + database.as_posix()
    assert cfg.workflow.retrieval_path.endswith("chatbi_mvp/chroma") or cfg.workflow.retrieval_path.endswith(
        "chatbi_mvp\\chroma"
    )
