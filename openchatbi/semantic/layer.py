"""Explicitly constructed, read-only facade over one validated semantic snapshot."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence

from openchatbi.semantic.closure import expand_dependencies
from openchatbi.semantic.config import SemanticConfig
from openchatbi.semantic.join import JoinGraph
from openchatbi.semantic.models import BusinessRule, JoinPlan, MetricSpec, SemanticBundle
from openchatbi.semantic.render import render_semantic_block
from openchatbi.semantic.rules import match_rules, validate_metric_rules


class SemanticLayer:
    def __init__(self, bundle: SemanticBundle, *, max_depth: int = 4) -> None:
        if max_depth < 0:
            raise ValueError("max_depth must be nonnegative")
        self._bundle = bundle
        self._max_depth = max_depth
        self._graph = JoinGraph(bundle.edges, tables=tuple(bundle.tables))
        self.warnings: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, cfg: object, catalog: object) -> SemanticLayer:
        """按 cfg.semantic.sources 装配适配器并构建 bundle。enabled=False 时返回空 bundle。"""
        raw = cfg.get("semantic", {}) if isinstance(cfg, Mapping) else getattr(cfg, "semantic", {})
        enabled = raw.get("enabled", False) if isinstance(raw, Mapping) else getattr(raw, "enabled", False)
        if enabled is False:
            return cls(SemanticBundle.model_construct(metrics={}, rules=(), edges=(), tables=frozenset()))
        semantic_cfg = raw if isinstance(raw, SemanticConfig) else SemanticConfig.model_validate(raw)
        if not semantic_cfg.enabled:
            return cls(
                SemanticBundle(metrics={}, rules=(), edges=(), tables=frozenset()),
                max_depth=semantic_cfg.max_dependency_depth,
            )
        from openchatbi.semantic.loader import load_bundle

        bundle, warnings = load_bundle(semantic_cfg, catalog)
        layer = cls(bundle, max_depth=semantic_cfg.max_dependency_depth)
        layer.warnings = warnings
        return layer

    def metric(self, name: str) -> MetricSpec | None:
        return self._bundle.metrics.get(name)

    def list_metrics(self) -> tuple[MetricSpec, ...]:
        """Enumerate canonical records without retrieval or I/O."""
        return tuple(self._bundle.metrics.values())

    def list_rules(self) -> tuple[BusinessRule, ...]:
        """Enumerate rules without manufacturing a matching question."""
        return self._bundle.rules

    @property
    def snapshot_id(self) -> str:
        """Identity includes metric expressions, rule constraints and directed joins."""
        payload = self._bundle.model_dump(mode="json")
        payload["tables"] = sorted(payload["tables"])
        return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()

    @property
    def is_empty(self) -> bool:
        return not (self._bundle.metrics or self._bundle.rules or self._bundle.edges)

    def search_metrics(self, question: str, k: int = 5) -> tuple[MetricSpec, ...]:
        if k <= 0:
            return ()
        text = question.casefold()
        matches = []
        for metric in self._bundle.metrics.values():
            for alias in (metric.name, metric.label, *metric.aliases):
                word = alias.casefold()
                if not word:
                    continue
                pattern = re.escape(word)
                if word.isascii():
                    pattern = r"(?<!\w)" + pattern + r"(?!\w)"
                for match in re.finditer(pattern, text):
                    matches.append((match.start(), match.end(), metric.name))
        accepted: list[tuple[int, int]] = []
        names: list[str] = []
        for start, end, name in sorted(matches, key=lambda m: (-(m[1] - m[0]), m[0], m[2])):
            if any(start < other_end and end > other_start for other_start, other_end in accepted):
                continue
            accepted.append((start, end))
            if name not in names:
                names.append(name)
        return tuple(self._bundle.metrics[name] for name in names[:k])

    def closure(self, names: Sequence[str]) -> tuple[MetricSpec, ...]:
        """递归展开依赖，去重、保序、环检测、深度上限。"""
        return expand_dependencies(self._bundle.metrics, names, max_depth=self._max_depth)

    def plan_join(self, anchor: str, targets: Sequence[str]) -> JoinPlan:
        return self._graph.plan_join(anchor, targets)

    def relationships(self):
        """Declared relationship facts, without selecting an anchor or execution order."""
        return self._bundle.edges

    def rules_for(self, question: str, tables: Sequence[str] = ()) -> tuple[BusinessRule, ...]:
        return match_rules(self._bundle.rules, question, tables)

    def render(self, *, question: str, selected_tables: Sequence[str], metric_names: Sequence[str] = ()) -> str:
        """渲染成注入 prompt 的文本块。唯一允许产生自然语言的地方（见 render.py）。"""
        names = tuple(metric_names) or tuple(m.name for m in self.search_metrics(question))
        metrics = self.closure(names)
        rules = self.rules_for(question, selected_tables)
        validate_metric_rules(metrics, rules)
        plan = self.plan_join("", selected_tables)
        return render_semantic_block(metrics=metrics, edges=plan.steps, rules=rules)
