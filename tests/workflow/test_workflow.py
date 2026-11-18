"""Behavioral contracts for four-domain planning, retrieval and delivery."""

import asyncio
import csv
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from langchain_core.messages import HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import create_engine, text

from openchatbi.semantic.layer import SemanticLayer
from openchatbi.semantic.models import BusinessRule, MetricLevel, MetricSpec, SemanticBundle
from openchatbi.workflow.config import WorkflowConfig
from openchatbi.workflow.graph import build_workflow_graph, resolve_arguments, validate_plan
from openchatbi.workflow.models import Contract, QueryPlan, ResolvedQuery, Task
from openchatbi.workflow.registry import CallContext, Registry, ToolSpec
from openchatbi.workflow.retrieval import EmbeddingSettings, RetrievalService
from openchatbi.workflow.runtime import WorkflowRuntime
from openchatbi.workflow.security import SQLPolicyError, check_sql, enforce_active_policy, execute_readonly
from openchatbi.workflow.tools import PayloadOutput


class Catalog:
    def __init__(self):
        self.engine = create_engine("sqlite://")

    def get_table_list(self):
        return ["orders"]

    def get_column_list(self, table):
        return [{"column_name": name, "description": name} for name in ("amount", "tax", "status")]

    def get_sql_engine(self):
        return self.engine


class FakeEmbedding:
    def __init__(self):
        self.calls = []

    def embed(self, texts):
        self.calls.extend(texts)
        return [[1.0, float(len(text) % 7 + 1)] for text in texts]


@pytest.fixture
def runtime(tmp_path):
    layer = SemanticLayer(
        SemanticBundle(
            metrics={
                "revenue": MetricSpec(
                    name="revenue",
                    label="销售额",
                    level=MetricLevel.ATOMIC,
                    formula="SUM(amount)",
                    source_tables=("orders",),
                ),
                "margin": MetricSpec(
                    name="margin",
                    label="毛利率",
                    level=MetricLevel.DERIVED,
                    formula="revenue / 2",
                    depends_on=("revenue",),
                    source_tables=("orders",),
                ),
            },
            rules=(
                BusinessRule(
                    name="revenue_rule",
                    trigger_any=("销售额",),
                    scope_tables=("orders",),
                    require_columns=("orders.amount", "orders.status"),
                    forbid_columns=("orders.tax",),
                ),
            ),
            edges=(),
            tables=frozenset({"orders"}),
        )
    )
    catalog = Catalog()
    cfg = SimpleNamespace(
        workflow=WorkflowConfig(
            max_replans=0,
            retrieval_path=str(tmp_path / "chroma"),
            authorization={"principal": "test_user", "table_columns": {"orders": ["*"]}},
        ),
        report_directory=str(tmp_path / "reports"),
        dialect="sqlite",
        sql_result_limit=100,
    )
    settings = EmbeddingSettings(dimensions=2, base_url="fake://test")
    retrieval = RetrievalService(
        catalog, layer, cfg.workflow.retrieval_path, settings=settings, embedder=FakeEmbedding()
    )
    sql_calls = []

    async def sql(state):
        enforce_active_policy("SELECT 1", "sqlite")
        sql_calls.append(state)
        question = state["messages"][0]["content"]
        amount = "20" if "2025" in question else "10"
        return {"data": f"国家,amount\n中国,{amount}\n", "sql": "SELECT 1"}

    llm = Mock()
    llm.ainvoke.side_effect = AssertionError("Unexpected model call")
    value = WorkflowRuntime(cfg, catalog, layer, RunnableLambda(sql), Registry(), llm, retrieval=retrieval)
    value.sql_calls = sql_calls

    async def resolved_fixture(args, context, task_id):
        # Scheduling/CSV tests isolate the semantic model. Boundary tests use
        # the real SemanticResolver and assert its actual retrieval/model calls.
        import hashlib

        identifier = hashlib.md5(args.question.encode()).hexdigest()
        resolved = ResolvedQuery(
            id=identifier,
            snapshot_id=value.snapshot_id,
            request_question=args.question,
            rewrite_question=args.question,
            tables=[{"table": "orders", "columns": ["amount", "status"]}],
            schema_prompt="orders(amount, status)",
        )
        value.persist_semantic(context, resolved)
        return {"semantic_ref": identifier, "context": {}, "operations": []}

    value.semantic_resolver.resolve = resolved_fixture
    return value


def task(name, domain, operation, args=None, depends_on=None):
    return Task(id=name, domain=domain, operation=operation, args=args or {}, depends_on=depends_on or [])


def ref(name, path):
    return {"$ref": {"task_id": name, "path": path}}


