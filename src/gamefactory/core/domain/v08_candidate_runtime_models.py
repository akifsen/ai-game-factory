"""Pydantic models for published V0.8-3B candidate runtime JSON schemas."""

from __future__ import annotations

import math
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.asset_contracts import _as_number, _reject_bool_and_string
from gamefactory.core.domain.v08_candidate_runtime_contract import (
    FramingContract,
    ViewportContract,
    load_packaged_candidate_runtime_contract,
)
from gamefactory.core.domain.v08_candidate_runtime_integers import (
    JSON_SAFE_INTEGER_MAX,
    CandidateRuntimeIntegerError,
    strict_runtime_int,
)


def _strict_counter(value: Any, info: Any) -> int:
    try:
        return strict_runtime_int(value, info.field_name, minimum=1)
    except CandidateRuntimeIntegerError as exc:
        raise ValueError(str(exc)) from exc


def _finite_coord_list(value: Any, label: str) -> Any:
    if not isinstance(value, list) or len(value) != 3:
        raise ValueError(f"{label} must be a length-3 array")
    out: list[float] = []
    for index, item in enumerate(value):
        _reject_bool_and_string(item, f"{label}[{index}]", "number")
        number = _as_number(item)
        if not math.isfinite(number):
            raise ValueError(f"{label}[{index}] must be finite")
        out.append(number)
    return out


