"""Standalone V0.7 Godot assembly runtime verification adapter.

Proves real engine runtime representation, semantic ROOT, PART hierarchy,
pivots, sockets, colliders, articulation invariants, restoration, and review captures
for typed V0.7 assembly profiles and processed GLBs.
Core domain code does not import this adapter directly.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import sys
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from gamefactory.adapters.assets.glb_validator import (
    _inspect,
    _InvalidGLB,
    _mat_mul,
    _node_matrix,
    _read_glb_bytes,
)
from gamefactory.adapters.assets.v07_geometry_validation import _angular_difference_degrees
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.engines.godot_image import decode_png
from gamefactory.adapters.engines.godot_staging import _is_reparse
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.asset_profiles import AssetProfileV07
from gamefactory.core.domain.camera_framing import (
    PLACED_VIEWS,
    view_axis_label,
    view_direction,
    view_up,
)
from gamefactory.core.domain.errors import ToolExecutionError, ValidationError
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

REQUEST_SCHEMA_VERSION_V07 = "assembly-runtime-request-0.7.0"
OBSERVATION_SCHEMA_VERSION_V07 = "assembly-runtime-observation-0.7.0"
_MAX_OBSERVATION_BYTES = 1024 * 1024
_MAX_PROCESSED_GLB_BYTES = 50 * 1024 * 1024
_Matrix3 = tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]
_Matrix4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]


@dataclass(frozen=True)
class AssemblyRuntimeResult:
    """Outcome of Godot assembly runtime verification."""

    status: str  # "PASS" | "FAIL"
    observation: dict[str, Any]
    artifacts: dict[str, bytes]
    findings: list[str]
    request_digest: str
    processed_glb_sha256: str
    captures: dict[str, bytes]


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _display_driver() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "x11"
    raise ValidationError(f"rendered capture is not supported on {sys.platform}")


def _isolated_environment(scratch: Path) -> dict[str, str]:
    home = scratch / "isolated-user"
    temp = scratch / "temp"
    appdata = home / "AppData" / "Roaming"
    local = home / "AppData" / "Local"
    for path in (home, temp, appdata, local):
        path.mkdir(parents=True, exist_ok=True)
    return {
        "HOME": str(home),
        "USERPROFILE": str(home),
        "APPDATA": str(appdata),
        "LOCALAPPDATA": str(local),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "TMP": str(temp),
        "TEMP": str(temp),
        "TMPDIR": str(temp),
    }


def _binary_read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _reject_reparse_components(path: Path, label: str) -> None:
    """Reject links anywhere in a caller-controlled path before resolving it."""
    if ".." in path.parts:
        raise ValidationError(f"{label} cannot contain parent-directory traversal: {path}")
    absolute = Path(os.path.abspath(path))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        if _is_reparse(current):
            raise ValidationError(
                f"{label} path cannot contain a symlink or reparse point: {current}"
            )


def _read_bounded_regular_file(path: Path, *, limit: int, label: str) -> bytes:
    _reject_reparse_components(path, label)
    try:
        descriptor = os.open(path, _binary_read_flags())
    except OSError as exc:
        raise ValidationError(f"{label} could not be opened safely: {exc}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise ValidationError(f"{label} must be a regular file")
        if info.st_size > limit:
            raise ValidationError(f"{label} exceeds the {limit}-byte limit")
        result = bytearray()
        while len(result) <= limit:
            chunk = os.read(descriptor, min(1024 * 1024, limit + 1 - len(result)))
            if not chunk:
                break
            result.extend(chunk)
        if len(result) > limit:
            raise ValidationError(f"{label} exceeds the {limit}-byte limit")
        return bytes(result)
    finally:
        os.close(descriptor)


def _validate_review_views(views: list[str] | tuple[str, ...]) -> list[str]:
    if not isinstance(views, (list, tuple)) or not views:
        raise ValidationError("review_views must be a non-empty list of view names")
    seen: set[str] = set()
    validated: list[str] = []
    for v in views:
        if not isinstance(v, str) or not v:
            raise ValidationError(f"invalid review view identifier: {v!r}")
        if v not in PLACED_VIEWS:
            raise ValidationError(f"unknown review view identifier: '{v}'")
        if v in seen:
            raise ValidationError(f"duplicate review view identifier: '{v}'")
        seen.add(v)
        validated.append(v)
    return validated


def _linear(matrix: list[list[float]]) -> _Matrix3:
    return cast(_Matrix3, tuple(tuple(matrix[r][c] for c in range(3)) for r in range(3)))


def _determinant(matrix: _Matrix3) -> float:
    return (
        matrix[0][0] * (matrix[1][1] * matrix[2][2] - matrix[1][2] * matrix[2][1])
        - matrix[0][1] * (matrix[1][0] * matrix[2][2] - matrix[1][2] * matrix[2][0])
        + matrix[0][2] * (matrix[1][0] * matrix[2][1] - matrix[1][1] * matrix[2][0])
    )


def _column_scales(basis: _Matrix3) -> tuple[float, float, float]:
    return cast(
        tuple[float, float, float],
        tuple(math.sqrt(sum(basis[r][c] ** 2 for r in range(3))) for c in range(3)),
    )


def _rotation_from_uniform_basis(basis: _Matrix3, label: str) -> tuple[_Matrix3, float]:
    scales = _column_scales(basis)
    if min(scales) <= 1e-8 or max(scales) - min(scales) > 1e-5:
        raise ValidationError(f"processed GLB {label} must have positive uniform scale")
    scale = sum(scales) / 3.0
    rotation = cast(_Matrix3, tuple(tuple(basis[r][c] / scale for c in range(3)) for r in range(3)))
    if _determinant(rotation) <= 0:
        raise ValidationError(f"processed GLB {label} contains reflection or singular basis")
    for a in range(3):
        for b in range(a + 1, 3):
            dot = sum(rotation[r][a] * rotation[r][b] for r in range(3))
            if abs(dot) > 1e-5:
                raise ValidationError(f"processed GLB {label} contains shear")
    return rotation, scale


def _matrix_position(matrix: list[list[float]]) -> tuple[float, float, float]:
    return matrix[0][3], matrix[1][3], matrix[2][3]


def _matrix_rows(matrix: list[list[float]]) -> _Matrix3:
    return _linear(matrix)


def _bounded_processed_facts(
    raw_glb: bytes, spec: AssetSpecificationV07, profile: AssetProfileV07
) -> dict[str, Any]:
    """Decode transforms from the exact staged GLB bytes using bounded shared parsing."""
    try:
        document, binary = _read_glb_bytes(raw_glb, _MAX_PROCESSED_GLB_BYTES)
        meshes, _, _, _, _, _ = _inspect(document, binary)
    except (_InvalidGLB, KeyError, IndexError, TypeError, ValueError, OverflowError) as exc:
        raise ValidationError(f"processed_glb failed bounded structural validation: {exc}") from exc
    nodes = document.get("nodes", [])
    names: dict[str, int] = {}
    parents: dict[int, int] = {}
    for index, node in enumerate(nodes):
        if not isinstance(node, dict) or not isinstance(node.get("name"), str):
            continue
        name = node["name"]
        if name in names:
            raise ValidationError(f"processed GLB contains duplicate node name {name!r}")
        names[name] = index
        for child in node.get("children", []):
            parents[child] = index
    local_matrices = [_node_matrix(node) for node in nodes]
    world_matrices: dict[int, list[list[float]]] = {}

    def world_matrix(index: int) -> list[list[float]]:
        if index not in world_matrices:
            parent = parents.get(index)
            world_matrices[index] = (
                local_matrices[index]
                if parent is None
                else _mat_mul(world_matrix(parent), local_matrices[index])
            )
        return world_matrices[index]

    for node_index in range(len(nodes)):
        world_matrix(node_index)
    scene = document["scenes"][document.get("scene", 0)]
    roots = scene.get("nodes", [])
    if len(roots) != 1 or nodes[roots[0]].get("name") != "ROOT":
        raise ValidationError("processed GLB active scene must have exact ROOT identity")
    root_index = roots[0]
    root_local = _node_matrix(nodes[root_index])
    if any(
        abs(root_local[r][c] - (1.0 if r == c else 0.0)) > 1e-6 for r in range(4) for c in range(4)
    ):
        raise ValidationError("processed GLB ROOT transform must be identity")
    assembly = profile.assembly
    if assembly is None:
        raise ValidationError("assembly profile has no assembly tolerances")
    part_facts: dict[str, dict[str, Any]] = {}
    mesh_names = {mesh.name for mesh in meshes if mesh.triangle_count > 0}
    for part in spec.parts or []:
        name = f"PART_{part.part_id}"
        part_index = names.get(name)
        if part_index is None:
            raise ValidationError(f"processed GLB is missing {name}")
        parent_index = parents.get(part_index)
        expected_parent: int | None = (
            root_index if part.parent == "root" else names.get(f"PART_{part.parent}")
        )
        if expected_parent is None or parent_index != expected_parent:
            raise ValidationError(f"processed GLB {name} parent differs from specification")
        local = _node_matrix(nodes[part_index])
        basis, scale = _matrix_rows(local), None
        rotation, scale = _rotation_from_uniform_basis(basis, name)
        position = _matrix_position(local)
        part_position = cast(tuple[float, float, float], tuple(part.pivot.position_m))
        if not all(
            abs(actual - declared) <= assembly.pivot_tolerance_m
            for actual, declared in zip(position, part_position, strict=True)
        ):
            raise ValidationError(f"processed GLB {name} local position differs from specification")
        expected_rotation = _quaternion_matrix(part.pivot.basis)
        if (
            _angular_difference_degrees(
                [list(row) for row in rotation], [list(row) for row in expected_rotation]
            )
            > assembly.basis_tolerance_deg
        ):
            raise ValidationError(f"processed GLB {name} local basis differs from specification")
        extras = nodes[part_index].get("extras", {})
        if not isinstance(extras, dict) or extras.get("gf_motion") != part.pivot.motion.kind:
            raise ValidationError(f"processed GLB {name} gf_motion differs from specification")
        axis = extras.get("gf_axis")
        if part.pivot.motion.kind != "fixed":
            declared_axis = part.pivot.motion.axis
            if declared_axis is None or not _vector_close(
                axis, cast(tuple[float, float, float], tuple(declared_axis))
            ):
                raise ValidationError(f"processed GLB {name} gf_axis differs from specification")
        lod0 = f"SM_{spec.asset_id}_{part.part_id}_LOD0"
        lod1 = f"SM_{spec.asset_id}_{part.part_id}_LOD1"
        for lod_name in (lod0, lod1):
            if lod_name in names:
                lod_node = nodes[names[lod_name]]
                if parents.get(names[lod_name]) != part_index:
                    raise ValidationError(f"processed GLB {lod_name} must be a direct PART child")
                lod_matrix = _node_matrix(lod_node)
                if any(
                    abs(lod_matrix[r][c] - (1.0 if r == c else 0.0)) > 1e-5
                    for r in range(4)
                    for c in range(4)
                ):
                    raise ValidationError(
                        f"processed GLB {lod_name} local transform must be identity"
                    )
        if lod0 not in mesh_names:
            raise ValidationError(f"processed GLB {lod0} has no non-empty mesh")
        lod1_present = lod1 in names and lod1 in mesh_names
        part_facts[part.part_id] = {
            "name": name,
            "parent": part.parent,
            "position": position,
            "basis": basis,
            "scale": scale,
            "motion": part.pivot.motion.kind,
            "axis": axis,
            "lod0": True,
            "lod1": lod1_present,
        }
    sockets: dict[str, dict[str, Any]] = {}
    for socket in spec.sockets or []:
        name = f"SOCKET_{socket.socket_id}"
        socket_index = names.get(name)
        parent_index = names.get(f"PART_{socket.parent_part}")
        if (
            socket_index is None
            or parent_index is None
            or parents.get(socket_index) != parent_index
        ):
            raise ValidationError(
                f"processed GLB {name} must be a direct child of its declared PART"
            )
        local = _node_matrix(nodes[socket_index])
        rotation, scale = _rotation_from_uniform_basis(_matrix_rows(local), name)
        expected_rotation = _quaternion_matrix(socket.rotation)
        socket_position = cast(tuple[float, float, float], tuple(socket.translation_m))
        if abs(scale - 1.0) > 1e-5 or not all(
            abs(actual - declared) <= assembly.socket_position_tolerance_m
            for actual, declared in zip(_matrix_position(local), socket_position, strict=True)
        ):
            raise ValidationError(
                f"processed GLB {name} local transform differs from specification"
            )
        if (
            _angular_difference_degrees(
                [list(row) for row in rotation], [list(row) for row in expected_rotation]
            )
            > assembly.socket_angle_tolerance_deg
        ):
            raise ValidationError(f"processed GLB {name} local basis differs from specification")
        sockets[socket.socket_id] = {
            "name": name,
            "position": _matrix_position(local),
            "basis": _matrix_rows(local),
            "world": world_matrices[socket_index],
        }
    collider_name = f"COL_{spec.asset_id}"
    collider_index = names.get(collider_name)
    collider_mesh = next((mesh for mesh in meshes if mesh.name == collider_name), None)
    if (
        collider_index is None
        or parents.get(collider_index) != root_index
        or collider_mesh is None
        or not collider_mesh.points
    ):
        raise ValidationError(
            f"processed GLB requires direct non-empty {collider_name} mesh under ROOT"
        )
    collider_matrix = _node_matrix(nodes[collider_index])
    if any(
        abs(collider_matrix[r][c] - (1.0 if r == c else 0.0)) > 1e-5
        for r in range(4)
        for c in range(4)
    ):
        raise ValidationError(f"processed GLB {collider_name} transform must be identity")
    collider_min = tuple(min(point[i] for point in collider_mesh.points) for i in range(3))
    collider_max = tuple(max(point[i] for point in collider_mesh.points) for i in range(3))
    collider_bounds = {
        "position": collider_min,
        "size": tuple(collider_max[i] - collider_min[i] for i in range(3)),
    }
    return {
        "nodes": nodes,
        "names": names,
        "parents": parents,
        "parts": part_facts,
        "sockets": sockets,
        "meshes": mesh_names,
        "world": world_matrices,
        "collider_bounds": collider_bounds,
    }


def _load_harness_resource() -> bytes:
    harness_path = files("gamefactory").joinpath("resources/godot/asset_runtime_harness_v07.gd")
    if not harness_path.is_file():
        raise ToolExecutionError(
            "Packaged Godot assembly harness resource is missing",
            details={"resource": "resources/godot/asset_runtime_harness_v07.gd"},
        )
    return harness_path.read_bytes()


def _validate_observation(
    observation: dict[str, Any],
    *,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    review_views: list[str],
    processed_hash: str,
    revision: int,
    workflow_id: str,
    execution_id: str,
    attempt_number: int,
    request_digest: str,
    actual_facts: dict[str, Any],
    harness_sha256: str,
    profile_sha256: str,
    specification_sha256: str,
) -> None:
    """Validate engine findings and bind them to the declared assembly and captures."""

    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValidationError(f"Godot observation failed independent validation: {message}")

    # Bind failures just as strictly as success. Otherwise an old failed report
    # could be replayed to mask the actual attempt or profile under review.
    for key, expected in (
        ("workflow_id", workflow_id),
        ("execution_id", execution_id),
        ("asset_id", spec.asset_id),
        ("processed_glb_sha256", processed_hash),
        ("request_digest", request_digest),
        ("revision", revision),
        ("attempt_number", attempt_number),
        ("profile_id", profile.profile_id),
        ("profile_version", profile.version),
        ("harness_sha256", harness_sha256),
        ("profile_sha256", profile_sha256),
        ("specification_sha256", specification_sha256),
    ):
        value = observation.get(key)
        if key in ("revision", "attempt_number", "profile_version"):
            require(type(value) is int, f"{key} must be an integer")
        elif not isinstance(value, str):
            require(False, f"{key} must be a string")
        if key == "request_digest":
            require(value == expected, "request_digest mismatch (stale observation rejected)")
        else:
            require(value == expected, f"{key} mismatch")
    status = observation.get("status")
    errors = observation.get("errors")
    require(
        isinstance(status, str) and status in ("PASS", "FAIL"), "status is neither PASS nor FAIL"
    )
    if status == "FAIL":
        require(
            isinstance(errors, list)
            and bool(errors)
            and all(isinstance(e, str) and e for e in errors),
            "failed observation has no valid findings",
        )
        return
    require(isinstance(errors, list) and errors == [], "errors is not an empty list")
    root_info = observation.get("semantic_root")
    if not isinstance(root_info, dict) or root_info.get("name") != "ROOT":
        raise ValidationError(
            "Godot observation failed independent validation: semantic ROOT missing"
        )
    root_transform = root_info.get("transform", {})
    if not isinstance(root_transform, dict):
        raise ValidationError(
            "Godot observation failed independent validation: ROOT transform missing"
        )
    require(
        _vector_close(root_transform.get("origin"), (0.0, 0.0, 0.0)), "ROOT origin is not identity"
    )
    basis = root_transform.get("basis")
    require(
        isinstance(basis, list)
        and len(basis) == 3
        and all(
            _vector_close(row, expected)
            for row, expected in zip(basis, ((1, 0, 0), (0, 1, 0), (0, 0, 1)), strict=True)
        ),
        "ROOT basis is not identity",
    )
    require(observation.get("restoration_verified") is True, "restoration was not verified")

    expected_parts = {part.part_id: part for part in (spec.parts or [])}
    parts = observation.get("parts_verified")
    if not isinstance(parts, list):
        raise ValidationError(
            "Godot observation failed independent validation: parts_verified is not a list"
        )
    actual_parts = {item.get("part_id"): item for item in parts if isinstance(item, dict)}
    require(
        len(actual_parts) == len(parts) and actual_parts.keys() == expected_parts.keys(),
        "PART inventory differs from specification",
    )
    lod1_required = spec.lod_policy == "lod0_lod1" or profile.document.processing.lod1_required
    for part_id, part in expected_parts.items():
        item = actual_parts[part_id]
        facts = actual_facts["parts"][part_id]
        require(item.get("parent") == part.parent, f"PART_{part_id} parent mismatch")
        require(
            item.get("local_position") is not None
            and _vector_close(item.get("local_position"), facts["position"]),
            f"PART_{part_id} local position differs from processed GLB",
        )
        require(
            _matrix3_close(item.get("local_basis"), facts["basis"]),
            f"PART_{part_id} local basis differs from processed GLB",
        )
        require(
            _vector_close(item.get("local_scale"), (facts["scale"],) * 3),
            f"PART_{part_id} local scale differs from processed GLB",
        )
        require(
            item.get("motion_kind") == part.pivot.motion.kind,
            f"PART_{part_id} motion kind mismatch",
        )
        if part.pivot.motion.kind != "fixed":
            axis = part.pivot.motion.axis
            require(
                axis is not None
                and _vector_close(
                    item.get("gf_axis"), cast(tuple[float, float, float], tuple(axis))
                ),
                f"PART_{part_id} gf_axis differs from processed GLB",
            )
        require(item.get("lod0_present") is True, f"PART_{part_id} has no verified LOD0")
        require(
            item.get("lod1_present") is facts["lod1"],
            f"PART_{part_id} LOD1 differs from processed GLB",
        )
        require(item.get("lod1_present") is lod1_required, f"PART_{part_id} LOD1 presence mismatch")

    expected_sockets = {socket.socket_id for socket in (spec.sockets or [])}
    sockets = observation.get("sockets_verified")
    if not isinstance(sockets, list):
        raise ValidationError(
            "Godot observation failed independent validation: sockets_verified is not a list"
        )
    actual_sockets = {item.get("socket_id"): item for item in sockets if isinstance(item, dict)}
    require(
        len(actual_sockets) == len(sockets) and actual_sockets.keys() == expected_sockets,
        "socket inventory differs from specification",
    )
    require(
        all(item.get("marker_created") is True for item in actual_sockets.values()),
        "socket Marker3D missing",
    )
    for socket in spec.sockets or []:
        item = actual_sockets[socket.socket_id]
        facts = actual_facts["sockets"][socket.socket_id]
        require(
            item.get("parent_part") == socket.parent_part,
            f"SOCKET_{socket.socket_id} parent mismatch",
        )
        require(
            _vector_close(item.get("local_position"), facts["position"]),
            f"SOCKET_{socket.socket_id} local position differs from processed GLB",
        )
        require(
            _matrix3_close(item.get("local_basis"), facts["basis"]),
            f"SOCKET_{socket.socket_id} local basis differs from processed GLB",
        )
        world = facts["world"]
        expected_position = _matrix_position(world)
        expected_basis = _matrix_rows(world)
        require(
            _vector_close(item.get("world_position"), expected_position),
            f"SOCKET_{socket.socket_id} full-ancestor world position mismatch",
        )
        reported_basis = item.get("world_basis")
        require(
            isinstance(reported_basis, list)
            and len(reported_basis) == 3
            and all(
                _vector_close(row, target)
                for row, target in zip(reported_basis, expected_basis, strict=True)
            ),
            f"SOCKET_{socket.socket_id} full-ancestor world basis mismatch",
        )

    collider = observation.get("collider")
    if not isinstance(collider, dict):
        raise ValidationError(
            "Godot observation failed independent validation: collider observation missing"
        )
    require(spec.collider.policy == "box", "this assembly verifier supports box collider only")
    require(collider.get("shape") == "box", "runtime shape is not a box")
    require(
        collider.get("body_kind") == profile.document.godot.body_kind, "runtime body kind mismatch"
    )
    bounds = collider.get("bounds", {})
    size = bounds.get("size") if isinstance(bounds, dict) else None
    require(_vector_positive(size), "COL rest bounds are empty or invalid")
    expected_bounds = actual_facts["collider_bounds"]
    require(
        _vector_close(bounds.get("position"), expected_bounds["position"]),
        "COL bounds position differs from processed GLB",
    )
    require(
        _vector_close(size, expected_bounds["size"]), "COL bounds size differs from processed GLB"
    )
    if profile.document.godot.require_ray_hit:
        require(collider.get("physics_ray_hit") is True, "required physics ray did not hit")

    moving_parts = {
        part_id for part_id, part in expected_parts.items() if part.pivot.motion.kind != "fixed"
    }
    articulation = observation.get("articulation_results")
    if not isinstance(articulation, list):
        raise ValidationError(
            "Godot observation failed independent validation: articulation_results is not a list"
        )
    actual_motion = {item.get("part_id"): item for item in articulation if isinstance(item, dict)}
    require(
        len(actual_motion) == len(articulation) and actual_motion.keys() == moving_parts,
        "moving PART verification inventory mismatch",
    )
    for moving_part_id, item in actual_motion.items():
        for key in (
            "motion_applied",
            "pivot_world_ok",
            "axis_world_ok",
            "descendants_rigid_ok",
            "descendant_moved",
            "ancestors_siblings_unchanged",
            "restored_ok",
        ):
            require(item.get(key) is True, f"PART_{moving_part_id} articulation {key} failed")

    framing = observation.get("view_framing")
    if not isinstance(framing, dict) or framing.keys() != set(review_views):
        raise ValidationError(
            "Godot observation failed independent validation: view framing set does not match requested review views"
        )
    captures = observation.get("captures")
    if not isinstance(captures, dict) or captures.keys() != set(review_views):
        raise ValidationError(
            "Godot observation failed independent validation: capture binding set does not match requested views"
        )
    policy = profile.framing
    for view in review_views:
        frame = framing[view]
        require(isinstance(frame, dict) and frame.get("ok") is True, f"{view} framing did not pass")
        require(
            frame.get("view_axis") == view_axis_label(view), f"{view} camera axis binding mismatch"
        )
        direction = view_direction(view)
        expected_forward = (-direction[0], -direction[1], -direction[2])
        require(
            _vector_close(frame.get("camera_direction"), expected_forward),
            f"{view} camera direction mismatch",
        )
        expected_up = _camera_up(view, expected_forward)
        require(_vector_close(frame.get("camera_up"), expected_up), f"{view} camera up mismatch")
        ratio = frame.get("height_ratio")
        require(
            isinstance(ratio, (int, float))
            and policy.min_screen_fraction <= ratio <= policy.max_screen_fraction,
            f"{view} framing ratio is outside the profile policy",
        )
        require(
            frame.get("inside_viewport") is True and frame.get("margin_ok") is True,
            f"{view} framing escaped the viewport",
        )
        capture = captures[view]
        require(
            isinstance(capture, dict)
            and re.fullmatch(r"[0-9a-f]{64}", str(capture.get("sha256", ""))) is not None,
            f"{view} capture hash missing",
        )


def _vector_close(
    value: Any, expected: tuple[float, float, float], tolerance: float = 1e-4
) -> bool:
    return (
        isinstance(value, list | tuple)
        and len(value) == 3
        and all(
            isinstance(component, int | float) and abs(float(component) - target) <= tolerance
            for component, target in zip(value, expected, strict=True)
        )
    )


def _matrix3_close(value: Any, expected: _Matrix3, tolerance: float = 1e-4) -> bool:
    return (
        isinstance(value, list | tuple)
        and len(value) == 3
        and all(_vector_close(row, expected[index], tolerance) for index, row in enumerate(value))
    )


def _vector_positive(value: Any) -> bool:
    return (
        isinstance(value, list | tuple)
        and len(value) == 3
        and all(isinstance(component, int | float) and float(component) > 0 for component in value)
    )


def _camera_up(view: str, forward: tuple[float, float, float]) -> tuple[float, float, float]:
    requested = view_up(view)
    projection = sum(a * b for a, b in zip(requested, forward, strict=True))
    planar = tuple(requested[i] - projection * forward[i] for i in range(3))
    length = sum(component * component for component in planar) ** 0.5
    return tuple(component / length for component in planar)


def _quaternion_matrix(quaternion: Any) -> _Matrix3:
    if quaternion == "identity":
        x, y, z, w = 0.0, 0.0, 0.0, 1.0
    else:
        x, y, z, w = (float(component) for component in quaternion)
    return (
        (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
        (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
        (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
    )


def _matmul3(a: _Matrix3, b: _Matrix3) -> _Matrix3:
    return cast(
        _Matrix3,
        tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3)),
    )


def _expected_socket_world(
    spec: AssetSpecificationV07, socket: Any
) -> tuple[tuple[float, float, float], _Matrix3]:
    parts = {part.part_id: part for part in (spec.parts or [])}
    chain: list[Any] = []
    current = parts[socket.parent_part]
    while True:
        chain.append(current)
        if current.parent == "root":
            break
        current = parts[current.parent]
    chain.reverse()
    matrix = _quaternion_matrix("identity")
    position = (0.0, 0.0, 0.0)
    for part in chain:
        local = _quaternion_matrix(part.pivot.basis)
        translated = tuple(
            sum(matrix[row][col] * part.pivot.position_m[col] for col in range(3))
            for row in range(3)
        )
        position = tuple(position[i] + translated[i] for i in range(3))
        matrix = _matmul3(matrix, local)
    local_socket = _quaternion_matrix(socket.rotation)
    translated_socket = tuple(
        sum(matrix[row][col] * socket.translation_m[col] for col in range(3)) for row in range(3)
    )
    position = tuple(position[i] + translated_socket[i] for i in range(3))
    matrix = _matmul3(matrix, local_socket)
    return position, matrix


def verify_godot_assembly(
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    processed_glb: Path | str,
    processed_glb_sha256: str,
    execution_id: str,
    attempt_number: int = 1,
    workflow_id: str = "assembly-verification",
    revision: int = 1,
    output_dir: Path | str | None = None,
    godot_executable: Path | str | None = None,
    runner: ProcessRunner | Any | None = None,
    timeout_seconds: float = 90.0,
    raise_on_failure: bool = True,
) -> AssemblyRuntimeResult:
    """Execute standalone Godot assembly runtime verification.

    Accepts typed V0.7 test-only profile and specification, validates GLB hash,
    stages an isolated Godot environment, executes import and runtime harness,
    verifies observation integrity, and validates rendered PNG captures.
    """
    safe_component = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    if not safe_component.fullmatch(execution_id) or not safe_component.fullmatch(workflow_id):
        raise ValidationError(
            f"execution_id ('{execution_id}') and workflow_id ('{workflow_id}') must be safe single path components"
        )
    if (
        isinstance(attempt_number, bool)
        or not isinstance(attempt_number, int)
        or attempt_number < 1
    ):
        raise ValidationError(f"attempt_number must be >= 1, got {attempt_number}")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise ValidationError(f"revision must be >= 1, got {revision}")

    if type(spec) is not AssetSpecificationV07 or type(profile) is not AssetProfileV07:
        raise ValidationError(
            "verify_godot_assembly requires exact typed V0.7 specification and profile"
        )
    # Geometry mode check: only assembly mode is supported in this slice
    if profile.geometry_mode != "assembly":
        raise ValidationError(
            f"only assembly geometry mode is supported in this slice, got '{profile.geometry_mode}'"
        )
    if not spec.parts:
        raise ValidationError("assembly specification must declare parts")
    if spec.source_kind != "local_operator_assembly":
        raise ValidationError(
            "Godot assembly verification requires source_kind 'local_operator_assembly'"
        )
    try:
        bound_profile = spec.bound_profile()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError(f"assembly specification has no valid bound profile: {exc}") from exc
    if bound_profile != profile:
        raise ValidationError(
            "supplied profile does not exactly match the specification-bound profile"
        )
    try:
        profile.check_specification(spec)
    except ValueError as exc:
        raise ValidationError(
            f"assembly specification is not accepted by profile {profile.qualified}: {exc}"
        ) from exc

    # Validate review views before load
    review_views = _validate_review_views(profile.review_views)

    # Validate processed GLB and rehash input directly
    glb_path = Path(processed_glb)
    if not glb_path.is_file():
        raise ToolExecutionError(
            f"processed_glb file not found: {glb_path}", details={"path": str(glb_path)}
        )

    raw_glb = _read_bounded_regular_file(
        glb_path, limit=_MAX_PROCESSED_GLB_BYTES, label="processed_glb"
    )
    actual_glb_hash = hashlib.sha256(raw_glb).hexdigest()
    if actual_glb_hash != processed_glb_sha256:
        raise ValidationError(
            f"processed_glb SHA-256 mismatch: claimed {processed_glb_sha256}, actual {actual_glb_hash}"
        )
    # Decode and independently validate actual source transforms before any
    # staging or engine dispatch. Runtime observations are compared to these
    # facts, never accepted as their own proof.
    actual_facts = _bounded_processed_facts(raw_glb, spec, profile)

    # Output directory and clobber guard
    if output_dir is None:
        out_dir = Path(
            tempfile.mkdtemp(
                prefix=f"godot-assembly-{execution_id}-a{attempt_number}-{actual_glb_hash[:12]}-"
            )
        )
    else:
        out_dir = Path(output_dir)
        _reject_reparse_components(out_dir, "output_dir")
        try:
            out_dir.mkdir(parents=False, exist_ok=False)
        except FileExistsError as exc:
            raise ValidationError(
                f"output_dir must be a fresh, attempt-specific directory; refusing to reuse: {out_dir}"
            ) from exc
        except OSError as exc:
            raise ValidationError(
                f"output_dir parent must exist and be writable: {out_dir}: {exc}"
            ) from exc

    observation_path = out_dir / "runtime-observation.json"

    # Locate Godot executable
    adapter = GodotAdapter(runner=runner)
    godot_exe: str | None = None
    if godot_executable is not None:
        p = Path(godot_executable).resolve()
        if not p.is_file():
            raise ToolExecutionError(
                f"specified Godot executable not found: {godot_executable}",
                details={"path": str(godot_executable)},
            )
        godot_exe = str(p)
    else:
        env_godot = os.environ.get("GAMEFACTORY_TEST_GODOT") or os.environ.get(
            "GAMEFACTORY_GODOT_PATH"
        )
        if env_godot and Path(env_godot).is_file():
            godot_exe = str(Path(env_godot).resolve())
        else:
            godot_exe = adapter.find_candidate_executable()

    if godot_exe is None:
        raise ToolExecutionError(
            "Godot executable not found. Configure GAMEFACTORY_TEST_GODOT or ensure Godot is on PATH"
        )

    # Create isolated stage
    stage_dir = out_dir / ".stage"
    stage_dir.mkdir(parents=True, exist_ok=False)

    # Stage project.godot with node suffixes disabled so COL_ proxies are preserved
    project_godot = """config_version=5

