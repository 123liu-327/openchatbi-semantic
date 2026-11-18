"""Use the original SQL graph and real SQLite execution behind the new adapter."""

import json
from unittest.mock import AsyncMock, Mock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda

from openchatbi.catalog.store.file_system import FileSystemCatalogStore
from openchatbi.workflow.graph import build_workflow_graph
from openchatbi.workflow.models import QueryPlan
from openchatbi.workflow.registry import Registry
from openchatbi.workflow.retrieval import EmbeddingSettings, RetrievalService
from openchatbi.workflow.runtime import WorkflowRuntime
from scripts.validate_semantic_data import read_text
from tests.semantic.test_layer_from_config import DATA, demo_layer
from tests.workflow.test_workflow import FakeEmbedding, task


@pytest.mark.asyncio
@pytest.mark.parametrize("retry", [False, True])
async def test_original_sql_graph_through_new_adapter_and_csv(runtime, tmp_path, mock_config, retry):
    mock_config.embedding_model = None
    import openchatbi.text2sql.sql_graph as module

    mock_config.dialect = "sqlite"
    mock_config.report_directory = runtime.cfg.report_directory

    layer = demo_layer()
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": f"sqlite:///{(tmp_path/'demo.sqlite').as_posix()}"})
    engine = catalog.get_sql_engine()
    with engine.connect() as connection:
        connection.connection.driver_connection.executescript(read_text(DATA / "catalog/schema.sql"))
    llm = Mock()
    llm.bind_tools.return_value = llm
    llm.invoke.side_effect = ([AIMessage("SELECT made_up FROM sales_orders")] if retry else []) + [
        AIMessage(content=layer.metric("revenue").sql_template)
    ]

    with (
        patch.object(module, "config") as graph_config,
        patch("openchatbi.text2sql.generate_sql.config") as execution_config,
        patch.object(module, "get_text2sql_llm", return_value=llm),
        patch("openchatbi.text2sql.generate_sql.sql_example_retriever") as examples,
    ):
        graph_config.get.return_value = mock_config
        execution_config.get.return_value = mock_config
        examples.invoke.return_value = []
        original = module.build_sql_graph(catalog, None, None, prepared_only=True)
        assert not {"information_extraction", "table_selection", "search_knowledge"} & original.get_graph().nodes.keys()

        async def sql_without_discovery(state):
            with (
                patch.object(
                    catalog, "get_table_information", side_effect=AssertionError("SQL reloaded table metadata")
                ),
                patch.object(catalog, "get_column_list", side_effect=AssertionError("SQL reloaded fields")),
                patch.object(catalog, "get_table_list", side_effect=AssertionError("SQL discovered tables")),
            ):
                return await original.ainvoke(state)

        retrieval = RetrievalService(
            catalog, layer, tmp_path / "chroma", settings=EmbeddingSettings(dimensions=2), embedder=FakeEmbedding()
        )
        retrieval.build()
        semantic_llm = Mock()
        semantic_llm.ainvoke = AsyncMock(
            side_effect=[
                AIMessage(json.dumps({"rewrite_question": "收入"})),
                AIMessage(
                    json.dumps(
                        {
                            "tables": [
                                {"table": name, "columns": []} for name in layer.metric("revenue").source_tables
                            ],
                            "metric_names": ["revenue"],
                        }
                    )
                ),
            ]
        )
        new_runtime = WorkflowRuntime(
            runtime.cfg,
            catalog,
            layer,
            RunnableLambda(sql_without_discovery),
            Registry(),
            semantic_llm,
            retrieval=retrieval,
        )
        new_runtime.cfg.workflow.authorization.table_columns = {name: ["*"] for name in catalog.get_table_list()}
        plan = QueryPlan(
            tasks=[
                task(
                    "meaning",
                    "semantic",
                    "resolve_query",
                    {
                        "question": "收入",
                        "tables": list(layer.metric("revenue").source_tables),
                        "metric_names": ["revenue"],
                    },
                ),
                task(
                    "sales",
                    "text2sql",
                    "query",
                    {
                        "question": "收入",
                        "visualize": False,
                        "semantic_ref": {"$ref": {"task_id": "meaning", "path": "/semantic_ref"}},
                    },
                    ["meaning"],
                ),
                task(
                    "csv",
                    "delivery",
                    "export_csv",
                    {"dataset_refs": [{"$ref": {"task_id": "sales", "path": "/dataset_ref"}}]},
                    ["sales"],
                ),
            ]
        )
        result = await build_workflow_graph(new_runtime, planner=lambda *_: plan).ainvoke(
            {"messages": [HumanMessage("query and export")]}
        )
    assert result["results"]["sales"]["status"] == "ok", result
    assert result["results"]["csv"]["status"] == "ok"
    dataset = new_runtime.context(result["request_id"]).datasets[result["results"]["sales"]["data"]["dataset_ref"]]
    assert float(dataset.rows[0][0]) == 66_814_000
    assert "/api/download/report/" in result["final_answer"]
    assert semantic_llm.ainvoke.await_count == 1
    assert llm.invoke.call_count == (2 if retry else 1)
    engine.dispose()
