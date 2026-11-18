"""Extract additive retrieval descriptions using AST literals, without importing the MVP."""

import argparse
import ast
import csv
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def literal(path: Path, name: str):
    tree = ast.parse(path.read_text(encoding="utf-8-sig"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise ValueError(f"Missing metadata literal {name} in {path.name}")


def extract(source: Path, catalog: Path):
    with (catalog / "table_spec_columns.csv").open(encoding="utf-8-sig", newline="") as stream:
        columns = list(csv.DictReader(stream))
    tables = {row["table_name"] for row in columns}
    fields = {f"{row['table_name']}.{row['column_name']}" for row in columns}
    source_tables = literal(source / "table_retriever.py", "TABLE_METADATA")
    source_fields = literal(source / "field_matcher.py", "FIELD_METADATA")
    return {
        "tables": {
            name: " ".join(str(record.get(key, "")) for key in ("description", "domain", "key_fields"))
            for name, record in source_tables.items()
            if name in tables
        },
        "fields": {name: str(record["description"]) for name, record in source_fields.items() if name in fields},
        "provenance": {
            "tables": "chatbi_mvp/table_retriever.py:TABLE_METADATA",
            "fields": "chatbi_mvp/field_matcher.py:FIELD_METADATA",
            "method": "AST literal_eval; canonical catalog filter; additive index metadata only",
        },
        "excluded_tables": sorted(set(source_tables) - tables),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--catalog", type=Path, default=ROOT / "example/semantic/catalog")
    parser.add_argument("--output", type=Path, default=ROOT / "example/workflow/retrieval_metadata.json")
    args = parser.parse_args()
    metadata = extract(args.source, args.catalog)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "tables": len(metadata["tables"]),
                "fields": len(metadata["fields"]),
                "excluded_tables": len(metadata["excluded_tables"]),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
