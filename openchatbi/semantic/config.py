"""Semantic configuration without importing the application's configuration stack."""

from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator


class SemanticSourceConfig(BaseModel):
    model_config = ConfigDict(defer_build=True)
    type: Literal["yaml", "json", "catalog"] = "yaml"
    path: str | None = None
    priority: int = 100
    mapping_path: str | None = None

    @model_validator(mode="after")
    def _path_required(self) -> "SemanticSourceConfig":
        if self.type != "catalog" and not self.path:
            raise ValueError("semantic source path is required unless type=catalog")
        return self


class SemanticConfig(BaseModel):
    model_config = ConfigDict(defer_build=True)
    enabled: bool = False
    sources: list[SemanticSourceConfig] = Field(default_factory=list)
    join_source: Literal["catalog_fk", "declared", "both"] = "both"
    max_dependency_depth: int = Field(default=4, ge=0)
    strict: bool = False
    _config_path: Path | None = PrivateAttr(default=None)
