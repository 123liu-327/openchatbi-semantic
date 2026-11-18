"""Four-domain workflow configuration. Importing it performs no configuration I/O."""

from typing import Literal

from pydantic import BaseModel, Field


class AuthorizationConfig(BaseModel):
    principal: str | None = None
    # Trusted local-workbench configuration; absent grants deny access.
    table_columns: dict[str, list[str]] = Field(default_factory=dict)


class WorkflowConfig(BaseModel):
    mode: Literal["four_domain"] = "four_domain"
    max_concurrency: int = Field(default=3, ge=1, le=16)
    max_replans: int = Field(default=2, ge=0, le=5)
    max_tasks: int = Field(default=20, ge=1, le=100)
    max_tool_calls: int = Field(default=40, ge=1, le=200)
    retrieval_path: str = "data/semantic_retrieval/chroma"
    # Optional additive index metadata. Never rewrites the canonical catalog.
    retrieval_metadata_path: str | None = None
    external_tool_domains: dict[str, Literal["semantic", "text2sql", "delivery"]] = Field(default_factory=dict)
    authorization: AuthorizationConfig = Field(default_factory=AuthorizationConfig)
