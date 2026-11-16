from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage

from openchatbi.catalog.store.file_system import FileSystemCatalogStore
from openchatbi.graph_state import SQLGraphState
from openchatbi.semantic.layer import SemanticLayer
from openchatbi.semantic.models import SemanticBundle
from openchatbi.text2sql.generate_sql import create_sql_nodes
from tests.semantic.test_layer_from_config import DATA, demo_layer


@pytest.mark.parametrize("node_index", [0, 2])
def test_none_and_empty_provider_preserve_actual_prompt(node_index):
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    empty = SemanticLayer(SemanticBundle(metrics={}, rules=(), edges=(), tables=frozenset()))
    outputs = []
    for provider in (None, empty):
        llm = Mock()
        llm.invoke.return_value = AIMessage(content="SELECT 1")
        state = SQLGraphState(messages=[], rewrite_question="收入", tables=[{"table": "sales_orders", "columns": []}])
        with (
            patch("openchatbi.text2sql.generate_sql.sql_example_retriever") as examples,
            patch("openchatbi.text2sql.generate_sql.datetime.datetime") as clock,
        ):
            clock.now.return_value.strftime.return_value = "FIXED_TIME"
            examples.invoke.return_value = []
            create_sql_nodes(llm, catalog, "sqlite", semantic=provider)[node_index](state)
        outputs.append(llm.invoke.call_args.args[0][0].content)
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize("node_index", [0, 2])
def test_semantic_block_is_injected_once_and_columns_are_hard_filtered(node_index):
    catalog = FileSystemCatalogStore(str(DATA / "catalog"), {"uri": "sqlite:///:memory:"})
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="SELECT 1")
    state = SQLGraphState(
        messages=[],
        rewrite_question="收入",
        tables=[{"table": "sales_orders", "columns": []}, {"table": "exchange_rates", "columns": []}],
    )
    with patch("openchatbi.text2sql.generate_sql.sql_example_retriever") as examples:
        examples.invoke.return_value = []
        create_sql_nodes(llm, catalog, "sqlite", semantic=demo_layer())[node_index](state)
    prompt = llm.invoke.call_args.args[0][0].content
    assert prompt.count("## Metrics (canonical") == 1
    assert prompt.count("## Table relationships") == 1
    assert prompt.count("## Business rules") == 1
    assert 'Column("gross_amount"' not in prompt
    assert 'Column("net_amount"' in prompt
