"""Frozen CLOSED candidate runtime contract (V0.8-3B, ADR 0021)."""

from __future__ import annotations

import hashlib
import json
import math
from importlib import resources
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.asset_contracts import _as_number, _reject_bool_and_string
from gamefactory.core.domain.camera_framing import PLACED_VIEWS

RUNTIME_CONTRACT_SCHEMA = "candidate-runtime-contract-0.8.0"
RUNTIME_REQUEST_SCHEMA = "candidate-runtime-request-0.8.0"
RUNTIME_OBSERVATION_SCHEMA = "candidate-runtime-observation-0.8.0"


class CandidateRuntimeContractError(ValueError):
    """Packaged candidate runtime contract is invalid or mismatched."""


class ViewportContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width: Literal[1280]
    height: Literal[720]


class FramingContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fov_degrees: float = Field(..., ge=1.0, le=120.0)
    min_screen_fraction: float = Field(..., gt=0.0, lt=1.0)
    max_screen_fraction: float = Field(..., gt=0.0, lt=1.0)
    target_screen_fraction: float = Field(..., gt=0.0, lt=1.0)
    margin_fraction: float = Field(..., ge=0.0, lt=0.5)

    @field_validator(
        "fov_degrees",
        "min_screen_fraction",
        "max_screen_fraction",
        "target_screen_fraction",
        "margin_fraction",
        mode="before",
    )
    @classmethod
    def reject_bool_str_framing(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "number")
        if not math.isfinite(_as_number(value)):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @model_validator(mode="after")
    def ordered_fractions(self) -> FramingContract:
        if not self.min_screen_fraction <= self.target_screen_fraction <= self.max_screen_fraction:
            raise ValueError("framing fractions are not ordered")
        return self


class CandidateRuntimeContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["candidate-runtime-contract-0.8.0"]
    candidate_state: Literal["CLOSED"]
    public_status: Literal["UNSUPPORTED"]
    production_eligible: Literal[False]
    visual_mesh_name: Literal["SM_HumanoidSkin"]
    scene_root_name: Literal["HumanoidRoot"]
    origin_contract: Literal["humanoid_reference_root"]
    runtime_body_kind: Literal["static_body"]
    collision_shape_class: Literal["CapsuleShape3D"]
    min_capsule_radius_m: float
    viewport: ViewportContract
    renderer_profile: Literal["gl_compatibility"]
    nine_view_set: list[str] = Field(..., min_length=9, max_length=9)
    framing: FramingContract
    require_physics_ray_hit: Literal[True]
    max_request_bytes: int = Field(..., ge=1024, le=1_000_000)

    @field_validator("production_eligible", "require_physics_ray_hit", mode="before")
    @classmethod
    def strict_bool(cls, value: Any, info: Any) -> Any:
        if type(value) is not bool:
            raise ValueError(f"{info.field_name} must be a boolean")
        return value

    @field_validator("min_capsule_radius_m")
    @classmethod
    def frozen_min_radius(cls, value: float) -> float:
        if value != 0.1:
            raise ValueError("min_capsule_radius_m is frozen at 0.1")
        return value

    @field_validator("nine_view_set")
    @classmethod
    def views_are_placed(cls, value: list[str]) -> list[str]:
        if sorted(value) != sorted(PLACED_VIEWS) or len(set(value)) != len(PLACED_VIEWS):
            raise ValueError("nine_view_set must list every placed review view exactly once")
        return value


def _canonical_json_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def runtime_contract_sha256(contract: CandidateRuntimeContract) -> str:
    return hashlib.sha256(_canonical_json_bytes(contract)).hexdigest()


def load_packaged_candidate_runtime_contract() -> CandidateRuntimeContract:
    raw = (
        resources.files("gamefactory.resources.v08_candidate")
        .joinpath("candidate-runtime-contract-0.8.0.json")
        .read_text(encoding="utf-8")
    )
    try:
        return CandidateRuntimeContract.model_validate(json.loads(raw))
    except Exception as exc:
        raise CandidateRuntimeContractError(f"packaged runtime contract invalid: {exc}") from exc


def packaged_runtime_contract_digest() -> str:
    return runtime_contract_sha256(load_packaged_candidate_runtime_contract())
