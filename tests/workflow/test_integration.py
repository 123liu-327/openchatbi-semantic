"""Actual app construction, Qwen wire contracts and extension boundaries."""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.tools import StructuredTool

from openchatbi.workflow.graph import build_workflow_graph, planner_prompt
from openchatbi.workflow.models import QueryPlan
from openchatbi.workflow.registry import CallContext
from openchatbi.workflow.retrieval import EmbeddingSettings, QwenEmbedder
from openchatbi.workflow.security import SQLPolicyError, check_sql, enforce_active_policy, guarded_sql
from tests.workflow.test_workflow import task


@pytest.mark.asyncio
async def test_model_planner_and_domain_local_function_calls(runtime):
    plan = QueryPlan(tasks=[task("s", "semantic", "auto", {"question": "解释毛利率"})])
    runtime.llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=plan)
    runtime.llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(
                content="",
                tool_calls=[{"name": "search_metrics", "args": {"mode": "get", "id": "margin"}, "id": "call1"}],
            ),
            AIMessage(content="完成"),
        ]
    )
    result = await build_workflow_graph(runtime).ainvoke({"messages": [HumanMessage("解释毛利率")]})
    assert result["results"]["s"]["status"] == "ok"
    assert "毛利率" in result["final_answer"]
    assert runtime.llm.with_structured_output.return_value.ainvoke.await_count == 1
    tool_schemas = runtime.llm.bind_tools.call_args.args[0]
    assert {item["function"]["name"] for item in tool_schemas} == {
        "search_metrics",
        "search_tables",
        "search_fields",
        "search_rules",
        "plan_join",
        "resolve_query",
        "clarify",
    }
    assert not runtime.retrieval.embedder.calls and not runtime.sql_calls


@pytest.mark.asyncio
async def test_mcp_json_schema_adapts_and_domain_dispatch_discovers_tool(runtime):
    tool = StructuredTool.from_function(
        name="mcp_demo",
        description="Existing external computation",
        coroutine=AsyncMock(return_value="external result"),
        args_schema={
            "type": "object",
            "properties": {"count": {"type": "integer"}},
            "required": ["count"],
            "additionalProperties": False,
        },
    )
    runtime.registry.register_external(tool, "delivery")
    runtime.llm.bind_tools.return_value.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(content="", tool_calls=[{"name": "mcp_demo", "args": {"count": 3}, "id": "c"}]),
            AIMessage(content="done"),
        ]
    )
    plan = QueryPlan(tasks=[task("plugin", "delivery", "auto", {"question": "Use external computation"})])
    result = await build_workflow_graph(runtime, planner=lambda *_: plan).ainvoke({"messages": [HumanMessage("run")]})
    assert "external result" in result["final_answer"]
    assert "mcp_demo" in {item["function"]["name"] for item in runtime.llm.bind_tools.call_args.args[0]}
    invalid = await runtime.registry.call("mcp_demo", {"count": "wrong"}, CallContext("a" * 32), "bad")
    assert invalid.status == "error" and tool.coroutine.await_count == 1
    assert "mcp_demo" not in planner_prompt(
        runtime
    )  # Outer planner sees domain capability, not plugin argument schemas.


@pytest.mark.asyncio
async def test_analysis_agent_additional_queries_use_shared_entry(runtime):
    async def analyze_input(state):
        sql_tool = build.call_args.kwargs["text2sql_tool"]
        assert sql_tool.name == "text2sql"
        schema_tool = next(tool for tool in build.call_args.kwargs["schema_tools"] if tool.name == "show_schema")
        schema = await schema_tool.ainvoke({"reasoning": "inspect", "tables": ["orders"]})
        assert schema["operations"][0]["function"] == "search_tables"
        assert schema["operations"][0]["data"]["items"][0]["table"] == "orders"
        first, second = await asyncio.gather(
            sql_tool.ainvoke({"reasoning": "need data", "context": "2024销售额"}),
            sql_tool.ainvoke({"reasoning": "same data", "context": "2024销售额"}),
        )
        assert json.loads(first)["data"]["dataset_ref"] == json.loads(second)["data"]["dataset_ref"]
        return {"messages": [AIMessage("analysis complete")]}

    with patch(
        "openchatbi.analysis.agent.build_data_analysis_agent", return_value=RunnableLambda(analyze_input)
    ) as build:
        context = runtime.context("a" * 32)
        result = await runtime.registry.call("analyze", {"task": "分析销售额"}, context, "analysis")
    assert result.status == "ok" and result.data["payload"] == "analysis complete"
    assert len(runtime.sql_calls) == 1
    assert len(result.data["dataset_refs"]) == 1


