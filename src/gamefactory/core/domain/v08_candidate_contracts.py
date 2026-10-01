"""CLOSED rigged_character@1 candidate profile and specification contracts (V0.8-3A).

Separate from production asset-spec-0.7.0 / asset-profile-0.7.0. Loaded only
through explicit candidate parsers; public registry and parse_any stay unchanged.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.asset_contracts import (
    DimensionsConfig,
    GeometryBudgetConfig,
    MaterialBudgetConfig,
    OrientationConfig,
    SpecColliderConfig,
    StyleConstraintsConfig,
    TextureBudgetConfig,
    UniqueKeyLoader,
    _as_number,
    _load_spec_data,
    _reject_bool_and_string,
    _validate_asset_id_format,
    _validate_import_path,
)
from gamefactory.core.domain.asset_profiles import DimensionRules, ProfileContractError
from gamefactory.core.domain.errors import SpecInvalidError

PROFILE_SCHEMA_VERSION_V08_CANDIDATE = "asset-profile-0.8.0-candidate"
SPEC_SCHEMA_VERSION_V08_CANDIDATE = "0.8.0-candidate"
CANDIDATE_PROFILE_ID = "rigged_character"
CANDIDATE_PROFILE_VERSION = 1
CANDIDATE_STATE_CLOSED = "CLOSED"
CANDIDATE_PUBLIC_STATUS_UNSUPPORTED = "UNSUPPORTED"
CANDIDATE_VISUAL_MESH_NAME = "SM_HumanoidSkin"
CANDIDATE_ORIGIN_CONTRACT = "humanoid_reference_root"
CANDIDATE_RIG_CONTRACT_ID = "humanoid_12bone_v1"
VALIDATION_REPORT_SCHEMA_V08_CANDIDATE = "asset-validation-report-0.8.0-candidate"

CANDIDATE_RULE_GROUPS: tuple[str, ...] = (
    "core_v08_candidate",
    "rig_skin",
    "collider_capsule",
)


def _require_strict_bool(value: Any, field_name: str) -> bool:
    """Reject JSON-style 0/1 and other non-bool coercions for policy literals."""
    if type(value) is not bool:
        raise ValueError(f"{field_name} must be a boolean")
    return value


class CandidateContractError(SpecInvalidError):
    """A candidate profile, specification, or binding is invalid."""


class CandidateProcessingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lod0_required: Literal[True]
    lod1_required: Literal[False]
    allowed_lod_policies: list[Literal["lod0_only"]] = Field(..., min_length=1, max_length=1)
    dimension_tolerance_m: float = Field(..., gt=0.0, le=1.0)
    rig_forbidden: Literal[False]
    animation_forbidden: Literal[True]
    max_materials: int = Field(..., ge=1, le=8)
    max_texture_dimension: int = Field(..., ge=256, le=4096)
    max_triangles_lod0: int = Field(..., ge=100, le=100000)

    @field_validator(
        "lod0_required",
        "lod1_required",
        "rig_forbidden",
        "animation_forbidden",
        mode="before",
    )
    @classmethod
    def require_strict_bool_policy(cls, value: Any, info: Any) -> Any:
        return _require_strict_bool(value, info.field_name)

    @field_validator(
        "dimension_tolerance_m",
        "max_materials",
        "max_texture_dimension",
        "max_triangles_lod0",
        mode="before",
    )
    @classmethod
    def reject_bool_and_str_policy(cls, value: Any, info: Any) -> Any:
        if value is None:
            return value
        _reject_bool_and_string(value, info.field_name, "number")
        return value


class CandidateRuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bounds_tolerance_ratio: float = Field(..., ge=0.0, le=1.0)
    bounds_tolerance_floor_m: float = Field(..., ge=0.0, le=10.0)

    @field_validator("bounds_tolerance_ratio", "bounds_tolerance_floor_m", mode="before")
    @classmethod
    def reject_bool_and_str_runtime(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "number")
        if not math.isfinite(_as_number(value)):
            raise ValueError(f"{info.field_name} must be finite")
        return value


class ProfileDocumentV08Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["asset-profile-0.8.0-candidate"]
    profile_id: Literal["rigged_character"]
    version: Literal[1]
    candidate_state: Literal["CLOSED"]
    public_status: Literal["UNSUPPORTED"]
    categories: list[Literal["character"]] = Field(..., min_length=1, max_length=1)
    geometry_mode: Literal["single_mesh"]
    rigging: Literal["humanoid_skin"]
    rest_pose: Literal["T"]
    runtime_body: Literal["static_body"]
    animation_forbidden: Literal[True]
    collider_policy: Literal["capsule"]
    origin_contract: Literal["humanoid_reference_root"]
    rig_contract_id: Literal["humanoid_12bone_v1"]
    visual_mesh_name: Literal["SM_HumanoidSkin"]
    accepted_source_kinds: list[Literal["local_verified_rig"]] = Field(
        ..., min_length=1, max_length=1
    )
    dimension_rules: DimensionRules
    processing: CandidateProcessingPolicy
    runtime: CandidateRuntimePolicy

    @field_validator("animation_forbidden", mode="before")
    @classmethod
    def require_strict_bool_profile_animation(cls, value: Any) -> Any:
        return _require_strict_bool(value, "animation_forbidden")

    @field_validator("version", mode="before")
    @classmethod
    def reject_bool_version(cls, value: Any) -> Any:
        _reject_bool_and_string(value, "version", "integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("version must be an integer")
        return value


class AssetSpecificationV08Candidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.8.0-candidate"]
    asset_id: str = Field(..., min_length=2, max_length=80)
    category: Literal["character"]
    profile: Literal["rigged_character"]
    profile_version: Literal[1]
    intent: str = Field(..., min_length=1, max_length=500)
    source_kind: Literal["local_verified_rig"]
    dimensions: DimensionsConfig
    orientation: OrientationConfig = Field(default_factory=OrientationConfig)
    origin_contract: Literal["humanoid_reference_root"]
    rig_contract_id: Literal["humanoid_12bone_v1"]
    visual_mesh_name: Literal["SM_HumanoidSkin"]
    geometry_budget: GeometryBudgetConfig
    material_budget: MaterialBudgetConfig
    texture_budget: TextureBudgetConfig
    lod_policy: Literal["lod0_only"]
    style_constraints: StyleConstraintsConfig = Field(default_factory=StyleConstraintsConfig)
    target_engine: Literal["godot"]
    target_import_path: str = Field(default="")
    collider: SpecColliderConfig
    profile_document_hash: str = Field(..., pattern=r"^[a-f0-9]{64}$")
    processed_glb_sha256: str = Field(..., pattern=r"^[a-f0-9]{64}$")

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id_format(cls, value: str) -> str:
        return _validate_asset_id_format(value)

    @field_validator("target_import_path")
    @classmethod
    def validate_import_path(cls, value: str) -> str:
        return _validate_import_path(value)

    @field_validator("profile_version", mode="before")
    @classmethod
    def reject_bool_and_str_version(cls, value: Any) -> Any:
        _reject_bool_and_string(value, "profile_version", "integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError("profile_version must be an integer")
        return value

    @model_validator(mode="after")
    def default_import_path_if_unset(self) -> AssetSpecificationV08Candidate:
        if not self.target_import_path:
            self.target_import_path = f"assets/generated/character/{self.asset_id}/"
        return self

    @model_validator(mode="after")
    def validate_collider_and_contract(self) -> AssetSpecificationV08Candidate:
        if self.collider.policy != "capsule" or self.collider.capsule is None:
            raise ValueError("candidate specification requires collider.policy capsule")
        capsule = self.collider.capsule
        r, h = float(capsule.radius_m), float(capsule.height_m)
        if not (math.isfinite(r) and math.isfinite(h)):
            raise ValueError("capsule dimensions must be finite")
        if r < 0.10:
            raise ValueError("capsule radius_m must be >= 0.10")
        if h <= 2 * r:
            raise ValueError("capsule height_m must be > 2 * radius_m")
        if self.rig_contract_id != CANDIDATE_RIG_CONTRACT_ID:
            raise ValueError("rig_contract_id mismatch")
        if self.visual_mesh_name != CANDIDATE_VISUAL_MESH_NAME:
            raise ValueError("visual_mesh_name mismatch")
        if self.origin_contract != CANDIDATE_ORIGIN_CONTRACT:
            raise ValueError("origin_contract mismatch")
        return self


@dataclass(frozen=True)
class AssetProfileV08Candidate:
    document: ProfileDocumentV08Candidate

    @property
    def profile_id(self) -> str:
        return self.document.profile_id

    @property
    def version(self) -> int:
        return self.document.version

    @property
    def qualified(self) -> str:
        return f"{self.profile_id}@{self.version}"

    def check_specification(self, spec: AssetSpecificationV08Candidate) -> None:
        doc = self.document
        expected_hash = profile_document_hash(doc)
        if spec.profile_document_hash != expected_hash:
            raise ValueError(
                "profile_document_hash does not match the canonical CLOSED candidate profile"
            )
        if spec.origin_contract != doc.origin_contract:
            raise ValueError("origin_contract does not match the bound candidate profile")
        if spec.rig_contract_id != doc.rig_contract_id:
            raise ValueError("rig_contract_id does not match the bound candidate profile")
        if spec.visual_mesh_name != doc.visual_mesh_name:
            raise ValueError("visual_mesh_name does not match the bound candidate profile")
        if spec.collider.policy != doc.collider_policy:
            raise ValueError("collider policy does not match the bound candidate profile")
        if spec.source_kind not in doc.accepted_source_kinds:
            raise ValueError(f"source_kind {spec.source_kind!r} is not accepted")
        if spec.category not in doc.categories:
            raise ValueError(f"category {spec.category} is not supported by {self.qualified}")
        if spec.profile != self.profile_id or spec.profile_version != self.version:
            raise ValueError("specification profile binding mismatch")
        for axis_name, axis_value in (
            ("width_m", spec.dimensions.width_m),
            ("depth_m", spec.dimensions.depth_m),
            ("height_m", spec.dimensions.height_m),
        ):
            if not doc.dimension_rules.min_m <= axis_value <= doc.dimension_rules.max_m:
                raise ValueError(f"{axis_name} {axis_value} is outside profile dimension rules")
        if spec.geometry_budget.max_triangles_lod0 > doc.processing.max_triangles_lod0:
            raise ValueError("LOD0 triangle budget exceeds profile maximum")
        if spec.material_budget.max_materials > doc.processing.max_materials:
            raise ValueError("material budget exceeds profile maximum")
        if spec.texture_budget.max_dimension > doc.processing.max_texture_dimension:
            raise ValueError("texture budget exceeds profile maximum")
        if spec.lod_policy not in doc.processing.allowed_lod_policies:
            raise ValueError("lod_policy is not allowed by candidate profile")

    def processing_contract(self, spec: AssetSpecificationV08Candidate) -> dict[str, Any]:
        processing = self.document.processing
        return {
            "schema_version": "asset-processing-contract-0.8.0-candidate",
            "profile_id": self.profile_id,
            "profile_version": self.version,
            "profile_qualified": self.qualified,
            "asset_id": spec.asset_id,
            "geometry_mode": self.document.geometry_mode,
            "target_width_m": spec.dimensions.width_m,
            "target_depth_m": spec.dimensions.depth_m,
            "target_height_m": spec.dimensions.height_m,
            "origin_contract": spec.origin_contract,
            "lod_policy": spec.lod_policy,
            "lod1_required": False,
            "collider_policy": self.document.collider_policy,
            "dimension_tolerance_m": processing.dimension_tolerance_m,
            "rig_forbidden": processing.rig_forbidden,
            "animation_forbidden": processing.animation_forbidden,
            "visual_mesh_name": spec.visual_mesh_name,
            "rig_contract_id": spec.rig_contract_id,
        }


def _canonical_json_document(model: BaseModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return payload


def profile_document_hash(document: ProfileDocumentV08Candidate) -> str:
    return hashlib.sha256(_canonical_json_document(document).encode("utf-8")).hexdigest()


def candidate_spec_fingerprint(spec: AssetSpecificationV08Candidate) -> str:
    return hashlib.sha256(_canonical_json_document(spec).encode("utf-8")).hexdigest()


def parse_profile_document_v08_candidate(
    content: str | dict[str, Any],
) -> ProfileDocumentV08Candidate:
    if isinstance(content, str):
        try:
            parsed = yaml.load(content, Loader=UniqueKeyLoader)
        except SpecInvalidError:
            raise
        except Exception as exc:
            raise ProfileContractError(f"profile document is not valid YAML: {exc}") from exc
    else:
        parsed = content
    if not isinstance(parsed, dict):
        raise ProfileContractError("profile document must be a mapping")
    if parsed.get("schema_version") != PROFILE_SCHEMA_VERSION_V08_CANDIDATE:
        raise ProfileContractError(
            f"candidate profile requires schema_version '{PROFILE_SCHEMA_VERSION_V08_CANDIDATE}'"
        )
    try:
        return ProfileDocumentV08Candidate.model_validate(parsed)
    except Exception as exc:
        raise ProfileContractError(f"candidate profile document rejected: {exc}") from exc


def revalidate_candidate_profile(profile: AssetProfileV08Candidate) -> AssetProfileV08Candidate:
    """Re-parse the bound profile document (no hash bypass for invalid nested contents)."""
    try:
        document = ProfileDocumentV08Candidate.model_validate(
            profile.document.model_dump(mode="json")
        )
    except Exception as exc:
        raise CandidateContractError(
            f"candidate profile revalidation failed: {exc}", details={"error": str(exc)}
        ) from exc
    return AssetProfileV08Candidate(document=document)


def revalidate_candidate_binding(
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
) -> tuple[AssetSpecificationV08Candidate, AssetProfileV08Candidate]:
    """Strict validator-boundary re-parse and profile binding (no hash bypass)."""
    profile = revalidate_candidate_profile(profile)
    try:
        rebound = AssetSpecificationV08Candidate.model_validate(spec.model_dump(mode="json"))
    except Exception as exc:
        raise CandidateContractError(
            f"candidate specification revalidation failed: {exc}", details={"error": str(exc)}
        ) from exc
    try:
        profile.check_specification(rebound)
    except ValueError as exc:
        raise CandidateContractError(str(exc)) from exc
    return rebound, profile


def parse_asset_specification_v08_candidate(
    content: str | dict[str, Any] | Path,
    *,
    profile: AssetProfileV08Candidate | None = None,
) -> AssetSpecificationV08Candidate:
    data = _load_spec_data(content)
    if data.get("schema_version") != SPEC_SCHEMA_VERSION_V08_CANDIDATE:
        raise CandidateContractError(
            f"candidate specification requires schema_version '{SPEC_SCHEMA_VERSION_V08_CANDIDATE}', "
            f"got '{data.get('schema_version')}'"
        )
    try:
        spec = AssetSpecificationV08Candidate.model_validate(data)
    except Exception as exc:
        raise CandidateContractError(
            f"candidate specification validation failed: {exc}", details={"error": str(exc)}
        ) from exc
    bound_profile = profile or load_packaged_candidate_profile()
    rebound, _ = revalidate_candidate_binding(spec, bound_profile)
    return rebound


def load_packaged_candidate_profile() -> AssetProfileV08Candidate:
    text = (
        resources.files("gamefactory.resources.profiles")
        .joinpath("rigged_character_candidate.yml")
        .read_text(encoding="utf-8")
    )
    document = parse_profile_document_v08_candidate(text)
    return AssetProfileV08Candidate(document)


def load_packaged_candidate_specification() -> AssetSpecificationV08Candidate:
    text = (
        resources.files("gamefactory.resources.specs")
        .joinpath("humanoid_skin_candidate_01.yml")
        .read_text(encoding="utf-8")
    )
    return parse_asset_specification_v08_candidate(text)


def candidate_capabilities(
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
) -> dict[str, Any]:
    """Typed capabilities used to derive validation composition (never profile-id branches)."""
    doc = profile.document
    return {
        "geometry_mode": doc.geometry_mode,
        "rigging": doc.rigging,
        "collider_policy": doc.collider_policy,
        "source_kind": spec.source_kind,
        "rest_pose": doc.rest_pose,
        "runtime_body": doc.runtime_body,
        "animation_forbidden": doc.animation_forbidden,
    }


def candidate_composition_name(capabilities: dict[str, Any]) -> str:
    return (
        f"{capabilities['geometry_mode']}_{capabilities['rigging']}_"
        f"{capabilities['collider_policy']}"
    )


def is_candidate_schema_version(schema_version: Any) -> bool:
    return schema_version in {
        SPEC_SCHEMA_VERSION_V08_CANDIDATE,
        PROFILE_SCHEMA_VERSION_V08_CANDIDATE,
    }
