"""Source adapters and the sole read-only consumer-facing provider contract."""

from collections.abc import Sequence
from typing import Protocol

from openchatbi.semantic.models import BusinessRule, JoinEdge, JoinPlan, MetricSpec


class MetricSource(Protocol):
    """指标来源。一个来源一个适配器；新增来源不改 loader。"""

    def load_metrics(self) -> Sequence[MetricSpec]: ...


class JoinSource(Protocol):
    def load_edges(self) -> Sequence[JoinEdge]: ...


class RuleSource(Protocol):
    def load_rules(self) -> Sequence[BusinessRule]: ...


class SemanticProvider(Protocol):
    """text2sql / patrol 消费的唯一门面。所有方法必须是纯读、无 I/O。"""

    def metric(self, name: str) -> MetricSpec | None: ...

    def search_metrics(self, question: str, k: int = 5) -> tuple[MetricSpec, ...]: ...

    def closure(self, names: Sequence[str]) -> tuple[MetricSpec, ...]:
        """递归展开依赖，去重、保序、环检测、深度上限。"""
        ...

    def plan_join(self, anchor: str, targets: Sequence[str]) -> JoinPlan: ...

    def rules_for(self, question: str, tables: Sequence[str] = ()) -> tuple[BusinessRule, ...]: ...

    def render(self, *, question: str, selected_tables: Sequence[str], metric_names: Sequence[str] = ()) -> str:
        """渲染成注入 prompt 的文本块。唯一允许产生自然语言的地方（见 render.py）。"""
        ...
