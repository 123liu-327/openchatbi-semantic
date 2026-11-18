"""Prepare a compact candidate schema and canonical metric context for SQL."""

import asyncio
import json
import re
import uuid
from datetime import datetime

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import Field

from openchatbi.semantic.render import render_semantic_block
from openchatbi.semantic.rules import apply_rules, validate_metric_rules
from openchatbi.workflow.models import Contract, ResolvedQuery, SelectedTable
from openchatbi.workflow.retrieval import digest


class ExtractedIntent(Contract):
    reasoning: str = ""
    keywords: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)
    filter: list[str] = Field(default_factory=list)
    start_time: str | None = None
    end_time: str | None = None
    timezone: str | None = None
    rewrite_question: str = Field(min_length=1)


class FieldChoice(Contract):
    reasoning: str = ""
    tables: list[SelectedTable] = Field(min_length=1)


class MetricChoice(Contract):
    reasoning: str = ""
    metric_names: list[str] = Field(default_factory=list)


def parse_json_message(message):
    content = message.content
    if not isinstance(content, str):
        raise ValueError("Semantic model must return JSON text")
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip())
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Semantic model returned invalid JSON") from exc


class SemanticResolver:
    def __init__(self, runtime):
        self.runtime = runtime

    async def model_step(self, name, messages, model, context, task_id):
        response = None
        try:
            response = await self.runtime.llm.ainvoke(messages)
            result = model.model_validate(parse_json_message(response))
        except Exception as exc:
            if context.record:
                context.record(
                    task_id=task_id,
                    domain="semantic",
                    node=name,
                    inputs={"messages": [message.content for message in messages]},
                    outputs={"raw_output": response.content if response else None, "error_type": type(exc).__name__},
                    status="error",
                )
            raise
        if context.record:
            context.record(
                task_id=task_id,
                domain="semantic",
                node=name,
                inputs={"messages": [message.content for message in messages]},
                outputs=result.model_dump(),
            )
        return result

    async def resolve(self, args, context, task_id):
        key = digest({"args": args.model_dump(), "snapshot": self.runtime.snapshot_id})
        if key not in context.semantic_cache:
            context.semantic_cache[key] = asyncio.create_task(self._resolve(args, context, task_id))
        try:
            return await asyncio.shield(context.semantic_cache[key])
        except BaseException:
            context.semantic_cache.pop(key, None)
            raise

    async def select_metrics(self, args, context, task_id):
        runtime = self.runtime
        names = args.metric_names
        candidates = []
        if names is None:
            found = await runtime.registry.call(
                "search_metrics",
                {"query": args.question, "top_k": args.top_k, "threshold": args.threshold},
                context,
                f"{task_id}_retrieve",
                "semantic",
            )
            if found.errors:
                raise ValueError(found.errors[0].message)
            candidates = [
                {key: row[key] for key in ("name", "label", "definition", "score")}
                for row in found.data["items"]
                if not row["is_dependency"]
            ]
            names = []
            if candidates:
                choice = await self.model_step(
                    "metric_choice",
                    [
                        SystemMessage(
                            "根据业务问题从候选指标中选择直接请求的指标 ID。核对定义和粒度，"
                            "可选择多个，也可以留空。依赖由系统展开。返回 JSON 对象 metric_names 和 reasoning。"
                        ),
                        HumanMessage(
                            json.dumps({"question": args.question, "candidates": candidates}, ensure_ascii=False)
                        ),
                    ],
                    MetricChoice,
                    context,
                    task_id,
                )
                names = choice.metric_names
                if set(names) - {row["name"] for row in candidates}:
                    raise ValueError("Selected metric was not retrieved")
        names = list(dict.fromkeys(names))
        if any(runtime.semantic.metric(name) is None for name in names):
            raise ValueError("Unknown metric ID")
        metrics = runtime.semantic.closure(names)
        tables = sorted({table for metric in metrics for table in metric.source_tables})
        rules = runtime.semantic.rules_for(args.question + " " + " ".join(m.label for m in metrics), tables)
        validate_metric_rules(metrics, rules)
        return {
            "metric_names": names,
            "dependency_names": [m.name for m in metrics if m.name not in names],
            "candidates": candidates,
            "source_tables": tables,
            "multi_step_metrics": [m.name for m in metrics if m.requires_multi_step],
            "semantic_block": render_semantic_block(metrics=metrics, rules=rules) if metrics else "",
        }

    async def _resolve(self, args, context, task_id):
        from openchatbi.prompts.system_prompt import get_extraction_prompt_template

        runtime = self.runtime
        operations = []

        async def call(name, values):
            result = await runtime.registry.call(name, values, context, f"{task_id}_{len(operations)}", "semantic")
            operations.append(result.model_dump(mode="json"))
            if result.status not in ("ok", "empty"):
                raise ValueError(f"Semantic {name} failed: " + "; ".join(error.message for error in result.errors))
            return result.data

        available = set(runtime.catalog.get_table_list())
        if set(args.tables) - available:
            raise ValueError("Unknown candidate table")
        from types import SimpleNamespace

        choice = await self.select_metrics(
            SimpleNamespace(question=args.question, metric_names=args.metric_names, top_k=3, threshold=0.3),
            context,
            task_id,
        )
        if choice["multi_step_metrics"]:
            raise ValueError(
                "Split multi-step metric into queries for its dependencies: " + ", ".join(choice["dependency_names"])
            )
        names = choice["metric_names"]
        metrics = runtime.semantic.closure(names)
        question = args.question + " " + " ".join(m.label for m in metrics)
        candidates = set(args.tables)
        if not candidates:
            candidates.update(row["table"] for row in (await call("search_tables", {"query": args.question}))["items"])
        required_tables = {table for m in metrics for table in m.source_tables}
        candidates.update(required_tables)
        rules = runtime.semantic.rules_for(question, sorted(candidates))
        required_tables.update(table for rule in rules for table in rule.require_tables)
        required_tables.update(
            field.rsplit(".", 1)[0] for rule in rules for field in rule.require_columns if "." in field
        )
        candidates.update(required_tables)
        if not candidates or candidates - available:
            raise ValueError("No valid candidate tables; refine the semantic query")
        validate_metric_rules(metrics, rules)
        if args.field_selection == "hybrid":
            field_data = await call("search_fields", {"query": question.strip(), "tables": sorted(candidates)})
        else:
            items = []
            required_ids = set()
            for table in sorted(candidates):
                for col in runtime.catalog.get_column_list(table):
                    key = f"{table}.{col['column_name']}"
                    items.append({**col, "id": key, "table": table})
                    for rule in rules:
                        if key in rule.require_columns or (
                            col["column_name"] in rule.require_columns
                            and (not rule.scope_tables or table in rule.scope_tables)
                        ):
                            required_ids.add(key)
            field_data = {"items": items, "context": {"required_fields": sorted(required_ids)}}
        allowed = {
            row["table"]: set(row["columns"])
            for row in apply_rules([{"table": table} for table in sorted(candidates)], rules, runtime.catalog)
        }
        required = {table: set() for table in candidates}
        for key in field_data["context"]["required_fields"]:
            table, column = key.rsplit(".", 1)
            required[table].add(column)
            required_tables.add(table)
        field_data["items"] = [
            r for r in field_data["items"] if r["id"].rsplit(".", 1)[1] in allowed[r["id"].rsplit(".", 1)[0]]
        ]
        self.add_metric_fields(metrics, required, allowed)
        columns = {table: set(fields) for table, fields in required.items()}
        for hit in field_data["items"]:
            table, column = hit["id"].rsplit(".", 1)
            if column in allowed[table]:
                columns[table].add(column)

        # Only bridge keys can enter the SQL scope from outside the retrieved tables.
        edges = runtime.semantic.relationships()
        connected = set(candidates)
        while True:
            expanded = connected | {
                name
                for edge in edges
                if connected.intersection((edge.child, edge.parent))
                for name in (edge.child, edge.parent)
            }
            if expanded == connected:
                break
            connected = expanded
        bridge_tables = connected - candidates
        if bridge_tables:
            for row in apply_rules([{"table": t} for t in sorted(bridge_tables)], rules, runtime.catalog):
                allowed.setdefault(row["table"], set(row["columns"]))
                columns.setdefault(row["table"], set())
        relationships = []
        for edge in edges:
            if edge.child not in connected or edge.parent not in connected:
                continue
            keys = [(edge.child, key.left) for key in edge.keys] + [(edge.parent, key.right) for key in edge.keys]
            if all(name in allowed[table] for table, name in keys):
                relationships.append(edge)
                for table, name in keys:
                    columns[table].add(name)
        rows = [{"table": t, "columns": sorted(columns[t])} for t in sorted(candidates)]
        join_rows = [{"table": t, "columns": sorted(columns[t])} for t in sorted(bridge_tables)]
        prompt = get_extraction_prompt_template().replace(
            "[time_field_placeholder]", datetime.now().astimezone().isoformat()
        )
        intent = await self.model_step(
            "question_extraction",
            [
                SystemMessage(prompt + "\n返回原格式 JSON。保留原问题的时间、过滤和分组范围。"),
                HumanMessage(
                    json.dumps(
                        {"question": args.question, "selected_metric_ids": names, "context": args.semantic_context},
                        ensure_ascii=False,
                    )
                ),
            ],
            ExtractedIntent,
            context,
            task_id,
        )
        if args.field_selection == "llm":
            from openchatbi.prompts.system_prompt import (
                get_data_warehouse_introduction,
                get_table_selection_prompt_template,
            )

            # The model and hybrid scoring share the same hard business constraints.
            # LLM selection receives candidate-table fields, then produces a compact schema.
            llm_rows = [{"table": t, "columns": sorted(allowed[t])} for t in sorted(candidates)]
            prompt = get_table_selection_prompt_template().replace(
                "[tables]", json.dumps(self.schemas(llm_rows), ensure_ascii=False)
            )
            prompt = prompt.replace("[data_warehouse_introduction]", get_data_warehouse_introduction())
            prompt = prompt.replace("[examples]", self.selection_examples(args.question, candidates))
            selected = await self.model_step(
                "field_selection",
                [SystemMessage(prompt), HumanMessage(intent.rewrite_question)],
                FieldChoice,
                context,
                task_id,
            )
            chosen = {}
            for row in selected.tables:
                if row.table not in candidates or set(row.columns) - allowed[row.table]:
                    raise ValueError("Selection introduced an unknown or forbidden field")
                chosen[row.table] = set(row.columns)
            for table in required_tables:
                chosen.setdefault(table, set())
            for table, fields in required.items():
                if fields:
                    chosen.setdefault(table, set()).update(fields)
            for edge in relationships:
                for table, keys in (
                    (edge.child, [k.left for k in edge.keys]),
                    (edge.parent, [k.right for k in edge.keys]),
                ):
                    if table in chosen:
                        chosen[table].update(keys)
            rows = [{"table": t, "columns": sorted(fields)} for t, fields in chosen.items()]
        block = render_semantic_block(metrics=metrics, rules=rules)
        schema = self.schemas(rows + join_rows)
        if block:
            for table in schema:
                table.pop("derived_metric", None)
        resolved = ResolvedQuery(
            id=uuid.uuid4().hex,
            snapshot_id=runtime.snapshot_id,
            request_question=args.question,
            rewrite_question=intent.rewrite_question,
            tables=rows,
            info_entities=intent.model_dump(),
            metric_names=names,
            relationships=relationships,
            join_tables=join_rows,
            table_metadata={table["table"]: table for table in schema},
            semantic_block=block,
            anchor_hint=metrics[0].source_tables[0] if metrics and metrics[0].source_tables else "",
            required_tables=sorted(required_tables),
            required_fields={t: sorted(v) for t, v in required.items() if v},
            field_selection=args.field_selection,
            field_evidence=field_data["items"],
            schema_prompt=json.dumps([t for t in schema if t["table"] in candidates], ensure_ascii=False)
            + "\n"
            + block,
        )
        runtime.persist_semantic(context, resolved)
        if context.record:
            context.record(
                task_id=task_id,
                domain="semantic",
                node="semantic_preselection",
                inputs={"field_evidence": field_data["items"]},
                outputs=resolved.model_dump(mode="json"),
            )
        return {
            "semantic_ref": resolved.id,
            "context": {
                "source_tables": sorted(candidates),
                "tables": rows,
                "metric_names": names,
                "field_selection": args.field_selection,
                "semantic_block": block,
            },
            "operations": operations,
        }

    def add_metric_fields(self, metrics, required, allowed):
        """Keep canonical measure/filter/time columns even when TopK misses them."""
        from types import SimpleNamespace

        import sqlglot
        from sqlglot import exp

        from openchatbi.workflow.security import sql_resources

        for metric in metrics:
            sources = metric.source_tables
            if metric.sql_template:
                schema = [
                    SelectedTable(table=t, columns=[c["column_name"] for c in self.runtime.catalog.get_column_list(t)])
                    for t in sources
                ]
                used = sql_resources(metric.sql_template, SimpleNamespace(tables=schema), self.runtime.cfg.dialect)
                for table, fields in used.items():
                    if table not in allowed or not fields <= allowed[table]:
                        raise ValueError(f"Canonical fields unavailable for {metric.name}")
                    required[table].update(fields)
            for expression in (metric.time_field, metric.formula, *metric.filters):
                if not expression:
                    continue
                for col in sqlglot.parse_one(expression).find_all(exp.Column):
                    for table in sources:
                        if col.table in ("", table) and col.name in allowed[table]:
                            required[table].add(col.name)

    def schemas(self, rows):
        catalog = self.runtime.catalog
        output = []
        for row in rows:
            name = row["table"]
            info = catalog.get_table_information(name) if hasattr(catalog, "get_table_information") else {}
            fields = catalog.get_column_list(name)
            output.append(
                {
                    "table": row["table"],
                    "description": info.get("description", ""),
                    "selection_rule": info.get("selection_rule", ""),
                    "sql_rule": info.get("sql_rule", ""),
                    "derived_metric": info.get("derived_metric", ""),
                    "columns": [column for column in fields if column["column_name"] in row["columns"]],
                }
            )
        return output

    @staticmethod
    def selection_examples(question, candidates):
        # Preserve the original selection examples, now in their owning domain.
        from openchatbi.text2sql.data import table_selection_example_dict, table_selection_retriever

        if table_selection_retriever is None:
            return ""
        lines = []
        for document in table_selection_retriever.invoke(question):
            example = document.page_content
            tables = [table for table in table_selection_example_dict.get(example, []) if table in candidates]
            if example and tables:
                lines.append(f"- Question: {example}   Selected Tables: [{','.join(tables)}]")
        return "\n".join(lines)