@pytest.mark.asyncio
async def test_actual_workbench_download_functions_render_exported_csv(runtime):
    import ast
    from unittest.mock import MagicMock

    context = runtime.context("a" * 32)
    query = await runtime.request_query("销售额", context, "q")
    exported = await runtime.registry.call("export_csv", {"dataset_refs": [query.data["dataset_ref"]]}, context, "csv")
    artifact = exported.data["files"][0]
    page = Path(__file__).resolve().parents[2] / "sample_ui/streamlit_ui.py"
    tree = __import__("ast").parse(page.read_text(encoding="utf-8"))
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in {"get_report_file_content", "process_download_links", "render_content_with_downloads"}
    ]
    st = MagicMock()
    namespace = {"Path": Path, "st": st}
    exec(compile(ast.Module(body=selected, type_ignores=[]), str(page), "exec"), namespace)
    with patch("openchatbi._get_config", return_value=SimpleNamespace(get=lambda: runtime.cfg)):
        content, mime = namespace["get_report_file_content"](artifact["filename"])
        assert "中国" in content.decode("utf-8-sig") and mime == "text/csv"
        visible = namespace["process_download_links"](artifact["download_url"])
        namespace["render_content_with_downloads"](visible)
    assert st.download_button.call_args.kwargs["data"] == content
    assert st.download_button.call_args.kwargs["file_name"] == artifact["filename"]


@pytest.mark.asyncio
async def test_replanning_failure_is_bounded_and_does_not_claim_success(runtime):
    runtime.cfg.workflow.max_replans = 2
    plans = []

    def planner(*_):
        plans.append(1)
        return QueryPlan(tasks=[task("bad", "semantic", "search_metrics", {"mode": "get", "id": "absent"})])

    result = await build_workflow_graph(runtime, planner=planner).ainvoke({"messages": [HumanMessage("run")]})
    assert len(plans) == 3 and result["replans"] == 2
    assert result["messages"][-1].response_metadata["workflow_error"]


@pytest.mark.asyncio
async def test_bad_result_reference_is_visible_and_never_calls_sql(runtime):
    plan = QueryPlan(
        tasks=[
            task("a", "semantic", "search_metrics", {"mode": "list"}),
            task(
                "b",
                "text2sql",
                "query",
                {"question": "销售额", "semantic_ref": {"$ref": {"task_id": "a", "path": "/missing"}}},
                ["a"],
            ),
        ]
    )
    result = await build_workflow_graph(runtime, planner=lambda *_: plan).ainvoke({"messages": [HumanMessage("run")]})
    assert result["results"]["b"]["errors"][0]["code"] == "invalid_reference"
    assert not runtime.sql_calls


@pytest.mark.asyncio
async def test_valid_long_plan_does_not_hit_default_recursion_limit(runtime):
    plan = QueryPlan(
        tasks=[
            task(f"t{i}", "semantic", "search_metrics", {"mode": "list"}, [f"t{i-1}"] if i else []) for i in range(15)
        ]
    )
    result = await build_workflow_graph(runtime, planner=lambda *_: plan).ainvoke({"messages": [HumanMessage("run")]})
    assert len(result["results"]) == 15


def test_future_authorizer_receives_sql_and_cannot_be_overridden_by_plan(runtime):
    def deny(sql):
        raise PermissionError("Denied by trusted runtime")

    with guarded_sql(deny), pytest.raises(PermissionError):
        enforce_active_policy("SELECT 1", "sqlite")
    # Original execution remains inert outside a new-mode adapter.
    enforce_active_policy("DELETE FROM orders", "sqlite")


@pytest.mark.parametrize(
    "sql", ["SELECT * FROM read_csv('/tmp/secret')", "SELECT nextval('sequence')", "SELECT pg_sleep(3)"]
)
def test_ast_rejects_external_reads_and_side_effects(sql):
    with pytest.raises(SQLPolicyError):
        check_sql(sql, "postgres")


def test_qwen_batches_dimensions_and_no_chat_key_reuse(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "chat-only-secret")
    monkeypatch.delenv("QWEN_EMBEDDING_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_EMBEDDING_BASE_URL", raising=False)
    with pytest.raises(ValueError, match="QWEN_EMBEDDING"):
        QwenEmbedder(EmbeddingSettings.from_env()).embed(["a"])
    calls = []

    def response(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(
            data=[SimpleNamespace(index=index, embedding=[1.0, 2.0]) for index in reversed(range(len(kwargs["input"])))]
        )

    settings = EmbeddingSettings(api_key="embedding-secret", base_url="https://embedding.invalid/v1", dimensions=2)
    with patch("openai.OpenAI") as client:
        client.return_value.__enter__.return_value.embeddings.create.side_effect = response
        assert len(QwenEmbedder(settings).embed(["a"] * 21)) == 21
        assert [len(item["input"]) for item in calls] == [10, 10, 1]
        assert all(item["dimensions"] == 2 and item["model"] == "text-embedding-v4" for item in calls)
        assert client.call_args.kwargs["api_key"] == "embedding-secret"
        assert "embedding-secret" not in repr(settings)


@pytest.mark.skipif(os.getenv("CHATBI_LIVE_EMBEDDING_TEST") != "1", reason="Explicit opt-in Qwen service check")
def test_live_qwen_embedding():
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)
    settings = EmbeddingSettings.from_env()
    vectors = QwenEmbedder(settings).embed(["销售额指标", "国家维度"])
    assert len(vectors) == 2 and all(len(vector) == settings.dimensions for vector in vectors)


