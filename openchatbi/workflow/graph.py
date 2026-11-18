"""Precompiled domain graphs with task-level dynamic scheduling."""

import asyncio
import json
import logging
import re
import uuid
from datetime import datetime
from typing import Annotated, Any, TypedDict

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.runnables import RunnableLambda
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.types import Overwrite, Send
from pydantic import ValidationError

from openchatbi.workflow.models import QueryPlan, ResultRef, Task, ToolError, ToolResult, references
from openchatbi.workflow.registry import Registry
from openchatbi.workflow.tools import AutoInput

logger = logging.getLogger(__name__)


def plan_failure(exc: Exception, request_id: str) -> str:
    """Expose actionable categories without leaking provider responses or credentials."""
    status = getattr(exc, "status_code", None)
    logger.warning("Workflow planning failed request_id=%s type=%s status=%s", request_id, type(exc).__name__, status)
    if status in (401, 403):
        return f"模型服务拒绝访问（HTTP {status}），请检查 OPENAI_API_KEY 与 OPENAI_BASE_URL，保存后重启工作台。"
    if status == 400:
        return "模型服务拒绝规划请求（HTTP 400），请检查所选模型的工具调用能力及参数兼容性。"
    if status == 429:
        return "模型服务限流或额度不足（HTTP 429），请检查服务额度后重试。"
    if isinstance(exc, (TimeoutError, ConnectionError)) or type(exc).__name__ in {
        "APITimeoutError",
        "APIConnectionError",
    }:
        return "连接模型服务失败或超时，请检查 OPENAI_BASE_URL 和网络后重试。"
    if isinstance(exc, (ValidationError, ValueError, KeyError)):
        return "模型返回的查询计划未通过格式、工具参数或依赖校验，本次执行已停止，请查看规划校验记录。"
    return "未能生成合法查询计划，请检查模型配置或明确查询需求。"


def dual(function):
    """Support both graph.invoke and graph.ainvoke with one implementation."""

    def sync(state):
        return asyncio.run(function(state))

    return RunnableLambda(sync, afunc=function)


def merge_results(left: dict, right: dict):
    return {**left, **right}


class WorkflowState(MessagesState):
    request_id: str
    question: str
    plan: dict
    results: Annotated[dict, merge_results]
    replans: int
    task_count: int
    plan_error: str
    final_answer: str
    workflow_events: list[dict]


class DomainState(TypedDict):
    task: dict
    request_id: str
    result: dict
    workflow_events: list[dict]


def resolve_arguments(value: Any, results: dict):
    if isinstance(value, dict):
        if "$ref" in value:
            ref = ResultRef.model_validate(value["$ref"])
            result = results.get(ref.task_id)
            if not result or result["status"] not in ("ok", "empty"):
                raise ValueError("Referenced task has no usable result")
            current = result["data"]
            if ref.path:
                for part in ref.path[1:].split("/"):
                    part = part.replace("~1", "/").replace("~0", "~")
                    current = current[int(part)] if isinstance(current, list) else current[part]
            return current
        return {key: resolve_arguments(item, results) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve_arguments(item, results) for item in value]
    return value


def validate_plan(plan: QueryPlan, registry: Registry, limit: int):
    if len(plan.tasks) > limit:
        raise ValueError("Task budget exhausted")
    for task in plan.tasks:
        if task.operation == "auto":
            if not references(task.args):
                AutoInput.model_validate(task.args)
            elif set(task.args) - set(AutoInput.model_fields):
                raise ValueError("Unknown capability argument fields")
            elif "question" not in task.args:
                raise ValueError("Capability question is required")
        else:
            spec = registry.get(task.operation, task.domain)
            if not references(task.args):
                spec.input_model.model_validate(task.args)
            else:
                # Names and references are validated now; concrete types are
                # validated immediately before the handler can be called.
                unknown = set(task.args) - set(spec.input_model.model_json_schema().get("properties", {}))
                if unknown:
                    raise ValueError("Unknown argument fields in task")
                if set(spec.input_model.model_json_schema().get("required", [])) - set(task.args):
                    raise ValueError("Missing required arguments in task")
    return plan


