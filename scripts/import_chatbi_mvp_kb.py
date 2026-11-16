"""Statically migrate a local MVP checkout; never import its Python modules."""

from __future__ import annotations

import argparse
import ast
import csv
import io
import json
import re
from pathlib import Path
from typing import Any

import yaml

try:
    from .validate_semantic_data import SemanticDataError, read_text, read_yaml, repo_path, validate_directory
except ImportError:  # Direct CLI invocation puts scripts/ on sys.path.
    from validate_semantic_data import SemanticDataError, read_text, read_yaml, repo_path, validate_directory


def literal_constant(path: Path, name: str) -> Any:
    try:
        tree = ast.parse(read_text(path), filename=str(path))
        matches = [
            node.value
            for node in tree.body
            if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == name for t in node.targets)
        ]
        if len(matches) != 1:
            raise ValueError(f"expected exactly one literal assignment to {name}")
        return ast.literal_eval(matches[0])
    except (SyntaxError, ValueError, TypeError) as exc:
        raise SemanticDataError(path, name, "AST literal", str(exc)) from exc


def dump_yaml(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(value, allow_unicode=True, sort_keys=False, width=110), encoding="utf-8", newline="\n"
    )


def write_csv(path: Path, fields: list[str], rows: list[dict]) -> None:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    path.write_text(output.getvalue(), encoding="utf-8", newline="\n")


def build_catalog(source: Path, out: Path, mapping: dict) -> dict[str, set[str]]:
    """Extract this source's documented MySQL DDL/VALUES subset, without executing it."""
    file = source / "database/init.sql"
    text = read_text(file)
    blocks = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\) ENGINE=[^;]+;", text, re.S)
    tables: dict[str, set[str]] = {}
    column_rows = []
    schema = ["-- Statically converted from chatbi_mvp:database/init.sql; SQLite demo only.\nPRAGMA foreign_keys = ON;"]
    info = {}
    for table, body in blocks:
        if table not in mapping["include_tables"]:
            continue
        definitions = []
        tables[table] = set()
        for line in body.splitlines():
            match = re.fullmatch(r"\s*(\w+)\s+([A-Z]+(?:\(\d+(?:,\d+)?\))?)\s+NOT NULL(?: DEFAULT \d+)?[,]?", line)
            if not match:
                if line.strip() and not re.match(r"\s*(PRIMARY|KEY|UNIQUE|CONSTRAINT|FOREIGN|CHECK)\b", line):
                    raise SemanticDataError(file, table, "DDL", f"unsupported line {line.strip()!r}")
                continue
            column, kind = match.groups()
            tables[table].add(column)
            definitions.append(f"    {column} {kind} NOT NULL")
            column_rows.append(
                {
                    "db_name": "",
                    "table_name": table,
                    "column_name": column,
                    "type": kind,
                    "display_name": column,
                    "description": column,
                    "alias": "",
                    "category": "metric" if f"{table}.{column}" in mapping.get("metric_columns", []) else "dimension",
                    "provenance": f"chatbi_mvp:database/init.sql#{table}.{column}",
                }
            )
        for keys in re.findall(r"PRIMARY KEY\s*\(([^)]+)\)", body):
            definitions.append(f"    PRIMARY KEY ({keys})")
        for child, parent, parent_keys in re.findall(r"FOREIGN KEY\s*\(([^)]+)\) REFERENCES (\w+)\s*\(([^)]+)\)", body):
            definitions.append(f"    FOREIGN KEY ({child}) REFERENCES {parent} ({parent_keys})")
        schema.append(f"CREATE TABLE IF NOT EXISTS {table} (\n" + ",\n".join(definitions) + "\n);")
        info[table] = {
            "description": table,
            "selection_rule": "",
            "sql_rule": "",
            "provenance": f"chatbi_mvp:database/init.sql#{table}",
        }
    if set(tables) != set(mapping["include_tables"]):
        raise SemanticDataError(
            file, "tables", "DDL", f"missing selected tables {set(mapping['include_tables']) - set(tables)}"
        )
    inserts = re.findall(r"INSERT INTO (\w+)\s*\((.*?)\) VALUES\s*(.*?)\s*ON DUPLICATE KEY UPDATE", text, re.S)
    for table, columns, values in inserts:
        if table in tables:
            if any(col.strip() not in tables[table] for col in columns.split(",")):
                raise SemanticDataError(file, table, "INSERT", "unknown column")
            schema.append(f"INSERT OR REPLACE INTO {table} ({columns.strip()}) VALUES\n{values};")
    if {table for table, _, _ in inserts if table in tables} != set(tables):
        raise SemanticDataError(file, "rows", "INSERT", "each demo table must have a VALUES block")
    out.mkdir(parents=True, exist_ok=True)
    dump_yaml(out / "table_info.yaml", {"": info})
    write_csv(
        out / "table_columns.csv",
        ["db_name", "table_name", "column_name"],
        [{key: row[key] for key in ("db_name", "table_name", "column_name")} for row in column_rows],
    )
    write_csv(out / "table_spec_columns.csv", list(column_rows[0]), column_rows)
    common_fields = ["column_name", "type", "display_name", "description", "alias", "category", "provenance"]
    common: dict[str, dict] = {}
    for row in column_rows:
        name = row["column_name"]
        if name in common:
            if any(common[name][field] != row[field] for field in ("type", "category")):
                raise SemanticDataError(
                    file, name, "common_columns", "shared column metadata differs; explicit mapping required"
                )
            common[name]["provenance"] += "; " + row["provenance"]
        else:
            common[name] = {key: row[key] for key in common_fields}
    write_csv(out / "common_columns.csv", common_fields, list(common.values()))
    write_csv(out / "table_selection_example.csv", ["question", "selected_tables"], [])
    dump_yaml(out / "sql_example.yaml", {})
    (out / "schema.sql").write_text("\n\n".join(schema) + "\n", encoding="utf-8", newline="\n")
    return tables


