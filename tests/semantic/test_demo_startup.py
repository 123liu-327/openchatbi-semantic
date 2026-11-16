import json
import os
import subprocess
import sys
from pathlib import Path

import yaml

from scripts.run_semantic_demo import prepare

ROOT = Path(__file__).resolve().parents[2]


def test_offline_demo_from_outside_repo_without_key(tmp_path):
    env = {key: value for key, value in os.environ.items() if key not in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")}
    env["PYTHONUTF8"] = "1"
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/run_semantic_demo.py"), "--output", str(tmp_path / "demo")],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    data = json.loads(result.stdout)
    assert data["llm_called"] is False
    assert data["results"][0]["rows"] == [[66_814_000.0]]
    assert data["unreachable"] == []


def test_generated_config_cold_imports_real_app_modules(tmp_path):
    _, _, path = prepare(tmp_path / "demo", model="offline-test-placeholder")
    config = yaml.safe_load(path.read_text(encoding="utf-8"))
    config["default_llm"] = {
        "class": "langchain_core.language_models.FakeListChatModel",
        "params": {"responses": ["SELECT 1"]},
    }
    from scripts.import_chatbi_mvp_kb import dump_yaml

    dump_yaml(path, config)
    env = {**os.environ, "CONFIG_FILE": str(path), "PYTHONPATH": str(ROOT), "OPENAI_API_KEY": "", "PYTHONUTF8": "1"}
    code = """
import socket
def deny_connection(*args, **kwargs):
    raise AssertionError('network access during cold startup')
socket.socket.connect = deny_connection
from openchatbi import config
import openchatbi.agent_graph
from openchatbi.semantic.layer import SemanticLayer
layer = SemanticLayer.from_config(config.get(), config.get().catalog_store)
assert layer.metric('revenue')
print('SEMANTIC_APP_READY')
"""
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        encoding="utf-8",
        check=False,
        timeout=40,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "SEMANTIC_APP_READY" in result.stdout
    assert "api_key" not in path.read_text(encoding="utf-8")
