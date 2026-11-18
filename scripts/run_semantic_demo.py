"""Run an offline semantic demonstration or prepare an isolated chat configuration."""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from openchatbi.semantic.layer import SemanticLayer  # noqa: E402 -- Allow launching this file outside the repo.
from scripts.import_chatbi_mvp_kb import dump_yaml, write_csv  # noqa: E402
from scripts.validate_semantic_data import read_text, read_yaml, repo_path, validate_directory  # noqa: E402


class DemoCatalog:
    """Read-only CSV snapshot sufficient for the semantic layer; no app/LLM imports."""

    def __init__(self, path: Path):
        self.columns: dict[str, list[dict]] = {}
        for row in csv.DictReader(io.StringIO(read_text(path / "table_spec_columns.csv"))):
            self.columns.setdefault(row["table_name"], []).append(row)

    def get_table_list(self) -> list[str]:
        return list(self.columns)

    def get_column_list(self, table: str | None = None) -> list[dict]:
        return [dict(row) for row in self.columns.get(table or "", [])]


def prepare(output: Path, *, model: str | None = None) -> tuple[SemanticLayer, Path, Path]:
    source = ROOT / "example/semantic"
    validate_directory(source, strict=True)
    output.mkdir(parents=True, exist_ok=True)
    database = output / "demo.sqlite"
    if not database.exists():
        with sqlite3.connect(database) as db:
            db.executescript(read_text(source / "catalog/schema.sql"))
    catalog = output / "catalog"
    catalog.mkdir(exist_ok=True)
    for file in (source / "catalog").iterdir():
        if file.is_file():
            (catalog / file.name).write_bytes(file.read_bytes())
    # The upstream empty table-example fallback indexes an empty string. Supply
    # actual examples derived from canonical records, without another business KB.
    records = read_yaml(source / "metrics.yaml")["metrics"]
    write_csv(
        catalog / "table_selection_example.csv",
        ["question", "selected_tables"],
        [{"question": row["label"], "selected_tables": json.dumps(row["source_tables"])} for row in records],
    )
    examples: dict[str, str] = {}
    for row in records:
        if row.get("sql_template") and row["source_tables"]:
            table = row["source_tables"][0]
            examples[table] = examples.get(table, "") + f"Q: {row['label']}\nA: {row['sql_template']}\n"
    dump_yaml(catalog / "sql_example.yaml", {"": examples})
    semantic = {
        "enabled": True,
        "strict": True,
        "join_source": "declared",
        "sources": [
            {"type": "yaml", "path": str(source / name), "priority": 20}
            for name in ("metrics.yaml", "joins.yaml", "rules.yaml")
        ],
    }
    layer = SemanticLayer.from_config({"semantic": semantic}, DemoCatalog(catalog))
    config_path = output / "chat-config.yaml"
    if model:
        semantic["join_source"] = "both"
        semantic["sources"].insert(0, {"type": "catalog", "priority": 10})
        params = {"model": model, "temperature": 0}
        if os.environ.get("OPENAI_BASE_URL"):
            params["base_url"] = os.environ["OPENAI_BASE_URL"]
        dump_yaml(
            config_path,
            {
                "organization": "Semantic demo",
                "dialect": "sqlite",
                "python_executor": "local",
                "visualization_mode": "rule",
                "enable_fail_closed_sql_guard": True,
                "report_directory": str(output / "reports"),
                "catalog_store": {"store_type": "file_system", "data_path": str(catalog), "auto_load": False},
                "data_warehouse_config": {"uri": "sqlite:///" + database.as_posix(), "database_name": ""},
                "default_llm": {"class": "langchain_openai.ChatOpenAI", "params": params},
                "semantic": semantic,
                "workflow": {
                    "mode": os.getenv("CHATBI_WORKFLOW_MODE", "four_domain"),
                    "authorization": {
                        "principal": "local_workbench",
                        "table_columns": {table: ["*"] for table in DemoCatalog(catalog).get_table_list()},
                    },
                    "retrieval_metadata_path": str(ROOT / "example/workflow/retrieval_metadata.json"),
                },
            },
        )
    return layer, database, config_path


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question")
    parser.add_argument("--output", default="data/semantic_demo")
    parser.add_argument("--prepare-chat", action="store_true")
    args = parser.parse_args()
    model = os.getenv("CHATBI_MODEL") if args.prepare_chat else None
    if args.prepare_chat and not model:
        parser.error("--prepare-chat requires CHATBI_MODEL to name your configured model")
    layer, database, config = prepare(repo_path(args.output), model=model)
    if args.prepare_chat:
        print(json.dumps({"config_file": str(config), "database": str(database), "model": model}, ensure_ascii=False))
        return 0
    source = ROOT / "example/semantic"
    question = args.question or read_yaml(source / "metrics.yaml")["metrics"][0]["label"]
    matches = layer.search_metrics(question)
    if not matches:
        print(json.dumps({"question": question, "error": "No configured metric matched"}, ensure_ascii=False))
        return 2
    closure = layer.closure([metric.name for metric in matches])
    tables = sorted({table for metric in closure for table in metric.source_tables})
    plan = layer.plan_join("", tables)
    from openchatbi.semantic.join import to_sql_clause

    results = []
    with sqlite3.connect(database) as db:
        db.execute("PRAGMA query_only = ON")
        for metric in matches:
            if metric.requires_multi_step:
                results.append({"metric": metric.name, "requires_multi_step": True, "executed": False})
            elif metric.sql_template and re.match(r"\s*SELECT\b", metric.sql_template, re.I):
                cursor = db.execute(metric.sql_template)
                results.append(
                    {
                        "metric": metric.name,
                        "columns": [column[0] for column in cursor.description],
                        "rows": cursor.fetchall(),
                    }
                )
    print(
        json.dumps(
            {
                "question": question,
                "closure": [metric.name for metric in closure],
                "join_sql": to_sql_clause(plan),
                "unreachable": plan.unreachable,
                "prompt": layer.render(question=question, selected_tables=tables),
                "results": results,
                "llm_called": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