def migrate(source: Path, out: Path, mapping_path: Path) -> dict:
    mapping = read_yaml(mapping_path)
    names = mapping["metric_names"]
    catalog = build_catalog(source, out / "catalog", mapping)
    file = source / "indicators_full.json"
    try:
        original_metrics = json.loads(read_text(file))["indicators"]
    except (ValueError, KeyError, TypeError) as exc:
        raise SemanticDataError(file, "file", "indicators", str(exc)) from exc
    unresolved: list[dict] = []
    conversions = []
    metrics = []
    if not isinstance(original_metrics, list):
        raise SemanticDataError(file, "file", "indicators", "expected list")
    for index, original in enumerate(original_metrics):
        for field in ("name", "level", "definition", "sql_template", "data_source"):
            if not isinstance(original, dict) or not isinstance(original.get(field), str) or not original[field]:
                raise SemanticDataError(file, index, field, "nonempty text required")
        label = original["name"]
        if label not in names:
            unresolved.append(
                {
                    "provenance": f"chatbi_mvp:indicators_full.json#{label}",
                    "reason": "missing machine key mapping",
                    "record": original,
                }
            )
            continue
        multi = label in mapping["multi_step_metrics"]
        if original["level"] not in mapping["levels"]:
            raise SemanticDataError(file, index, "level", f"unmapped level {original['level']!r}")
        tables = re.split(r"\s+(?:JOIN|\+)\s+", original["data_source"])
        unknown = [table for table in tables if table not in catalog]
        if unknown:
            unresolved.append(
                {"provenance": f"chatbi_mvp:indicators_full.json#{label}", "field": "source_tables", "values": unknown}
            )
        sql = None if multi else original["sql_template"]
        # Resolve SQL aliases explicitly from FROM/JOIN, never by guessing names.
        aliases = {
            alias: table
            for table, alias in re.findall(r"\b(?:FROM|JOIN)\s+(\w+)\s+(\w+)", sql or "", re.I)
            if alias.upper() not in {"JOIN", "LEFT", "INNER", "WHERE", "ON", "GROUP"}
        }
        raw_time = original.get("time_field")
        time_field = None if multi else raw_time
        if time_field and "." in time_field:
            alias, column = time_field.split(".", 1)
            time_field = f"{aliases.get(alias, alias)}.{column}"
        dependencies = [names.get(name, name) for name in original.get("depends_on", [])]
        metrics.append(
            {
                "name": names[label],
                "label": label,
                "aliases": list(dict.fromkeys([label, *original.get("aliases", [])])),
                "level": mapping["levels"][original["level"]],
                "definition": original["definition"] + ("\n" + original["notes"] if original.get("notes") else ""),
                "sql_template": sql,
                "depends_on": dependencies,
                "time_field": time_field,
                "filters": original.get("filters", []),
                "source_tables": tables,
                "requires_multi_step": multi,
                "provenance": f"chatbi_mvp:indicators_full.json#{label}",
            }
        )
        conversions.append(
            {
                "provenance": f"chatbi_mvp:indicators_full.json#{label}",
                "name": names[label],
                "original_formula": original.get("formula"),
                "original_sql_template": original.get("sql_template"),
                "original_time_field": raw_time,
                "requires_multi_step": multi,
            }
        )
    source_rules = literal_constant(source / "field_matcher.py", "BUSINESS_RULES")
    if len(source_rules) != len(mapping["rule_names"]):
        raise SemanticDataError(
            source / "field_matcher.py", "BUSINESS_RULES", "count", "rule name mapping length differs"
        )
    rules = []
    for index, row in enumerate(source_rules):
        if row["type"] not in ("whitelist", "blacklist", "conditional"):
            raise SemanticDataError(source / "field_matcher.py", index, "type", f"unsupported {row['type']!r}")
        columns = row.get("force_include", [])
        rules.append(
            {
                "name": mapping["rule_names"][index],
                "trigger_any": row["trigger_keywords"],
                "require_columns": columns,
                "forbid_columns": row.get("force_exclude", []),
                "require_tables": list(dict.fromkeys(column.rsplit(".", 1)[0] for column in columns)),
                "note": row["reason"],
                "provenance": f"chatbi_mvp:field_matcher.py#BUSINESS_RULES[{index}]",
            }
        )
    graph = literal_constant(source / "join_resolver.py", "TABLE_RELATIONSHIPS")
    primary_keys = {}
    for table, body in re.findall(
        r"CREATE TABLE IF NOT EXISTS (\w+)\s*\((.*?)\n\);", read_text(out / "catalog/schema.sql"), re.S
    ):
        match = re.search(r"PRIMARY KEY\s*\(([^)]+)\)", body)
        primary_keys[table] = {v.strip() for v in match[1].split(",")} if match else set()
    edges = []
    for table, records in graph.items():
        for index, row in enumerate(records):
            left, right = [v.strip() for v in row["fk_col"].split(",")], [v.strip() for v in row["pk_col"].split(",")]
            if len(left) != len(right) or not all(left + right):
                raise SemanticDataError(
                    source / "join_resolver.py", f"{table}[{index}]", "keys", "composite key lengths differ"
                )
            if row["join_type"] not in ("JOIN", "LEFT JOIN"):
                raise SemanticDataError(source / "join_resolver.py", index, "join_type", "unsupported join type")
            # The source declares both directions; preserve them, including outer-join semantics.
            child_unique = set(left) == primary_keys.get(table)
            parent_unique = set(right) == primary_keys.get(row["target"])
            if not child_unique and not parent_unique:
                raise SemanticDataError(
                    source / "join_resolver.py",
                    f"{table}[{index}]",
                    "relationship",
                    "neither endpoint is a primary key; mapping required",
                )
            relationship = (
                "one_to_one" if child_unique and parent_unique else "many_to_one" if parent_unique else "one_to_many"
            )
            edges.append(
                {
                    "child": table,
                    "parent": row["target"],
                    "keys": [
                        {"left": left_key, "right": right_key} for left_key, right_key in zip(left, right, strict=True)
                    ],
                    "relationship": relationship,
                    "join_type": "inner" if row["join_type"] == "JOIN" else "left",
                    "source": "declared",
                    "provenance": f"chatbi_mvp:join_resolver.py#TABLE_RELATIONSHIPS.{table}[{index}]",
                }
            )
    old_file = source / "indicators.json"
    conflicts = []
    if old_file.exists():
        full = {row["name"]: row for row in original_metrics}
        for row in json.loads(read_text(old_file))["indicators"]:
            differences = {
                key: {"legacy": value, "full": full.get(row["name"], {}).get(key)}
                for key, value in row.items()
                if full.get(row["name"], {}).get(key) != value
            }
            if differences:
                conflicts.append(
                    {
                        "provenance": f"chatbi_mvp:indicators.json#{row['name']}",
                        "resolution": "use indicators_full.json",
                        "differences": differences,
                    }
                )
    report = {
        "version": 1,
        "unresolved": unresolved,
        "legacy_conflicts": conflicts,
        "conversions": conversions,
        "excluded_tables": mapping["exclude_tables"],
        "review_notes": mapping.get("review_notes", []),
    }
    dump_yaml(out / "metrics.yaml", {"version": 1, "metrics": metrics})
    dump_yaml(out / "rules.yaml", {"version": 1, "rules": rules})
    dump_yaml(out / "joins.yaml", {"version": 1, "edges": edges})
    dump_yaml(out / "_unresolved.yaml", report)
    try:
        preliminary = validate_directory(out, strict=False)
        unresolved.extend({"reason": warning} for warning in preliminary.warnings)
        if unresolved:
            dump_yaml(out / "_unresolved.yaml", report)
            raise SemanticDataError(
                out / "_unresolved.yaml", "records", "unresolved", "resolve migration failures before use"
            )
        result = validate_directory(out, strict=True)
    except SemanticDataError as exc:
        report["unresolved"].append({"reason": str(exc)})
        dump_yaml(out / "_unresolved.yaml", report)
        raise
    return result.counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True)
    parser.add_argument("--out", default="example/semantic")
    parser.add_argument("--mapping", default="example/semantic/migration.yaml")
    args = parser.parse_args()
    try:
        counts = migrate(repo_path(args.source), repo_path(args.out), repo_path(args.mapping))
    except SemanticDataError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Migrated {counts}; see _unresolved.yaml for conversion evidence and legacy conflicts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
