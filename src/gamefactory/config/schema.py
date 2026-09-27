"""Strict schema for AI Game Factory project contracts (.gamefactory/factory.yml).

Uses Pydantic with extra='forbid' to reject unknown fields and validate schema version.
"""

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_SECRET_KEY = re.compile(
    r"(secret|token|password|api[_-]?key|credential|authorization)", re.IGNORECASE
)


class ProjectInfo(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1)
    version: str = "0.1.0"

    @field_validator("id")
    @classmethod
    def validate_project_id(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._-]*", value):
            raise ValueError(
                "project id must contain only letters, digits, dot, underscore, or hyphen"
            )
        return value

    @field_validator("name")
    @classmethod
    def validate_project_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("project name cannot be blank")
        return value


class EngineConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: str = "godot"
    executable_path: str | None = None


class DccConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    blender_path: str | None = None


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    paid_operations_require_approval: bool = True
    destructive_operations_require_approval: bool = True
    require_approval_for_repo_write: bool = False
    max_operation_cost: float = Field(default=50.0, ge=0, allow_inf_nan=False)
    project_budget: float = Field(default=500.0, ge=0, allow_inf_nan=False)


class ProjectConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.1.0"]
    project: ProjectInfo
    engine: EngineConfig = Field(default_factory=EngineConfig)
    dcc: DccConfig = Field(default_factory=DccConfig)
    policies: PolicyConfig = Field(default_factory=PolicyConfig)
    targets: list[str] = Field(default_factory=lambda: ["desktop"])
    documents: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def reject_secret_fields(cls, value: Any) -> Any:
        def scan(node: Any) -> None:
            if isinstance(node, dict):
                for key, child in node.items():
                    if _SECRET_KEY.search(str(key)):
                        raise ValueError(
                            "secret fields are not allowed in Factory configuration; use environment variables"
                        )
                    scan(child)
            elif isinstance(node, list):
                for child in node:
                    scan(child)

        scan(value)
        return value


class UserConfig(BaseModel):
    """User-wide defaults; project-specific identity and docs stay in factory.yml."""

    model_config = ConfigDict(extra="forbid")

    engine: EngineConfig = Field(default_factory=EngineConfig)
    dcc: DccConfig = Field(default_factory=DccConfig)
    policies: PolicyConfig = Field(default_factory=PolicyConfig)