class RestAabb(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min: list[float] = Field(..., min_length=3, max_length=3)
    max: list[float] = Field(..., min_length=3, max_length=3)

    @field_validator("min", "max", mode="before")
    @classmethod
    def finite_corners(cls, value: Any, info: Any) -> Any:
        return _finite_coord_list(value, info.field_name)


class CapsuleDims(BaseModel):
    model_config = ConfigDict(extra="forbid")

    radius_m: float = Field(..., gt=0.0)
    height_m: float = Field(..., gt=0.0)

    @field_validator("radius_m", "height_m", mode="before")
    @classmethod
    def finite_dims(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "number")
        number = _as_number(value)
        if not math.isfinite(number):
            raise ValueError(f"{info.field_name} must be finite")
        return number


class CandidateRuntimeRequestBound(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["candidate-runtime-request-0.8.0"]
    asset_id: str = Field(..., min_length=1)
    workflow_id: str = Field(..., min_length=1)
    revision: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    execution_id: str = Field(..., min_length=1)
    strict_attempt_number: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    processed_glb_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    profile_document_hash: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    spec_fingerprint: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    runtime_contract_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    visual_mesh_name: Literal["SM_HumanoidSkin"]
    rest_aabb: RestAabb
    rest_aabb_canonical_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    capsule_center_m: list[float] = Field(..., min_length=3, max_length=3)
    capsule: CapsuleDims
    harness_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    nine_view_set: list[str] = Field(..., min_length=9, max_length=9)
    framing: FramingContract
    viewport: ViewportContract
    renderer_profile: Literal["gl_compatibility"]
    godot_version: str = Field(..., min_length=1)
    candidate_state: Literal["CLOSED"]
    public_status: Literal["UNSUPPORTED"]
    production_eligible: Literal[False]
    request_digest: str = Field(..., pattern=r"^[a-f0-9]{64}$")

    @field_validator("revision", "strict_attempt_number", mode="before")
    @classmethod
    def reject_bool_counters(cls, value: Any, info: Any) -> Any:
        return _strict_counter(value, info)

    @field_validator("capsule_center_m", mode="before")
    @classmethod
    def finite_center(cls, value: Any) -> Any:
        return _finite_coord_list(value, "capsule_center_m")

    @field_validator("production_eligible", mode="before")
    @classmethod
    def strict_false(cls, value: Any) -> Any:
        if value is not False:
            raise ValueError("production_eligible must be false")
        return value

    @model_validator(mode="after")
    def frozen_contract_fields(self) -> CandidateRuntimeRequestBound:
        contract = load_packaged_candidate_runtime_contract()
        if self.nine_view_set != contract.nine_view_set:
            raise ValueError("nine_view_set must match frozen runtime contract")
        if self.framing != contract.framing:
            raise ValueError("framing must match frozen runtime contract")
        if self.viewport != contract.viewport:
            raise ValueError("viewport must match frozen runtime contract")
        if self.renderer_profile != contract.renderer_profile:
            raise ValueError("renderer_profile must match frozen runtime contract")
        if self.candidate_state != contract.candidate_state:
            raise ValueError("candidate_state must match frozen runtime contract")
        if self.public_status != contract.public_status:
            raise ValueError("public_status must match frozen runtime contract")
        if self.production_eligible is not contract.production_eligible:
            raise ValueError("production_eligible must match frozen runtime contract")
        center = self.capsule_center_m
        mins, maxs = self.rest_aabb.min, self.rest_aabb.max
        for index in range(3):
            expected = (mins[index] + maxs[index]) / 2.0
            if center[index] != expected:
                raise ValueError("capsule_center_m must be the rest AABB midpoint")
        return self


class ObservedCapsule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    shape_class: Literal["CapsuleShape3D"]
    observed_radius_m: float = Field(..., gt=0.0)
    observed_height_m: float = Field(..., gt=0.0)
    center_m: list[float] = Field(..., min_length=3, max_length=3)

    @field_validator("observed_radius_m", "observed_height_m", mode="before")
    @classmethod
    def finite_observed_dims(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "number")
        number = _as_number(value)
        if not math.isfinite(number):
            raise ValueError(f"{info.field_name} must be finite")
        return number

    @field_validator("center_m", mode="before")
    @classmethod
    def finite_center(cls, value: Any) -> Any:
        return _finite_coord_list(value, "center_m")


class ProjectedRectPixels(BaseModel):
    model_config = ConfigDict(extra="forbid")

    x: float
    y: float
    width: float
    height: float

    @field_validator("x", "y", "width", "height", mode="before")
    @classmethod
    def finite_rect(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "number")
        number = _as_number(value)
        if not math.isfinite(number):
            raise ValueError(f"{info.field_name} must be finite")
        return number


class ViewFramingEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: bool
    reason: str
    height_ratio: float
    fill_ratio: float
    horizontally_centered: bool
    view_axis: str = Field(..., min_length=1)
    camera_distance: float = Field(..., gt=0.0)
    projected_rect_pixels: ProjectedRectPixels
    center_offset: float = Field(..., ge=0.0)

    @field_validator("ok", "horizontally_centered", mode="before")
    @classmethod
    def strict_bool(cls, value: Any, info: Any) -> Any:
        if type(value) is not bool:
            raise ValueError(f"{info.field_name} must be a boolean")
        return value

    @field_validator(
        "height_ratio", "fill_ratio", "camera_distance", "center_offset", mode="before"
    )
    @classmethod
    def finite_numbers(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "number")
        number = _as_number(value)
        if not math.isfinite(number):
            raise ValueError(f"{info.field_name} must be finite")
        return number


class CaptureEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    view: str = Field(..., min_length=1)
    png_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    png_width: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    png_height: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    execution_id: str = Field(..., min_length=1)
    revision: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    strict_attempt_number: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    request_digest: str = Field(..., pattern=r"^[a-f0-9]{64}$")

    @field_validator("revision", "strict_attempt_number", "png_width", "png_height", mode="before")
    @classmethod
    def strict_ints(cls, value: Any, info: Any) -> Any:
        return _strict_counter(value, info)


class CandidateRuntimeObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["candidate-runtime-observation-0.8.0"]
    workflow_id: str
    revision: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    execution_id: str
    strict_attempt_number: int = Field(..., ge=1, le=JSON_SAFE_INTEGER_MAX)
    asset_id: str
    processed_glb_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    observed_glb_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    request_digest: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    rest_aabb_canonical_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    godot_version: str
    harness_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    harness_sha256_raw: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    status: Literal["PASS", "FAIL"]
    candidate_state: Literal["CLOSED"]
    public_status: Literal["UNSUPPORTED"]
    production_eligible: Literal[False]
    visual_mesh_name: Literal["SM_HumanoidSkin"]
    runtime_body_kind: Literal["static_body"]
    collision_shape_class: Literal["CapsuleShape3D"]
    physics_ray_hit: bool
    rest_mesh_bounds: RestAabb
    capsule: ObservedCapsule
    view_framing: dict[str, ViewFramingEntry]
    captures: list[CaptureEntry]
    errors: list[str]

    @field_validator("revision", "strict_attempt_number", mode="before")
    @classmethod
    def reject_bool_counters(cls, value: Any, info: Any) -> Any:
        return _strict_counter(value, info)

    @model_validator(mode="after")
    def view_framing_has_exact_nine_views(self) -> CandidateRuntimeObservation:
        contract = load_packaged_candidate_runtime_contract()
        expected = set(contract.nine_view_set)
        actual = set(self.view_framing.keys())
        if actual != expected:
            raise ValueError("view_framing must contain exactly the nine contract views")
        return self

    @field_validator("physics_ray_hit", mode="before")
    @classmethod
    def strict_physics_bool(cls, value: Any) -> Any:
        if type(value) is not bool:
            raise ValueError("physics_ray_hit must be a boolean")
        return value

    @field_validator("production_eligible", mode="before")
    @classmethod
    def strict_false(cls, value: Any) -> Any:
        if value is not False:
            raise ValueError("production_eligible must be false")
        return value
