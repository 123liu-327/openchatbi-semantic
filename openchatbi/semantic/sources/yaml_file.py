"""Typed YAML join source, with paths independent of the process working directory."""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from openchatbi.semantic.models import BusinessRule, JoinEdge, MetricSpec
from openchatbi.semantic.sources import SemanticSourceError

T = TypeVar("T", bound=BaseModel)


class YamlJoinSource:
    def __init__(self, path: str | Path, *, config_path: str | Path | None = None) -> None:
        root = Path(__file__).resolve().parents[3]
        base = Path(config_path) if config_path is not None else None
        if base is not None:
            if not base.is_absolute():
                base = root / base
            root = base.parent
        self.path = Path(path)
        if not self.path.is_absolute():
            self.path = root / self.path
        self.path = self.path.resolve()

    def document(self) -> dict:
        try:
            text = self.path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise SemanticSourceError(f"{self.path} [file] encoding/path: {exc}") from exc
        if text.startswith("\ufeff"):
            raise SemanticSourceError(f"{self.path} [file] encoding: UTF-8 BOM is not allowed")
        try:
            data = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            raise SemanticSourceError(f"{self.path} [file] YAML: {exc}") from exc
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1:
            raise SemanticSourceError(f"{self.path} [file] version: expected version: 1")
        return data

    def records(self, model: type[T], section: str, data: dict | None = None) -> tuple[T, ...]:
        records = (self.document() if data is None else data).get(section)
        if not isinstance(records, list):
            raise SemanticSourceError(f"{self.path} [file] {section}: expected list")
        result = []
        for index, record in enumerate(records):
            if isinstance(record, dict):
                unknown = set(record) - model.model_fields.keys()
                if unknown:
                    raise SemanticSourceError(f"{self.path} [{section}:{index}] unknown fields: {sorted(unknown)}")
            try:
                edge = model.model_validate(record)
            except ValidationError as exc:
                raise SemanticSourceError(f"{self.path} [{section}:{index}]: {exc}") from exc
            if isinstance(edge, JoinEdge) and edge.source != "declared":
                raise SemanticSourceError(f"{self.path} [edges:{index}] source: YAML must declare relationships")
            result.append(edge)
        return tuple(result)

    def load_edges(self) -> tuple[JoinEdge, ...]:
        return self.records(JoinEdge, "edges")


class YamlMetricSource(YamlJoinSource):
    def load_metrics(self) -> tuple[MetricSpec, ...]:
        return self.records(MetricSpec, "metrics")


class YamlRuleSource(YamlJoinSource):
    def load_rules(self) -> tuple[BusinessRule, ...]:
        return self.records(BusinessRule, "rules")
