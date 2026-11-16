"""Offline migration validation; runtime loading belongs to T7, not this CLI."""

from __future__ import annotations

import argparse
import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


def repo_path(value: str | Path) -> Path:
    path = Path(value)
    return path.resolve() if path.is_absolute() else (Path(__file__).resolve().parents[1] / path).resolve()


class SemanticDataError(ValueError):
    def __init__(self, file: Path, index: object, field: str, reason: str):
        super().__init__(f"{file} [{index}] {field}: {reason}")


def read_text(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (UnicodeError, OSError) as exc:
        raise SemanticDataError(path, "file", "encoding/path", str(exc)) from exc
    if text.startswith("\ufeff"):
        raise SemanticDataError(path, "file", "encoding", "UTF-8 BOM is not allowed")
    return text


def read_yaml(path: Path) -> dict[str, Any]:
    try:
        data = yaml.safe_load(read_text(path))
    except yaml.YAMLError as exc:
        raise SemanticDataError(path, "file", "yaml", str(exc)) from exc
    if not isinstance(data, dict) or data.get("version") != 1:
        raise SemanticDataError(path, "file", "version", "expected mapping with version: 1")
    return data


def load_catalog(path: Path) -> dict[str, set[str]]:
    file = path / "table_columns.csv"
    rows = csv.DictReader(io.StringIO(read_text(file)))
    if not {"table_name", "column_name"}.issubset(rows.fieldnames or ()):
        raise SemanticDataError(file, "header", "columns", "table_name and column_name required")
    result: dict[str, set[str]] = {}
    for index, row in enumerate(rows):
        if not row["table_name"] or not row["column_name"]:
            raise SemanticDataError(file, index, "columns", "empty table or column")
        result.setdefault(row["table_name"], set()).add(row["column_name"])
    if not result:
        raise SemanticDataError(file, "file", "tables", "catalog is empty")
    return result


@dataclass
class ValidationReport:
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def validate_directory(directory: Path, catalog_path: Path | None = None, *, strict: bool = False) -> ValidationReport:
    directory = repo_path(directory)
    catalog_path = repo_path(catalog_path) if catalog_path else directory / "catalog"
    catalog = load_catalog(catalog_path)
    report = ValidationReport()

    def fail(file: Path, index: object, field: str, reason: str) -> None:
        raise SemanticDataError(file, index, field, reason)

    def reference(file: Path, index: object, field: str, valid: bool, reason: str) -> None:
        if not valid:
            if strict:
                fail(file, index, field, reason)
            report.warnings.append(str(SemanticDataError(file, index, field, reason)))

    def strings(file: Path, index: object, row: dict, key: str) -> list[str]:
        values = row.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            fail(file, index, key, "expected list of nonempty strings")
        return values

    def column(file: Path, index: object, field: str, value: str, tables: list[str]) -> None:
        if "." in value:
            table, name = value.rsplit(".", 1)
            valid = name in catalog.get(table, set()) and (not tables or table in tables)
        else:
            valid = any(value in catalog.get(table, set()) for table in tables or catalog)
        reference(file, index, field, valid, f"unresolved column {value!r}")

    docs = {}
    for filename, key in [("metrics.yaml", "metrics"), ("rules.yaml", "rules"), ("joins.yaml", "edges")]:
        file = directory / filename
        records = read_yaml(file).get(key)
        if not isinstance(records, list):
            fail(file, "file", key, "expected list")
        docs[key] = records
        report.counts[key] = len(records)
        seen: set[str] = set()
        for index, row in enumerate(records):
            if not isinstance(row, dict):
                fail(file, index, key, "expected record mapping")
            for text_key in ("provenance", "label", "definition", "note", "time_field"):
                if row.get(text_key) is not None and not isinstance(row[text_key], str):
                    fail(file, index, text_key, "expected text or null")
            if not row.get("provenance"):
                report.warnings.append(str(SemanticDataError(file, index, "provenance", "missing source")))
            if key != "edges":
                name = row.get("name", "")
                if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9]*(?:_[a-z0-9]+)*", name):
                    fail(file, index, "name", "expected snake_case machine key")
                if name in seen:
                    fail(file, index, "name", f"duplicate name {name!r}")
                seen.add(name)

    file = directory / "metrics.yaml"
    metrics = {row["name"]: row for row in docs["metrics"]}
    alias_owners: dict[str, dict] = {}
    for index, row in enumerate(docs["metrics"]):
        label = row.get("label")
        aliases = strings(file, index, row, "aliases")
        if not isinstance(label, str) or not label or label not in aliases:
            fail(file, index, "aliases", "aliases must include the nonempty label")
        for alias in [row["name"], *aliases]:
            previous = alias_owners.get(alias.casefold())
            if previous is not None and previous["name"] != row["name"]:
                fail(
                    file,
                    index,
                    "aliases",
                    f"{alias!r} conflicts: {previous.get('provenance')} vs {row.get('provenance')}",
                )
            alias_owners[alias.casefold()] = row
        if row.get("level") not in ("atomic", "derived", "composite"):
            fail(file, index, "level", "invalid metric level")
        if type(row.get("requires_multi_step", False)) is not bool:
            fail(file, index, "requires_multi_step", "expected boolean")
        expr = [row.get("formula"), row.get("sql_template")]
        if any(value is not None and (not isinstance(value, str) or not value.strip()) for value in expr):
            fail(file, index, "expression", "expected nonempty expression or null")
        if all(expr) or (not any(expr) and not row.get("requires_multi_step")):
            fail(file, index, "expression", "choose formula or sql_template, or explicit multi-step")
        if row["level"] == "atomic" and not any(expr):
            fail(file, index, "expression", "atomic metric requires one SQL expression")
        tables = strings(file, index, row, "source_tables")
        for table in tables:
            reference(file, index, "source_tables", table in catalog, f"unknown table {table!r}")
        for dependency in strings(file, index, row, "depends_on"):
            reference(file, index, "depends_on", dependency in metrics, f"unknown metric {dependency!r}")
        strings(file, index, row, "filters")
        if row.get("time_field"):
            column(file, index, "time_field", row["time_field"], tables)

    def visit(name: str, path: tuple[str, ...]) -> None:
        if name in path:
            fail(file, name, "depends_on", f"dependency cycle: {' -> '.join((*path, name))}")
        for dependency in metrics[name].get("depends_on", []):
            if dependency in metrics:
                visit(dependency, (*path, name))

    for name in metrics:
        visit(name, ())

    file = directory / "rules.yaml"
    for index, row in enumerate(docs["rules"]):
        if not strings(file, index, row, "trigger_any"):
            fail(file, index, "trigger_any", "at least one trigger required")
        strings(file, index, row, "exclude_any")
        scope = strings(file, index, row, "scope_tables")
        required = strings(file, index, row, "require_tables")
        for table in [*scope, *required]:
            reference(file, index, "tables", table in catalog, f"unknown table {table!r}")
        for key in ("require_columns", "forbid_columns"):
            for value in strings(file, index, row, key):
                column(file, index, key, value, scope + required)
        if set(row.get("require_columns", [])) & set(row.get("forbid_columns", [])):
            fail(file, index, "columns", "same column required and forbidden")

    file = directory / "joins.yaml"
    for index, row in enumerate(docs["edges"]):
        for endpoint in ("child", "parent"):
            if not isinstance(row.get(endpoint), str):
                fail(file, index, endpoint, "expected table name")
            reference(file, index, endpoint, row.get(endpoint) in catalog, f"unknown table {row.get(endpoint)!r}")
        if row.get("join_type", "left") not in ("inner", "left"):
            fail(file, index, "join_type", "expected inner or left")
        if row.get("relationship", "many_to_one") not in ("many_to_one", "one_to_one", "one_to_many"):
            fail(file, index, "relationship", "invalid relationship")
        if row.get("source", "declared") not in ("declared", "foreign_key"):
            fail(file, index, "source", "invalid edge source")
        keys = row.get("keys")
        if not isinstance(keys, list) or not keys:
            fail(file, index, "keys", "at least one key pair required")
        for pair in keys:
            if not isinstance(pair, dict) or set(pair) != {"left", "right"}:
                fail(file, index, "keys", "expected left/right pair")
            if any(not isinstance(value, str) or not value for value in pair.values()):
                fail(file, index, "keys", "expected nonempty column names")
            for side, table in (("left", row["child"]), ("right", row["parent"])):
                column(file, index, "keys", f"{table}.{pair[side]}", [table])

    # Execute complete single-query definitions on a private in-memory copy.
    schema_file = catalog_path / "schema.sql"
    if schema_file.exists() and not report.warnings:
        with sqlite3.connect(":memory:") as db:
            try:
                db.executescript(read_text(schema_file))
            except sqlite3.Error as exc:
                fail(schema_file, "file", "schema", str(exc))
            db.execute("PRAGMA query_only = ON")
            for index, row in enumerate(docs["metrics"]):
                sql = row.get("sql_template")
                if sql:
                    try:
                        if not re.match(r"\s*SELECT\b", sql, re.I):
                            fail(directory / "metrics.yaml", index, "sql_template", "expected SELECT")
                        db.execute(sql).fetchall()
                    except sqlite3.Error as exc:
                        fail(directory / "metrics.yaml", index, "sql_template", str(exc))
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="example/semantic")
    parser.add_argument("--catalog")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    try:
        report = validate_directory(
            repo_path(args.data), repo_path(args.catalog) if args.catalog else None, strict=args.strict
        )
    except SemanticDataError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"0 errors; counts={report.counts}; warnings={len(report.warnings)}")
    for warning in report.warnings:
        print(f"WARNING: {warning}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
