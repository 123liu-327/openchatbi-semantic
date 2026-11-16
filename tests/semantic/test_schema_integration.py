import json
from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage

from openchatbi.catalog.store.file_system import FileSystemCatalogStore
from openchatbi.constants import SQL_SUCCESS
from openchatbi.graph_state import SQLGraphState
from openchatbi.semantic.integration import prepare_selection
from openchatbi.semantic.layer import SemanticLayer
from openchatbi.semantic.models import SemanticBundle
from openchatbi.semantic.rules import RuleConflictError
from openchatbi.text2sql.generate_sql import create_sql_nodes
from openchatbi.text2sql.schema_linking import schema_linking
from scripts.validate_semantic_data import read_text
from tests.semantic.test_layer_from_config import DATA, demo_layer


def select(question, catalog, provider):
    llm = Mock()
    llm.invoke.return_value = AIMessage(
        content=json.dumps({"tables": [{"table": "sales_orders", "columns": ["order_id"]}]})
    )
    column = {"column_name": "order_id", "category": "dimension", "display_name": "ID", "description": "ID"}
    with (
        patch("openchatbi.text2sql.schema_linking.get_relevant_columns", return_value=["order_id"]),
        patch("openchatbi.text2sql.schema_linking.column_tables_mapping", {"order_id": ["sales_orders"]}),
        patch("openchatbi.text2sql.schema_linking.col_dict", {"order_id": column}),
        patch("openchatbi.text2sql.schema_linking.table_selection_retriever") as examples,
    ):
        examples.invoke.return_value = []
        return schema_linking(llm, catalog, semantic=provider)(
            SQLGraphState(messages=[], rewrite_question=question, info_entities={})
        )


@pytest.mark.parametrize(
    "question",
    [
        "收入",
        "销售额",
        "营业收入",
        "营收",
        "今年收入",
        "月度销售额",
        "按产品收入",
        "按区域收入",
        "成本",
        "销售成本",
        "毛利",
        "利润",
        "毛利润",
        "产品成本",
        "季度成本",
        "年度利润",
        "客户收入",
        "订单收入",
        "各区毛利",
        "本月营收",
    ],
)
def test_twenty_schema_questions_have_zero_forbidden_columns(question):
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    result = select(question, catalog, demo_layer())
    columns = {col for row in result["tables"] for col in row["columns"]}
    assert "gross_amount" not in columns and "standard_cost" not in columns
    if "利润" not in question:
        assert result["tables"]


def test_none_and_empty_provider_keep_selection_exactly():
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    empty = SemanticLayer(SemanticBundle(metrics={}, rules=(), edges=(), tables=frozenset()))
    assert (
        select("income", catalog, None)
        == select("income", catalog, empty)
        == {"tables": [{"table": "sales_orders", "columns": ["order_id"]}]}
    )


@pytest.mark.parametrize("question", ["revenue", "sales cost", "gross profit", "gross margin rate"])
def test_english_rewrites_keep_the_same_constraints(question):
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    result = select(question, catalog, demo_layer())
    assert result["tables"]
    names = {col for row in result["tables"] for col in row["columns"]}
    assert "gross_amount" not in names and "standard_cost" not in names


def test_unreachable_metric_source_blocks_single_query_but_optional_table_is_removed():
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    layer = demo_layer()
    required = prepare_selection(layer, "利润", [{"table": "sales_orders"}], catalog)
    assert required.tables == [] and "finance_expenses" in required.unreachable
    optional = prepare_selection(layer, "收入", [{"table": "sales_orders"}, {"table": "finance_expenses"}], catalog)
    assert optional.tables and "finance_expenses" in optional.unreachable
    assert "finance_expenses" not in {row["table"] for row in optional.tables}


def test_conflicting_canonical_metric_is_not_injected():
    layer = demo_layer()
    with pytest.raises(RuleConflictError, match="canonical metric revenue"):
        layer.render(question="含税收入", selected_tables=["sales_orders"])


def test_graph_assembly_injects_one_provider_into_both_consumers(mock_config):
    import openchatbi.text2sql.sql_graph as graph_module

    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    layer = demo_layer()
    llm = Mock()
    llm.bind_tools.return_value = llm
    with (
        patch.object(graph_module, "config") as config_handle,
        patch.object(graph_module, "get_llm", return_value=llm),
        patch.object(graph_module, "get_text2sql_llm", return_value=llm),
        patch.object(graph_module, "create_sql_nodes", wraps=graph_module.create_sql_nodes) as nodes,
        patch.object(graph_module, "schema_linking", wraps=graph_module.schema_linking) as linking,
    ):
        config_handle.get.return_value = mock_config
        graph = graph_module.build_sql_graph(catalog, None, None, semantic=layer)
    assert graph is not None
    assert nodes.call_args.kwargs["semantic"] is layer
    assert linking.call_args.kwargs["semantic"] is layer


def test_real_selection_generate_execute_path_with_deterministic_llm():
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    engine = catalog.get_sql_engine()
    try:
        with engine.connect() as conn:
            conn.connection.driver_connection.executescript(read_text(DATA / "catalog/schema.sql"))
        layer = demo_layer()
        result = select("收入", catalog, layer)
        assert {row["table"] for row in result["tables"]} == {"sales_orders", "exchange_rates"}
        llm = Mock()
        llm.invoke.return_value = AIMessage(content=layer.metric("revenue").sql_template)
        nodes = create_sql_nodes(llm, catalog, "sqlite", semantic=layer)
        state = SQLGraphState(messages=[], rewrite_question="收入", tables=result["tables"])
        with patch("openchatbi.text2sql.generate_sql.sql_example_retriever") as examples:
            examples.invoke.return_value = []
            generated = nodes[0](state)
        executed = nodes[1]({**state, **generated})
        assert executed["sql_execution_result"] == SQL_SUCCESS
        assert "66814000" in executed["data"]
        assert 'Column("gross_amount"' not in llm.invoke.call_args.args[0][0].content
    finally:
        engine.dispose()