@pytest.mark.asyncio
async def test_metric_list_is_offline_and_uses_only_semantic(runtime):
    graph = build_workflow_graph(runtime)
    result = await graph.ainvoke({"messages": [HumanMessage("有哪些指标")]})
    assert set(result["results"]) == {"metrics"}
    assert "销售额" in result["final_answer"] and "毛利率" in result["final_answer"]
    assert not runtime.sql_calls
    assert not runtime.retrieval.embedder.calls
    runtime.llm.ainvoke.assert_not_called()


def test_sync_invocation_is_supported(runtime):
    result = build_workflow_graph(runtime).invoke({"messages": [HumanMessage("有哪些指标")]})
    assert result["results"]["metrics"]["status"] == "ok"


@pytest.mark.asyncio
async def test_independent_metric_get_and_recursive_dependencies(runtime):
    context = CallContext("a" * 32)
    result = await runtime.registry.call("search_metrics", {"mode": "get", "id": "margin"}, context, "m")
    assert result.status == "ok"
    assert [item["name"] for item in result.data["items"]] == ["margin", "revenue"]
    assert next(item for item in result.data["items"] if item["name"] == "revenue")["is_dependency"]
    assert context.calls == 1
    assert not runtime.retrieval.embedder.calls


@pytest.mark.asyncio
async def test_vector_scores_metric_is_pure_vector_and_fields_hard_rules(runtime):
    def search(kind, query, top_k, tables=None):
        if kind == "metrics":
            return [{"id": "margin", "distance": 0.4}]
        if kind == "tables":
            return [{"id": "orders", "distance": 0.4}]
        return [{"id": "orders.tax", "distance": 0.0}, {"id": "orders.amount", "distance": 0.4}]

    runtime.retrieval.search = search
    context = CallContext("a" * 32)
    metric = await runtime.registry.call("search_metrics", {"query": "销售额"}, context, "m")
    hit = next(item for item in metric.data["items"] if item["name"] == "margin")
    assert hit["score"] == pytest.approx(0.8)
    assert "rule_score" not in hit
    table = await runtime.registry.call("search_tables", {"query": "销售额"}, context, "t")
    assert table.data["items"][0]["score"] == pytest.approx(0.6)
    fields = await runtime.registry.call(
        "search_fields", {"query": "销售额", "tables": ["orders"], "top_k": 1, "threshold": 0.99}, context, "f"
    )
    assert {item["id"] for item in fields.data["items"]} == {"orders.amount", "orders.status"}
    assert next(item for item in fields.data["items"] if item["id"] == "orders.amount")["score"] == pytest.approx(0.72)


@pytest.mark.asyncio
async def test_query_compare_export_real_csv(runtime):
    plan = QueryPlan(
        tasks=[
            task("s1", "semantic", "resolve_query", {"question": "2024销售额"}),
            task("s2", "semantic", "resolve_query", {"question": "2025销售额"}),
            task(
                "first",
                "text2sql",
                "query",
                {"question": "2024销售额", "semantic_ref": ref("s1", "/semantic_ref")},
                ["s1"],
            ),
            task(
                "second",
                "text2sql",
                "query",
                {"question": "2025销售额", "semantic_ref": ref("s2", "/semantic_ref")},
                ["s2"],
            ),
            task(
                "csv",
                "delivery",
                "export_csv",
                {
                    "dataset_refs": [ref("first", "/dataset_ref"), ref("second", "/dataset_ref")],
                    "title": "国家销售对比",
                    "operation": "compare",
                    "value_column": "amount",
                    "group_by": ["国家"],
                },
                ["first", "second"],
            ),
        ]
    )
    result = await build_workflow_graph(runtime, planner=lambda *_: plan).ainvoke(
        {"messages": [HumanMessage("比较并导出")]}
    )
    assert result["results"]["csv"]["status"] == "ok"
    data = result["results"]["csv"]["data"]
    path = __import__("pathlib").Path(runtime.cfg.report_directory) / data["files"][0]["filename"]
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert rows == [{"国家": "中国", "amount_left": "10", "amount_right": "20", "difference": "10"}]
    assert "/api/download/report/" in result["final_answer"]
    assert len(runtime.sql_calls) == 2


@pytest.mark.asyncio
async def test_parallel_ready_tasks_and_dependency_barrier(runtime):
    class Input(Contract):
        value: str

    arrived, release, finished = set(), asyncio.Event(), []

    async def probe(args, context, task_id):
        arrived.add(task_id)
        if len(arrived) == 2:
            release.set()
        await asyncio.wait_for(release.wait(), 5)
        finished.append(task_id)
        return {"payload": args.value}

    async def dependent(args, context, task_id):
        assert set(finished) == {"a", "b"}
        return {"payload": args.value}

    runtime.registry.register(ToolSpec("probe", "semantic", "test", Input, PayloadOutput, probe))
    runtime.registry.register(ToolSpec("dependent", "delivery", "test", Input, PayloadOutput, dependent))
    plan = QueryPlan(
        tasks=[
            task("a", "semantic", "probe", {"value": "a"}),
            task("b", "semantic", "probe", {"value": "b"}),
            task("c", "delivery", "dependent", {"value": ref("a", "/payload")}, ["a", "b"]),
        ]
    )
    result = await build_workflow_graph(runtime, planner=lambda *_: plan).ainvoke({"messages": [HumanMessage("run")]})
    assert result["results"]["c"]["data"]["payload"] == "a"


