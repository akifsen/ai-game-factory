"""Built-in asset profiles.

A profile describes how a class of asset is processed. An AssetSpecification
holds the values for one asset. Profiles are explicit registry entries loaded
from strict YAML; they are not plugins and they do not execute caller code.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from importlib.resources import files
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.camera_framing import PLACED_VIEWS
from gamefactory.core.domain.errors import SpecInvalidError

PROFILE_SCHEMA_VERSION = "asset-profile-0.5.0"
SUPPORTED_PROFILE_SCHEMA_VERSIONS = {"asset-profile-0.5.0", "asset-profile-0.7.0"}
PROCESSING_CONTRACT_VERSION = "asset-processing-contract-0.5.0"

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
        colliders = {"box", "capsule"}
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


class AssemblySocketRequirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    socket_id: str
    parent_role: str
    placement: Literal["forward_end"] = "forward_end"
    forward_end_fraction: float = Field(default=0.25, gt=0.0, le=1.0)
    rest_forward: list[float] | None = None

    @field_validator("socket_id", "parent_role")
    @classmethod
    def validate_identifier(cls, v: str) -> str:
        if not v.replace("_", "").isalnum() or not v[0].isalpha() or v != v.lower():
            raise ValueError(f"identifier must be lowercase alphanumeric: {v}")
        return v

    @field_validator("rest_forward")
    @classmethod
    def validate_rest_forward(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return None
        if len(v) != 3 or any(not math.isfinite(x) for x in v):
            raise ValueError("rest_forward must be 3 finite floats")
        norm = math.sqrt(sum(x * x for x in v))
        if abs(norm - 1.0) > 1e-6:
            raise ValueError("rest_forward must be a unit vector within 1e-6")
        return v


class RoleMotionConstraint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["fixed", "revolute", "prismatic"]
    axis: list[float] | None = None

    @model_validator(mode="after")
    def validate_motion(self) -> RoleMotionConstraint:
        if self.kind == "fixed":
            if self.axis is not None:
                raise ValueError("motion axis must not be specified for fixed motion")
        else:
            if self.axis is None:
                raise ValueError(f"motion axis is required for {self.kind} motion")
            if len(self.axis) != 3 or any(not math.isfinite(x) for x in self.axis):
                raise ValueError("motion axis must be 3 finite floats")
            norm = math.sqrt(sum(x * x for x in self.axis))
            if abs(norm - 1.0) > 1e-6:
                raise ValueError("motion axis must be a unit vector within 1e-6")
        return self


class AssemblyContract(BaseModel):
    model_config = ConfigDict(extra="forbid")

    roles: list[str] = Field(..., min_length=1)
    required_roles: list[str] = Field(..., min_length=1)
    role_motion_constraints: dict[str, RoleMotionConstraint] = Field(default_factory=dict)
    required_sockets: list[AssemblySocketRequirement] = Field(default_factory=list)
    socket_angle_tolerance_deg: float = Field(default=5.0, gt=0.0, le=90.0)
    pivot_tolerance_m: float = Field(default=0.01, gt=0.0, le=1.0)
    basis_tolerance_deg: float = Field(default=1.0, gt=0.0, le=90.0)

    @field_validator("roles", "required_roles")
    @classmethod
    def validate_roles_list(cls, v: list[str]) -> list[str]:
        if len(v) != len(set(v)):
            raise ValueError("roles list contains duplicates")
        for r in v:
            if not r.replace("_", "").isalnum() or not r[0].isalpha() or r != r.lower():
                raise ValueError(f"role name must be lowercase alphanumeric: {r}")
        return v

    @model_validator(mode="after")
    def validate_cross_role_rules(self) -> AssemblyContract:
        role_set = set(self.roles)
        for req in self.required_roles:
            if req not in role_set:
                raise ValueError(f"required role '{req}' is not in role vocabulary")
        for r_name in self.role_motion_constraints:
            if r_name not in role_set:
                raise ValueError(f"role motion constraint '{r_name}' is not in role vocabulary")
        for socket in self.required_sockets:
            if socket.parent_role not in role_set:
                raise ValueError(
                    f"required socket '{socket.socket_id}' parent_role '{socket.parent_role}' is not in role vocabulary"
                )
        return self


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

    # V0.7 fields
    geometry_mode: Literal["single_mesh", "assembly"] | None = None
    accepted_source_kinds: list[Literal["provider_generated", "local_operator_assembly"]] | None = (
        None
    )
    assembly: AssemblyContract | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_v07_fields_in_historical_profiles(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        schema_version = data.get("schema_version")
        if schema_version == "asset-profile-0.5.0":
            v07_keys = ("geometry_mode", "accepted_source_kinds", "assembly")
            for key in v07_keys:
                if key in data:
                    raise ValueError(
                        f"Extra inputs are not permitted: asset-profile-0.5.0 does not accept field '{key}'"
                    )
        return data

    @field_validator("schema_version")
    @classmethod
    def schema_is_current(cls, value: str) -> str:
        if value not in SUPPORTED_PROFILE_SCHEMA_VERSIONS:
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
            if item not in {
                "prop",
                "pickup",
                "modular",
                "character",
                "vehicle",
                "weapon",
                "aircraft",
            }:
                raise ValueError(f"unsupported asset category {item}")
        return value

    @model_validator(mode="after")
    def validate_schema_version_rules(self) -> ProfileDocument:
        if self.schema_version == "asset-profile-0.5.0":
            if self.geometry_mode is not None:
                raise ValueError("geometry_mode is forbidden for asset-profile-0.5.0")
            if self.accepted_source_kinds is not None:
                raise ValueError("accepted_source_kinds is forbidden for asset-profile-0.5.0")
            if self.assembly is not None:
                raise ValueError("assembly contract is forbidden for asset-profile-0.5.0")
            if "capsule" in self.processing.allowed_collider_policies:
                raise ValueError("capsule collider is only allowed for asset-profile-0.7.0")
            for cat in self.categories:
                if cat not in {"prop", "pickup", "modular"}:
                    raise ValueError(f"unsupported asset category {cat} for asset-profile-0.5.0")
        elif self.schema_version == "asset-profile-0.7.0":
            if self.geometry_mode is None:
                raise ValueError("geometry_mode is required for asset-profile-0.7.0")
            if self.accepted_source_kinds is None:
                raise ValueError("accepted_source_kinds is required for asset-profile-0.7.0")
            if len(self.accepted_source_kinds) == 0:
                raise ValueError("accepted_source_kinds cannot be empty")
            if self.geometry_mode == "assembly":
                if self.accepted_source_kinds != ["local_operator_assembly"]:
                    raise ValueError(
                        "geometry_mode 'assembly' requires accepted_source_kinds == ['local_operator_assembly']"
                    )
                if self.assembly is None:
                    raise ValueError("geometry_mode 'assembly' requires an assembly contract")
            elif self.geometry_mode == "single_mesh":
                if self.assembly is not None:
                    raise ValueError("geometry_mode 'single_mesh' forbids assembly contract")
        return self

    def model_dump(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        payload = super().model_dump(*args, **kwargs)
        if payload.get("schema_version") == "asset-profile-0.5.0":
            for field_name in ("geometry_mode", "accepted_source_kinds", "assembly"):
                payload.pop(field_name, None)
        return payload


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

    @property
    def geometry_mode(self) -> str:
        return self.document.geometry_mode or "single_mesh"

    @property
    def accepted_source_kinds(self) -> tuple[str, ...]:
        if self.document.accepted_source_kinds is not None:
            return tuple(self.document.accepted_source_kinds)
        return ("provider_generated",)

    @property
    def assembly(self) -> AssemblyContract | None:
        return self.document.assembly

    def check_specification(self, spec: Any) -> None:
        """Reject specification choices this profile does not implement."""
        document = self.document

        # Version matching
        if spec.schema_version == "0.7.0":
            if self.schema_version != "asset-profile-0.7.0":
                raise ValueError(
                    f"asset-spec-0.7.0 cannot bind to {self.schema_version} profile {self.qualified}"
                )
            if spec.profile_version != self.version:
                raise ValueError(
                    f"specification profile_version {spec.profile_version} does not match {self.qualified}"
                )
        elif spec.schema_version in ("0.4.0", "0.5.0"):
            if self.schema_version != "asset-profile-0.5.0":
                raise ValueError(
                    f"asset-spec-{spec.schema_version} cannot bind to {self.schema_version} profile {self.qualified}"
                )
            if spec.schema_version == "0.5.0" and spec.profile_version != self.version:
                raise ValueError(
                    f"specification profile_version {spec.profile_version} does not match {self.qualified}"
                )

        if spec.category not in document.categories:
            raise ValueError(f"category {spec.category} is not supported by {self.qualified}")
        if spec.profile != self.profile_id:
            raise ValueError("specification profile does not match the bound profile")

        # Source kind
        if spec.schema_version == "0.7.0":
            if not spec.source_kind:
                raise ValueError("specification requires explicit source_kind")
            if spec.source_kind not in self.accepted_source_kinds:
                raise ValueError(
                    f"source_kind '{spec.source_kind}' is not allowed by {self.qualified} (accepted: {self.accepted_source_kinds})"
                )

        # Geometry mode & parts/sockets
        if self.geometry_mode == "assembly":
            if not getattr(spec, "parts", None):
                raise ValueError("assembly profile requires parts in specification")
        elif self.geometry_mode == "single_mesh":
            if getattr(spec, "parts", None):
                raise ValueError("single_mesh profile forbids parts in specification")
            if getattr(spec, "sockets", None):
                raise ValueError("single_mesh profile forbids sockets in specification")

        # Roles and assembly constraints
        if self.geometry_mode == "assembly" and self.assembly is not None:
            vocab = set(self.assembly.roles)
            spec_roles = set()
            for part in spec.parts or []:
                if part.role not in vocab:
                    raise ValueError(
                        f"part '{part.part_id}' role '{part.role}' is not in profile role vocabulary"
                    )
                spec_roles.add(part.role)
            for req in self.assembly.required_roles:
                if req not in spec_roles:
                    raise ValueError(f"required role '{req}' is missing from parts specification")

            for part in spec.parts or []:
                if part.role in self.assembly.role_motion_constraints:
                    constraint = self.assembly.role_motion_constraints[part.role]
                    if part.pivot.motion.kind != constraint.kind:
                        raise ValueError(
                            f"part '{part.part_id}' motion kind '{part.pivot.motion.kind}' "
                            f"violates role '{part.role}' constraint '{constraint.kind}'"
                        )
                    if constraint.axis is not None:
                        if part.pivot.motion.axis is None:
                            raise ValueError(
                                f"part '{part.part_id}' is missing required motion axis for role '{part.role}'"
                            )
                        if not all(
                            abs(a - b) <= 1e-6
                            for a, b in zip(part.pivot.motion.axis, constraint.axis, strict=True)
                        ):
                            raise ValueError(
                                f"part '{part.part_id}' motion axis {part.pivot.motion.axis} "
                                f"does not match required axis {constraint.axis} for role '{part.role}'"
                            )

            if self.assembly.required_sockets:
                parts_by_id = {p.part_id: p for p in (spec.parts or [])}
                spec_sockets_by_id = {s.socket_id: s for s in (spec.sockets or [])}
                for req_socket in self.assembly.required_sockets:
                    if req_socket.socket_id not in spec_sockets_by_id:
                        raise ValueError(
                            f"required socket '{req_socket.socket_id}' is missing from specification"
                        )
                    spec_sock = spec_sockets_by_id[req_socket.socket_id]
                    parent_part = parts_by_id.get(spec_sock.parent_part)
                    if parent_part is None:
                        raise ValueError(
                            f"socket '{spec_sock.socket_id}' parent part '{spec_sock.parent_part}' not found in parts"
                        )
                    if parent_part.role != req_socket.parent_role:
                        raise ValueError(
                            f"socket '{spec_sock.socket_id}' parent part '{parent_part.part_id}' has role '{parent_part.role}', "
                            f"expected required role '{req_socket.parent_role}'"
                        )

        processing = document.processing
        if spec.lod_policy not in processing.allowed_lod_policies:
            raise ValueError(f"lod_policy {spec.lod_policy} is not allowed by {self.qualified}")
        if spec.collider_policy not in processing.allowed_collider_policies:
            raise ValueError(
                f"collider_policy {spec.collider_policy} is not allowed by {self.qualified}"
            )
        if spec.collider_policy == "capsule":
            if not getattr(spec, "collider", None) or not spec.collider.capsule:
                raise ValueError(
                    "capsule collider requires collider.capsule definition in specification"
                )
            capsule = spec.collider.capsule
            tolerance = processing.dimension_tolerance_m
            if capsule.height_m > spec.dimensions.height_m + tolerance:
                raise ValueError(
                    f"capsule height_m ({capsule.height_m}) exceeds specification height "
                    f"({spec.dimensions.height_m}) + tolerance ({tolerance})"
                )
            max_horiz = max(spec.dimensions.width_m, spec.dimensions.depth_m)
            if 2 * capsule.radius_m > max_horiz + tolerance:
                raise ValueError(
                    f"capsule diameter ({2 * capsule.radius_m}) exceeds max horizontal dimension "
                    f"({max_horiz}) + tolerance ({tolerance})"
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


@dataclass(frozen=True)
class ProfileRegistry:
    available: tuple[AssetProfile, ...]
    unsupported: tuple[str, ...] = ()

    def get(self, profile_id: str, version: int | None = None) -> AssetProfile:
        if profile_id in self.unsupported:
            raise ProfileContractError(f"profile {profile_id} is UNSUPPORTED")
        wanted = 1 if version is None else version
        for profile in self.available:
            if profile.profile_id == profile_id and profile.version == wanted:
                return profile
        raise ProfileContractError(f"profile {profile_id}@{wanted} is not registered")

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
