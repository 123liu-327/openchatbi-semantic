"""Validated plans and tool boundaries; plans contain data, never Python."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from openchatbi.semantic.models import JoinEdge

Domain = Literal["semantic", "text2sql", "delivery"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResultRef(Contract):
    task_id: str
    path: str = ""  # JSON pointer relative to the upstream result's data


class Task(Contract):
    id: str = Field(pattern=r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
    domain: Domain
    operation: str
    args: dict[str, Any] = Field(default_factory=dict)
    depends_on: list[str] = Field(default_factory=list)


def references(value: Any) -> list[ResultRef]:
    if isinstance(value, dict):
        if "$ref" in value:
            if set(value) != {"$ref"}:
                raise ValueError("A result reference cannot contain other keys")
            return [ResultRef.model_validate(value["$ref"])]
        return [ref for item in value.values() for ref in references(item)]
    if isinstance(value, list):
        return [ref for item in value for ref in references(item)]
    return []


class QueryPlan(Contract):
    tasks: list[Task] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_dependencies(self):
        tasks = {task.id: task for task in self.tasks}
        if len(tasks) != len(self.tasks):
            raise ValueError("Duplicate task ID")
        for task in self.tasks:
            if len(set(task.depends_on)) != len(task.depends_on):
                raise ValueError("Duplicate dependency")
            if any(dep not in tasks or dep == task.id for dep in task.depends_on):
                raise ValueError(f"Invalid dependencies for {task.id}")
            for ref in references(task.args):
                if ref.task_id not in task.depends_on:
                    raise ValueError(f"Reference {ref.task_id} must be an explicit dependency")
                if ref.path and not ref.path.startswith("/"):
                    raise ValueError("Reference paths must be JSON pointers")
        visited: set[str] = set()
        active: set[str] = set()

        def visit(name):
            if name in active:
                raise ValueError("Cyclic plan")
            if name in visited:
                return
            active.add(name)
            for dep in tasks[name].depends_on:
                visit(dep)
            active.remove(name)
            visited.add(name)

        for name in tasks:
            visit(name)
        return self


class ToolError(Contract):
    code: str
    message: str


class ToolResult(Contract):
    schema_version: int = 1
    task_id: str
    function: str
    status: Literal["ok", "empty", "blocked", "error"] = "ok"
    data: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)
    errors: list[ToolError] = Field(default_factory=list)
    duration_ms: float = 0
    snapshot_id: str = ""


class DataSet(Contract):
    id: str
    columns: list[str]
    rows: list[list[Any]]
    source_task: str

    @model_validator(mode="after")
    def validate_rows(self):
        if len(self.columns) != len(set(self.columns)):
            raise ValueError("Duplicate dataset column names")
        if any(len(row) != len(self.columns) for row in self.rows):
            raise ValueError("Dataset row width differs from its schema")
        return self


class SelectedTable(Contract):
    table: str
    columns: list[str] = Field(default_factory=list)


class ResolvedQuery(Contract):
    """Server-owned semantic contract, persisted separately from data results."""

    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    snapshot_id: str
    request_question: str
    rewrite_question: str = Field(min_length=1)
    tables: list[SelectedTable] = Field(min_length=1)
    schema_prompt: str = Field(min_length=1)
    info_entities: dict[str, Any] = Field(default_factory=dict)
    metric_names: list[str] = Field(default_factory=list)
    relationships: list[JoinEdge] = Field(default_factory=list)
    join_tables: list[SelectedTable] = Field(default_factory=list)
    table_metadata: dict[str, dict[str, Any]] = Field(default_factory=dict)
    semantic_block: str = ""
    anchor_hint: str = ""
    field_selection: Literal["hybrid", "llm"] = "hybrid"
    required_tables: list[str] = Field(default_factory=list)
    required_fields: dict[str, list[str]] = Field(default_factory=dict)
    field_evidence: list[dict[str, Any]] = Field(default_factory=list)


class FileArtifact(Contract):
    filename: str
    mime_type: str
    download_url: str
    rows: int
