"""Immutable semantic data contracts, independent of configuration and catalog I/O."""

from __future__ import annotations

from enum import Enum
from typing import Literal, Never

from pydantic import BaseModel, ConfigDict, Field, model_validator


class _SemanticModel(BaseModel):
    # Building validators scans installed Pydantic plugins; defer that I/O until
    # an explicit model construction, rather than importing these contracts.
    model_config = ConfigDict(frozen=True, defer_build=True)


class MetricLevel(str, Enum):  # noqa: UP042 -- Preserve the published str/Enum contract, including str().
    ATOMIC = "atomic"
    DERIVED = "derived"
    COMPOSITE = "composite"


class MetricSpec(_SemanticModel):
    name: str
    label: str
    aliases: tuple[str, ...] = ()
    level: MetricLevel
    definition: str = ""
    formula: str | None = None
    sql_template: str | None = None
    depends_on: tuple[str, ...] = ()
    time_field: str | None = None
    filters: tuple[str, ...] = ()
    source_tables: tuple[str, ...] = ()
    requires_multi_step: bool = False
    provenance: str = ""

    @model_validator(mode="after")
    def _one_of_formula_template_or_multistep(self) -> MetricSpec:
        if self.formula and self.sql_template:
            raise ValueError(f"metric {self.name!r} must choose formula or sql_template")
        has_expr = bool(self.formula or self.sql_template)
        if not has_expr and not self.requires_multi_step:
            raise ValueError(f"metric {self.name!r} needs formula, sql_template, or requires_multi_step=True")
        if self.level is MetricLevel.ATOMIC and not has_expr:
            raise ValueError(f"atomic metric {self.name!r} must be expressible in one SQL")
        return self


class BusinessRule(_SemanticModel):
    name: str
    trigger_any: tuple[str, ...] = Field(min_length=1)
    scope_tables: tuple[str, ...] = ()
    require_columns: tuple[str, ...] = ()
    forbid_columns: tuple[str, ...] = ()
    require_tables: tuple[str, ...] = ()
    note: str = ""
    provenance: str = ""


class JoinKey(_SemanticModel):
    left: str
    right: str


class JoinEdge(_SemanticModel):
    child: str
    parent: str
    keys: tuple[JoinKey, ...] = Field(min_length=1)
    relationship: Literal["many_to_one", "one_to_one", "one_to_many"] = "many_to_one"
    join_type: Literal["inner", "left"] = "left"
    source: Literal["foreign_key", "declared"] = "declared"
    note: str = ""
    provenance: str = ""


class JoinPlan(_SemanticModel):
    anchor: str
    steps: tuple[JoinEdge, ...]
    unreachable: tuple[str, ...] = ()
    reason: str = ""


class _FrozenMetrics(dict[str, MetricSpec]):
    """Retain the dict contract and Pydantic serialization, while blocking writes."""

    def _deny_mutation(self, *args: object, **kwargs: object) -> Never:
        raise TypeError("SemanticBundle.metrics is read-only")

    __setitem__ = _deny_mutation
    __delitem__ = _deny_mutation
    __ior__ = _deny_mutation
    clear = _deny_mutation
    pop = _deny_mutation
    popitem = _deny_mutation
    setdefault = _deny_mutation
    update = _deny_mutation

    def __copy__(self) -> _FrozenMetrics:
        return self

    def __deepcopy__(self, memo: dict[int, object]) -> _FrozenMetrics:
        return self


class SemanticBundle(_SemanticModel):
    """一次加载的完整只读视图。构造后不可变。"""

    metrics: dict[str, MetricSpec]
    rules: tuple[BusinessRule, ...]
    edges: tuple[JoinEdge, ...]
    tables: frozenset[str]

    @model_validator(mode="after")
    def _freeze_metrics(self) -> SemanticBundle:
        object.__setattr__(self, "metrics", _FrozenMetrics(self.metrics))
        return self
