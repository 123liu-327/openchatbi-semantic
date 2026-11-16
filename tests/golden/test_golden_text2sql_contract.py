"""Golden contracts for the text2sql pipeline.

These tests exist to make a refactor *visible*. The semantic-layer work injects
metric definitions, join clauses and business rules into the prompt built by
``generate_sql._get_table_schema_prompt``, and adds constraints around schema
linking; both sit on the hot path of every SQL answer. Before touching either,
freeze what the pipeline does today so a behaviour change has to be a deliberate
edit to this file.

Rule of thumb for this module: assert on *contracts* (ordering, taxonomy,
routing labels, state surface), not on incidental formatting. If a change here
is legitimate, update the test *and* say why in the commit message.
"""

from unittest.mock import Mock, patch

import pytest
from langchain_core.messages import AIMessage
from sqlalchemy.exc import OperationalError, ProgrammingError, TimeoutError

from openchatbi.constants import SQL_EXECUTE_TIMEOUT, SQL_SUCCESS
from openchatbi.graph_state import SQLGraphState
from openchatbi.prompts.system_prompt import get_text2sql_dialect_prompt_template
from openchatbi.text2sql.errors import (
    DBTimeoutError,
    RecoveryStrategy,
    SQLSyntaxError,
    classify_sql_exception,
)
from openchatbi.text2sql.generate_sql import create_sql_nodes
from openchatbi.text2sql.sql_graph import _route_after_execute


def _mock_llm() -> Mock:
    llm = Mock()
    llm.invoke.return_value = AIMessage(content="SELECT * FROM users")
    return llm


def _mock_catalog() -> Mock:
    catalog = Mock()
    catalog.get_table_information.return_value = {
        "description": "User data table",
        "sql_rule": "### SQL Rules\n- always filter active users",
        "derived_metric": "#### Derived Ratio Metrics\nActive Rate: COUNT(active) / COUNT(*)",
    }
    catalog.get_column_list.return_value = [
        {
            "column_name": "user_id",
            "type": "bigint",
            "display_name": "User ID",
            "description": "Unique user identifier",
            "alias": "",
        }
    ]
    return catalog


def _generate_prompt(catalog: Mock | None = None) -> str:
    """Run generate_sql_node and return the system prompt it actually sent."""
    llm = _mock_llm()
    generate_node, _, _, _, _, _ = create_sql_nodes(llm, catalog or _mock_catalog(), "presto")
    state = SQLGraphState(
        messages=[],
        rewrite_question="How many active users are there?",
        tables=[{"table": "users", "columns": []}],
    )
    with patch("openchatbi.text2sql.generate_sql.sql_example_retriever") as retriever:
        retriever.invoke.return_value = []
        generate_node(state)
    messages = llm.invoke.call_args[0][0]
    return messages[0].content


class TestTableSchemaPromptContract:
    """Contract of the per-table schema block injected into the SQL prompt."""

    def test_table_block_orders_description_columns_metric_rule(self):
        """The semantic layer will replace the derived_metric slot.

        Order matters: columns must precede metrics (metrics are defined in terms
        of columns), and metrics must precede free-form sql_rule (machine-readable
        definitions outrank prose when they disagree).
        """
        prompt = _generate_prompt()

        description_at = prompt.index("User data table")
        columns_at = prompt.index('Column("user_id", bigint, User ID,')
        derived_metric_at = prompt.index("Active Rate: COUNT(active)")
        sql_rule_at = prompt.index("always filter active users")

        assert description_at < columns_at < derived_metric_at < sql_rule_at

    def test_table_block_header_and_column_line_format(self):
        """HEADER + Column(...) shape is what the dialect prompt is written against."""
        prompt = _generate_prompt()

        assert "## Table users" in prompt
        assert 'Column("user_id", bigint, User ID,' in prompt
        assert "Unique user identifier" in prompt

    def test_derived_metric_is_passed_through_verbatim(self):
        """Today the metric text is opaque prose; no parsing, no expansion.

        This is the exact gap the semantic layer closes, so the test pins the
        *current* passthrough to make the change explicit rather than accidental.
        """
        prompt = _generate_prompt()

        assert "Active Rate: COUNT(active) / COUNT(*)" in prompt
        # No dependency expansion, no level annotation exists yet.
        assert "depends on" not in prompt

    def test_missing_table_information_does_not_invent_metric_or_rule(self):
        catalog = _mock_catalog()
        catalog.get_table_information.return_value = {
            "description": "Bare table",
            "sql_rule": "",
            "derived_metric": "",
        }
        prompt = _generate_prompt(catalog)

        assert "Bare table" in prompt
        assert "Active Rate" not in prompt


