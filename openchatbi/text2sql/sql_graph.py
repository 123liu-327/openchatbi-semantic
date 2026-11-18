"""SQL generation graph construction and execution."""

from openchatbi import config
from openchatbi.constants import SQL_EXECUTE_TIMEOUT, SQL_SUCCESS
from openchatbi.graph_state import SQLGraphState
from openchatbi.llm.llm import get_text2sql_llm
from openchatbi.text2sql.data import get_learned_sql_store
from openchatbi.text2sql.errors import RecoveryStrategy
from openchatbi.text2sql.generate_sql import create_sql_nodes


def _get_sql_retry_config() -> tuple[int, bool]:
    """Read retry settings from Config, defaulting to legacy values."""
    try:
        cfg = config.get()
    except ValueError:
        return 3, False
    max_retries = getattr(cfg, "sql_max_retries", 3)
    if not isinstance(max_retries, int) or max_retries < 0:
        max_retries = 3
    return max_retries, bool(getattr(cfg, "retry_on_timeout", False))


def _route_after_execute(state: SQLGraphState) -> str:
    """Conditional edge function to determine next action after execute_sql.

    Routing is strategy-driven: the last classified error's recovery_strategy
    decides whether to regenerate or end. Falls back to legacy code-based routing
    when no recovery_strategy is present (e.g. timeouts, untouched states).

    The returned value is a decision label (not a node name); the edge mapping
    in build_sql_graph binds each label to its destination node.

    Args:
        state (SQLGraphState): Current state

    Returns:
        str: "success" when execution succeeded, "regenerate_sql" to retry, "end" otherwise.
    """
    execution_result = state.get("sql_execution_result", "")
    retry_count = state.get("sql_retry_count", 0)
    max_retries, retry_on_timeout = _get_sql_retry_config()

    if execution_result == SQL_SUCCESS:
        return "success"

    # Timeouts are classified with non-retry strategies (ABORT / SURFACE_TO_USER),
    # which would make retry_on_timeout dead config if left to strategy routing;
    # honor the explicit opt-in before the strategy-driven branch.
    if execution_result == SQL_EXECUTE_TIMEOUT:
        if retry_on_timeout and retry_count < max_retries:
            return "regenerate_sql"
        return "end"

    previous_errors = state.get("previous_sql_errors", [])
    strategy = previous_errors[-1].get("recovery_strategy") if previous_errors else None

    if strategy is not None:
        if strategy in (RecoveryStrategy.SURFACE_TO_USER.value, RecoveryStrategy.ABORT.value):
            return "end"
        if strategy in (RecoveryStrategy.RETRY.value, RecoveryStrategy.RETRY_WITH_NEW_TABLE.value):
            return "regenerate_sql" if retry_count < max_retries else "end"
        return "end"

    # Legacy fallback: no structured strategy recorded.
    if retry_count < max_retries and (execution_result != SQL_EXECUTE_TIMEOUT or retry_on_timeout):
        return "regenerate_sql"
    return "end"


def route_after_confidence(state: SQLGraphState) -> str:
    """Route after the confidence gate based on the human decision.

    approve -> visualization; reject -> regenerate; edit -> re-execute the
    user-edited SQL. Approved SQL skips visualization when the caller sets
    visualize=False. Defaults to visualization when no decision is present
    (gate disabled or score above threshold) and visualization is not disabled.
    """
    decision = state.get("human_sql_decision", "approve")
    if decision == "reject":
        return "regenerate_sql"
    if decision == "edit":
        return "execute_sql"
    if state.get("visualize", True) is False:
        return "end"
    return "generate_visualization"


def build_sql_graph(
    catalog, checkpointer=None, memory_store=None, llm_provider=None, *, semantic=None, prepared_only=True
):
    """Build the sole SQL compilation graph; semantic discovery is a separate domain."""
    if not prepared_only:
        raise ValueError("Legacy SQL orchestration was removed; provide resolved semantics")
    from functools import partial

    from openchatbi.workflow.sql_graph import build_sql_generation_graph

    return build_sql_generation_graph(
        catalog,
        config.get(),
        get_text2sql_llm(llm_provider),
        partial(create_sql_nodes, learned_sql_store=get_learned_sql_store()),
        checkpointer=checkpointer,
        store=memory_store,
    )