def direct_plan(question: str, runtime) -> QueryPlan | None:
    text = question.strip()
    listing = re.search(r"有哪些|有什么|列出|列表|清单|list", text, re.I)
    # Count only catalog objects. Business filters and row counts must use the
    # planner; e.g. '有多少表包含客户字段' is not the total catalog table count.
    count_text = text
    tables = runtime.catalog.get_table_list()
    for table in sorted(tables, key=len, reverse=True):
        count_text = count_text.replace(table, "")
    metadata_only = re.fullmatch(
        r"(?:现在|目前|当前|请问|帮我|看下|看看|查下|查询|一下|我们|我的|这个|这里|数据库|数据源|catalog|"
        r"里面|里|中|的|一共|总共|全部|所有|有|是|几|多少|数量|总数|张|个|条|表|业务|指标|字段|哪些|什么|"
        r"列出|列表|清单|list|tables|metrics|fields|我想看|啊|呢|吗|呀|吧|嘛|[\s？?。！!，,])+",
        count_text,
        re.I,
    )
    counting = re.search(r"几|多少|数量|总数", count_text) and metadata_only
    listing = listing and metadata_only
    # Avoid treating '列出去年各国销售额' or compound report requests as metadata lists.
    if not (listing or counting) or re.search(r"导出|对比|比较|并且|然后|csv|pdf", text, re.I):
        return None
    if "指标" in text or re.search(r"\bmetrics\b", text, re.I):
        return QueryPlan(
            tasks=[Task(id="metrics", domain="semantic", operation="search_metrics", args={"mode": "list"})]
        )
    if "字段" in text:
        tables = [table for table in tables if table in text]
        return QueryPlan(
            tasks=[
                Task(id="fields", domain="semantic", operation="search_fields", args={"mode": "list", "tables": tables})
            ]
        )
    if re.search(r"哪些表|什么表|表列表|表有哪些|\btables\b", text, re.I) or (counting and "表" in text):
        return QueryPlan(tasks=[Task(id="tables", domain="semantic", operation="search_tables", args={"mode": "list"})])
    return None


def build_domain_graph(domain, runtime):
    async def begin(state):
        task = state["task"]
        return {
            "workflow_events": [
                {
                    "kind": "task_start",
                    "domain": domain,
                    "task_id": task["id"],
                    "operation": task["operation"],
                    "args": task["args"],
                }
            ]
        }

    async def execute(state):
        task = Task.model_validate(state["task"])
        context = runtime.context(state["request_id"])
        if task.operation == "auto":
            from langgraph.errors import GraphInterrupt

            try:
                AutoInput.model_validate(task.args)
                result, events = await capability_agent(task, context, runtime)
            except GraphInterrupt:
                raise
            except Exception:
                result = ToolResult(
                    task_id=task.id,
                    function=domain,
                    status="error",
                    errors=[ToolError(code="capability_error", message="能力调用失败，请检查模型、工具配置或输入。")],
                )
                events = [
                    {
                        "kind": "function",
                        "domain": domain,
                        "task_id": task.id,
                        "operation": "auto",
                        "result": result.model_dump(mode="json"),
                    }
                ]
        else:
            result = await runtime.registry.call(task.operation, task.args, context, task.id, domain)
            events = [
                {
                    "kind": "function",
                    "domain": domain,
                    "task_id": task.id,
                    "operation": task.operation,
                    "args": task.args,
                    "result": result.model_dump(mode="json"),
                }
            ]
        return {"result": result.model_dump(mode="json"), "workflow_events": events}

    graph = StateGraph(DomainState)
    graph.add_node("begin", dual(begin))
    graph.add_node("invoke_function", dual(execute))
    graph.add_edge(START, "begin")
    graph.add_edge("begin", "invoke_function")
    graph.add_edge("invoke_function", END)
    return graph.compile(name=f"{domain}_capability")


