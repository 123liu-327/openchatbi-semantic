"""Typed YAML join source, with paths independent of the process working directory."""

from pathlib import Path

import yaml
from pydantic import ValidationError

from openchatbi.semantic.models import JoinEdge
from openchatbi.semantic.sources import SemanticSourceError


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

    def load_edges(self) -> tuple[JoinEdge, ...]:
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
        records = data.get("edges")
        if not isinstance(records, list):
            raise SemanticSourceError(f"{self.path} [file] edges: expected list")
        result = []
        for index, record in enumerate(records):
            if isinstance(record, dict):
                unknown = set(record) - JoinEdge.model_fields.keys()
                if unknown:
                    raise SemanticSourceError(f"{self.path} [edges:{index}] unknown fields: {sorted(unknown)}")
            try:
                edge = JoinEdge.model_validate(record)
            except ValidationError as exc:
                raise SemanticSourceError(f"{self.path} [edges:{index}]: {exc}") from exc
            if edge.source != "declared":
                raise SemanticSourceError(f"{self.path} [edges:{index}] source: YAML must declare relationships")
            result.append(edge)
        return tuple(result)
