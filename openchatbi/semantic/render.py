"""The single deterministic text renderer; callers select records before rendering."""

from collections.abc import Sequence

from openchatbi.semantic.models import BusinessRule, JoinEdge, MetricSpec


def render_semantic_block(
    *, metrics: Sequence[MetricSpec] = (), edges: Sequence[JoinEdge] = (), rules: Sequence[BusinessRule] = ()
) -> str:
    """Preserve input order and emit only nonempty sections, without I/O.

    This formats canonical data; it never infers definitions, join keys or rule
    precedence. Matching and planning belong to the provider and their modules.
    """
    sections: list[str] = []
    if metrics:
        lines = ["## Metrics (canonical definitions — do not redefine)"]
        for metric in metrics:
            prefix = f"- {metric.name} [{metric.level.value}]"
            if metric.requires_multi_step:
                text = prefix + " requires multi-step"
                if metric.depends_on:
                    text += "; depends on " + ", ".join(metric.depends_on)
            else:
                text = prefix + " = " + (metric.formula or metric.sql_template or "")
            details = [metric.label]
            if metric.definition:
                details.append("口径: " + metric.definition)
            if metric.time_field:
                details.append("时间字段: " + metric.time_field)
            if metric.filters:
                details.append("过滤: " + "; ".join(metric.filters))
            lines.append(text + "  -- " + "；".join(details))
        sections.append("\n".join(lines))
    if edges:
        lines = ["## Table relationships (use these ON conditions; do not invent join keys)"]
        for edge in edges:
            join_type = "JOIN" if edge.join_type == "inner" else "LEFT JOIN"
            conditions = [f"{edge.child}.{key.left} = {edge.parent}.{key.right}" for key in edge.keys]
            lines.append(f"- {edge.child} {join_type} {edge.parent} ON " + "\n    AND ".join(conditions))
        sections.append("\n".join(lines))
    if rules:
        lines = ["## Business rules (hard constraints)"]
        for rule in rules:
            constraints = []
            if rule.require_columns:
                constraints.append("必须使用 " + ", ".join(rule.require_columns))
            if rule.forbid_columns:
                constraints.append("禁止使用 " + ", ".join(rule.forbid_columns))
            if rule.require_tables:
                constraints.append("必须包含表 " + ", ".join(rule.require_tables))
            if rule.scope_tables:
                constraints.append("作用域 " + ", ".join(rule.scope_tables))
            text = "- 命中 [" + ", ".join(rule.trigger_any) + "] 时：" + "；".join(constraints)
            if rule.note:
                text += "（" + rule.note + "）"
            lines.append(text)
        sections.append("\n".join(lines))
    return "\n\n".join(sections)
