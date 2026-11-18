"""Both application entry points use the sole four-domain workflow."""

from unittest.mock import patch
import pytest
from langchain_core.messages import HumanMessage
from openchatbi.agent_graph import build_agent_graph_async, build_agent_graph_sync
from openchatbi.workflow.graph import build_workflow_graph
from tests.workflow.test_workflow import runtime  # noqa: F401


def test_build_agent_graph_sync_includes_data_analysis(runtime):
    compiled = build_workflow_graph(runtime)
    with (
        patch("openchatbi.workflow.factory.build_configured_workflow", return_value=compiled),
        patch("openchatbi.agent_graph.config.get", return_value=runtime.cfg),
        patch("openchatbi.agent_graph._get_mcp_servers", return_value=[]),
        patch("openchatbi.agent_graph.create_mcp_tools_sync", return_value=[]),
    ):
        graph = build_agent_graph_sync(runtime.catalog)
        result = graph.invoke({"messages": [HumanMessage("有哪些指标")]})
    assert {"semantic", "text2sql", "delivery"} <= graph.get_graph().nodes.keys()
    assert runtime.registry.get("analyze", "delivery").enabled
    assert result["results"]["metrics"]["status"] == "ok"


@pytest.mark.asyncio
async def test_build_agent_graph_async_includes_data_analysis(runtime):
    compiled = build_workflow_graph(runtime)
    with (
        patch("openchatbi.workflow.factory.build_configured_workflow", return_value=compiled),
        patch("openchatbi.agent_graph.config.get", return_value=runtime.cfg),
        patch("openchatbi.agent_graph._get_mcp_servers", return_value=[]),
        patch("openchatbi.agent_graph.get_mcp_tools_async", return_value=[]),
    ):
        graph = await build_agent_graph_async(runtime.catalog)
        result = await graph.ainvoke({"messages": [HumanMessage("有哪些指标")]})
    assert result["results"]["metrics"]["status"] == "ok"
    assert "llm_node" not in graph.get_graph().nodes