async def capability_agent(task, context, runtime):
    """Domain-local tool selection; no per-function plan or forced tool sequence."""
    specs = runtime.registry.describe(task.domain)
    tools = [
        {
            "type": "function",
            "function": {"name": spec["name"], "description": spec["description"], "parameters": spec["input_schema"]},
        }
        for spec in specs
    ]
    model = runtime.llm.bind_tools(tools)
    messages = [
        SystemMessage(
            f"你负责{task.domain}能力域。按需调用本域工具，不必全部调用。"
            "列举和按ID查询用list/get，相似检索才用search。指标search只使用向量。不要杜撰口径或数据。"
            "优先使用已有数据集，未获得工具返回结果不得宣称已执行。"
        ),
        HumanMessage(json.dumps(task.args, ensure_ascii=False)),
    ]
    events, outputs = [], []
    from langchain_core.messages import ToolMessage

    for _ in range(6):
        response = await model.ainvoke(messages)
        messages.append(response)
        if not response.tool_calls:
            break
        for call in response.tool_calls:
            result = await runtime.registry.call(
                call["name"], call["args"], context, f"{task.id}_{len(outputs)}", task.domain
            )
            outputs.append(result)
            events.append(
                {
                    "kind": "function",
                    "domain": task.domain,
                    "task_id": result.task_id,
                    "operation": call["name"],
                    "args": call["args"],
                    "result": result.model_dump(mode="json"),
                }
            )
            messages.append(ToolMessage(content=result.model_dump_json(), tool_call_id=call["id"]))
        if context.calls >= context.max_calls:
            break
    if not outputs:
        return (
            ToolResult(
                task_id=task.id,
                function=task.domain,
                status="error",
                errors=[ToolError(code="no_evidence", message="能力调用没有返回工具证据。")],
            ),
            events,
        )
    usable = [item for item in outputs if item.status in ("ok", "empty")]
    context_values = {}
    for item in usable:
        for key, value in item.data.get("context", {}).items():
            if isinstance(value, list) and isinstance(context_values.get(key), list):
                context_values[key] = list(dict.fromkeys([*context_values[key], *value]))
            elif key == "semantic_block" and context_values.get(key) and context_values[key] != value:
                context_values[key] += "\n\n" + value
            else:
                context_values[key] = value
    failed = [error for item in outputs for error in item.errors]
    return (
        ToolResult(
            task_id=task.id,
            function=task.domain,
            status="error" if failed else "ok",
            data={
                **(usable[-1].data if usable else {}),
                "operations": [item.model_dump(mode="json") for item in outputs],
                "context": context_values,
            },
            errors=failed,
            snapshot_id=runtime.snapshot_id,
        ),
        events,
    )


def planner_prompt(runtime):
    from pathlib import Path

    from openchatbi.prompts.system_prompt import get_agent_extra_tool_use_rule, get_basic_knowledge

    interfaces = [
        spec
        for spec in runtime.registry.describe()
        if spec["name"] in {"resolve_query", "clarify", "query", "prepare_data", "export_csv", "analyze"}
    ]
    prompt = (Path(__file__).parents[1] / "prompts/workflow_planner_prompt.md").read_text(encoding="utf-8")
    return (
        prompt.replace("[basic_knowledge_glossary]", get_basic_knowledge())
        .replace("[extra_tool_use_rule]", get_agent_extra_tool_use_rule())
        .replace("[current_time]", datetime.now().astimezone().isoformat())
        .replace("[extensions]", json.dumps(runtime.registry.extension_summaries(), ensure_ascii=False))
        .replace("[interfaces]", json.dumps(interfaces, ensure_ascii=False))
    )


