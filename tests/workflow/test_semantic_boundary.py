"""The semantic domain owns retrieval and selection; SQL consumes only its contract."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage

from openchatbi.workflow.graph import validate_plan
from openchatbi.workflow.models import QueryPlan
from openchatbi.workflow.security import SQLPolicyError, check_resolved_sql
from openchatbi.workflow.semantic_query import SemanticResolver
from tests.workflow.test_workflow import task


def real_resolver(runtime, *, selection=None):
    runtime.semantic_resolver = SemanticResolver(runtime)
    runtime.retrieval.search = Mock(
        side_effect=lambda kind, query, top_k, tables=None: {
            "metrics": [{"id": "revenue", "distance": 0.2}],
            "tables": [{"id": "orders", "distance": 0.1}],
            "fields": [{"id": "orders.amount", "distance": 0.1}, {"id": "orders.tax", "distance": 0}],
        }[kind]
    )
    runtime.llm.ainvoke = AsyncMock(
        side_effect=[
            AIMessage(json.dumps({"metric_names": ["revenue"]})),
            AIMessage(json.dumps({"rewrite_question": "销售额", "metrics": ["销售额"]})),
            AIMessage(json.dumps(selection or {"tables": [{"table": "orders", "columns": ["amount"]}]})),
        ]
    )
    return runtime


@pytest.mark.asyncio
async def test_semantic_selects_once_and_sql_never_reads_catalog_metadata(runtime):
    real_resolver(runtime)
    context = runtime.context("a" * 32)
    with patch.object(SemanticResolver, "selection_examples", return_value="Question: 销售额 Selected Tables: orders"):
        meaning = await runtime.registry.call(
            "resolve_query", {"question": "销售额", "field_selection": "llm"}, context, "meaning", "semantic"
        )
    assert meaning.status == "ok", meaning.errors
    assert [operation["function"] for operation in meaning.data["operations"]] == [
        "search_tables",
    ]
    prepared = context.semantics[meaning.data["semantic_ref"]]
    assert prepared.tables[0].columns == ["amount", "status"]
    assert "tax" not in {column for row in prepared.tables for column in row.columns}
    calls = runtime.llm.ainvoke.call_args_list
    assert "specialized language expert" in calls[1].args[0][0].content
    assert "professional SQL engineer" in calls[2].args[0][0].content
    assert "Question: 销售额 Selected Tables: orders" in calls[2].args[0][0].content
    with (
        patch.object(runtime.catalog, "get_table_list", side_effect=AssertionError("SQL must not discover tables")),
        patch.object(runtime.catalog, "get_column_list", side_effect=AssertionError("SQL must not retrieve fields")),
    ):
        result = await runtime.registry.call(
            "query",
            {
                "question": "销售额",
                "semantic_ref": meaning.data["semantic_ref"],
            },
            context,
            "sql",
            "text2sql",
        )
    assert result.status == "ok", result.errors
    state = runtime.sql_calls[0]
    assert state["tables"] == [{"table": "orders", "columns": ["amount", "status"]}]
    assert state["prepared_schema"] == prepared.schema_prompt
    assert runtime.llm.ainvoke.await_count == 3


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "selection",
    [
        {"tables": [{"table": "made_up", "columns": []}]},
        {"tables": [{"table": "orders", "columns": ["tax"]}]},
        {"tables": [{"table": "orders", "columns": ["made_up"]}]},
        {"tables": [{"table": "orders", "columns": []}], "metric_names": ["made_up"]},
    ],
)
async def test_bad_semantic_selection_never_reaches_sql(runtime, selection):
    real_resolver(runtime, selection=selection)
    context = runtime.context("a" * 32)
    with patch.object(SemanticResolver, "selection_examples", return_value=""):
        result = await runtime.registry.call(
            "resolve_query", {"question": "销售额", "field_selection": "llm"}, context, "meaning"
        )
    assert result.status == "error"
    assert not context.semantics and not runtime.sql_calls


@pytest.mark.asyncio
async def test_resolved_contract_is_request_scoped_persistent_and_rejects_changed_question(runtime):
    context = runtime.context("a" * 32)
    meaning = await runtime.registry.call("resolve_query", {"question": "销售额"}, context, "s")
    arguments = {"question": "销售额", "semantic_ref": meaning.data["semantic_ref"]}
    other = await runtime.registry.call("query", arguments, runtime.context("b" * 32), "other")
    changed = await runtime.registry.call("query", {**arguments, "question": "利润"}, context, "changed")
    missing = await runtime.registry.call("query", {"question": "销售额"}, context, "missing")
    assert all(result.status == "error" for result in (other, changed, missing))
    assert not runtime.sql_calls
    runtime.contexts.clear()
    result = await runtime.registry.call("query", arguments, runtime.context("a" * 32), "restored")
    assert result.status == "ok"
    runtime.snapshot_id = "changed"
    stale = await runtime.registry.call("query", arguments, runtime.context("a" * 32), "stale")
    assert stale.status == "error" and len(runtime.sql_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sql,allowed",
    [
        ("SELECT amount FROM orders", True),
        ("WITH x AS (SELECT amount FROM orders) SELECT SUM(amount) FROM x", True),
        ("SELECT SUM(amount) AS total FROM orders ORDER BY total", True),
        ("SELECT COUNT(*) FROM orders", True),
        ("SELECT * FROM orders", False),
        ("SELECT o.* FROM orders o", False),
        ("SELECT * FROM other_table", False),
        ("SELECT tax FROM orders", False),
        ("SELECT missing FROM orders", False),
        ("WITH x AS (SELECT tax FROM orders) SELECT * FROM x", False),
        ("SELECT * FROM other_database.orders", False),
    ],
)
async def test_sql_cannot_expand_confirmed_table_or_field_scope(runtime, sql, allowed):
    context = runtime.context("a" * 32)
    output = await runtime.registry.call("resolve_query", {"question": "销售额"}, context, "s")
    prepared = context.semantics[output.data["semantic_ref"]]
    if allowed:
        check_resolved_sql(sql, prepared, "sqlite")
    else:
        with pytest.raises(SQLPolicyError):
            check_resolved_sql(sql, prepared, "sqlite")


@pytest.mark.asyncio
async def test_unavailable_vector_index_is_not_replaced_by_sql_discovery(runtime):
    runtime.semantic_resolver = SemanticResolver(runtime)
    result = await runtime.registry.call("resolve_query", {"question": "销售额"}, runtime.context("a" * 32), "s")
    assert result.status == "error" and not runtime.sql_calls
    runtime.llm.ainvoke.assert_not_called()


def test_plan_must_provide_semantic_dependency_for_sql(runtime):
    plan = QueryPlan(tasks=[task("sql", "text2sql", "query", {"question": "销售额"})])
    with pytest.raises(ValueError):
        validate_plan(plan, runtime.registry, 20)


@pytest.mark.asyncio
async def test_hybrid_field_selection_skips_llm_selection(runtime):
    real_resolver(runtime)
    context = runtime.context("c" * 32)
    output = await runtime.registry.call("resolve_query", {"question": "销售额"}, context, "hybrid")
    assert output.status == "ok", output.errors
    assert runtime.llm.ainvoke.await_count == 2  # Metric choice and question extraction only.
    prepared = context.semantics[output.data["semantic_ref"]]
    assert prepared.field_selection == "hybrid"
    assert prepared.tables[0].columns == ["amount", "status"]
    assert any(op["function"] == "search_fields" for op in output.data["operations"])
