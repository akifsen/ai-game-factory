"""Strict, versioned domain contracts and schemas for production 3D assets.

Defines:
- AssetSpecification: strict Pydantic model with extra='forbid'
- AssetRevision, AssetValidationResult, ValidationFinding, CostRecord
- Helpers for canonical hashing, loading YAML/JSON with duplicate-key rejection.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.errors import SpecInvalidError, ValidationError
from gamefactory.core.domain.models import utc_now_iso
from gamefactory.core.execution.path_guard import PathGuard

# Pattern for safe asset identifiers: lowercase alphanumeric, underscore, hyphen
_ASSET_ID_REGEX = re.compile(r"^[a-z0-9][a-z0-9_-]{1,79}$")


class UniqueKeyLoader(yaml.SafeLoader):
    """YAML SafeLoader that strictly rejects duplicate keys in any mapping."""


def _construct_unique_mapping(loader: yaml.SafeLoader, node: yaml.MappingNode) -> dict[Any, Any]:
    loader.flatten_mapping(node)
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=False)
        if key in mapping:
            line = key_node.start_mark.line + 1
            col = key_node.start_mark.column + 1
            raise SpecInvalidError(
                f"Duplicate YAML key '{key}' detected at line {line}, column {col}"
            )
        mapping[key] = loader.construct_object(value_node, deep=False)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_unique_mapping
)


def _reject_bool_and_string(value: Any, field_name: str, expected_type: str = "number") -> Any:
    """Helper to strictly reject bool and string coercion for numeric fields."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be a {expected_type}, got boolean")
    if isinstance(value, str):
        raise ValueError(f"{field_name} must be a {expected_type}, got string")
    return value


class DimensionsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    width_m: float = Field(
        ..., ge=0.001, le=100.0, allow_inf_nan=False, description="Width in meters (X axis)"
    )
    depth_m: float = Field(
        ..., ge=0.001, le=100.0, allow_inf_nan=False, description="Depth in meters (Z axis)"
    )
    height_m: float = Field(
        ..., ge=0.001, le=100.0, allow_inf_nan=False, description="Height in meters (Y axis)"
    )

    @field_validator("width_m", "depth_m", "height_m", mode="before")
    @classmethod
    def reject_bool_and_str(cls, value: Any, info: Any) -> Any:
        return _reject_bool_and_string(value, info.field_name, "number")

    @field_validator("width_m", "depth_m", "height_m")
    @classmethod
    def validate_finite_positive(cls, value: float, info: Any) -> float:
        if math.isnan(value) or math.isinf(value):
            raise ValueError(f"{info.field_name} must be a finite number; got {value}")
        if value <= 0.0:
            raise ValueError(f"{info.field_name} must be strictly positive; got {value}")
        return value


class OrientationConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # For the static_prop profile, only +Y up and -Z front are supported
    up: Literal["+Y"] = "+Y"
    front: Literal["-Z"] = "-Z"

    @model_validator(mode="after")
    def validate_orientation(self) -> OrientationConfig:
        if self.up != "+Y" or self.front != "-Z":
            raise ValueError(
                f"Unsupported orientation: up={self.up}, front={self.front}. "
                "Only up='+Y' and front='-Z' are supported."
            )
        return self


class GeometryBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_triangles_lod0: int = Field(default=20000, ge=100, le=100000)
    max_triangles_lod1: int = Field(default=10000, ge=50, le=50000)
    lod_ratio: float = Field(
        default=0.5,
        ge=0.05,
        le=0.95,
        description="LOD1 triangles to LOD0 ratio supported by the Blender processor (0.05-0.95)",
    )

    @field_validator("max_triangles_lod0", "max_triangles_lod1", mode="before")
    @classmethod
    def reject_bool_and_str_int(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(
                f"{info.field_name} must be an integer, got non-integral float {value}"
            )
        return value

    @field_validator("lod_ratio", mode="before")
    @classmethod
    def reject_bool_and_str_ratio(cls, value: Any, info: Any) -> Any:
        return _reject_bool_and_string(value, info.field_name, "number")

    @model_validator(mode="after")
    def validate_lod_order(self) -> GeometryBudgetConfig:
        if self.max_triangles_lod1 >= self.max_triangles_lod0:
            raise ValueError(
                f"max_triangles_lod1 ({self.max_triangles_lod1}) must be less than "
                f"max_triangles_lod0 ({self.max_triangles_lod0})"
            )
        return self


class MaterialBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_materials: int = Field(default=2, ge=1, le=8)

    @field_validator("max_materials", mode="before")
    @classmethod
    def reject_bool_and_str_materials(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(
                f"{info.field_name} must be an integer, got non-integral float {value}"
            )
        return value


class TextureBudgetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_dimension: int = Field(default=2048, ge=256, le=4096)

    @field_validator("max_dimension", mode="before")
    @classmethod
    def reject_bool_and_str_texture(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "integer")
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(
                f"{info.field_name} must be an integer, got non-integral float {value}"
            )
        return value

    @field_validator("max_dimension")
    @classmethod
    def validate_power_of_two_or_reasonable(cls, value: int) -> int:
        allowed = (256, 512, 1024, 2048, 4096)
        if value not in allowed:
            raise ValueError(f"max_dimension must be one of {allowed}, got {value}")
        return value


class StyleConstraintsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    family: str = Field(default="stylized_scifi", min_length=1, max_length=100)
    silhouette: str = Field(default="chunky", min_length=1, max_length=100)
    readability: str = Field(default="high", min_length=1, max_length=100)
    detail_density: str = Field(default="medium", min_length=1, max_length=100)


class AssetSpecification(BaseModel):
    """Strict specification for one asset revision.

    schema 0.4.0 is the historical static_prop document. Its fingerprint omits
    profile_version; readers bind those documents to static_prop@1.
    schema 0.5.0 records profile_version explicitly. Profile behavior lives on
    the profile registry, not in this value object.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["0.4.0", "0.5.0"] = "0.4.0"
    asset_id: str = Field(..., min_length=2, max_length=80)
    category: Literal["prop", "pickup", "modular"] = "prop"
    profile: Literal["static_prop", "pickup", "modular_piece"] = "static_prop"
    profile_version: int | None = None
    intent: str = Field(..., min_length=1, max_length=500)
    dimensions: DimensionsConfig
    orientation: OrientationConfig = Field(default_factory=OrientationConfig)
    origin_policy: Literal["bottom_center", "center"] = "bottom_center"
    geometry_budget: GeometryBudgetConfig = Field(default_factory=GeometryBudgetConfig)
    material_budget: MaterialBudgetConfig = Field(default_factory=MaterialBudgetConfig)
    texture_budget: TextureBudgetConfig = Field(default_factory=TextureBudgetConfig)
    collider_policy: Literal["box"] = "box"
    lod_policy: Literal["lod0_lod1", "lod0_only"] = "lod0_lod1"
    style_constraints: StyleConstraintsConfig = Field(default_factory=StyleConstraintsConfig)
    target_engine: Literal["godot"] = "godot"
    target_import_path: str = Field(default="")

    @field_validator("asset_id")
    @classmethod
    def validate_asset_id_format(cls, value: str) -> str:
        if isinstance(value, bool) or not isinstance(value, str):
            raise ValueError(f"asset_id must be a string, got {type(value).__name__}")
        if not _ASSET_ID_REGEX.fullmatch(value):
            raise ValueError(
                f"asset_id '{value}' is invalid: must match ^[a-z0-9][a-z0-9_-]{{1,79}}$"
            )
        # Use PathGuard lexical checks to catch Windows device names (con, aux, nul, etc.)
        guard = PathGuard(Path.cwd().resolve())
        try:
            guard._validate_raw(value)
        except ValidationError as err:
            raise ValueError(f"asset_id '{value}' violates path safety: {err}") from err
        return value

    @field_validator("target_import_path")
    @classmethod
    def validate_import_path(cls, value: str, info: Any) -> str:
        if not value:
            return value
        if value != value.strip():
            raise ValueError("target_import_path cannot have leading or trailing whitespace")
        # Check the exact supplied spelling before normalizing it. In particular,
        # stripping first would hide Windows-invalid trailing spaces.
        raw_normalized = value.replace("\\", "/")
        # Enforce PathGuard lexical protections against ADS, Windows devices, null bytes, UNC
        guard = PathGuard(Path.cwd().resolve())
        try:
            guard._validate_raw(raw_normalized)
        except ValidationError as err:
            raise ValueError(f"target_import_path violates path safety: {err}") from err

        cleaned = raw_normalized

        if cleaned.startswith("/"):
            raise ValueError(f"target_import_path must be relative, got absolute: '{value}'")
        if re.match(r"^[a-zA-Z]:", cleaned):
            raise ValueError(f"target_import_path must be relative, got drive letter: '{value}'")
        parts = cleaned.split("/")
        if any(p == ".." for p in parts):
            raise ValueError(f"target_import_path contains path traversal: '{value}'")
        return cleaned

    @field_validator("profile_version", mode="before")
    @classmethod
    def reject_bool_profile_version(cls, value: Any) -> Any:
        if value is None:
            return value
        return _reject_bool_and_string(value, "profile_version", "integer")

    @model_validator(mode="after")
    def bind_profile_contract(self) -> AssetSpecification:
        from gamefactory.core.domain.asset_profiles import builtin_registry

        if self.schema_version == "0.4.0":
            if self.profile != "static_prop" or self.category != "prop":
                raise ValueError(
                    "asset-spec-0.4.0 only accepts category prop and profile static_prop"
                )
            if self.profile_version is not None:
                raise ValueError("asset-spec-0.4.0 does not carry profile_version")
            if self.collider_policy != "box" or self.lod_policy != "lod0_lod1":
                raise ValueError("asset-spec-0.4.0 only accepts a box collider and lod0_lod1")
            version = 1
        else:
            if self.profile_version is None:
                raise ValueError("asset-spec-0.5.0 requires profile_version")
            version = self.profile_version
        try:
            profile = builtin_registry().get(self.profile, version)
        except SpecInvalidError as exc:
            raise ValueError(str(exc)) from exc
        profile.check_specification(self)
        return self

    @model_validator(mode="after")
    def default_import_path_if_unset(self) -> AssetSpecification:
        if not self.target_import_path:
            folder = "props" if self.category == "prop" else self.category
            self.target_import_path = f"assets/generated/{folder}/{self.asset_id}/"
        return self

    def bound_profile(self) -> Any:
        """Return the immutable profile this specification is bound to."""
        from gamefactory.core.domain.asset_profiles import builtin_registry

        version = 1 if self.schema_version == "0.4.0" else int(self.profile_version or 0)
        return builtin_registry().get(self.profile, version)

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Keep 0.4.0 fingerprints identical by omitting the unbound version field."""
        payload = super().model_dump(*args, **kwargs)
        if payload.get("schema_version") == "0.4.0":
            payload.pop("profile_version", None)
        return payload


def parse_asset_specification(content: str | dict[str, Any] | Path) -> AssetSpecification:
    """Parse and strictly validate an AssetSpecification from YAML, JSON, dict, or file."""
    data: dict[str, Any]
    if isinstance(content, Path):
        raw = content.read_text(encoding="utf-8")
        try:
            parsed = yaml.load(raw, Loader=UniqueKeyLoader)
        except SpecInvalidError:
            raise
        except Exception as exc:
            raise SpecInvalidError(f"Failed to parse specification file {content}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SpecInvalidError(f"Specification file {content} must contain a mapping/object")
        data = parsed
    elif isinstance(content, str):
        try:
            parsed = yaml.load(content, Loader=UniqueKeyLoader)
        except SpecInvalidError:
            raise
        except Exception as exc:
            raise SpecInvalidError(f"Failed to parse specification string: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SpecInvalidError("Specification content must be a mapping/object")
        data = parsed
    elif isinstance(content, dict):
        data = content
    else:
        raise SpecInvalidError(f"Unsupported specification input type: {type(content)}")

    try:
        return AssetSpecification.model_validate(data)
    except Exception as exc:
        raise SpecInvalidError(
            f"Asset specification validation failed: {exc}", details={"error": str(exc)}
        ) from exc


def spec_fingerprint(spec: AssetSpecification) -> str:
    """Compute deterministic SHA-256 fingerprint for a normalized asset specification."""
    normalized = spec.model_dump(mode="json")
    payload = json.dumps(normalized, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass
class AssetRevision:
    """A monotonic, immutable revision tracking one end-to-end iteration of an asset.

    Workflow lifecycle authority resides in workflow tasks; AssetRevision records the
    workflow_id and immutable spec/artifact relationships.
    """

    asset_id: str
    revision_number: int
    workflow_id: str
    spec_hash: str
    profile_id: str | None = None
    profile_version: int | None = None
    concept_hash: str | None = None
    raw_glb_hash: str | None = None
    processed_glb_hash: str | None = None
    validation_report_hash: str | None = None
    runtime_evidence_hashes: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    @property
    def revision_id(self) -> str:
        return f"r{self.revision_number:03d}"

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["revision_id"] = self.revision_id
        return d


class ValidationFindingSeverity(StrEnum):
    PASS = "PASS"
    WARNING = "WARNING"
    FAIL = "FAIL"


@dataclass
class ValidationFinding:
    rule_id: str
    severity: ValidationFindingSeverity
    expected: str
    actual: str
    artifact: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d


@dataclass
class AssetValidationResult:
    status: ValidationFindingSeverity
    findings: list[ValidationFinding]
    summary: str
    evaluated_at: str = field(default_factory=utc_now_iso)

    @property
    def passed(self) -> bool:
        return self.status != ValidationFindingSeverity.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "passed": self.passed,
            "summary": self.summary,
            "evaluated_at": self.evaluated_at,
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass
class CostRecord:
    provider: str
    operation: str
    estimated_cost: float | None = None
    actual_cost: float | None = None
    cost_unit: str = "credits"
    external_task_id: str | None = None
    timestamp: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
