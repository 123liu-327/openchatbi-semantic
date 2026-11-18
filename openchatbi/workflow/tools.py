"""Independently callable semantic, SQL and delivery functions."""

import asyncio
from typing import Any, Literal

from pydantic import Field

from openchatbi.workflow.models import Contract, DataSet
from openchatbi.workflow.registry import ToolSpec


class SearchInput(Contract):
    mode: Literal["search", "list", "get"] = "search"
    query: str = ""
    id: str | None = None
    tables: list[str] = Field(default_factory=list)
    top_k: int = Field(default=3, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=-1, le=1)


class FieldInput(SearchInput):
    top_k: int = Field(default=10, ge=1, le=100)
    rule_weight: float = Field(default=0.3, ge=0, le=1)


class SearchOutput(Contract):
    items: list[dict[str, Any]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    score_kind: str = "none"


class JoinInput(Contract):
    anchor: str = ""
    tables: list[str]


class JoinOutput(Contract):
    anchor: str
    steps: list[dict[str, Any]]
    unreachable: list[str]
    reason: str
    sql: str


class SQLInput(Contract):
    sql: str = Field(min_length=1)


class ExecuteSQLInput(SQLInput):
    semantic_ref: str


class CheckOutput(Contract):
    allowed: bool
    tables: list[str]


class QueryInput(Contract):
    question: str = Field(min_length=1)
    semantic_ref: str = Field(description="本次请求中 resolve_query 返回的 semantic_ref，必须通过依赖结果引用传入")
    visualize: bool = True


class ResolveInput(Contract):
    field_selection: Literal["hybrid", "llm"] = "hybrid"
    question: str = Field(min_length=1)
    tables: list[str] = Field(default_factory=list)
    metric_names: list[str] | None = None
    semantic_context: dict[str, Any] = Field(default_factory=dict)


class ResolveOutput(Contract):
    semantic_ref: str
    context: dict[str, Any]
    operations: list[dict[str, Any]] = Field(default_factory=list)


class AutoInput(Contract):
    question: str = Field(min_length=1)
    dataset_refs: list[str] = Field(default_factory=list)
    semantic_context: dict[str, Any] = Field(default_factory=dict)


class QueryOutput(Contract):
    dataset_ref: str
    columns: list[str]
    row_count: int
    sql: str = ""
    visualization_dsl: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    stages: list[dict[str, Any]] = Field(default_factory=list)
    evaluation_file: str = ""


class DeliveryInput(Contract):
    dataset_refs: list[str] = Field(min_length=1)
    title: str = "query_result"
    # concat aligns columns; compare groups per source and computes explicit numeric differences.
    operation: Literal["concat", "compare"] = "concat"
    value_column: str | None = None
    group_by: list[str] = Field(default_factory=list)


class DeliveryOutput(Contract):
    dataset_ref: str
    columns: list[str]
    row_count: int
    summary: str
    files: list[dict[str, Any]] = Field(default_factory=list)


class AnalysisInput(Contract):
    task: str = Field(min_length=1)
    dataset_refs: list[str] = Field(default_factory=list)


class PayloadOutput(Contract):
    payload: Any


class ClarifyInput(Contract):
    question: str = Field(min_length=1)
    options: list[str] = Field(default_factory=list, max_length=5)


class AnalysisOutput(Contract):
    payload: str
    dataset_refs: list[str]


def register_semantic(registry, catalog, semantic, retrieval):
    async def clarify(args, context, task_id):
        from langgraph.types import interrupt

        return {"payload": interrupt({"text": args.question, "buttons": args.options})}

    registry.register(
        ToolSpec("clarify", "semantic", "需求缺失或有歧义时向用户澄清", ClarifyInput, PayloadOutput, clarify)
    )

    def validate_tables(tables):
        if set(tables) - set(catalog.get_table_list()):
            raise ValueError("Unknown catalog table")

    def tables(args, context, task_id):
        available = catalog.get_table_list()
        if args.mode != "search":
            if args.mode == "get" and args.id not in available:
                raise ValueError("Unknown table ID")
            selected = [args.id] if args.mode == "get" else available
            return {"items": [{"table": name, "columns": catalog.get_column_list(name)} for name in selected]}
        hits = retrieval.search("tables", args.query, args.top_k)
        threshold = args.threshold if args.threshold is not None else 0.2
        return {
            "items": [
                {"table": hit["id"], "score": 1 - hit["distance"]} for hit in hits if 1 - hit["distance"] >= threshold
            ],
            "score_kind": "cosine_similarity",
        }

    def metrics(args, context, task_id):
        hits = []
        if args.mode == "search":
            threshold = args.threshold if args.threshold is not None else 0.3
            hits = [
                {"metric": semantic.metric(hit["id"]), "score": 1 - hit["distance"] / 2}
                for hit in retrieval.search("metrics", args.query, args.top_k)
                if 1 - hit["distance"] / 2 >= threshold
            ]
            matches = [hit["metric"] for hit in hits]
        elif args.mode == "get":
            metric = semantic.metric(args.id or "")
            if metric is None:
                raise ValueError("Unknown metric ID")
            matches = [metric]
        else:
            matches = list(semantic.list_metrics())
        scores = {hit["metric"].name: hit["score"] for hit in hits}
        closure = semantic.closure([metric.name for metric in matches])
        names = {metric.name for metric in matches}
        items = [
            {
                **metric.model_dump(mode="json"),
                "score": scores.get(metric.name),
                "is_dependency": metric.name not in names,
            }
            for metric in closure
        ]
        # Listing a catalog does not attempt to connect unrelated metrics' tables.
        result_context = {"metric_names": [metric.name for metric in matches]}
        if args.mode != "list" and matches:
            source_tables = sorted({table for metric in closure for table in metric.source_tables})
            result_context.update(
                source_tables=source_tables,
                semantic_block=semantic.render(
                    question=args.query, selected_tables=source_tables, metric_names=[m.name for m in matches]
                ),
            )
        return {
            "items": items,
            "context": result_context,
            "score_kind": "cosine_distance_half" if args.mode == "search" else "none",
        }

    def fields(args, context, task_id):
        validate_tables(args.tables)
        selected = args.tables or catalog.get_table_list()
        all_fields = {
            f"{table}.{col['column_name']}": {**col, "table": table, "id": f"{table}.{col['column_name']}"}
            for table in catalog.get_table_list()
            for col in catalog.get_column_list(table)
        }
        if args.mode != "search":
            if args.mode == "get":
                if args.id not in all_fields:
                    raise ValueError("Unknown qualified field ID")
                return {"items": [all_fields[args.id]]}
            return {"items": [field for field in all_fields.values() if field["table"] in selected]}
        rules = semantic.rules_for(args.query, selected)

        def expand(values):
            return {
                key
                for value in values
                for key in all_fields
                if key == value
                or ("." not in value and key.rsplit(".", 1)[-1] == value and all_fields[key]["table"] in selected)
            }

        required = expand([value for rule in rules for value in rule.require_columns])
        forbidden = expand([value for rule in rules for value in rule.forbid_columns])
        if required & forbidden:
            raise ValueError("Conflicting required and forbidden field rules")
        hits = retrieval.search("fields", args.query, min(args.top_k * 3, 30), selected)
        ranked = {}
        for hit in hits:
            key = hit["id"]
            if key in forbidden:
                continue
            embedding_score = 1 - hit["distance"]
            rule_score = 1.0 if key in required else 0.0
            score = (1 - args.rule_weight) * embedding_score + args.rule_weight * rule_score
            ranked[key] = {
                **all_fields[key],
                "score": score,
                "embedding_score": embedding_score,
                "rule_score": rule_score,
                "required": key in required,
            }
        for key in required:
            ranked.setdefault(
                key,
                {
                    **all_fields[key],
                    "score": args.rule_weight,
                    "embedding_score": 0.0,
                    "rule_score": 1.0,
                    "required": True,
                },
            )
        threshold = args.threshold if args.threshold is not None else 0.15
        mandatory = sorted((item for item in ranked.values() if item["required"]), key=lambda x: x["id"])
        optional = sorted(
            (item for item in ranked.values() if not item["required"] and item["score"] >= threshold),
            key=lambda x: (-x["score"], x["id"]),
        )[: max(0, args.top_k - len(mandatory))]
        return {
            "items": sorted(mandatory + optional, key=lambda x: (-x["score"], x["id"])),
            "score_kind": "vector_plus_rule",
            "context": {"required_fields": sorted(required), "forbidden_fields": sorted(forbidden)},
        }

    def rules(args, context, task_id):
        validate_tables(args.tables)
        matches = semantic.list_rules() if args.mode == "list" else semantic.rules_for(args.query, args.tables)
        if args.mode == "get":
            matches = tuple(rule for rule in semantic.list_rules() if rule.name == args.id)
            if not matches:
                raise ValueError("Unknown rule ID")
        return {"items": [rule.model_dump(mode="json") for rule in matches]}

    def join(args, context, task_id):
        from openchatbi.semantic.join import to_sql_clause

        validate_tables(args.tables)
        if args.anchor:
            validate_tables([args.anchor])
        plan = semantic.plan_join(args.anchor, args.tables)
        return {**plan.model_dump(mode="json"), "sql": to_sql_clause(plan) if not plan.unreachable else ""}

    def threaded(handler):
        async def invoke(args, context, task_id):
            return await asyncio.to_thread(handler, args, context, task_id)

        return invoke

    for name, description, input_model, output_model, handler in [
        ("search_tables", "查询业务表；支持 list/get/search", SearchInput, SearchOutput, tables),
        ("search_fields", "查询字段；search 为向量与规则评分，需要候选表时传 tables", FieldInput, SearchOutput, fields),
        (
            "search_metrics",
            "查询指标口径；search 为纯向量，list/get 不调用 embedding",
            SearchInput,
            SearchOutput,
            metrics,
        ),
        ("search_rules", "查询业务规则", SearchInput, SearchOutput, rules),
        ("plan_join", "根据声明的关系规划 JOIN", JoinInput, JoinOutput, join),
    ]:
        registry.register(ToolSpec(name, "semantic", description, input_model, output_model, threaded(handler)))


def prepare_delivery(args: DeliveryInput, context):
    """Only transform actual datasets; never manufacture rows from model prose."""
    import pandas as pd

    missing = set(args.dataset_refs) - set(context.datasets)
    if missing:
        raise ValueError("Unknown dataset reference in this request")
    datasets = [context.datasets[ref] for ref in args.dataset_refs]
    if args.operation == "concat":
        columns = list(dict.fromkeys(col for data in datasets for col in data.columns))
        rows = []
        for data in datasets:
            for row in data.rows:
                values = dict(zip(data.columns, row, strict=True))
                rows.append([values.get(col) for col in columns])
    else:
        if len(datasets) != 2 or not args.value_column:
            raise ValueError("compare requires exactly two datasets and value_column")
        frames = []
        for data in datasets:
            if {args.value_column, *args.group_by} - set(data.columns):
                raise ValueError("Comparison column is absent from a dataset")
            frame = pd.DataFrame(data.rows, columns=data.columns)
            frame[args.value_column] = pd.to_numeric(frame[args.value_column], errors="raise")
            if args.group_by:
                frame = frame.groupby(args.group_by, dropna=False, as_index=False)[args.value_column].sum()
            else:
                frame = pd.DataFrame({args.value_column: [frame[args.value_column].sum()]})
            frames.append(frame)
        if args.group_by:
            output = frames[0].merge(frames[1], on=args.group_by, how="outer", suffixes=("_left", "_right"))
        else:
            output = frames[0].join(frames[1], lsuffix="_left", rsuffix="_right")
        left, right = f"{args.value_column}_left", f"{args.value_column}_right"
        output["difference"] = output[right] - output[left]
        # Missing values remain explicit nulls; unmatched groups are not fabricated zeros.
        output = output.astype(object).where(output.notna(), None)
        columns, rows = list(output.columns), output.values.tolist()
    import uuid

    dataset = DataSet(id=uuid.uuid4().hex, columns=columns, rows=rows, source_task="delivery")
    context.datasets[dataset.id] = dataset
    return dataset
