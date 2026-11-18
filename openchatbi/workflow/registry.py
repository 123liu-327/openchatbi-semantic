"""Shared validation and execution for local functions and external tools."""

import inspect
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pydantic import BaseModel, ValidationError

from openchatbi.workflow.models import Domain, ToolError, ToolResult


@dataclass
class CallContext:
    request_id: str
    snapshot_id: str = ""
    datasets: dict = field(default_factory=dict)
    query_cache: dict = field(default_factory=dict)
    semantics: dict = field(default_factory=dict)
    semantic_cache: dict = field(default_factory=dict)
    record: Callable | None = None
    calls: int = 0
    max_calls: int = 20
    query: Callable | None = None


@dataclass(frozen=True)
class ToolSpec:
    name: str
    domain: Domain
    description: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    handler: Callable
    enabled: bool = True
    version: int = 1
    external: bool = False


class Registry:
    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec):
        if spec.name in self._tools:
            raise ValueError(f"Duplicate function: {spec.name}")
        self._tools[spec.name] = spec

    def get(self, name: str, domain: Domain | None = None) -> ToolSpec:
        spec = self._tools.get(name)
        if spec is None or not spec.enabled or (domain is not None and spec.domain != domain):
            raise ValueError(f"Unknown or disabled function: {name}")
        return spec

    def describe(self, domain: Domain | None = None):
        return [
            {
                "name": spec.name,
                "domain": spec.domain,
                "description": spec.description,
                "input_schema": spec.input_model.model_json_schema(),
                "output_schema": spec.output_model.model_json_schema(),
            }
            for spec in self._tools.values()
            if spec.enabled and (domain is None or spec.domain == domain)
        ]

    def extension_summaries(self):
        return {
            domain: [
                spec.description[:240]
                for spec in self._tools.values()
                if spec.enabled and spec.external and spec.domain == domain
            ]
            for domain in ("semantic", "text2sql", "delivery")
        }

    async def call(self, name: str, args: dict, context: CallContext, task_id: str, domain=None) -> ToolResult:
        from langgraph.errors import GraphInterrupt

        try:
            result = await self._execute(name, args, context, task_id, domain)
        except GraphInterrupt:
            if context.record:
                context.record(
                    task_id=task_id,
                    domain=domain or "semantic",
                    node=name,
                    inputs=args,
                    outputs={"awaiting": "user_input"},
                    status="paused",
                )
            raise
        if context.record:
            context.record(
                task_id=task_id,
                domain=domain or self._tools.get(name).domain if name in self._tools else "unknown",
                node=name,
                inputs=args,
                outputs=result.model_dump(mode="json"),
                status=result.status,
                duration_ms=result.duration_ms,
            )
        return result

    async def _execute(self, name: str, args: dict, context: CallContext, task_id: str, domain=None) -> ToolResult:
        from langgraph.errors import GraphInterrupt

        start = time.monotonic()
        try:
            spec = self.get(name, domain)
            parsed = spec.input_model.model_validate(args)
            if context.calls >= context.max_calls:
                raise ValueError("Tool call budget exhausted")
            context.calls += 1
            value = spec.handler(parsed, context, task_id)
            if inspect.isawaitable(value):
                value = await value
            data = spec.output_model.model_validate(value).model_dump(mode="json")
            status = "empty" if "items" in data and not data["items"] else "ok"
            return ToolResult(
                task_id=task_id,
                function=name,
                data=data,
                status=status,
                snapshot_id=context.snapshot_id,
                duration_ms=(time.monotonic() - start) * 1000,
            )
        except GraphInterrupt:
            raise
        except Exception as exc:
            if isinstance(exc, PermissionError):
                message, code = "SQL execution was denied by the configured policy.", "permission_denied"
            elif isinstance(exc, ValidationError):
                message = str(exc.errors(include_input=False, include_url=False))
                code = "validation_error"
            elif isinstance(exc, ValueError):
                message, code = str(exc), "invalid_call"
            else:
                # SDK exceptions can embed headers, request bodies or credentials.
                message, code = "Function failed; check service configuration and logs.", "service_error"
            return ToolResult(
                task_id=task_id,
                function=name,
                status="blocked" if isinstance(exc, PermissionError) else "error",
                errors=[ToolError(code=code, message=message)],
                snapshot_id=context.snapshot_id,
                duration_ms=(time.monotonic() - start) * 1000,
            )

    def register_external(self, tool, domain: Domain):
        from openchatbi.workflow.tools import PayloadOutput

        if isinstance(tool.args_schema, dict):
            from pydantic import RootModel, model_validator

            schema = tool.args_schema

            class ExternalInput(RootModel[dict]):
                @model_validator(mode="after")
                def validate_json_schema(self):
                    import jsonschema

                    try:
                        jsonschema.validate(self.root, schema)
                    except jsonschema.ValidationError as exc:
                        raise ValueError("External arguments do not match the registered schema") from exc
                    return self

                @classmethod
                def model_json_schema(cls, **kwargs):
                    return schema

            input_model = ExternalInput
        elif isinstance(tool.args_schema, type) and issubclass(tool.args_schema, BaseModel):
            input_model = tool.args_schema
        else:
            raise ValueError(f"External tool {tool.name} must expose an argument schema")

        async def invoke(args, context, task_id):
            from langgraph.errors import GraphInterrupt

            try:
                return {"payload": await tool.ainvoke(args.model_dump())}
            except GraphInterrupt:
                raise
            except Exception as exc:
                raise RuntimeError("External tool failed") from exc

        self.register(ToolSpec(tool.name, domain, tool.description, input_model, PayloadOutput, invoke, external=True))
