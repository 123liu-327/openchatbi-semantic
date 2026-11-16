"""Literal text contracts for the sole prompt-block renderer."""

from openchatbi.semantic.models import BusinessRule, JoinEdge, JoinKey, MetricSpec
from openchatbi.semantic.render import render_semantic_block


def test_render_snapshot():
    metrics = (
        MetricSpec(
            name="total",
            label="Total",
            level="atomic",
            formula="SUM(amount)",
            definition="Completed records",
            time_field="events.day",
            filters=("status = 'done'",),
        ),
        MetricSpec(name="ratio", label="Ratio", level="composite", requires_multi_step=True, depends_on=("total",)),
    )
    edges = (
        JoinEdge(
            child="events",
            parent="rates",
            keys=(JoinKey(left="day", right="day"), JoinKey(left="currency", right="currency")),
        ),
    )
    rules = (
        BusinessRule(
            name="required_amount",
            trigger_any=("total", "amount"),
            require_columns=("amount",),
            forbid_columns=("other_amount",),
            require_tables=("events",),
            scope_tables=("events",),
            note="Canonical amount",
        ),
    )
    expected = (
        "## Metrics (canonical definitions — do not redefine)\n"
        "- total [atomic] = SUM(amount)  -- Total；口径: Completed records；时间字段: events.day；过滤: status = 'done'\n"
        "- ratio [composite] requires multi-step; depends on total  -- Ratio\n\n"
        "## Table relationships (use these ON conditions; do not invent join keys)\n"
        "- events LEFT JOIN rates ON events.day = rates.day\n    AND events.currency = rates.currency\n\n"
        "## Business rules (hard constraints)\n"
        "- 命中 [total, amount] 时：必须使用 amount；禁止使用 other_amount；必须包含表 events；作用域 events（Canonical amount）"
    )
    assert render_semantic_block(metrics=metrics, edges=edges, rules=rules) == expected
    assert render_semantic_block(metrics=metrics, edges=edges, rules=rules) == expected


def test_empty_block_emits_no_titles():
    assert render_semantic_block() == ""


def test_single_section_and_complete_sql_are_preserved():
    sql = "SELECT SUM(amount)\nFROM events"
    metric = MetricSpec(name="total", label="Total", level="atomic", sql_template=sql)
    block = render_semantic_block(metrics=(metric,))
    assert sql in block
    assert "Table relationships" not in block
    assert "Business rules" not in block


def test_inner_join_and_multistep_without_dependencies():
    edge = JoinEdge(child="events", parent="users", keys=(JoinKey(left="user_id", right="id"),), join_type="inner")
    metric = MetricSpec(name="summary", label="Summary", level="composite", requires_multi_step=True)
    block = render_semantic_block(edges=(edge,), metrics=(metric,))
    assert "- events JOIN users ON events.user_id = users.id" in block
    assert "requires multi-step  -- Summary" in block
    assert "depends on" not in block
