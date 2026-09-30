"""Built-in asset profiles.

A profile describes how a class of asset is processed. An AssetSpecification
holds the values for one asset. Profiles are explicit registry entries loaded
from strict YAML; they are not plugins and they do not execute caller code.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.asset_contracts import _as_number, _reject_bool_and_string
from gamefactory.core.domain.camera_framing import PLACED_VIEWS
from gamefactory.core.domain.errors import SpecInvalidError

if TYPE_CHECKING:
    from gamefactory.core.domain.asset_contracts import AssetSpecificationV07

PROFILE_SCHEMA_VERSION = "asset-profile-0.5.0"
PROFILE_SCHEMA_VERSION_V07 = "asset-profile-0.7.0"
PROCESSING_CONTRACT_VERSION = "asset-processing-contract-0.5.0"
PROCESSING_CONTRACT_VERSION_V07 = "asset-processing-contract-0.7.0"
CANONICAL_FRAME = {"up": "+Y", "front": "-Z", "handedness": "right", "units": "m"}

UNSUPPORTED_PROFILE_IDS = (
    "character",
    "rigged_character",
    "weapon",
    "vehicle",
    "building",
    "terrain",
    "animation",
    "vfx",
    "foliage",
)


class ProfileContractError(SpecInvalidError):
    """A profile document or profile/spec binding is invalid."""


class FramingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_screen_fraction: float = Field(..., gt=0.0, lt=1.0)
    max_screen_fraction: float = Field(..., gt=0.0, lt=1.0)
    target_screen_fraction: float = Field(..., gt=0.0, lt=1.0)
    margin_fraction: float = Field(..., ge=0.0, le=0.2)
    fov_degrees: float = Field(..., ge=1.0, le=120.0)

    @model_validator(mode="after")
    def ranges_are_ordered(self) -> FramingPolicy:
        if not self.min_screen_fraction <= self.target_screen_fraction <= self.max_screen_fraction:
            raise ValueError(
                "framing target must lie between the minimum and maximum screen fractions"
            )
        return self


class DimensionRules(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_m: float = Field(..., gt=0.0, le=100.0)
    max_m: float = Field(..., gt=0.0, le=100.0)

    @model_validator(mode="after")
    def ordered(self) -> DimensionRules:
        if self.min_m >= self.max_m:
            raise ValueError("dimension minimum must be below the maximum")
        return self


class ProcessingPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lod0_required: bool
    lod1_required: bool
    allowed_lod_policies: list[str] = Field(..., min_length=1)
    allowed_collider_policies: list[str] = Field(..., min_length=1)
    allowed_origin_policies: list[str] = Field(..., min_length=1)
    default_origin_policy: str
    dimension_tolerance_m: float = Field(..., gt=0.0, le=1.0)
    snap_grid_m: float | None = None
    rig_forbidden: bool
    animation_forbidden: bool
    max_materials: int = Field(..., ge=1, le=8)
    max_texture_dimension: int = Field(..., ge=256, le=4096)
    max_triangles_lod0: int = Field(..., ge=100, le=100000)

    @field_validator("snap_grid_m")
    @classmethod
    def snap_is_positive(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("snap_grid_m must be positive when set")
        return value

    @model_validator(mode="after")
    def policies_are_known(self) -> ProcessingPolicy:
        lod = {"lod0_only", "lod0_lod1"}
        colliders = {"box"}
        origins = {"bottom_center", "center"}
        if any(item not in lod for item in self.allowed_lod_policies):
            raise ValueError("allowed_lod_policies contains an unsupported value")
        if any(item not in colliders for item in self.allowed_collider_policies):
            raise ValueError("allowed_collider_policies contains an unsupported value")
        if any(item not in origins for item in self.allowed_origin_policies):
            raise ValueError("allowed_origin_policies contains an unsupported value")
        if self.default_origin_policy not in self.allowed_origin_policies:
            raise ValueError("default origin is not in the allowed origin policies")
        if self.lod1_required and "lod0_lod1" not in self.allowed_lod_policies:
            raise ValueError("a required LOD1 needs the lod0_lod1 policy")
        if self.lod1_required and "lod0_only" in self.allowed_lod_policies:
            raise ValueError("lod0_only cannot be allowed when LOD1 is required")
        if not self.lod0_required:
            raise ValueError("LOD0 is required for every V0.5 profile")
        return self


class GodotPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body_kind: str
    require_ray_hit: bool
    require_area: bool

    @model_validator(mode="after")
    def body_matches_runtime(self) -> GodotPolicy:
        if self.body_kind not in {"static_body", "area"}:
            raise ValueError("body_kind must be static_body or area")
        if self.body_kind == "static_body" and self.require_area:
            raise ValueError("static_body profiles do not require an Area3D")
        if self.body_kind == "area" and self.require_ray_hit:
            raise ValueError("area profiles do not use a physics ray hit")
        if self.body_kind == "area" and not self.require_area:
            raise ValueError("area profiles require an Area3D")
        return self


class RuntimePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bounds_tolerance_ratio: float = Field(..., ge=0.0, le=1.0)
    bounds_tolerance_floor_m: float = Field(..., ge=0.0, le=10.0)


class ProfileDocument(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: str
    profile_id: str = Field(..., min_length=2, max_length=40)
    version: int = Field(..., ge=1, le=1000)
    categories: list[str] = Field(..., min_length=1)
    review_views: list[str] = Field(..., min_length=1)
    framing: FramingPolicy
    dimension_rules: DimensionRules
    processing: ProcessingPolicy
    godot: GodotPolicy
    runtime: RuntimePolicy

    @field_validator("schema_version")
    @classmethod
    def schema_is_current(cls, value: str) -> str:
        if value != PROFILE_SCHEMA_VERSION:
            raise ValueError(f"unsupported profile schema {value}")
        return value

    @field_validator("profile_id")
    @classmethod
    def identifier_is_safe(cls, value: str) -> str:
        if not value.replace("_", "").isalnum() or not value[0].isalpha() or value != value.lower():
            raise ValueError("profile_id must be a lowercase identifier")
        return value

    @field_validator("review_views")
    @classmethod
    def views_are_implemented(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("review_views contains a duplicate")
        unknown = [item for item in value if item not in PLACED_VIEWS]
        if unknown:
            raise ValueError(f"review view is not implemented: {unknown[0]}")
        return value

    @field_validator("categories")
    @classmethod
    def categories_are_safe(cls, value: list[str]) -> list[str]:
        for item in value:
            if item not in {"prop", "pickup", "modular"}:
                raise ValueError(f"unsupported asset category {item}")
        return value


def parse_profile_document(content: str | dict[str, Any]) -> ProfileDocument:
    """Validate one profile document. Unknown fields and unsafe values are rejected."""
    if isinstance(content, str):
        from gamefactory.core.domain.asset_contracts import UniqueKeyLoader

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
    try:
        return ProfileDocument.model_validate(parsed)
    except Exception as exc:
        raise ProfileContractError(f"profile document rejected: {exc}") from exc


@dataclass(frozen=True)
class AssetProfile:
    document: ProfileDocument

    @property
    def profile_id(self) -> str:
        return self.document.profile_id

    @property
    def version(self) -> int:
        return self.document.version

    @property
    def schema_version(self) -> str:
        return self.document.schema_version

    @property
    def qualified(self) -> str:
        return f"{self.profile_id}@{self.version}"

    @property
    def review_views(self) -> tuple[str, ...]:
        return tuple(self.document.review_views)

    @property
    def framing(self) -> FramingPolicy:
        return self.document.framing

    def check_specification(self, spec: Any) -> None:
        """Reject specification choices this profile does not implement."""
        document = self.document
        if spec.category not in document.categories:
            raise ValueError(f"category {spec.category} is not supported by {self.qualified}")
        if spec.profile != self.profile_id:
            raise ValueError("specification profile does not match the bound profile")
        if spec.schema_version == "0.5.0" and spec.profile_version != self.version:
            raise ValueError(
                f"specification profile_version {spec.profile_version} does not match {self.qualified}"
            )
        processing = document.processing
        if spec.lod_policy not in processing.allowed_lod_policies:
            raise ValueError(f"lod_policy {spec.lod_policy} is not allowed by {self.qualified}")
        if spec.collider_policy not in processing.allowed_collider_policies:
            raise ValueError(
                f"collider_policy {spec.collider_policy} is not allowed by {self.qualified}"
            )
        if spec.origin_policy not in processing.allowed_origin_policies:
            raise ValueError(
                f"origin_policy {spec.origin_policy} is not allowed by {self.qualified}"
            )
        for axis_name, axis_value in (
            ("width_m", spec.dimensions.width_m),
            ("depth_m", spec.dimensions.depth_m),
            ("height_m", spec.dimensions.height_m),
        ):
            if not document.dimension_rules.min_m <= axis_value <= document.dimension_rules.max_m:
                raise ValueError(
                    f"{axis_name} {axis_value} is outside {self.qualified} "
                    f"{document.dimension_rules.min_m}-{document.dimension_rules.max_m} m"
                )
            if processing.snap_grid_m is not None and not _on_snap_grid(
                axis_value, processing.snap_grid_m, processing.dimension_tolerance_m
            ):
                raise ValueError(
                    f"{axis_name} {axis_value} is not a multiple of snap grid {processing.snap_grid_m}"
                )
        if spec.material_budget.max_materials > processing.max_materials:
            raise ValueError(f"material budget exceeds {self.qualified}")
        if spec.texture_budget.max_dimension > processing.max_texture_dimension:
            raise ValueError(f"texture budget exceeds {self.qualified}")
        if spec.geometry_budget.max_triangles_lod0 > processing.max_triangles_lod0:
            raise ValueError(f"LOD0 triangle budget exceeds {self.qualified}")

    def lod1_mesh_required(self, spec: Any) -> bool:
        return bool(self.document.processing.lod1_required or spec.lod_policy == "lod0_lod1")

    def expected_mesh_names(self, spec: Any) -> set[str]:
        names = {f"SM_{spec.asset_id}_LOD0", f"COL_{spec.asset_id}"}
        if self.lod1_mesh_required(spec):
            names.add(f"SM_{spec.asset_id}_LOD1")
        return names

    def processing_contract(self, spec: Any) -> dict[str, Any]:
        processing = self.document.processing
        return {
            "schema_version": PROCESSING_CONTRACT_VERSION,
            "profile_id": self.profile_id,
            "profile_version": self.version,
            "profile_qualified": self.qualified,
            "asset_id": spec.asset_id,
            "target_width_m": spec.dimensions.width_m,
            "target_depth_m": spec.dimensions.depth_m,
            "target_height_m": spec.dimensions.height_m,
            "origin_policy": spec.origin_policy,
            "lod_policy": spec.lod_policy,
            "lod1_required": self.lod1_mesh_required(spec),
            "lod1_ratio": spec.geometry_budget.lod_ratio,
            "collider_policy": spec.collider_policy,
            "dimension_tolerance_m": processing.dimension_tolerance_m,
            "snap_grid_m": processing.snap_grid_m,
            "rig_forbidden": processing.rig_forbidden,
            "animation_forbidden": processing.animation_forbidden,
        }

    def scene_contract(self) -> dict[str, Any]:
        body = "StaticBody3D" if self.document.godot.body_kind == "static_body" else "Area3D"
        return {
            "root": "Node3D",
            "visual": "Visual",
            "physics": body,
            "collision": "CollisionShape3D",
            "body_kind": self.document.godot.body_kind,
        }

    def runtime_requirements(self) -> dict[str, Any]:
        godot = self.document.godot
        runtime = self.document.runtime
        return {
            "require_mesh_visible": True,
            "require_collision": True,
            "require_physics_body": godot.body_kind == "static_body",
            "require_area": godot.require_area,
            "require_ray_hit": godot.require_ray_hit,
            "bounds_tolerance_ratio": runtime.bounds_tolerance_ratio,
            "bounds_tolerance_floor_m": runtime.bounds_tolerance_floor_m,
            "dimension_tolerance_m": self.document.processing.dimension_tolerance_m,
        }

    def capture_request_profile(self) -> dict[str, Any]:
        framing = self.framing
        return {
            "profile_id": self.profile_id,
            "profile_version": self.version,
            "body_kind": self.document.godot.body_kind,
            "require_ray_hit": self.document.godot.require_ray_hit,
            "require_area": self.document.godot.require_area,
            "review_views": list(self.review_views),
            "framing": {
                "min_screen_fraction": framing.min_screen_fraction,
                "max_screen_fraction": framing.max_screen_fraction,
                "target_screen_fraction": framing.target_screen_fraction,
                "margin_fraction": framing.margin_fraction,
                "fov_degrees": framing.fov_degrees,
            },
            "scene": self.scene_contract(),
        }


def _on_snap_grid(value: float, grid: float, tolerance: float) -> bool:
    quotient = value / grid
    return abs(quotient - round(quotient)) * grid <= tolerance


CATEGORY_V07_LITERAL = Literal[
    "prop",
    "pickup",
    "modular",
    "vehicle",
    "weapon",
    "aircraft",
    "character",
]


class ProcessingPolicyV07(BaseModel):
    model_config = ConfigDict(extra="forbid")

    lod0_required: bool
    lod1_required: bool
    allowed_lod_policies: list[str] = Field(..., min_length=1)
    allowed_collider_policies: list[Literal["box", "capsule"]] = Field(..., min_length=1)
    allowed_origin_policies: list[str] = Field(..., min_length=1)
    default_origin_policy: str
    dimension_tolerance_m: float = Field(..., gt=0.0, le=1.0)
    snap_grid_m: float | None = Field(default=None, allow_inf_nan=False)
    rig_forbidden: bool
    animation_forbidden: bool
    max_materials: int = Field(..., ge=1, le=8)
    max_texture_dimension: int = Field(..., ge=256, le=4096)
    max_triangles_lod0: int = Field(..., ge=100, le=100000)

    @field_validator(
        "dimension_tolerance_m",
        "snap_grid_m",
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

    @field_validator("snap_grid_m")
    @classmethod
    def snap_is_positive(cls, value: float | None) -> float | None:
        if value is not None and value <= 0:
            raise ValueError("snap_grid_m must be positive when set")
        return value

    @model_validator(mode="after")
    def policies_are_known(self) -> ProcessingPolicyV07:
        lod = {"lod0_only", "lod0_lod1"}
        origins = {"bottom_center", "center"}
        if len(self.allowed_lod_policies) != len(set(self.allowed_lod_policies)):
            raise ValueError("allowed_lod_policies contains duplicate values")
        if len(self.allowed_collider_policies) != len(set(self.allowed_collider_policies)):
            raise ValueError("allowed_collider_policies contains duplicate values")
        if len(self.allowed_origin_policies) != len(set(self.allowed_origin_policies)):
            raise ValueError("allowed_origin_policies contains duplicate values")
        if any(item not in lod for item in self.allowed_lod_policies):
            raise ValueError("allowed_lod_policies contains an unsupported value")
        if any(item not in origins for item in self.allowed_origin_policies):
            raise ValueError("allowed_origin_policies contains an unsupported value")
        if self.default_origin_policy not in self.allowed_origin_policies:
            raise ValueError("default origin is not in the allowed origin policies")
        if self.lod1_required and "lod0_lod1" not in self.allowed_lod_policies:
            raise ValueError("a required LOD1 needs the lod0_lod1 policy")
        if self.lod1_required and "lod0_only" in self.allowed_lod_policies:
            raise ValueError("lod0_only cannot be allowed when LOD1 is required")
        if not self.lod0_required:
            raise ValueError("LOD0 is required for every V0.7 profile")
        return self


class RoleMotionConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fixed", "revolute", "prismatic"]
    axis: tuple[float, float, float] | list[float] | None = None

    @field_validator("axis", mode="before")
    @classmethod
    def validate_axis_type(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("axis must be a 3-element list or tuple")
        for item in value:
            _reject_bool_and_string(item, "axis component", "number")
            if not math.isfinite(_as_number(item)):
                raise ValueError("axis component must be finite")
        return [float(x) for x in value]

    @model_validator(mode="after")
    def validate_kind_and_axis(self) -> RoleMotionConstraint:
        if self.kind == "fixed":
            if self.axis is not None:
                raise ValueError("axis forbidden for fixed motion constraint")
        else:
            if self.axis is None:
                raise ValueError(f"axis required for {self.kind} motion constraint")
            norm = math.sqrt(sum(float(x) ** 2 for x in self.axis))
            if abs(norm - 1.0) > 1e-6:
                raise ValueError(f"axis must be a unit vector within 1e-6, norm is {norm}")
        return self


class RequiredSocketContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    socket_id: str
    parent_role: str
    placement: Literal["forward_end"]
    forward_end_fraction: float = Field(..., gt=0.0, le=1.0)
    rest_forward: tuple[float, float, float] | list[float] | None = None

    @field_validator("socket_id", "parent_role")
    @classmethod
    def validate_identifier(cls, value: str, info: Any) -> str:
        if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", value):
            raise ValueError(f"{info.field_name} must be a lowercase identifier, got '{value}'")
        return value

    @field_validator("forward_end_fraction", mode="before")
    @classmethod
    def reject_bool_and_str_fraction(cls, value: Any) -> Any:
        _reject_bool_and_string(value, "forward_end_fraction", "number")
        if not math.isfinite(_as_number(value)):
            raise ValueError("forward_end_fraction must be finite")
        return value

    @field_validator("rest_forward", mode="before")
    @classmethod
    def validate_rest_forward(cls, value: Any) -> Any:
        if value is None:
            return value
        if not isinstance(value, (list, tuple)) or len(value) != 3:
            raise ValueError("rest_forward must be a 3-element list or tuple")
        for item in value:
            _reject_bool_and_string(item, "rest_forward component", "number")
            if not math.isfinite(_as_number(item)):
                raise ValueError("rest_forward component must be finite")
        norm = math.sqrt(sum(float(x) ** 2 for x in value))
        if abs(norm - 1.0) > 1e-6:
            raise ValueError(f"rest_forward must be a unit vector within 1e-6, norm is {norm}")
        return [float(x) for x in value]


class AssemblyContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[str] = Field(..., min_length=1)
    required_roles: list[str] = Field(..., min_length=1)
    role_motion_constraints: dict[str, RoleMotionConstraint] = Field(default_factory=dict)
    required_sockets: list[RequiredSocketContract] = Field(default_factory=list)
    pivot_tolerance_m: float = Field(..., gt=0.0, le=1.0)
    basis_tolerance_deg: float = Field(..., gt=0.0, le=90.0)
    socket_position_tolerance_m: float = Field(..., gt=0.0, le=1.0)
    socket_angle_tolerance_deg: float = Field(..., gt=0.0, le=90.0)

    @field_validator(
        "pivot_tolerance_m",
        "basis_tolerance_deg",
        "socket_position_tolerance_m",
        "socket_angle_tolerance_deg",
        mode="before",
    )
    @classmethod
    def reject_bool_and_str_tolerances(cls, value: Any, info: Any) -> Any:
        _reject_bool_and_string(value, info.field_name, "number")
        if not math.isfinite(_as_number(value)):
            raise ValueError(f"{info.field_name} must be finite")
        return value

    @field_validator("roles", "required_roles")
    @classmethod
    def validate_roles_identifiers(cls, value: list[str], info: Any) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError(f"{info.field_name} contains duplicate roles")
        for item in value:
            if not re.fullmatch(r"^[a-z][a-z0-9_]{0,63}$", item):
                raise ValueError(
                    f"{info.field_name} items must be lowercase identifiers, got '{item}'"
                )
        return value

    @model_validator(mode="after")
    def validate_assembly_contract(self) -> AssemblyContract:
        roles_set = set(self.roles)
        for req in self.required_roles:
            if req not in roles_set:
                raise ValueError(f"required_role '{req}' is not in defined roles vocabulary")

        for r in self.role_motion_constraints:
            if r not in roles_set:
                raise ValueError(
                    f"role_motion_constraint key '{r}' is not in defined roles vocabulary"
                )

        seen_sockets: set[str] = set()
        for sock in self.required_sockets:
            if sock.socket_id in seen_sockets:
                raise ValueError(f"duplicate socket_id '{sock.socket_id}' in required_sockets")
            seen_sockets.add(sock.socket_id)
            if sock.parent_role not in roles_set:
                raise ValueError(
                    f"required_socket '{sock.socket_id}' parent_role '{sock.parent_role}' is not in defined roles vocabulary"
                )

        return self


class ProfileDocumentV07(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["asset-profile-0.7.0"]
    profile_id: str = Field(..., min_length=2, max_length=40)
    version: int = Field(..., ge=1, le=1000, strict=True)
    categories: list[CATEGORY_V07_LITERAL] = Field(..., min_length=1)
    review_views: list[str] = Field(..., min_length=1)
    framing: FramingPolicy
    dimension_rules: DimensionRules
    processing: ProcessingPolicyV07
    godot: GodotPolicy
    runtime: RuntimePolicy
    geometry_mode: Literal["single_mesh", "assembly"]
    accepted_source_kinds: list[Literal["provider_generated", "local_operator_assembly"]] = Field(
        ..., min_length=1
    )
    assembly: AssemblyContract | None = None

    @field_validator("profile_id")
    @classmethod
    def identifier_is_safe(cls, value: str) -> str:
        if not value.replace("_", "").isalnum() or not value[0].isalpha() or value != value.lower():
            raise ValueError("profile_id must be a lowercase identifier")
        return value

    @field_validator("review_views")
    @classmethod
    def views_are_implemented(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("review_views contains a duplicate")
        unknown = [item for item in value if item not in PLACED_VIEWS]
        if unknown:
            raise ValueError(f"review view is not implemented: {unknown[0]}")
        return value

    @field_validator("categories")
    @classmethod
    def categories_unique(cls, value: list[CATEGORY_V07_LITERAL]) -> list[CATEGORY_V07_LITERAL]:
        if len(value) != len(set(value)):
            raise ValueError("categories contains duplicate values")
        return value

    @field_validator("accepted_source_kinds")
    @classmethod
    def source_kinds_unique(
        cls, value: list[Literal["provider_generated", "local_operator_assembly"]]
    ) -> list[Literal["provider_generated", "local_operator_assembly"]]:
        if len(value) != len(set(value)):
            raise ValueError("accepted_source_kinds contains duplicate values")
        return value

    @model_validator(mode="after")
    def validate_geometry_mode_and_assembly(self) -> ProfileDocumentV07:
        if self.geometry_mode == "single_mesh":
            if self.accepted_source_kinds != ["provider_generated"]:
                raise ValueError(
                    "single_mesh requires accepted_source_kinds == ['provider_generated']"
                )
            if self.assembly is not None:
                raise ValueError("single_mesh forbids assembly contract")
        elif self.geometry_mode == "assembly":
            if self.accepted_source_kinds != ["local_operator_assembly"]:
                raise ValueError(
                    "assembly requires accepted_source_kinds == ['local_operator_assembly']"
                )
            if self.assembly is None:
                raise ValueError("assembly requires assembly contract to be defined")
        return self


def parse_profile_document_v07(content: str | dict[str, Any]) -> ProfileDocumentV07:
    """Validate one V0.7 profile document. Unknown fields and unsafe values are rejected."""
    if isinstance(content, str):
        from gamefactory.core.domain.asset_contracts import UniqueKeyLoader

        try:
            parsed = yaml.load(content, Loader=UniqueKeyLoader)
        except SpecInvalidError:
            raise
        except Exception as exc:
            raise ProfileContractError(f"profile document is not valid YAML: {exc}") from exc
    elif isinstance(content, dict):
        parsed = content
    else:
        raise ProfileContractError(
            f"profile document must be a mapping, got {type(content).__name__}"
        )
    if not isinstance(parsed, dict):
        raise ProfileContractError("profile document must be a mapping")
    try:
        return ProfileDocumentV07.model_validate(parsed)
    except Exception as exc:
        raise ProfileContractError(f"profile document rejected: {exc}") from exc


@dataclass(frozen=True)
class AssetProfileV07:
    document: ProfileDocumentV07

    @property
    def profile_id(self) -> str:
        return self.document.profile_id

    @property
    def version(self) -> int:
        return self.document.version

    @property
    def schema_version(self) -> str:
        return self.document.schema_version

    @property
    def qualified(self) -> str:
        return f"{self.profile_id}@{self.version}"

    @property
    def review_views(self) -> tuple[str, ...]:
        return tuple(self.document.review_views)

    @property
    def framing(self) -> FramingPolicy:
        return self.document.framing

    @property
    def geometry_mode(self) -> str:
        return self.document.geometry_mode

    @property
    def accepted_source_kinds(self) -> tuple[str, ...]:
        return tuple(self.document.accepted_source_kinds)

    @property
    def assembly(self) -> AssemblyContract | None:
        return self.document.assembly

    def check_specification(self, spec: AssetSpecificationV07) -> None:
        """Reject V0.7 specification choices this profile does not implement."""
        document = self.document
        if spec.category not in document.categories:
            raise ValueError(f"category {spec.category} is not supported by {self.qualified}")
        if spec.profile != self.profile_id:
            raise ValueError("specification profile does not match the bound profile")
        if spec.profile_version != self.version:
            raise ValueError(
                f"specification profile_version {spec.profile_version} does not match {self.qualified}"
            )
        if self.geometry_mode == "assembly":
            if spec.parts is None or len(spec.parts) == 0:
                raise ValueError(f"assembly profile {self.qualified} requires parts to be defined")
        elif self.geometry_mode == "single_mesh":
            if spec.parts is not None:
                raise ValueError(f"single_mesh profile {self.qualified} forbids parts")
            if spec.sockets is not None:
                raise ValueError(f"single_mesh profile {self.qualified} forbids sockets")

        if spec.source_kind not in document.accepted_source_kinds:
            raise ValueError(
                f"source_kind '{spec.source_kind}' is not accepted by {self.qualified}"
            )

        if self.geometry_mode == "assembly" and document.assembly is not None:
            assembly_contract = document.assembly
            parts = spec.parts or []
            declared_roles = {p.role for p in parts}

            for p in parts:
                if p.role not in assembly_contract.roles:
                    raise ValueError(
                        f"part '{p.part_id}' role '{p.role}' is not in profile role vocabulary: {assembly_contract.roles}"
                    )

            for req_role in assembly_contract.required_roles:
                if req_role not in declared_roles:
                    raise ValueError(
                        f"required role '{req_role}' is missing from specification parts"
                    )

            for role, constraint in assembly_contract.role_motion_constraints.items():
                for p in parts:
                    if p.role == role:
                        if p.pivot.motion.kind != constraint.kind:
                            raise ValueError(
                                f"part '{p.part_id}' (role '{role}') motion kind '{p.pivot.motion.kind}' "
                                f"does not match required kind '{constraint.kind}'"
                            )
                        if constraint.axis is not None:
                            if p.pivot.motion.axis is None:
                                raise ValueError(
                                    f"part '{p.part_id}' (role '{role}') is missing motion axis"
                                )
                            if not all(
                                math.isclose(a, b, abs_tol=1e-6)
                                for a, b in zip(p.pivot.motion.axis, constraint.axis, strict=True)
                            ):
                                raise ValueError(
                                    f"part '{p.part_id}' (role '{role}') motion axis {p.pivot.motion.axis} "
                                    f"does not match required axis {constraint.axis}"
                                )

            part_map = {p.part_id: p for p in parts}
            socket_map = {s.socket_id: s for s in (spec.sockets or [])}
            for req_sock in assembly_contract.required_sockets:
                if req_sock.socket_id not in socket_map:
                    raise ValueError(
                        f"required socket '{req_sock.socket_id}' is missing from specification sockets"
                    )
                sock = socket_map[req_sock.socket_id]
                parent_part = part_map.get(sock.parent_part)
                if parent_part is None or parent_part.role != req_sock.parent_role:
                    parent_role = parent_part.role if parent_part else None
                    raise ValueError(
                        f"socket '{sock.socket_id}' parent part '{sock.parent_part}' has role '{parent_role}', "
                        f"expected required role '{req_sock.parent_role}'"
                    )
                if sock.placement != req_sock.placement:
                    raise ValueError(
                        f"socket '{sock.socket_id}' placement '{sock.placement}' does not match expected placement '{req_sock.placement}'"
                    )

        processing = document.processing
        if spec.lod_policy not in processing.allowed_lod_policies:
            raise ValueError(f"lod_policy {spec.lod_policy} is not allowed by {self.qualified}")
        if spec.collider_policy not in processing.allowed_collider_policies:
            raise ValueError(
                f"collider_policy {spec.collider_policy} is not allowed by {self.qualified}"
            )
        if spec.origin_policy not in processing.allowed_origin_policies:
            raise ValueError(
                f"origin_policy {spec.origin_policy} is not allowed by {self.qualified}"
            )

        for axis_name, axis_value in (
            ("width_m", spec.dimensions.width_m),
            ("depth_m", spec.dimensions.depth_m),
            ("height_m", spec.dimensions.height_m),
        ):
            if not document.dimension_rules.min_m <= axis_value <= document.dimension_rules.max_m:
                raise ValueError(
                    f"{axis_name} {axis_value} is outside {self.qualified} "
                    f"{document.dimension_rules.min_m}-{document.dimension_rules.max_m} m"
                )
            if processing.snap_grid_m is not None and not _on_snap_grid(
                axis_value, processing.snap_grid_m, processing.dimension_tolerance_m
            ):
                raise ValueError(
                    f"{axis_name} {axis_value} is not a multiple of snap grid {processing.snap_grid_m}"
                )

        if spec.material_budget.max_materials > processing.max_materials:
            raise ValueError(f"material budget exceeds {self.qualified}")
        if spec.texture_budget.max_dimension > processing.max_texture_dimension:
            raise ValueError(f"texture budget exceeds {self.qualified}")
        if spec.geometry_budget.max_triangles_lod0 > processing.max_triangles_lod0:
            raise ValueError(f"LOD0 triangle budget exceeds {self.qualified}")

        if spec.collider.policy == "capsule":
            if spec.collider.capsule is None:
                raise ValueError(
                    "capsule specification must be present for capsule collider policy"
                )
            cap = spec.collider.capsule
            tol = processing.dimension_tolerance_m
            if cap.height_m > spec.dimensions.height_m + tol:
                raise ValueError(
                    f"capsule height_m ({cap.height_m}) exceeds specification height ({spec.dimensions.height_m}) + tolerance ({tol})"
                )
            max_horiz = max(spec.dimensions.width_m, spec.dimensions.depth_m)
            if 2 * cap.radius_m > max_horiz + tol:
                raise ValueError(
                    f"capsule diameter ({2 * cap.radius_m}) exceeds max horizontal bounds ({max_horiz}) + tolerance ({tol})"
                )

    # --- contracts --------------------------------------------------------------

    def lod1_mesh_required(self, spec: AssetSpecificationV07) -> bool:
        return bool(self.document.processing.lod1_required or spec.lod_policy == "lod0_lod1")

    def expected_mesh_names(self, spec: AssetSpecificationV07) -> set[str]:
        """Mesh node names of a single-mesh result (assemblies use the part tree)."""
        if self.geometry_mode == "assembly":
            names = {f"SM_{spec.asset_id}_{p.part_id}_LOD0" for p in spec.parts or []}
            if self.lod1_mesh_required(spec):
                names |= {f"SM_{spec.asset_id}_{p.part_id}_LOD1" for p in spec.parts or []}
        else:
            names = {f"SM_{spec.asset_id}_LOD0"}
            if self.lod1_mesh_required(spec):
                names.add(f"SM_{spec.asset_id}_LOD1")
        if spec.collider.policy == "box":
            names.add(f"COL_{spec.asset_id}")
        return names

    def _hierarchy(self, spec: AssetSpecificationV07) -> list[dict[str, Any]]:
        from gamefactory.core.domain.transforms import spec_quat

        lod1 = self.lod1_mesh_required(spec)
        rows = []
        for part in spec.parts or []:
            meshes = [f"SM_{spec.asset_id}_{part.part_id}_LOD0"]
            if lod1:
                meshes.append(f"SM_{spec.asset_id}_{part.part_id}_LOD1")
            motion = part.pivot.motion
            rows.append(
                {
                    "part_id": part.part_id,
                    "role": part.role,
                    "parent": part.parent,
                    "node": f"PART_{part.part_id}",
                    "parent_node": "ROOT" if part.parent == "root" else f"PART_{part.parent}",
                    "meshes": meshes,
                    "pivot": {
                        "position_m": [float(v) for v in part.pivot.position_m],
                        "basis_quaternion_xyzw": list(spec_quat(part.pivot.basis)),
                        "motion": motion.kind,
                        "axis": [float(v) for v in motion.axis] if motion.axis else None,
                        "limits": (
                            {"lower": motion.limits.lower, "upper": motion.limits.upper}
                            if motion.limits
                            else None
                        ),
                    },
                }
            )
        return rows

    def _sockets(self, spec: AssetSpecificationV07) -> list[dict[str, Any]]:
        from gamefactory.core.domain.transforms import spec_quat

        required = {
            r.socket_id: r for r in (self.assembly.required_sockets if self.assembly else [])
        }
        rows = []
        for socket in spec.sockets or []:
            rule = required.get(socket.socket_id)
            rows.append(
                {
                    "socket_id": socket.socket_id,
                    "node": f"SOCKET_{socket.socket_id}",
                    "parent_part": socket.parent_part,
                    "parent_node": f"PART_{socket.parent_part}",
                    "translation_m": [float(v) for v in socket.translation_m],
                    "rotation_quaternion_xyzw": list(spec_quat(socket.rotation)),
                    "forward_local": [0.0, 0.0, -1.0],
                    "placement": socket.placement,
                    "forward_end_fraction": rule.forward_end_fraction if rule else None,
                    "rest_forward": list(rule.rest_forward) if rule and rule.rest_forward else None,
                }
            )
        return rows

    def processing_contract(self, spec: AssetSpecificationV07) -> dict[str, Any]:
        processing = self.document.processing
        capsule = spec.collider.capsule
        contract: dict[str, Any] = {
            "schema_version": PROCESSING_CONTRACT_VERSION_V07,
            "profile_id": self.profile_id,
            "profile_version": self.version,
            "profile_qualified": self.qualified,
            "asset_id": spec.asset_id,
            "geometry_mode": self.geometry_mode,
            "source_kind": spec.source_kind,
            "canonical_frame": dict(CANONICAL_FRAME),
            "target_width_m": spec.dimensions.width_m,
            "target_depth_m": spec.dimensions.depth_m,
            "target_height_m": spec.dimensions.height_m,
            "origin_policy": spec.origin_policy,
            "lod_policy": spec.lod_policy,
            "lod1_required": self.lod1_mesh_required(spec),
            "lod1_ratio": spec.geometry_budget.lod_ratio,
            "collider_policy": spec.collider.policy,
            "capsule": (
                {"radius_m": capsule.radius_m, "height_m": capsule.height_m, "axis": "+Y"}
                if capsule
                else None
            ),
            "dimension_tolerance_m": processing.dimension_tolerance_m,
            "snap_grid_m": processing.snap_grid_m,
            "rig_forbidden": processing.rig_forbidden,
            "animation_forbidden": processing.animation_forbidden,
        }
        if self.geometry_mode == "assembly" and self.assembly is not None:
            assembly = self.assembly
            contract["root_node"] = "ROOT"
            contract["hierarchy"] = self._hierarchy(spec)
            contract["sockets"] = self._sockets(spec)
            contract["tolerances"] = {
                "pivot_m": assembly.pivot_tolerance_m,
                "basis_deg": assembly.basis_tolerance_deg,
                "socket_m": assembly.socket_position_tolerance_m,
                "socket_deg": assembly.socket_angle_tolerance_deg,
            }
            contract["source_front_values"] = ["-Z", "+Z"]
        return contract

    def scene_contract(self, spec: AssetSpecificationV07 | None = None) -> dict[str, Any]:
        body = "StaticBody3D" if self.document.godot.body_kind == "static_body" else "Area3D"
        policy = spec.collider.policy if spec is not None else "box"
        return {
            "root": "Node3D",
            "visual": "Visual",
            "physics": body,
            "collision": "CollisionShape3D",
            "body_kind": self.document.godot.body_kind,
            "shape": "CapsuleShape3D" if policy == "capsule" else "BoxShape3D",
            "geometry_mode": self.geometry_mode,
        }

    def runtime_requirements(self, spec: AssetSpecificationV07 | None = None) -> dict[str, Any]:
        godot = self.document.godot
        runtime = self.document.runtime
        requirements: dict[str, Any] = {
            "require_mesh_visible": True,
            "require_collision": True,
            "require_physics_body": godot.body_kind == "static_body",
            "require_area": godot.require_area,
            "require_ray_hit": godot.require_ray_hit,
            "bounds_tolerance_ratio": runtime.bounds_tolerance_ratio,
            "bounds_tolerance_floor_m": runtime.bounds_tolerance_floor_m,
            "dimension_tolerance_m": self.document.processing.dimension_tolerance_m,
            "geometry_mode": self.geometry_mode,
        }
        if spec is not None:
            capsule = spec.collider.capsule
            requirements["collider_policy"] = spec.collider.policy
            requirements["capsule"] = (
                {"radius_m": capsule.radius_m, "height_m": capsule.height_m} if capsule else None
            )
            if self.geometry_mode == "assembly":
                requirements["hierarchy"] = self._hierarchy(spec)
                requirements["sockets"] = self._sockets(spec)
                if self.assembly is not None:
                    requirements["pivot_tolerance_m"] = self.assembly.pivot_tolerance_m
                    requirements["socket_tolerance_m"] = self.assembly.socket_position_tolerance_m
        return requirements

    def capture_request_profile(self, spec: AssetSpecificationV07 | None = None) -> dict[str, Any]:
        framing = self.framing
        return {
            "profile_id": self.profile_id,
            "profile_version": self.version,
            "body_kind": self.document.godot.body_kind,
            "require_ray_hit": self.document.godot.require_ray_hit,
            "require_area": self.document.godot.require_area,
            "review_views": list(self.review_views),
            "framing": {
                "min_screen_fraction": framing.min_screen_fraction,
                "max_screen_fraction": framing.max_screen_fraction,
                "target_screen_fraction": framing.target_screen_fraction,
                "margin_fraction": framing.margin_fraction,
                "fov_degrees": framing.fov_degrees,
            },
            "scene": self.scene_contract(spec),
        }


@dataclass(frozen=True)
class ProfileRegistry:
    available: tuple[AssetProfile, ...]
    unsupported: tuple[str, ...]
    available_v07: tuple[AssetProfileV07, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.available, tuple) or any(
            not isinstance(p, AssetProfile) for p in self.available
        ):
            raise TypeError("available must be a tuple of AssetProfile instances")
        if not isinstance(self.unsupported, tuple) or any(
            not isinstance(u, str) for u in self.unsupported
        ):
            raise TypeError("unsupported must be a tuple of str identifiers")
        if not isinstance(self.available_v07, tuple) or any(
            not isinstance(p, AssetProfileV07) for p in self.available_v07
        ):
            raise TypeError("available_v07 must be a tuple of AssetProfileV07 instances")

        all_qualified = [p.qualified for p in self.available] + [
            p.qualified for p in self.available_v07
        ]
        if len(all_qualified) != len(set(all_qualified)):
            raise ValueError("duplicate qualified profile id in available / available_v07")

        unsupported_set = set(self.unsupported)
        if len(self.unsupported) != len(unsupported_set):
            raise ValueError("duplicate profile_id in unsupported")

        for profile_id in [p.profile_id for p in self.available] + [
            p.profile_id for p in self.available_v07
        ]:
            if profile_id in unsupported_set:
                raise ValueError(f"profile '{profile_id}' cannot be both available and unsupported")

    def get(self, profile_id: str, version: int | None = None) -> AssetProfile:
        if profile_id in self.unsupported:
            raise ProfileContractError(f"profile {profile_id} is UNSUPPORTED")
        wanted = 1 if version is None else version
        for profile in self.available:
            if profile.profile_id == profile_id and profile.version == wanted:
                return profile
        raise ProfileContractError(f"profile {profile_id}@{wanted} is not registered")

    def get_v07(self, profile_id: str, version: int) -> AssetProfileV07:
        if profile_id in self.unsupported:
            raise ProfileContractError(f"profile {profile_id} is UNSUPPORTED")
        for profile in self.available_v07:
            if profile.profile_id == profile_id and profile.version == version:
                return profile
        raise ProfileContractError(f"profile {profile_id}@{version} is not registered")

    def availability(self) -> list[dict[str, str]]:
        rows = [
            {
                "profile_id": profile.profile_id,
                "qualified": profile.qualified,
                "version": str(profile.version),
                "status": "AVAILABLE",
            }
            for profile in self.available
        ]
        rows.extend(
            {
                "profile_id": profile.profile_id,
                "qualified": profile.qualified,
                "version": str(profile.version),
                "status": "AVAILABLE",
            }
            for profile in self.available_v07
        )
        rows.extend(
            {
                "profile_id": profile_id,
                "qualified": profile_id,
                "version": "",
                "status": "UNSUPPORTED",
            }
            for profile_id in self.unsupported
        )
        return rows


def _load_builtin(name: str) -> AssetProfile:
    resource = files("gamefactory").joinpath(f"resources/profiles/{name}.yml")
    text = resource.read_text(encoding="utf-8")
    return AssetProfile(parse_profile_document(text))


def builtin_registry() -> ProfileRegistry:
    """Return the explicit V0.5 registry. The object has no mutating API."""
    return ProfileRegistry(
        available=(
            _load_builtin("static_prop"),
            _load_builtin("pickup"),
            _load_builtin("modular_piece"),
        ),
        unsupported=UNSUPPORTED_PROFILE_IDS,
        available_v07=(),
    )


def render_scene_contract(
    contract: dict[str, Any], glb_resource: str = "res://assets/asset.glb"
) -> str:
    """Write a Godot 4 scene whose node tree follows the profile scene contract."""
    physics = str(contract["physics"])
    if physics not in {"StaticBody3D", "Area3D"}:
        raise ProfileContractError(f"scene contract physics node is unsupported: {physics}")
    return (
        "[gd_scene load_steps=2 format=3]\n\n"
        f'[ext_resource type="PackedScene" path="{glb_resource}" id="1"]\n\n'
        f'[node name="{contract["root"]}" type="Node3D"]\n\n'
        f'[node name="{contract["visual"]}" parent="." instance=ExtResource("1")]\n\n'
        f'[node name="{physics}" type="{physics}" parent="."]\n\n'
        f'[node name="{contract["collision"]}" type="CollisionShape3D" parent="{physics}"]\n'
    )