def build_workflow_graph(runtime, *, checkpointer=None, store=None, planner=None):
    """One stable supervisor and three reusable capability graphs."""
    cfg = runtime.cfg.workflow
    children = {domain: build_domain_graph(domain, runtime) for domain in ("semantic", "text2sql", "delivery")}

    async def plan(state):
        last = state["messages"][-1].content
        if not isinstance(last, str) and runtime.input_adapter:
            adapted = runtime.input_adapter(last)
            last = await adapted if hasattr(adapted, "__await__") else adapted
        if not isinstance(last, str):
            return {
                "request_id": uuid.uuid4().hex,
                "question": "",
                "plan": {},
                "results": Overwrite({}),
                "replans": 0,
                "task_count": 0,
                "plan_error": "图片等多模态输入尚未接入。",
            }
        request_id = uuid.uuid4().hex
        try:
            candidate = direct_plan(last, runtime)
            if candidate is None:
                candidate = await create_plan(last, None, state["messages"][:-1], request_id=request_id)
            validate_plan(candidate, runtime.registry, cfg.max_tasks)
            runtime.context(request_id).record(
                task_id="plan",
                domain="orchestration",
                node="plan",
                inputs={"question": last, "prompt_version": "workflow_planner@3"},
                outputs=candidate.model_dump(),
            )
            return {
                "request_id": request_id,
                "question": last,
                "plan": candidate.model_dump(),
                "results": Overwrite({}),
                "replans": 0,
                "task_count": len(candidate.tasks),
                "plan_error": "",
                "workflow_events": [{"kind": "plan", "plan": candidate.model_dump(), "version": 1}],
            }
        except Exception as exc:
            runtime.context(request_id).record(
                task_id="plan",
                domain="orchestration",
                node="plan",
                inputs={"question": last},
                outputs={"error_type": type(exc).__name__},
                status="error",
            )
            return {
                "request_id": request_id,
                "question": last,
                "plan": {},
                "results": Overwrite({}),
                "replans": 0,
                "task_count": 0,
                "plan_error": plan_failure(exc, request_id),
            }

    async def create_plan(question, previous, history=(), request_id=None):
        if planner:
            value = planner(question, previous)
            if hasattr(value, "__await__"):
                value = await value
            return QueryPlan.model_validate(value)
        messages = [SystemMessage(planner_prompt(runtime)), *list(history)[-6:], HumanMessage(question)]
        if previous:
            messages.append(HumanMessage("执行状态，必要时修订后续计划：" + json.dumps(previous, ensure_ascii=False)))
        messages[0] = SystemMessage(messages[0].content + "\n请调用 QueryPlan 工具提交完整计划。")
        # OpenAI-compatible reasoning providers may reject response_format=json_schema
        # and forced tool_choice. Auto tool calling supports their thinking mode.
        model = runtime.llm.with_structured_output(QueryPlan, method="function_calling", tool_choice="auto")
        for attempt in range(cfg.max_replans + 1):
            candidate = None
            try:
                candidate = await model.ainvoke(messages)
                if candidate is None:
                    raise ValueError("Model returned no QueryPlan tool call")
                candidate = QueryPlan.model_validate(candidate)
                return validate_plan(candidate, runtime.registry, cfg.max_tasks)
            except (ValidationError, ValueError, KeyError) as exc:
                if isinstance(exc, ValidationError):
                    issues = [
                        {"type": row["type"], "loc": list(row["loc"]), "message": row["msg"]}
                        for row in exc.errors(include_input=False, include_url=False)
                    ]
                else:
                    # These diagnostics describe contract checks, never HTTP response bodies.
                    known = (
                        "Model returned no",
                        "Unknown or disabled function",
                        "Unknown argument",
                        "Missing required",
                        "Task budget",
                        "Capability question",
                        "Unknown capability",
                    )
                    message = str(exc) if str(exc).startswith(known) else type(exc).__name__
                    issues = [{"type": type(exc).__name__, "message": message}]
                if request_id:
                    runtime.context(request_id).record(
                        task_id="plan",
                        domain="orchestration",
                        node="plan_validation",
                        status="error",
                        inputs={"attempt": attempt + 1},
                        outputs={
                            "issues": issues,
                            "candidate": candidate.model_dump() if isinstance(candidate, QueryPlan) else None,
                        },
                    )
                if attempt == cfg.max_replans:
                    raise
                messages.append(
                    HumanMessage(
                        "计划校验失败，请修正后调用 QueryPlan 工具提交完整计划。"
                        "如果数据无法回答，使用 semantic.clarify 说明缺失数据并询问可执行的替代分析。"
                        "所有任务必须使用已注册操作及其参数格式。校验问题：" + json.dumps(issues, ensure_ascii=False)
                    )
                )

    def schedule(state):
        if state.get("plan_error"):
            return {}
        results = dict(state["results"])
        blocked = {}
        changed = True
        while changed:
            changed = False
            for task in QueryPlan.model_validate(state["plan"]).tasks:
                if task.id in results:
                    continue
                if any(dep in results and results[dep]["status"] not in ("ok", "empty") for dep in task.depends_on):
                    result = ToolResult(
                        task_id=task.id,
                        function=task.operation,
                        status="blocked",
                        errors=[ToolError(code="dependency_failed", message="上游任务失败，未执行依赖任务。")],
                    )
                    results[task.id] = blocked[task.id] = result.model_dump(mode="json")
                    changed = True
        return {"results": blocked}

    def dispatch(state):
        if state.get("plan_error"):
            return "finalize"
        tasks = QueryPlan.model_validate(state["plan"]).tasks
        ready = [
            task
            for task in tasks
            if task.id not in state["results"]
            and all(
                dep in state["results"] and state["results"][dep]["status"] in ("ok", "empty")
                for dep in task.depends_on
            )
        ]
        if not ready:
            return "review"
        return [
            Send(
                task.domain,
                {"task": task.model_dump(), "request_id": state["request_id"], "upstream_results": state["results"]},
            )
            for task in ready[: cfg.max_concurrency]
        ]

    def capability(domain):
        async def invoke(state):
            task = Task.model_validate(state["task"])
            try:
                task = task.model_copy(update={"args": resolve_arguments(task.args, state["upstream_results"])})
            except (KeyError, IndexError, TypeError, ValueError):
                result = ToolResult(
                    task_id=task.id,
                    function=task.operation,
                    status="error",
                    errors=[ToolError(code="invalid_reference", message="上游结果引用无法解析。")],
                )
                return {"results": {task.id: result.model_dump(mode="json")}}
            value = await children[domain].ainvoke({"task": task.model_dump(), "request_id": state["request_id"]})
            return {"results": {task.id: value["result"]}}

        return dual(invoke)

    async def review(state):
        if any(
            error.get("code") == "permission_denied"
            for result in state["results"].values()
            for error in result["errors"]
        ):
            return {"plan_error": ""}
        errors = any(result["status"] in ("blocked", "error") for result in state["results"].values())
        if not errors or state["replans"] >= cfg.max_replans or state["task_count"] >= cfg.max_tasks:
            return {"plan_error": ""}
        try:
            replacement = await create_plan(
                state["question"], {"plan": state["plan"], "results": state["results"]}, request_id=state["request_id"]
            )
            validate_plan(replacement, runtime.registry, cfg.max_tasks)
            old = {task["id"]: task for task in state["plan"]["tasks"]}
            new = {task.id: task.model_dump() for task in replacement.tasks}
            succeeded = {key: result for key, result in state["results"].items() if result["status"] in ("ok", "empty")}
            if any(new.get(key) != old[key] for key in succeeded):
                raise ValueError("Replan changed completed evidence")
            count = state["task_count"] + len(new) - len(succeeded)
            if count > cfg.max_tasks:
                raise ValueError("Task budget exhausted")
            if all(key in succeeded for key in new):
                raise ValueError("Replan did not repair failed work")
            return {
                "plan": replacement.model_dump(),
                "results": Overwrite(succeeded),
                "replans": state["replans"] + 1,
                "task_count": count,
                "workflow_events": [
                    {
                        "kind": "replan",
                        "version": state["replans"] + 2,
                        "reason": "失败分支或缺少必要数据",
                        "plan": replacement.model_dump(),
                    }
                ],
            }
        except Exception:
            return {"replans": cfg.max_replans}

    def after_review(state):
        pending = state.get("plan") and any(task["id"] not in state["results"] for task in state["plan"]["tasks"])
        return "schedule" if pending else "finalize"

    async def finalize(state):
        answer = render_results(state, runtime)
        failed = bool(state.get("plan_error")) or any(
            result["status"] in ("error", "blocked") for result in state["results"].values()
        )
        runtime.context(state["request_id"]).record(
            task_id="finalize",
            domain="orchestration",
            node="finalize",
            inputs={"plan": state.get("plan", {}), "results": state["results"]},
            outputs={"answer": answer},
            status="error" if failed else "ok",
        )
        return {"messages": [AIMessage(answer, response_metadata={"workflow_error": failed})], "final_answer": answer}

    graph = StateGraph(WorkflowState)
    graph.add_node("plan", dual(plan))
    graph.add_node("schedule", schedule)
    for domain in children:
        graph.add_node(domain, capability(domain))
        graph.add_edge(domain, "schedule")
    graph.add_node("review", dual(review))
    graph.add_node("finalize", dual(finalize))
    graph.add_edge(START, "plan")
    graph.add_edge("plan", "schedule")
    graph.add_conditional_edges("schedule", dispatch, ["semantic", "text2sql", "delivery", "review", "finalize"])
    graph.add_conditional_edges("review", after_review, ["schedule", "finalize"])
    graph.add_edge("finalize", END)
    return graph.compile(name="four_domain", checkpointer=checkpointer, store=store).with_config(
        {"recursion_limit": max(50, cfg.max_tasks * 4 + 16)}
    )


