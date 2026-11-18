"""Metadata routing and OpenAI-compatible reasoning model planning regressions."""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from langchain_core.messages import HumanMessage
from langchain_openai import ChatOpenAI

from openchatbi.workflow.graph import build_workflow_graph, direct_plan


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "question,operation",
    [
        ("表有几张啊", "search_tables"),
        ("数据库里有多少张表？", "search_tables"),
        ("我的数据库一共有几张表", "search_tables"),
        ("表的数量是多少", "search_tables"),
        ("表有哪些", "search_tables"),
        ("指标有几个啊", "search_metrics"),
        ("字段有多少个", "search_fields"),
    ],
)
async def test_metadata_counts_use_only_catalog(runtime, question, operation):
    runtime.llm.with_structured_output.side_effect = AssertionError("Metadata must not call planner")
    result = await build_workflow_graph(runtime).ainvoke({"messages": [HumanMessage(question)]})
    assert not result["plan_error"]
    assert len(result["results"]) == 1
    value = next(iter(result["results"].values()))
    assert value["function"] == operation and value["status"] == "ok"
    assert not runtime.retrieval.embedder.calls and not runtime.sql_calls
    if operation == "search_tables":
        assert "共 1 张表" in result["final_answer"]
        assert "orders" in result["final_answer"]


@pytest.mark.parametrize(
    "question",
    [
        "订单表有多少条数据",
        "orders有多少行",
        "有多少表包含客户字段",
        "数据库有几张表并导出CSV",
        "各国销售额有多少",
        "比较两个表的数据数量",
    ],
)
def test_business_counts_and_compound_requests_still_require_planner(runtime, question):
    assert direct_plan(question, runtime) is None


@pytest.mark.asyncio
async def test_reasoning_provider_planning_uses_auto_tools_without_response_format(runtime):
    def handle(request):
        body = json.loads(request.content)
        assert "response_format" not in body
        assert body["tool_choice"] == "auto"
        assert body["tools"][0]["function"]["name"] == "QueryPlan"
        return httpx.Response(
            200,
            json={
                "id": "plan",
                "object": "chat.completion",
                "model": "reasoning-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "p1",
                                    "type": "function",
                                    "function": {
                                        "name": "QueryPlan",
                                        "arguments": json.dumps(
                                            {
                                                "tasks": [
                                                    {
                                                        "id": "tables",
                                                        "domain": "semantic",
                                                        "operation": "search_tables",
                                                        "args": {"mode": "list"},
                                                    }
                                                ]
                                            }
                                        ),
                                    },
                                }
                            ],
                        },
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        runtime.llm = ChatOpenAI(
            model="reasoning-model",
            api_key="test-key",
            base_url="https://model.test/v1",
            http_async_client=client,
            max_retries=0,
        )
        result = await build_workflow_graph(runtime).ainvoke({"messages": [HumanMessage("查看元数据规模")]})
    assert not result["plan_error"]
    assert "共 1 张表" in result["final_answer"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 401, 403, 429])
async def test_planning_failure_identifies_http_error_without_leaking_response(runtime, caplog, status):
    class ProviderError(Exception):
        status_code = status

    runtime.llm.with_structured_output.return_value.ainvoke = AsyncMock(
        side_effect=ProviderError("private-key-and-response")
    )
    result = await build_workflow_graph(runtime).ainvoke({"messages": [HumanMessage("查询销售额")]})
    assert f"HTTP {status}" in result["final_answer"]
    assert result["messages"][-1].response_metadata["workflow_error"]
    assert "private-key-and-response" not in result["final_answer"] + caplog.text
    assert "ProviderError" in caplog.text


@pytest.mark.asyncio
async def test_missing_plan_tool_call_is_reported_as_validation_failure(runtime):
    runtime.llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=None)
    result = await build_workflow_graph(runtime).ainvoke({"messages": [HumanMessage("查询销售额")]})
    assert "校验" in result["final_answer"]


def test_named_table_field_count_limits_field_listing(runtime):
    plan = direct_plan("orders表有几个字段", runtime)
    assert plan.tasks[0].operation == "search_fields"
    assert plan.tasks[0].args["tables"] == ["orders"]


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [None, {"tasks": [{"id": "bad", "domain": "semantic", "operation": "invented"}]}])
async def test_plan_contract_repair_is_bounded_and_records_diagnostics(runtime, invalid):
    runtime.cfg.workflow.max_replans = 1
    valid = {
        "tasks": [{"id": "metrics", "domain": "semantic", "operation": "search_metrics", "args": {"mode": "list"}}]
    }
    invoke = AsyncMock(side_effect=[invalid, valid])
    runtime.llm.with_structured_output.return_value.ainvoke = invoke
    result = await build_workflow_graph(runtime).ainvoke({"messages": [HumanMessage("查看可用业务口径")]})
    assert not result["plan_error"] and invoke.await_count == 2
    assert "校验问题" in invoke.call_args.args[0][-1].content
    path = runtime.cfg.report_directory + "/.runs/" + result["request_id"] + "/events.jsonl"
    from pathlib import Path

    events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines()]
    failure = next(event for event in events if event["node"] == "plan_validation")
    assert failure["outputs"]["issues"]
    assert failure["inputs"]["attempt"] == 1


@pytest.mark.asyncio
async def test_unrepairable_plan_stops_at_configured_limit(runtime):
    runtime.cfg.workflow.max_replans = 1
    invoke = AsyncMock(return_value=None)
    runtime.llm.with_structured_output.return_value.ainvoke = invoke
    result = await build_workflow_graph(runtime).ainvoke({"messages": [HumanMessage("查询销售额")]})
    assert result["plan_error"] and invoke.await_count == 2
    assert not runtime.sql_calls


@pytest.mark.asyncio
async def test_clarification_interrupts_and_resumes_with_checkpoint(runtime):
    from langgraph.checkpoint.memory import InMemorySaver
    from langgraph.types import Command

    from openchatbi.workflow.models import QueryPlan

    plan = QueryPlan.model_validate(
        {
            "tasks": [
                {
                    "id": "clarify",
                    "domain": "semantic",
                    "operation": "clarify",
                    "args": {"question": "缺少利润数据，是否改做销量分析？", "options": ["分析销量", "补充数据"]},
                }
            ]
        }
    )
    graph = build_workflow_graph(runtime, checkpointer=InMemorySaver(), planner=lambda *_: plan)
    cfg = {"configurable": {"thread_id": "clarify-regression"}}
    result = await graph.ainvoke({"messages": [HumanMessage("利润归因")]}, cfg)
    assert result["__interrupt__"]
    resumed = await graph.ainvoke(Command(resume="分析销量"), cfg)
    assert resumed["results"]["clarify"]["status"] == "ok"
    assert not runtime.sql_calls
