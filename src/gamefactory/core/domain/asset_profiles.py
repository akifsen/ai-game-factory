"""Built-in asset profiles.

A profile describes how a class of asset is processed. An AssetSpecification
holds the values for one asset. Profiles are explicit registry entries loaded
from strict YAML; they are not plugins and they do not execute caller code.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib.resources import files
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from gamefactory.core.domain.camera_framing import PLACED_VIEWS
from gamefactory.core.domain.errors import SpecInvalidError

PROFILE_SCHEMA_VERSION = "asset-profile-0.5.0"
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


@dataclass(frozen=True)
class ProfileRegistry:
    available: tuple[AssetProfile, ...]
    unsupported: tuple[str, ...]

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
