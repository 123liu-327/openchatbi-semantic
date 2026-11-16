"""Explicitly injected facade skeleton; implementation follows in T4–T7."""

from __future__ import annotations

from collections.abc import Sequence

from openchatbi.semantic.models import BusinessRule, JoinPlan, MetricSpec, SemanticBundle


class SemanticLayer:
    def __init__(self, bundle: SemanticBundle, *, max_depth: int = 4) -> None:
        raise NotImplementedError("SemanticLayer assembly is implemented in T7")

    @classmethod
    def from_config(cls, cfg: object, catalog: object) -> SemanticLayer:
        """按 cfg.semantic.sources 装配适配器并构建 bundle。enabled=False 时返回空 bundle。"""
        raise NotImplementedError("SemanticLayer assembly is implemented in T7")

    def metric(self, name: str) -> MetricSpec | None:
        raise NotImplementedError

    def search_metrics(self, question: str, k: int = 5) -> tuple[MetricSpec, ...]:
        raise NotImplementedError

    def closure(self, names: Sequence[str]) -> tuple[MetricSpec, ...]:
        """递归展开依赖，去重、保序、环检测、深度上限。"""
        raise NotImplementedError

    def plan_join(self, anchor: str, targets: Sequence[str]) -> JoinPlan:
        raise NotImplementedError

    def rules_for(self, question: str, tables: Sequence[str] = ()) -> tuple[BusinessRule, ...]:
        raise NotImplementedError

    def render(self, *, question: str, selected_tables: Sequence[str], metric_names: Sequence[str] = ()) -> str:
        """渲染成注入 prompt 的文本块。唯一允许产生自然语言的地方（见 render.py）。"""
        raise NotImplementedError