[importer_defaults]

scene={
"nodes/use_name_suffixes": false,
"nodes/use_node_type_suffixes": false
}
"""
    (stage_dir / "project.godot").write_text(project_godot, encoding="utf-8")

    harness_bytes = _load_harness_resource()
    harness_sha256 = hashlib.sha256(harness_bytes).hexdigest()
    # Copy the exact hashed bytes; the observation is bound to this tool as well
    # as the exact canonical profile and specification configuration.
    (stage_dir / ".factory-harness.gd").write_bytes(harness_bytes)

    # Copy GLB to stage
    staged_glb = stage_dir / "asset.glb"
    staged_glb.write_bytes(raw_glb)
    dispatch_glb = _read_bounded_regular_file(
        staged_glb, limit=_MAX_PROCESSED_GLB_BYTES, label="staged processed_glb"
    )
    if hashlib.sha256(dispatch_glb).hexdigest() != actual_glb_hash or dispatch_glb != raw_glb:
        raise ValidationError("staged processed_glb changed before Godot dispatch")

    # Build request dictionary
    spec_dict = spec.model_dump(mode="json")
    profile_dict = profile.document.model_dump(mode="json")
    profile_sha256 = hashlib.sha256(
        json.dumps(profile_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    specification_sha256 = hashlib.sha256(
        json.dumps(spec_dict, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
            "utf-8"
        )
    ).hexdigest()
    assembly_conf = profile_dict.get("assembly", {}) or {}

    request_payload: dict[str, Any] = {
        "schema_version": REQUEST_SCHEMA_VERSION_V07,
        "workflow_id": workflow_id,
        "revision": revision,
        "asset_id": spec.asset_id,
        "execution_id": execution_id,
        "attempt_number": attempt_number,
        "glb": "res://asset.glb",
        "processed_glb_sha256": actual_glb_hash,
        "harness_sha256": harness_sha256,
        "profile_sha256": profile_sha256,
        "specification_sha256": specification_sha256,
        "output_dir": str(out_dir.resolve()),
        "observation_path": str(observation_path.resolve()),
        "review_views": review_views,
        "spec": {
            "dimensions": spec_dict.get("dimensions", {}),
            "origin_policy": spec_dict.get("origin_policy", "bottom_center"),
            "collider": spec_dict.get("collider", {}),
            "parts": spec_dict.get("parts", []),
            "sockets": spec_dict.get("sockets", []),
        },
        "profile": {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "geometry_mode": profile.geometry_mode,
            "body_kind": profile.document.godot.body_kind,
            "require_ray_hit": profile.document.godot.require_ray_hit,
            "require_area": profile.document.godot.require_area,
            "lod1_required": spec.lod_policy == "lod0_lod1"
            or profile.document.processing.lod1_required,
            "framing": profile_dict.get("framing", {}),
            "pivot_tolerance_m": assembly_conf.get("pivot_tolerance_m", 0.01),
            "basis_tolerance_deg": assembly_conf.get("basis_tolerance_deg", 1.0),
            "socket_position_tolerance_m": assembly_conf.get("socket_position_tolerance_m", 0.01),
            "socket_angle_tolerance_deg": assembly_conf.get("socket_angle_tolerance_deg", 1.0),
        },
    }

    # Bind request digest
    canonical_request_bytes = json.dumps(
        request_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    request_digest = hashlib.sha256(canonical_request_bytes).hexdigest()
    request_payload["request_digest"] = request_digest

    staged_request_bytes = json.dumps(
        request_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    request_path = stage_dir / "runtime-request.json"
    request_path.write_bytes(staged_request_bytes)

    # Process execution
    proc_runner = runner or ProcessRunner(sanitize_output=True)
    scratch_dir = stage_dir / "scratch"
    scratch_dir.mkdir(parents=True, exist_ok=True)
    env = _isolated_environment(scratch_dir)

    # Phase 1: Headless project import
    import_cmd = CommandRequest(
        args=[godot_exe, "--headless", "--path", str(stage_dir), "--import"],
        cwd=stage_dir,
        env_overrides=env,
        timeout_seconds=min(timeout_seconds, 60.0),
        minimal_env=True,
    )
    import_res = proc_runner.run(import_cmd)
    if import_res.exit_code != 0:
        raise ToolExecutionError(
            "Godot project import failed",
            exit_code=import_res.exit_code,
            stderr=import_res.stderr,
            details={"stdout": import_res.stdout, "stderr": import_res.stderr},
        )

    # Phase 2: Runtime harness execution with display driver for real rendering
    display = _display_driver()
    runtime_args = [
        godot_exe,
        "--path",
        str(stage_dir),
        "--display-driver",
        display,
        "--rendering-driver",
        "opengl3",
        "--rendering-method",
        "gl_compatibility",
        "--audio-driver",
        "Dummy",
        "--windowed",
        "--resolution",
        "1280x720",
        "--position",
        "40,40",
        "--script",
        "res://.factory-harness.gd",
        "--",
        "--request",
        str(request_path.resolve()),
    ]
    runtime_cmd = CommandRequest(
        args=runtime_args,
        cwd=stage_dir,
        env_overrides=env,
        timeout_seconds=timeout_seconds,
        minimal_env=True,
    )
    runtime_res = proc_runner.run(runtime_cmd)

    # Validate observation output
    if not observation_path.exists():
        raise ToolExecutionError(
            "Godot assembly verification exited without writing an observation report",
            exit_code=runtime_res.exit_code,
            stderr=runtime_res.stderr,
            details={"stdout": runtime_res.stdout, "stderr": runtime_res.stderr},
        )

    raw_obs = _read_bounded_regular_file(
        observation_path, limit=_MAX_OBSERVATION_BYTES, label="Godot observation output"
    )

    try:
        observation_dict = json.loads(
            raw_obs.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda val: (_ for _ in ()).throw(
                ValueError(f"invalid JSON constant: {val}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(f"Godot observation is not valid UTF-8 JSON: {exc}") from exc

    if not isinstance(observation_dict, dict):
        raise ValidationError("Godot observation root must be a JSON object")

    # Exact runtime binding checks
    if observation_dict.get("schema_version") != OBSERVATION_SCHEMA_VERSION_V07:
        raise ValidationError(
            f"Observation schema_version mismatch: expected {OBSERVATION_SCHEMA_VERSION_V07}, got {observation_dict.get('schema_version')}"
        )
    _validate_observation(
        observation_dict,
        spec=spec,
        profile=profile,
        review_views=review_views,
        processed_hash=actual_glb_hash,
        revision=revision,
        workflow_id=workflow_id,
        execution_id=execution_id,
        attempt_number=attempt_number,
        request_digest=request_digest,
        actual_facts=actual_facts,
        harness_sha256=harness_sha256,
        profile_sha256=profile_sha256,
        specification_sha256=specification_sha256,
    )

    status = str(observation_dict.get("status", "FAIL"))
    errors = observation_dict.get("errors", [])
    if not isinstance(errors, list):
        errors = [str(errors)]

    artifacts: dict[str, bytes] = {
        "runtime-request.json": staged_request_bytes,
        "runtime-observation.json": raw_obs,
        "asset_runtime_harness_v07.gd": harness_bytes,
    }
    captures: dict[str, bytes] = {}

    # If verification failed
    if runtime_res.exit_code != 0 or status != "PASS" or errors:
        if raise_on_failure:
            raise ToolExecutionError(
                f"Godot assembly runtime verification failed: {errors}",
                exit_code=runtime_res.exit_code,
                stderr=runtime_res.stderr,
                details={
                    "observation": observation_dict,
                    "errors": errors,
                    "stdout": runtime_res.stdout,
                },
            )
        return AssemblyRuntimeResult(
            status="FAIL",
            observation=observation_dict,
            artifacts=artifacts,
            findings=[str(e) for e in errors],
            request_digest=request_digest,
            processed_glb_sha256=actual_glb_hash,
            captures={},
        )

    # Decode and validate rendered PNG captures
    for v in review_views:
        png_path = out_dir / f"{v}.png"
        if not png_path.exists():
            raise ToolExecutionError(f"Expected review capture missing: {png_path.name}")
        png_bytes = _read_bounded_regular_file(
            png_path, limit=4 * 1024 * 1024, label=f"capture {v}"
        )
        decoded = decode_png(png_bytes, 1280, 720)
        expected_capture = observation_dict["captures"][v]
        if hashlib.sha256(png_bytes).hexdigest() != expected_capture["sha256"]:
            raise ValidationError(f"Rendered capture hash mismatch for view '{v}'")
        colors = decoded.image.convert("RGB").getcolors(maxcolors=2)
        if decoded.image.getbbox() is None or (colors is not None and len(colors) <= 1):
            raise ValidationError(f"Rendered capture for view '{v}' is blank or uniform")
        artifacts[f"{v}.png"] = png_bytes
        captures[v] = png_bytes

    return AssemblyRuntimeResult(
        status="PASS",
        observation=observation_dict,
        artifacts=artifacts,
        findings=[],
        request_digest=request_digest,
        processed_glb_sha256=actual_glb_hash,
        captures=captures,
    )
