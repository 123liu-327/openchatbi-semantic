"""Explicit rule matching and hard schema constraints; no business vocabulary."""

import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from openchatbi.semantic.models import BusinessRule, MetricSpec


class RuleCatalog(Protocol):
    def get_table_list(self) -> list[str]: ...

    def get_column_list(self, table: str | None = None) -> list[dict[str, Any]]: ...


class RuleConflictError(ValueError):
    pass


def validate_metric_rules(metrics: Sequence[MetricSpec], rules: Sequence[BusinessRule]) -> None:
    """Reject known canonical-expression conflicts; this is not an SQL AST validator."""
    forbidden = {column for rule in rules for column in rule.forbid_columns}
    for metric in metrics:
        expression = metric.sql_template or metric.formula or ""
        aliases = {table: table for table in metric.source_tables}
        for match in re.finditer(
            r"\b(?:FROM|JOIN)\s+([\w.]+)(?:\s+(?!WHERE\b|JOIN\b|ON\b|GROUP\b|LEFT\b|INNER\b)(\w+))?", expression, re.I
        ):
            table, alias = match.groups()
            aliases[alias or table] = table
        referenced = {
            f"{aliases[alias]}.{column}"
            for alias, column in re.findall(r"\b(\w+)\.(\w+)\b", expression)
            if alias in aliases
        }
        for value in forbidden:
            if "." not in value:
                if re.search(r"\b" + re.escape(value) + r"\b", expression):
                    referenced.add(value)
            elif len(metric.source_tables) == 1 and value.rsplit(".", 1)[0] == metric.source_tables[0]:
                if re.search(r"(?<![\w.])" + re.escape(value.rsplit(".", 1)[1]) + r"\b", expression):
                    referenced.add(value)
        if referenced & forbidden:
            raise RuleConflictError(
                f"canonical metric {metric.name} conflicts with active rules: {sorted(referenced & forbidden)}; {metric.provenance}"
            )


def match_rules(rules: Sequence[BusinessRule], question: str, tables: Sequence[str] = ()) -> tuple[BusinessRule, ...]:
    text = question.casefold()
    return tuple(
        rule
        for rule in rules
        if any(word.casefold() in text for word in rule.trigger_any)
        and not any(word.casefold() in text for word in rule.exclude_any)
        and (not tables or not rule.scope_tables or bool(set(tables) & set(rule.scope_tables)))
    )


def apply_rules(
    selected: Sequence[Mapping[str, Any]], rules: Sequence[BusinessRule], catalog: RuleCatalog
) -> list[dict[str, Any]]:
    """Return an independent table/column selection, restoring required tables.

    Missing columns in model output mean full catalog columns; explicit empty
    output after applying rules remains empty and must not be expanded later.
    """
    result = {row["table"]: dict(row) for row in selected}
    available: dict[str, set[str]] = {}
    for rule in rules:
        for table in rule.require_tables:
            result.setdefault(table, {"table": table, "columns": []})
        for column in rule.require_columns:
            if "." in column:
                table = column.rsplit(".", 1)[0]
                result.setdefault(table, {"table": table, "columns": []})
    for table, row in result.items():
        available[table] = {col["column_name"] for col in catalog.get_column_list(table)}
        raw = row.get("columns") or sorted(available[table])
        names = {value if isinstance(value, str) else value["column_name"] for value in raw}
        row["columns"] = sorted(names & available[table])
    required: set[tuple[str, str]] = set()
    forbidden: set[tuple[str, str]] = set()
    for rule in rules:
        for field, output in (("require_columns", required), ("forbid_columns", forbidden)):
            for value in getattr(rule, field):
                if "." in value:
                    table, name = value.rsplit(".", 1)
                    candidates = [(table, name)] if table in result else []
                else:
                    candidates = [
                        (table, value)
                        for table in result
                        if value in available[table] and (not rule.scope_tables or table in rule.scope_tables)
                    ]
                if field == "require_columns" and not candidates:
                    raise RuleConflictError(f"{rule.name}: unresolved required column {value}; {rule.provenance}")
                for table, name in candidates:
                    if name not in available[table]:
                        raise RuleConflictError(f"{rule.name}: unknown column {table}.{name}; {rule.provenance}")
                    output.add((table, name))
    conflict = required & forbidden
    if conflict:
        raise RuleConflictError(
            f"required and forbidden columns overlap: {sorted(conflict)}; rules: {[r.name for r in rules]}"
        )
    for table, row in result.items():
        names = set(row["columns"]) | {name for owner, name in required if owner == table}
        row["columns"] = sorted(names - {name for owner, name in forbidden if owner == table})
    return list(result.values())