@pytest.mark.asyncio
async def test_replan_keeps_completed_results_and_is_bounded(runtime):
    runtime.cfg.workflow.max_replans = 2
    calls = []

    class Input(Contract):
        valid: bool

    async def unstable(args, context, task_id):
        calls.append(task_id)
        if not args.valid:
            raise ValueError("Need a repaired argument")
        return {"payload": "ok"}

    runtime.registry.register(ToolSpec("unstable", "semantic", "test", Input, PayloadOutput, unstable))

    def planner(question, previous):
        return QueryPlan(
            tasks=[
                task("a", "semantic", "unstable", {"valid": True}),
                task("b", "semantic", "unstable", {"valid": bool(previous)}),
            ]
        )

    result = await build_workflow_graph(runtime, planner=planner).ainvoke({"messages": [HumanMessage("repair")]})
    assert result["replans"] == 1
    assert calls.count("a") == 1 and calls.count("b") == 2
    assert result["results"]["b"]["status"] == "ok"


@pytest.mark.asyncio
async def test_failed_branch_blocks_dependents_but_keeps_other_results(runtime):
    plan = QueryPlan(
        tasks=[
            task("good", "semantic", "search_metrics", {"mode": "list"}),
            task("bad", "semantic", "search_metrics", {"mode": "get", "id": "absent"}),
            task(
                "dependent",
                "text2sql",
                "query",
                {"question": "should not run", "semantic_ref": ref("bad", "/semantic_ref")},
                ["bad"],
            ),
        ]
    )
    result = await build_workflow_graph(runtime, planner=lambda *_: plan).ainvoke({"messages": [HumanMessage("run")]})
    assert result["results"]["good"]["status"] == "ok"
    assert result["results"]["dependent"]["status"] == "blocked"
    assert "部分任务未完成" in result["final_answer"] and not runtime.sql_calls


@pytest.mark.parametrize(
    "tasks",
    [
        [task("a", "semantic", "search_metrics", depends_on=["missing"])],
        [
            task("a", "semantic", "search_metrics", depends_on=["b"]),
            task("b", "semantic", "search_metrics", depends_on=["a"]),
        ],
        [task("a", "semantic", "search_metrics"), task("a", "semantic", "search_metrics")],
        [task("a", "semantic", "search_metrics", {"query": ref("b", "/context")})],
    ],
)
def test_invalid_plans(tasks):
    with pytest.raises(ValueError):
        QueryPlan(tasks=tasks)


def test_disabled_tools_and_task_budget(runtime):
    with pytest.raises(ValueError):
        validate_plan(
            QueryPlan(tasks=[task("pdf", "delivery", "export_pdf", {"dataset_refs": ["a"]})]), runtime.registry, 20
        )
    with pytest.raises(ValueError):
        validate_plan(QueryPlan(tasks=[task("a", "semantic", "search_metrics")]), runtime.registry, 0)


@pytest.mark.asyncio
async def test_registry_validates_before_call_and_enforces_budget(runtime):
    context = CallContext("a" * 32, max_calls=1)
    bad = await runtime.registry.call("search_metrics", {"mode": "delete"}, context, "a")
    assert bad.status == "error" and context.calls == 0
    assert (await runtime.registry.call("search_metrics", {"mode": "list"}, context, "b")).status == "ok"
    assert (await runtime.registry.call("search_metrics", {"mode": "list"}, context, "c")).status == "error"


def test_json_pointer_resolution():
    result = {"a": {"status": "ok", "data": {"x/y": [{"~": 7}]}}}
    assert resolve_arguments(ref("a", "/x~1y/0/~0"), result) == 7


@pytest.mark.asyncio
async def test_same_query_coalesces_and_context_is_request_scoped(runtime):
    context = runtime.context("a" * 32)
    a, b = await asyncio.gather(*(runtime.request_query("销售额", context, name) for name in ("a", "b")))
    assert a.data["dataset_ref"] == b.data["dataset_ref"] and len(runtime.sql_calls) == 1
    runtime.contexts.clear()
    restored = runtime.context("a" * 32)
    assert a.data["dataset_ref"] in restored.datasets
    assert a.data["dataset_ref"] not in runtime.context("b" * 32).datasets


