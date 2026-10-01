"""Declared contract for internal skinned GLB verification (ADR 0019)."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal


class InternalSkinContractError(ValueError):
    """Malformed internal skin contract payload."""


@dataclass(frozen=True)
class BoneSpec:
    name: str
    parent: str | None


@dataclass(frozen=True)
class RegionBox:
    """Axis-aligned box in mesh local space (+Y up, -Z front)."""

    min_xyz: tuple[float, float, float]
    max_xyz: tuple[float, float, float]


@dataclass(frozen=True)
class JointRestBasis:
    """Reference rest orientation in ADR 0014 frame (+Y / -Z measured axes)."""

    y_axis: tuple[float, float, float]
    neg_z_axis: tuple[float, float, float]


@dataclass(frozen=True)
class DeformationOracleSpec:
    pose_bone: str
    rotation_axis: tuple[float, float, float]
    rotation_degrees: float
    affected: RegionBox
    unaffected: RegionBox
    min_affected_displacement: float
    max_unaffected_displacement: float
    max_affected_displacement: float


@dataclass(frozen=True)
class InternalSkinContract:
    contract_id: str
    joint_count_max: int
    rest_pose: Literal["T", "A"]
    asset_root_name: str
    bones: tuple[BoneSpec, ...]
    oracle: DeformationOracleSpec
    rest_joint_origins: dict[str, tuple[float, float, float]]
    rest_joint_bases: dict[str, JointRestBasis]
    rest_pose_tolerance: float

    @property
    def bone_names(self) -> tuple[str, ...]:
        return tuple(b.name for b in self.bones)

    def parent_of(self, name: str) -> str | None:
        for bone in self.bones:
            if bone.name == name:
                return bone.parent
        raise KeyError(name)


def _finite_float(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise InternalSkinContractError(f"{label} must be a number")
    out = float(value)
    if not math.isfinite(out):
        raise InternalSkinContractError(f"{label} must be finite")
    return out


def _box(data: dict[str, Any], label: str) -> RegionBox:
    if not isinstance(data, dict):
        raise InternalSkinContractError(f"{label} must be an object")
    lo, hi = data.get("min"), data.get("max")
    if not isinstance(lo, list) or not isinstance(hi, list) or len(lo) != 3 or len(hi) != 3:
        raise InternalSkinContractError(f"{label} min/max must be length-3 arrays")
    mins = (
        _finite_float(lo[0], f"{label}.min[0]"),
        _finite_float(lo[1], f"{label}.min[1]"),
        _finite_float(lo[2], f"{label}.min[2]"),
    )
    maxs = (
        _finite_float(hi[0], f"{label}.max[0]"),
        _finite_float(hi[1], f"{label}.max[1]"),
        _finite_float(hi[2], f"{label}.max[2]"),
    )
    for i in range(3):
        if mins[i] > maxs[i]:
            raise InternalSkinContractError(f"{label} min must not exceed max on axis {i}")
    return RegionBox(mins, maxs)


def parse_internal_skin_contract(data: dict[str, Any]) -> InternalSkinContract:
    if not isinstance(data, dict):
        raise InternalSkinContractError("contract must be a JSON object")
    raw_bones = data.get("bones")
    if not isinstance(raw_bones, list) or not raw_bones:
        raise InternalSkinContractError("bones must be a non-empty array")
    names: set[str] = set()
    bones: list[BoneSpec] = []
    for entry in raw_bones:
        if not isinstance(entry, dict):
            raise InternalSkinContractError("bone entry must be an object")
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            raise InternalSkinContractError("bone name must be a non-empty string")
        if name in names:
            raise InternalSkinContractError(f"duplicate bone name {name}")
        names.add(name)
        parent = entry.get("parent")
        if parent is not None and not isinstance(parent, str):
            raise InternalSkinContractError("bone parent must be a string or null")
        bones.append(BoneSpec(name=name, parent=parent))
    for bone in bones:
        if (
            bone.parent is not None
            and bone.parent not in names
            and bone.parent != data.get("asset_root_name")
        ):
            raise InternalSkinContractError(f"bone {bone.name} references unknown parent")

    rest_pose = data.get("rest_pose")
    if rest_pose not in ("T", "A"):
        raise InternalSkinContractError("rest_pose must be T or A")
    if rest_pose == "A":
        raise InternalSkinContractError(
            "A-pose reference is deferred; only T is supported in V0.8-1"
        )

    origins_raw = data.get("rest_joint_origins")
    if not isinstance(origins_raw, dict) or not origins_raw:
        raise InternalSkinContractError("rest_joint_origins must be a non-empty object")
    origins: dict[str, tuple[float, float, float]] = {}
    for key, vec in origins_raw.items():
        if not isinstance(key, str) or not isinstance(vec, list) or len(vec) != 3:
            raise InternalSkinContractError("rest_joint_origins entries must be length-3 arrays")
        origins[key] = (
            _finite_float(vec[0], f"rest_joint_origins.{key}[0]"),
            _finite_float(vec[1], f"rest_joint_origins.{key}[1]"),
            _finite_float(vec[2], f"rest_joint_origins.{key}[2]"),
        )
    for bone in bones:
        if bone.name not in origins:
            raise InternalSkinContractError(f"rest_joint_origins missing bone {bone.name}")

    bases_raw = data.get("rest_joint_bases")
    if not isinstance(bases_raw, dict) or not bases_raw:
        raise InternalSkinContractError("rest_joint_bases must be a non-empty object")
    bases: dict[str, JointRestBasis] = {}
    for key, entry in bases_raw.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            raise InternalSkinContractError("rest_joint_bases entries must be objects")
        y_raw = entry.get("y_axis")
        nz_raw = entry.get("neg_z_axis")
        if (
            not isinstance(y_raw, list)
            or not isinstance(nz_raw, list)
            or len(y_raw) != 3
            or len(nz_raw) != 3
        ):
            raise InternalSkinContractError("rest_joint_bases axes must be length-3 arrays")
        bases[key] = JointRestBasis(
            y_axis=(
                _finite_float(y_raw[0], f"rest_joint_bases.{key}.y_axis[0]"),
                _finite_float(y_raw[1], f"rest_joint_bases.{key}.y_axis[1]"),
                _finite_float(y_raw[2], f"rest_joint_bases.{key}.y_axis[2]"),
            ),
            neg_z_axis=(
                _finite_float(nz_raw[0], f"rest_joint_bases.{key}.neg_z_axis[0]"),
                _finite_float(nz_raw[1], f"rest_joint_bases.{key}.neg_z_axis[1]"),
                _finite_float(nz_raw[2], f"rest_joint_bases.{key}.neg_z_axis[2]"),
            ),
        )
    for bone in bones:
        if bone.name not in bases:
            raise InternalSkinContractError(f"rest_joint_bases missing bone {bone.name}")

    tolerance = _finite_float(data.get("rest_pose_tolerance", 0.05), "rest_pose_tolerance")
    if tolerance <= 0:
        raise InternalSkinContractError("rest_pose_tolerance must be positive")

    oracle = data.get("deformation_oracle")
    if not isinstance(oracle, dict):
        raise InternalSkinContractError("deformation_oracle must be an object")
    axis_raw = oracle.get("rotation_axis")
    if not isinstance(axis_raw, list) or len(axis_raw) != 3:
        raise InternalSkinContractError("rotation_axis must be length 3")
    axis: tuple[float, float, float] = (
        _finite_float(axis_raw[0], "rotation_axis[0]"),
        _finite_float(axis_raw[1], "rotation_axis[1]"),
        _finite_float(axis_raw[2], "rotation_axis[2]"),
    )
    axis_len = math.sqrt(sum(c * c for c in axis))
    if axis_len < 1e-8:
        raise InternalSkinContractError("rotation_axis must be non-zero")
    min_aff = _finite_float(oracle.get("min_affected_displacement"), "min_affected_displacement")
    max_unaff = _finite_float(
        oracle.get("max_unaffected_displacement"), "max_unaffected_displacement"
    )
    max_aff = _finite_float(oracle.get("max_affected_displacement"), "max_affected_displacement")
    if min_aff <= 0 or max_unaff < 0 or max_aff <= 0:
        raise InternalSkinContractError("oracle displacement bounds must be positive/non-negative")
    if min_aff > max_aff:
        raise InternalSkinContractError("min_affected_displacement must not exceed max")

    pose_bone = oracle.get("pose_bone")
    if not isinstance(pose_bone, str) or not pose_bone:
        raise InternalSkinContractError("pose_bone must be a non-empty string")
    if pose_bone not in names:
        raise InternalSkinContractError(f"pose_bone {pose_bone} is not a declared bone")

    rot_deg = _finite_float(oracle.get("rotation_degrees"), "rotation_degrees")
    if abs(rot_deg) < 1e-9:
        raise InternalSkinContractError("rotation_degrees must be non-zero")

    rot_type = oracle.get("rotation_type", "axis_angle")
    if rot_type != "axis_angle":
        raise InternalSkinContractError("rotation_type must be axis_angle")

    joint_max = data.get("joint_count_max")
    if not isinstance(joint_max, int) or joint_max <= 0:
        raise InternalSkinContractError("joint_count_max must be a positive integer")

    return InternalSkinContract(
        contract_id=str(data["contract_id"]),
        joint_count_max=joint_max,
        rest_pose=rest_pose,
        asset_root_name=str(data["asset_root_name"]),
        bones=tuple(bones),
        oracle=DeformationOracleSpec(
            pose_bone=pose_bone,
            rotation_axis=axis,
            rotation_degrees=rot_deg,
            affected=_box(oracle["affected_region"], "affected_region"),
            unaffected=_box(oracle["unaffected_region"], "unaffected_region"),
            min_affected_displacement=min_aff,
            max_unaffected_displacement=max_unaff,
            max_affected_displacement=max_aff,
        ),
        rest_joint_origins=origins,
        rest_joint_bases=bases,
        rest_pose_tolerance=tolerance,
    )


def load_internal_skin_contract(path: Path | None = None) -> InternalSkinContract:
    if path is not None:
        return parse_internal_skin_contract(json.loads(path.read_text(encoding="utf-8")))
    raw = (
        resources.files("gamefactory.resources.internal_skin")
        .joinpath("humanoid_12bone_contract.json")
        .read_text(encoding="utf-8")
    )
    return parse_internal_skin_contract(json.loads(raw))