class TestSQLErrorTaxonomyContract:
    """The retry loop routes on this taxonomy; a refactor must not reclassify."""

    @pytest.mark.parametrize(
        ("exc", "expected_type", "expected_strategy"),
        [
            (ProgrammingError("stmt", {}, Exception("1064 syntax error")), SQLSyntaxError, RecoveryStrategy.RETRY),
            (TimeoutError("timed out"), DBTimeoutError, RecoveryStrategy.ABORT),
        ],
    )
    def test_classification_pairs(self, exc, expected_type, expected_strategy):
        err = classify_sql_exception(exc)

        assert isinstance(err, expected_type)
        assert err.recovery_strategy == expected_strategy

    def test_unknown_operational_error_stays_retryable(self):
        err = classify_sql_exception(OperationalError("stmt", {}, Exception("some driver noise")))

        assert err.recovery_strategy == RecoveryStrategy.RETRY


class TestExecuteRoutingContract:
    """``_route_after_execute`` decides success / regenerate / end."""

    def test_success_routes_to_score_sql(self):
        assert _route_after_execute({"sql_execution_result": SQL_SUCCESS}) == "success"

    def test_timeout_is_not_retried_unless_opted_in(self):
        state = {
            "sql_execution_result": SQL_EXECUTE_TIMEOUT,
            "sql_retry_count": 0,
            "previous_sql_errors": [],
        }
        assert _route_after_execute(state) == "end"

    def test_retryable_error_regenerates_until_budget_is_spent(self):
        state = {
            "sql_execution_result": "some error",
            "sql_retry_count": 0,
            "previous_sql_errors": [{"recovery_strategy": RecoveryStrategy.RETRY.value}],
        }
        assert _route_after_execute(state) == "regenerate_sql"
        # Default budget is 3 attempts (config default); 3 retries already spent -> stop.
        state["sql_retry_count"] = 3
        assert _route_after_execute(state) == "end"

    def test_surface_to_user_never_regenerates(self):
        state = {
            "sql_execution_result": "some error",
            "sql_retry_count": 0,
            "previous_sql_errors": [{"recovery_strategy": RecoveryStrategy.SURFACE_TO_USER.value}],
        }
        assert _route_after_execute(state) == "end"


class TestGraphStateContract:
    """Adding a state field is a schema change for persistence; make it explicit."""

    FROZEN_FIELDS = {
        "messages",
        "visualize",
        "rewrite_question",
        "tables",
        "info_entities",
        "sql",
        "sql_retry_count",
        "extraction_tool_rounds",
        "sql_execution_result",
        "schema_info",
        "data",
        "previous_sql_errors",
        "visualization_dsl",
        "sql_confidence",
        "confidence_status",
        "confidence_reasons",
        "confidence_diagnostics",
        "human_sql_decision",
        "recovery_strategy",
    }

    def test_sql_graph_state_is_a_superset_of_the_frozen_contract(self):
        declared = set(SQLGraphState.__annotations__)

        missing = self.FROZEN_FIELDS - declared
        assert not missing, f"state fields removed without a migration plan: {sorted(missing)}"


class TestDialectPromptContract:
    """The dialect prompt is the template the schema block is substituted into."""

    def test_presto_prompt_keeps_its_three_placeholders(self):
        prompt = get_text2sql_dialect_prompt_template("presto")

        for placeholder in ("[table_schema]", "[examples]", "[time_field_placeholder]"):
            assert placeholder in prompt
