"""Freeze the legacy prompt before introducing the optional semantic provider.

T8 must also exercise these snapshots with explicit None and an empty provider
once those injection arguments exist. No production seam is added by T1.
"""

import datetime
from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage

from openchatbi.constants import datetime_format
from openchatbi.graph_state import SQLGraphState
from openchatbi.prompts.system_prompt import get_text2sql_dialect_prompt_template
from openchatbi.text2sql.generate_sql import create_sql_nodes


@pytest.mark.parametrize("node_index", [0, 2], ids=["generate", "regenerate"])
@pytest.mark.parametrize("metric", [None, "", "### Derived metrics\nRatio = COUNT(active) / COUNT(*)\n"])
def test_legacy_full_prompt_snapshot(node_index, metric):
    """Pin every byte, including aliases, Unicode, table order and separators."""
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="SELECT 1")
    catalog = Mock()
    first = {"description": "用户记录", "sql_rule": "### Rules\nOnly active users\n"}
    if metric is not None:
        first["derived_metric"] = metric
    catalog.get_table_information.side_effect = [first, {"description": "Event records", "sql_rule": ""}]
    catalog.get_column_list.side_effect = [
        [
            {"column_name": "id", "type": "bigint", "display_name": "编号", "description": "主键", "alias": "ID"},
            {"column_name": "active", "type": "boolean", "display_name": "Active", "description": "Flag", "alias": ""},
        ],
        [{"column_name": "user_id", "type": "bigint", "display_name": "User", "description": "Owner"}],
    ]
    state = SQLGraphState(
        messages=[],
        rewrite_question="Count active users",
        tables=[{"table": "users", "columns": ["id"]}, {"table": "events", "columns": []}],
    )
    fixed_time = datetime.datetime(2026, 11, 16, 9, 0, 0)
    expected_schema = (
        "## Table users\n用户记录\n"
        "### Columns\nColumn(Name, Type, Display Name, Description):\n[\n"
        '    Column("id", bigint, 编号, "alias(ID)主键"),\n'
        '    Column("active", boolean, Active, "Flag"),\n]\n' + (metric or "") + "### Rules\nOnly active users\n\n"
        "## Table events\nEvent records\n"
        "### Columns\nColumn(Name, Type, Display Name, Description):\n[\n"
        '    Column("user_id", bigint, User, "Owner"),\n]\n'
    )
    expected = (
        get_text2sql_dialect_prompt_template("presto")
        .replace("[table_schema]", expected_schema)
        .replace("[examples]", "")
        .replace("[time_field_placeholder]", fixed_time.strftime(datetime_format))
    )
    with (
        patch("openchatbi.text2sql.generate_sql.sql_example_retriever") as retriever,
        patch("openchatbi.text2sql.generate_sql.datetime.datetime") as clock,
    ):
        retriever.invoke.return_value = []
        clock.now.return_value = fixed_time
        result = create_sql_nodes(llm, catalog, "presto")[node_index](state)
    assert result["sql"] == "SELECT 1"
    assert llm.invoke.call_args.args[0][0].content == expected
    assert catalog.get_table_information.call_args_list == [(("users",),), (("events",),)]
