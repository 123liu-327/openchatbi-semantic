"""Explicit source assembly and reference validation, never run at import time."""

import re
from collections.abc import Sequence
from typing import TypeVar, cast

from openchatbi.semantic.closure import expand_dependencies
from openchatbi.semantic.config import SemanticConfig, SemanticSourceConfig
from openchatbi.semantic.join import merge_edges
from openchatbi.semantic.models import BusinessRule, JoinEdge, MetricSpec, SemanticBundle
from openchatbi.semantic.rules import RuleCatalog
from openchatbi.semantic.sources import SemanticSourceError
from openchatbi.semantic.sources.catalog_fk import ForeignKeyCatalog, ForeignKeyJoinSource
from openchatbi.semantic.sources.json_file import JsonMetricSource
from openchatbi.semantic.sources.yaml_file import YamlJoinSource


class SemanticDataError(SemanticSourceError):
    pass


T = TypeVar("T", MetricSpec, BusinessRule)


def load_bundle(cfg: SemanticConfig, catalog: object) -> tuple[SemanticBundle, tuple[str, ...]]:
    metrics: dict[str, MetricSpec] = {}
    rules: dict[str, BusinessRule] = {}
    priorities: dict[tuple[str, str], int] = {}
    edges: list[JoinEdge] = []
    warnings: list[str] = []

    def yaml_source(
        source: SemanticSourceConfig,
    ) -> tuple[tuple[MetricSpec, ...], tuple[BusinessRule, ...], tuple[JoinEdge, ...]]:
        adapter = YamlJoinSource(source.path or "", config_path=cfg._config_path)
        data = adapter.document()
        if not any(section in data for section in ("metrics", "rules", "edges")):
            raise SemanticDataError(f"{adapter.path}: no metrics/rules/edges section")
        return (
            adapter.records(MetricSpec, "metrics", data) if "metrics" in data else (),
            adapter.records(BusinessRule, "rules", data) if "rules" in data else (),
            adapter.records(JoinEdge, "edges", data) if "edges" in data and cfg.join_source != "catalog_fk" else (),
        )

    def json_source(
        source: SemanticSourceConfig,
    ) -> tuple[tuple[MetricSpec, ...], tuple[BusinessRule, ...], tuple[JoinEdge, ...]]:
        return (
            JsonMetricSource(
                source.path or "", config_path=cfg._config_path, mapping_path=source.mapping_path
            ).load_metrics(),
            (),
            (),
        )

    def catalog_source(
        source: SemanticSourceConfig,
    ) -> tuple[tuple[MetricSpec, ...], tuple[BusinessRule, ...], tuple[JoinEdge, ...]]:
        return (
            (),
            (),
            (
                ForeignKeyJoinSource(cast(ForeignKeyCatalog, catalog)).load_edges()
                if cfg.join_source != "declared"
                else ()
            ),
        )

    factories = {"yaml": yaml_source, "json": json_source, "catalog": catalog_source}

    def add(kind: str, values: Sequence[T], destination: dict[str, T], source: SemanticSourceConfig) -> None:
        for record in values:
            record_key = (kind, record.name)
            if record_key in priorities:
                if priorities[record_key] == source.priority:
                    raise SemanticDataError(
                        f"{source.path} [{kind}:{record.name}] equal-priority duplicate; {record.provenance}"
                    )
                continue
            priorities[record_key] = source.priority
            destination[record.name] = record

    for source in sorted(cfg.sources, key=lambda item: item.priority):
        loaded_metrics, loaded_rules, loaded_edges = factories[source.type](source)
        add("metric", loaded_metrics, metrics, source)
        add("rule", loaded_rules, rules, source)
        edges.extend(loaded_edges)
    read_catalog = cast(RuleCatalog, catalog)
    tables = frozenset(read_catalog.get_table_list())
    columns = {table: {row["column_name"] for row in read_catalog.get_column_list(table)} for table in tables}

    def reference(valid: bool, context: str) -> None:
        if not valid:
            if cfg.strict:
                raise SemanticDataError(context)
            warnings.append(context)

    def column(value: str, context: str, scope: tuple[str, ...] = ()) -> None:
        if "." in value:
            table, name = value.rsplit(".", 1)
            valid = name in columns.get(table, set())
        else:
            valid = any(value in columns.get(table, set()) for table in scope or tables)
        reference(valid, f"{context}: unknown column {value}")

    aliases: dict[str, MetricSpec] = {}
    for kind, records in (("metric", tuple(metrics.values())), ("rule", tuple(rules.values()))):
        for record in records:
            if not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", record.name):
                raise SemanticDataError(f"{record.provenance} [{kind}:{record.name}] invalid snake_case name")
            if not record.provenance:
                warnings.append(f"[{kind}:{record.name}] missing provenance")
    for metric in metrics.values():
        context = f"{metric.provenance} [metric:{metric.name}]"
        if metric.label not in metric.aliases:
            raise SemanticDataError(f"{context} aliases must include label {metric.label!r}")
        for alias in (metric.name, *metric.aliases):
            previous = aliases.get(alias.casefold())
            if previous is not None and previous.name != metric.name:
                raise SemanticDataError(f"{context} alias {alias!r} conflicts with {previous.provenance}")
            aliases[alias.casefold()] = metric
        for dependency in metric.depends_on:
            reference(dependency in metrics, f"{context}: unknown dependency {dependency}")
        for table in metric.source_tables:
            reference(table in tables, f"{context}: unknown table {table}")
        if metric.time_field:
            column(metric.time_field, context, metric.source_tables)
    for rule in rules.values():
        context = f"{rule.provenance} [rule:{rule.name}]"
        for table in (*rule.scope_tables, *rule.require_tables):
            reference(table in tables, f"{context}: unknown table {table}")
        for value in (*rule.require_columns, *rule.forbid_columns):
            column(value, context, rule.scope_tables)
    merged = merge_edges(edges)
    for edge in merged:
        context = f"{edge.provenance} [edge:{edge.child}->{edge.parent}]"
        for table in (edge.child, edge.parent):
            reference(table in tables, f"{context}: unknown table {table}")
        for key in edge.keys:
            column(f"{edge.child}.{key.left}", context)
            column(f"{edge.parent}.{key.right}", context)
    # Unknown dependencies are warnings in non-strict mode; cycles still fail.
    known = {
        name: metric.model_copy(update={"depends_on": tuple(d for d in metric.depends_on if d in metrics)})
        for name, metric in metrics.items()
    }
    expand_dependencies(known, tuple(known), max_depth=max(len(known), 1))
    return SemanticBundle(metrics=metrics, rules=tuple(rules.values()), edges=merged, tables=tables), tuple(warnings)
