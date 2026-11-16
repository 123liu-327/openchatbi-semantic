"""Schema selection boundary for the optional semantic provider."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from openchatbi.semantic.ports import SemanticProvider
from openchatbi.semantic.rules import RuleCatalog, RuleConflictError, apply_rules, validate_metric_rules


@dataclass(frozen=True)
class SemanticSelection:
    tables: list[dict[str, Any]]
    unreachable: tuple[str, ...] = ()
    reason: str = ""


def prepare_selection(
    semantic: SemanticProvider, question: str, selected: Sequence[Mapping[str, Any]], catalog: RuleCatalog
) -> SemanticSelection:
    if getattr(semantic, "is_empty", False) is True:
        return SemanticSelection([dict(row) for row in selected])
    metrics = semantic.closure([metric.name for metric in semantic.search_metrics(question)])
    active = semantic.rules_for(question, [row["table"] for row in selected])
    validate_metric_rules(metrics, active)
    required = {table for metric in metrics for table in metric.source_tables}
    required.update(table for rule in active for table in rule.require_tables)
    required.update(value.rsplit(".", 1)[0] for rule in active for value in rule.require_columns if "." in value)
    rows = {row["table"]: dict(row) for row in selected}
    for table in sorted(required):
        rows.setdefault(table, {"table": table, "columns": []})
    plan = semantic.plan_join("", tuple(rows))
    if plan.unreachable and (set(plan.unreachable) & required or not metrics):
        return SemanticSelection([], plan.unreachable, plan.reason)
    for table in plan.unreachable:
        rows.pop(table, None)
    if plan.anchor:
        rows.setdefault(plan.anchor, {"table": plan.anchor, "columns": []})
    for edge in plan.steps:
        rows.setdefault(edge.parent, {"table": edge.parent, "columns": []})
    # Preserve the legacy full-schema basis, then apply hard exclusions. A
    # model's incomplete column list must not hide metric/filter/join fields.
    constrained = apply_rules([{**row, "columns": []} for row in rows.values()], active, catalog)
    columns = {row["table"]: set(row["columns"]) for row in constrained}
    for edge in plan.steps:
        for key in edge.keys:
            if key.left not in columns[edge.child] or key.right not in columns[edge.parent]:
                raise RuleConflictError(
                    f"business rule removes join key: {edge.child}->{edge.parent}; {edge.provenance}"
                )
    return SemanticSelection(constrained, plan.unreachable, plan.reason)
