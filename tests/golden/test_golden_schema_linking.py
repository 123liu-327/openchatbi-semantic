"""Pin selection output and retry behavior before adding semantic rules."""

import json
from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage

from openchatbi.graph_state import SQLGraphState
from openchatbi.text2sql.schema_linking import schema_linking


@pytest.mark.parametrize(
    ("responses", "expected", "calls"),
    [
        (
            [{"tables": [{"table": "users", "columns": ["id"], "reason": "matched"}]}],
            {"tables": [{"table": "users", "columns": ["id"], "reason": "matched"}]},
            1,
        ),
        ([{"tables": [{"table": "unknown"}]}, {"tables": [{"table": "users"}]}], {"tables": [{"table": "users"}]}, 2),
        ([{"tables": []}] * 3, {}, 3),
        ([{"tables": [{"table": "unknown"}]}] * 3, {}, 3),
        (["malformed"] * 3, {}, 3),
    ],
    ids=["preserve-selection", "retry-invalid-table", "empty", "reject-unknown", "malformed-json"],
)
def test_legacy_selection_without_semantic_rules(responses, expected, calls):
    llm = Mock()
    llm.invoke.side_effect = [
        AIMessage(content=response if isinstance(response, str) else json.dumps(response)) for response in responses
    ]
    catalog = Mock()
    catalog.get_table_information.return_value = {"description": "Users", "selection_rule": "Use for users"}
    state = SQLGraphState(
        messages=[], rewrite_question="Count users", info_entities={"keywords": ["users"], "metrics": ["count"]}
    )
    column = {"column_name": "id", "category": "dimension", "display_name": "ID", "description": "Identifier"}
    with (
        patch("openchatbi.text2sql.schema_linking.get_relevant_columns", return_value=["id"]) as retrieve,
        patch("openchatbi.text2sql.schema_linking.column_tables_mapping", {"id": ["users"]}),
        patch("openchatbi.text2sql.schema_linking.col_dict", {"id": column}),
        patch("openchatbi.text2sql.schema_linking.table_selection_retriever") as examples,
    ):
        examples.invoke.return_value = []
        assert schema_linking(llm, catalog)(state) == expected
    retrieve.assert_called_once_with(["users"], [], ["count"])
    assert llm.invoke.call_count == calls
    prompt = llm.invoke.call_args.args[0][0].content
    assert "\n## Table: users \n### Table Description: \nUsers \n### Rule: \nUse for users" in prompt
    assert '- dimension(id, ID, "Identifier")' in prompt


def test_missing_question_skips_selection():
    llm, catalog = Mock(), Mock()
    assert schema_linking(llm, catalog)(SQLGraphState(messages=[])) == {}
    llm.invoke.assert_not_called()
    catalog.get_table_information.assert_not_called()