def test_chroma_real_persistence_stale_index_and_incremental_build(runtime):
    service = runtime.retrieval
    manifest = service.build()
    assert manifest["counts"] == {"tables": 1, "fields": 3, "metrics": 2}
    count = len(service.embedder.calls)
    assert service.build() == manifest and len(service.embedder.calls) == count
    reopened = RetrievalService(
        runtime.catalog, runtime.semantic, service.directory, settings=service.settings, embedder=FakeEmbedding()
    )
    assert reopened.search("metrics", "销售额", 3)
    assert reopened.search("fields", "销售额", 3, ["orders"])
    assert len(reopened.embedder.calls) == 1  # Query embedding shared across collections.
    changed = RetrievalService(
        runtime.catalog,
        runtime.semantic,
        service.directory,
        settings=service.settings,
        embedder=FakeEmbedding(),
        overlay={"fields": {"orders.amount": "新字段描述"}},
    )
    with pytest.raises(ValueError, match="stale"):
        changed.search("fields", "销售额", 1)
    new = changed.build()
    assert new["generation"] != manifest["generation"]
    assert len(changed.embedder.calls) == 1  # Only the changed document is embedded.


def test_failed_index_build_does_not_publish(runtime):
    service = runtime.retrieval
    original = service.build()
    changed = RetrievalService(
        runtime.catalog,
        runtime.semantic,
        service.directory,
        settings=EmbeddingSettings(model="new", dimensions=2),
        embedder=Mock(),
    )
    changed.embedder.embed.side_effect = ValueError("embedding unavailable")
    with pytest.raises(ValueError):
        changed.build()
    assert service.manifest()["generation"] == original["generation"]


@pytest.mark.asyncio
async def test_missing_vector_index_visible_without_silent_fallback(runtime):
    result = await runtime.registry.call("search_metrics", {"query": "销售额"}, CallContext("a" * 32), "m")
    assert result.status == "error" and "index is missing" in result.errors[0].message
    assert not runtime.retrieval.embedder.calls


@pytest.mark.parametrize(
    "sql", ["SELECT 1", "WITH x AS (SELECT 1 AS a) SELECT a FROM x", "SELECT 1 UNION ALL SELECT 2"]
)
def test_ast_allows_readonly_sql(sql):
    assert check_sql(sql)["allowed"]


@pytest.mark.parametrize(
    "sql",
    [
        "DELETE FROM orders",
        "SELECT 1; SELECT 2",
        "SELECT 1 INTO OUTFILE 'a'",
        "SELECT load_extension('x')",
        "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
        "SELECT * FROM orders FOR UPDATE",
        "ATTACH DATABASE 'a' AS other",
    ],
)
def test_ast_blocks_writes_and_escape_hatches(sql):
    with pytest.raises(SQLPolicyError):
        check_sql(sql)


def test_direct_execution_cannot_bypass_guard(runtime):
    with runtime.catalog.engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (amount INTEGER)"))
        connection.execute(text("INSERT INTO orders VALUES (7)"))
    with pytest.raises(SQLPolicyError):
        execute_readonly(runtime.catalog, "DELETE FROM orders", "sqlite")
    assert execute_readonly(runtime.catalog, "SELECT * FROM orders", "sqlite") == (["amount"], [[7]])


@pytest.mark.asyncio
async def test_new_sql_adapter_guards_changed_sql(runtime):
    async def unsafe(state):
        enforce_active_policy("DELETE FROM orders", "sqlite")
        return {"data": "fake\n1\n"}

    runtime.sql_graph = RunnableLambda(unsafe)
    result = await runtime.request_query("anything", runtime.context("a" * 32), "q")
    assert result.status == "error" and "read-only" in result.errors[0].message


@pytest.mark.asyncio
async def test_checkpoint_turns_do_not_reuse_previous_results(runtime):
    graph = build_workflow_graph(runtime, checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "workflow-test"}}
    first = await graph.ainvoke({"messages": [HumanMessage("有哪些指标")]}, config)
    second = await graph.ainvoke({"messages": [HumanMessage("有哪些表")]}, config)
    assert first["request_id"] != second["request_id"]
    assert set(second["results"]) == {"tables"}


def test_stream_events_keep_full_results():
    from openchatbi.streaming import AgentStreamProcessor

    payload = {
        "kind": "function",
        "domain": "semantic",
        "task_id": "m",
        "operation": "search_metrics",
        "result": {"status": "ok", "data": {"long": "x" * 1000}},
    }
    events = AgentStreamProcessor().process(
        ("semantic:1",), "updates", {"invoke_function": {"workflow_events": [payload]}}
    )
    assert events[0].kind == "workflow" and events[0].data == payload
