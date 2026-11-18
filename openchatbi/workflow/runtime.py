"""Adapters around the existing SQL/analysis graphs and request-scoped data."""

import asyncio
import json
import uuid
from pathlib import Path

from openchatbi.workflow.artifacts import dataset_from_csv, save_csv
from openchatbi.workflow.ledger import RunLedger
from openchatbi.workflow.models import DataSet, ResolvedQuery
from openchatbi.workflow.registry import CallContext, ToolSpec
from openchatbi.workflow.retrieval import RetrievalService, digest
from openchatbi.workflow.security import (
    authorize_resources,
    check_resolved_sql,
    check_sql,
    execute_readonly,
    guarded_sql,
)
from openchatbi.workflow.tools import (
    AnalysisInput,
    AnalysisOutput,
    CheckOutput,
    DeliveryInput,
    DeliveryOutput,
    ExecuteSQLInput,
    QueryInput,
    QueryOutput,
    ResolveInput,
    ResolveOutput,
    SQLInput,
    prepare_delivery,
    register_semantic,
)


class WorkflowRuntime:
    def __init__(
        self,
        cfg,
        catalog,
        semantic,
        sql_graph,
        registry,
        llm,
        *,
        provider=None,
        retrieval=None,
        authorizer=None,
        input_adapter=None,
        identity_provider=None,
    ):
        self.cfg, self.catalog, self.semantic = cfg, catalog, semantic
        self.sql_graph, self.registry, self.llm, self.provider = sql_graph, registry, llm, provider
        # Trusted application hooks. Neither is accepted as an LLM tool argument.
        self.authorizer, self.input_adapter = authorizer, input_adapter
        self.identity_provider = identity_provider
        overlay = None
        if cfg.workflow.retrieval_metadata_path:
            overlay = json.loads(Path(cfg.workflow.retrieval_metadata_path).read_text(encoding="utf-8"))
        self.retrieval = retrieval or RetrievalService(catalog, semantic, cfg.workflow.retrieval_path, overlay=overlay)
        self.snapshot_id = self.retrieval.snapshot_id
        self.contexts = {}
        register_semantic(registry, catalog, semantic, self.retrieval)
        from openchatbi.workflow.semantic_query import SemanticResolver

        self.semantic_resolver = SemanticResolver(self)
        self._register_execution()

    def context(self, request_id):
        if request_id not in self.contexts:
            if len(self.contexts) >= 128:
                self.contexts.pop(next(iter(self.contexts)))
            context = CallContext(request_id, self.snapshot_id, max_calls=self.cfg.workflow.max_tool_calls)
            context.record = RunLedger(self.cfg.report_directory, request_id).record
            context.query = self.query
            root = self.dataset_directory(request_id)
            if root.exists():
                for path in root.glob("*.json"):
                    dataset = DataSet.model_validate_json(path.read_text(encoding="utf-8"))
                    context.datasets[dataset.id] = dataset
                for path in (root / "semantics").glob("*.json"):
                    resolved = ResolvedQuery.model_validate_json(path.read_text(encoding="utf-8"))
                    context.semantics[resolved.id] = resolved
            self.contexts[request_id] = context
        return self.contexts[request_id]

    def dataset_directory(self, request_id):
        if len(request_id) != 32 or any(char not in "0123456789abcdef" for char in request_id):
            raise ValueError("Invalid request ID")
        return Path(self.cfg.report_directory) / ".datasets" / request_id

    def persist(self, context, dataset):
        context.datasets[dataset.id] = dataset
        root = self.dataset_directory(context.request_id)
        root.mkdir(parents=True, exist_ok=True)
        target = root / f"{dataset.id}.json"
        temporary = root / f"{dataset.id}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(dataset.model_dump_json(), encoding="utf-8")
        temporary.replace(target)

    def query_key(self, args):
        return digest(
            {
                "question": args.question,
                "semantic_ref": args.semantic_ref,
                "snapshot": self.snapshot_id,
                "visualize": args.visualize,
            }
        )

    def persist_semantic(self, context, resolved):
        context.semantics[resolved.id] = resolved
        root = self.dataset_directory(context.request_id) / "semantics"
        root.mkdir(parents=True, exist_ok=True)
        temporary = root / f"{resolved.id}.{uuid.uuid4().hex}.tmp"
        temporary.write_text(resolved.model_dump_json(), encoding="utf-8")
        temporary.replace(root / f"{resolved.id}.json")

    async def request_query(self, question, context, task_id, visualize=True):
        """Supplemental analysis requests still resolve semantics before entering SQL."""
        semantic = await self.registry.call(
            "resolve_query", {"question": question}, context, f"{task_id}_semantic", "semantic"
        )
        if semantic.status != "ok":
            return semantic
        return await self.registry.call(
            "query",
            {
                "question": question,
                "semantic_ref": semantic.data["semantic_ref"],
                "visualize": visualize,
            },
            context,
            task_id,
            "text2sql",
        )

    def analysis_schema_tools(self, context, task_id):
        from langchain_core.tools import StructuredTool

        from openchatbi.tool.search_knowledge import SearchSchemaInput, ShowSchemaInput

        async def show(reasoning, tables):
            outputs = []
            for table in tables:
                result = await self.registry.call(
                    "search_tables",
                    {"mode": "get", "id": table},
                    context,
                    f"{task_id}_schema_{len(outputs)}",
                    "semantic",
                )
                outputs.append(result.model_dump(mode="json"))
            return {"operations": outputs}

        async def search(reasoning, query_list, dimensions=None, metrics=None, max_tables=5, include_columns=True):
            question = " ".join([*query_list, *(dimensions or []), *(metrics or [])])
            result = await self.registry.call(
                "search_tables",
                {"query": question, "top_k": min(max_tables, 100)},
                context,
                f"{task_id}_tables",
                "semantic",
            )
            outputs = [result.model_dump(mode="json")]
            if include_columns and result.status == "ok":
                fields = await self.registry.call(
                    "search_fields",
                    {"query": question, "tables": [row["table"] for row in result.data["items"]]},
                    context,
                    f"{task_id}_fields",
                    "semantic",
                )
                outputs.append(fields.model_dump(mode="json"))
            return {"operations": outputs}

        return [
            StructuredTool.from_function(
                coroutine=search,
                name="search_schema",
                description="委托语义域检索候选表字段",
                args_schema=SearchSchemaInput,
            ),
            StructuredTool.from_function(
                coroutine=show,
                name="show_schema",
                description="委托语义域读取已知表的字段",
                args_schema=ShowSchemaInput,
            ),
        ]

    async def query(self, args, context, task_id):
        resolved = context.semantics.get(args.semantic_ref)
        if resolved is None or resolved.snapshot_id != self.snapshot_id:
            raise ValueError("SQL requires a valid resolve_query semantic_ref from this request and snapshot")
        if args.question != resolved.request_question:
            raise ValueError("Query scope changed; resolve semantics for the new question first")

        def authorize(sql):
            scope = resolved.model_copy(update={"tables": [*resolved.tables, *resolved.join_tables]})
            check_resolved_sql(sql, scope, self.cfg.dialect)
            access = (
                self.identity_provider(context.request_id)
                if self.identity_provider
                else self.cfg.workflow.authorization.model_dump()
            )
            result = authorize_resources(sql, scope, self.cfg.dialect, access)
            if self.authorizer:
                self.authorizer(context, sql)
            return result

        key = self.query_key(args)
        if key in context.query_cache:
            result = await asyncio.shield(context.query_cache[key])
            authorize(result["sql"])
            return result

        async def run():
            with guarded_sql(authorize):
                result = await self.sql_graph.ainvoke(
                    {
                        "messages": [{"role": "user", "content": resolved.rewrite_question}],
                        "rewrite_question": resolved.rewrite_question,
                        "tables": [table.model_dump() for table in resolved.tables],
                        "info_entities": resolved.info_entities,
                        "prepared_schema": resolved.schema_prompt,
                        "resolved_query": resolved.model_dump(mode="json"),
                        "request_id": context.request_id,
                        "task_id": task_id,
                        "visualize": args.visualize,
                    }
                )
            if not result.get("data"):
                if any(stage["status"] == "blocked" for stage in result.get("sql_stages", [])):
                    raise PermissionError("SQL authorization rejected this query")
                if result.get("stage_error"):
                    raise ValueError(result["stage_error"])
                raise ValueError("Text2SQL did not return a successful dataset; inspect the SQL execution steps")
            dataset = dataset_from_csv(result["data"], task_id)
            self.persist(context, dataset)
            return {
                "dataset_ref": dataset.id,
                "columns": dataset.columns,
                "row_count": len(dataset.rows),
                "sql": result.get("sql", ""),
                "visualization_dsl": result.get("visualization_dsl") or {},
                "truncated": len(dataset.rows) >= self.cfg.sql_result_limit,
                "stages": result.get("sql_stages", []),
                "evaluation_file": str(RunLedger(self.cfg.report_directory, context.request_id).path),
            }

        future = asyncio.create_task(run())
        context.query_cache[key] = future
        try:
            return await asyncio.shield(future)
        except BaseException:
            context.query_cache.pop(key, None)
            raise

    def _register_execution(self):
        registry = self.registry

        async def query(args, context, task_id):
            return await self.query(args, context, task_id)

        async def resolve(args, context, task_id):
            return await self.semantic_resolver.resolve(args, context, task_id)

        def check(args, context, task_id):
            return check_sql(args.sql, self.cfg.dialect)

        async def execute(args, context, task_id):
            resolved = context.semantics.get(args.semantic_ref)
            if resolved is None or resolved.snapshot_id != self.snapshot_id:
                raise ValueError("Direct execution requires a current semantic_ref")

            def authorize(sql):
                scope = resolved.model_copy(update={"tables": [*resolved.tables, *resolved.join_tables]})
                check_resolved_sql(sql, scope, self.cfg.dialect)
                access = (
                    self.identity_provider(context.request_id)
                    if self.identity_provider
                    else self.cfg.workflow.authorization.model_dump()
                )
                authorize_resources(sql, scope, self.cfg.dialect, access)
                if self.authorizer:
                    self.authorizer(context, sql)

            columns, rows = await asyncio.to_thread(
                execute_readonly,
                self.catalog,
                args.sql,
                self.cfg.dialect,
                self.cfg.sql_result_limit,
                authorize,
            )
            dataset = DataSet(id=uuid.uuid4().hex, columns=columns, rows=rows, source_task=task_id)
            self.persist(context, dataset)
            return {
                "dataset_ref": dataset.id,
                "columns": columns,
                "row_count": len(rows),
                "sql": args.sql,
                "truncated": len(rows) >= self.cfg.sql_result_limit,
            }

        async def deliver(args, context, task_id):
            dataset = await asyncio.to_thread(prepare_delivery, args, context)
            self.persist(context, dataset)
            files = []
            return {
                "dataset_ref": dataset.id,
                "columns": dataset.columns,
                "row_count": len(dataset.rows),
                "summary": f"整理了 {len(args.dataset_refs)} 个数据集，共 {len(dataset.rows)} 行。",
                "files": files,
            }

        async def export(args, context, task_id):
            result = await deliver(args, context, task_id)
            dataset = context.datasets[result["dataset_ref"]]
            artifact = await asyncio.to_thread(save_csv, dataset, self.cfg.report_directory, args.title)
            result["files"] = [artifact.model_dump()]
            return result

        async def analyze(args, context, task_id):
            from langchain_core.tools import StructuredTool

            from openchatbi.agent_graph import CallSQLGraphInput
            from openchatbi.analysis.agent import build_data_analysis_agent

            if set(args.dataset_refs) - set(context.datasets):
                raise ValueError("Unknown dataset reference")
            previous_datasets = set(context.datasets)

            async def query_again(reasoning, context: str | dict | list, visualize=True):
                question = context if isinstance(context, str) else json.dumps(context, ensure_ascii=False)
                result = await self.request_query(question, self.contexts[context_id], f"{task_id}_query", visualize)
                return result.model_dump_json()

            context_id = context.request_id
            sql_tool = StructuredTool.from_function(
                coroutine=query_again,
                name="text2sql",
                description="通过统一入口补充查询，返回数据集引用。已有数据请直接使用。",
                args_schema=CallSQLGraphInput,
            )
            agent = build_data_analysis_agent(
                self.sql_graph,
                llm_provider=self.provider,
                text2sql_tool=sql_tool,
                schema_tools=self.analysis_schema_tools(context, task_id),
            )
            samples = [
                {
                    "dataset_ref": ref,
                    "columns": context.datasets[ref].columns,
                    "row_count": len(context.datasets[ref].rows),
                    "sample": context.datasets[ref].rows[:20],
                    "local_dataset_file": str((self.dataset_directory(context.request_id) / f"{ref}.json").resolve()),
                }
                for ref in args.dataset_refs
            ]
            with guarded_sql((lambda sql: self.authorizer(context, sql)) if self.authorizer else None):
                result = await agent.ainvoke(
                    {
                        "messages": [
                            {
                                "role": "user",
                                "content": args.task + "\n现有数据集：" + json.dumps(samples, ensure_ascii=False),
                            }
                        ]
                    }
                )
            from openchatbi.utils import get_text_from_content

            return {
                "payload": get_text_from_content(result["messages"][-1].content),
                "dataset_refs": list(
                    dict.fromkeys([*args.dataset_refs, *sorted(set(context.datasets) - previous_datasets)])
                ),
            }

        for name, domain, description, input_model, output_model, handler in [
            (
                "resolve_query",
                "semantic",
                "预选业务表、字段和指标，返回供 SQL 使用的 semantic_ref",
                ResolveInput,
                ResolveOutput,
                resolve,
            ),
            (
                "query",
                "text2sql",
                "使用语义层选好的表字段生成并执行 SQL",
                QueryInput,
                QueryOutput,
                query,
            ),
            ("check_sql", "text2sql", "检查一条 SQL 是否符合只读策略", SQLInput, CheckOutput, check),
            (
                "execute_sql",
                "text2sql",
                "执行已确认语义范围内的 SQL，强制安全与用户权限检查",
                ExecuteSQLInput,
                QueryOutput,
                execute,
            ),
            (
                "prepare_data",
                "delivery",
                "合并数据；compare 计算第二份减第一份，可按维度分组",
                DeliveryInput,
                DeliveryOutput,
                deliver,
            ),
            ("export_csv", "delivery", "整理真实数据集并导出可下载 CSV", DeliveryInput, DeliveryOutput, export),
            (
                "analyze",
                "delivery",
                "使用原数据分析 Agent 分析已有数据，可通过统一入口补充查询",
                AnalysisInput,
                AnalysisOutput,
                analyze,
            ),
        ]:
            registry.register(ToolSpec(name, domain, description, input_model, output_model, handler))

        def unavailable(args, context, task_id):
            raise ValueError("This extension is not implemented")

        registry.register(
            ToolSpec(
                "export_pdf",
                "delivery",
                "Reserved PDF extension",
                DeliveryInput,
                DeliveryOutput,
                unavailable,
                enabled=False,
            )
        )