def test_parallel_visualizations_keep_their_own_datasets():
    from openchatbi.streaming import AgentStreamProcessor

    processor = AgentStreamProcessor()
    processor.process(("query:a",), "updates", {"execute_sql": {"data": "amount\n10\n"}})
    processor.process(("query:b",), "updates", {"execute_sql": {"data": "amount\n20\n"}})
    event = processor.process(
        ("query:a",), "updates", {"generate_visualization": {"visualization_dsl": {"chart_type": "bar"}}}
    )
    assert event[0].data["data"] == "amount\n10\n"


def test_canonical_data_13_metrics_and_four_domain_cold_start_without_network(tmp_path):
    import yaml

    from scripts.import_chatbi_mvp_kb import dump_yaml
    from scripts.run_semantic_demo import prepare

    root = Path(__file__).resolve().parents[2]
    _, _, path = prepare(tmp_path / "demo", model="test-placeholder")
    cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg["default_llm"] = {
        "class": "langchain_core.language_models.FakeListChatModel",
        "params": {"responses": ["SELECT 1"]},
    }
    cfg["workflow"] = {"mode": "four_domain", "retrieval_path": str(tmp_path / "index")}
    dump_yaml(path, cfg)
    env = {**os.environ, "CONFIG_FILE": str(path), "PYTHONPATH": str(root), "PYTHONUTF8": "1"}
    code = """
import socket
original_connect = socket.socket.connect
def deny(sock, address, *args, **kwargs):
    # Windows asyncio's self-pipe uses a loopback socketpair.
    if isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
        return original_connect(sock, address, *args, **kwargs)
    raise AssertionError("Network during offline metadata query")
socket.socket.connect = deny
from langchain_core.language_models import FakeListChatModel
class ToolFake(FakeListChatModel):
    def bind_tools(self, *args, **kwargs):
        return self
from openchatbi import config
cfg = config.get()
cfg.default_llm = ToolFake(responses=["SELECT 1"])
from openchatbi.agent_graph import build_agent_graph_sync
graph = build_agent_graph_sync(cfg.catalog_store, enable_context_management=False)
result = graph.invoke({"messages": [{"role":"user","content":"有哪些指标"}]})
assert len(result["results"]["metrics"]["data"]["items"]) == 13
assert "销售" in result["final_answer"] or "收入" in result["final_answer"]
print("FOUR_DOMAIN_READY_13")
"""
    completed = subprocess.run(
        [sys.executable, "-c", code], cwd=tmp_path, env=env, capture_output=True, encoding="utf-8", timeout=40
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "FOUR_DOMAIN_READY_13" in completed.stdout


def test_additive_mvp_descriptions_cover_only_canonical_metadata():
    from openchatbi.workflow.retrieval import catalog_documents
    from scripts.run_semantic_demo import DemoCatalog
    from tests.semantic.test_layer_from_config import DATA, demo_layer

    root = Path(__file__).resolve().parents[2]
    original = (DATA / "catalog/table_spec_columns.csv").read_bytes()
    overlay = json.loads((root / "example/workflow/retrieval_metadata.json").read_text(encoding="utf-8"))
    assert len(overlay["tables"]) == 5 and len(overlay["fields"]) == 40
    assert len(overlay["excluded_tables"]) == 4
    documents = catalog_documents(DemoCatalog(DATA / "catalog"), demo_layer(), overlay)
    field = next(item for item in documents["fields"] if item["id"] == "sales_orders.net_amount")
    assert "不含税" in field["text"]
    assert (DATA / "catalog/table_spec_columns.csv").read_bytes() == original


def test_metadata_import_never_executes_source_and_filters_noise(tmp_path):
    from scripts.import_retrieval_metadata import extract

    (tmp_path / "table_retriever.py").write_text(
        "raise RuntimeError('must not execute')\nTABLE_METADATA = {'orders': {'description':'业务表'}, 'noise': {}}",
        encoding="utf-8",
    )
    (tmp_path / "field_matcher.py").write_text(
        "FIELD_METADATA = {'orders.amount': {'description':'收入'}, 'noise.x': {'description':'干扰'}}",
        encoding="utf-8",
    )
    (tmp_path / "table_spec_columns.csv").write_text("table_name,column_name\norders,amount\n", encoding="utf-8")
    overlay = extract(tmp_path, tmp_path)
    assert set(overlay["tables"]) == {"orders"}
    assert overlay["fields"] == {"orders.amount": "收入"}
    assert overlay["excluded_tables"] == ["noise"]