def render_results(state, runtime):
    if state.get("plan_error"):
        return "请求失败：" + state["plan_error"]
    lines = []
    context = runtime.context(state["request_id"])
    failed = False
    for task in state["plan"]["tasks"]:
        result = state["results"].get(task["id"])
        if not result:
            continue
        if result["status"] in ("error", "blocked"):
            failed = True
            lines.append(f"{task['id']}：" + "；".join(error["message"] for error in result["errors"]))
            continue
        data = result["data"]
        if task["operation"] == "search_tables" and task["args"].get("mode") == "list":
            lines.append(f"当前 catalog 共 {len(data.get('items', []))} 张表。")
        if data.get("semantic_ref"):
            selection = data.get("context", {})
            lines.append("语义候选表：" + ", ".join(selection.get("source_tables", [])))
        elif "operations" in data:
            for operation in data["operations"]:
                lines.extend(describe_items(operation["data"]))
        else:
            lines.extend(describe_items(data))
        if data.get("summary"):
            lines.append(data["summary"])
        if data.get("dataset_ref"):
            dataset = context.datasets.get(data["dataset_ref"])
            if dataset:
                lines.append(f"{task['id']}：{len(dataset.rows)} 行，字段：{', '.join(dataset.columns)}。")
                if dataset.columns:

                    def cell(value):
                        return str(value if value is not None else "NULL").replace("|", "\\|").replace("\n", " ")

                    lines.extend(
                        [
                            "| " + " | ".join(map(cell, dataset.columns)) + " |",
                            "| " + " | ".join("---" for _ in dataset.columns) + " |",
                        ]
                    )
                    lines.extend("| " + " | ".join(map(cell, row)) + " |" for row in dataset.rows[:20])
                if data.get("truncated"):
                    lines.append("结果达到返回行数上限，可能已截断。")
        for artifact in data.get("files", []):
            lines.append(f"[下载 {artifact['filename']}]({artifact['download_url']})")
        if "payload" in data:
            lines.append(str(data["payload"]))
    if failed:
        lines.insert(0, "部分任务未完成；下方保留已获得的结果及失败原因。")
    return "\n\n".join(lines) or "查询完成，没有匹配结果。"


def describe_items(data):
    lines = []
    for item in data.get("items", []):
        title = item.get("label") or item.get("name") or item.get("id") or item.get("table") or "记录"
        description = item.get("definition") or item.get("description") or item.get("note") or ""
        lines.append(f"- **{title}**：{description}" if description else f"- **{title}**")
    if data.get("sql"):
        lines.append("```sql\n" + data["sql"] + "\n```")
    if data.get("unreachable"):
        lines.append("存在无法连通的表：" + ", ".join(data["unreachable"]))
    return lines
