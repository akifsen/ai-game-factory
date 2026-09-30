"""Bounded standalone V0.7 authored-assembly Blender processing adapter.

Coordinates background Blender execution for authored multi-part assemblies:
- Strictly enforces V0.7 typed contracts and authenticated pinned provenance
- Verifies retained source package bytes before DCC invocation
- Derives source_front from verified provenance (never arbitrary caller report)
- Rejects provider_generated, single_mesh, and non-V07 specifications before DCC
- Enforces exact identity ROOT and direct root PART child topology
- Normalizes root PART by Ry(+180Y) if and only if source_front == "+Z"
- Proves exact preservation via outside-Blender preservation oracle and triangle multiset proof
- Generates per-part LOD1 and root box collider COL_{asset_id} under strict budgets
- Returns typed immutable AssemblyProcessResult including VerifiedSourceNormalization
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shlex
import stat
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from importlib.resources import files
from pathlib import Path
from typing import Any, Literal

from gamefactory.adapters.assets.assembly_ingest import verify_retained_assembly
from gamefactory.adapters.assets.glb_validator import (
    _inspect,
    _InvalidGLB,
    _node_matrix,
    _read_glb_bytes,
)
from gamefactory.adapters.assets.v07_geometry_validation import (
    VerifiedSourceNormalization,
    _matrix_is_identity,
    _triangle_soup,
    box_mesh_geometry_matches,
    triangle_soups_equivalent,
    verify_source_to_processed_preservation,
)
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.blender_environment import (
    BLENDER_PYTHON_FAILURE_EXIT_CODE,
    BLENDER_PYTHONPATH_ENV,
    BlenderDependencyPreflight,
    blender_env_overrides,
    format_blender_preflight_failure_message,
    parse_blender_python_paths,
    run_blender_dependency_preflight,
)
from gamefactory.core.domain.assembly_source import AssemblyIngestResult
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.asset_profiles import AssetProfileV07
from gamefactory.core.domain.errors import DccFailedError, ValidationError
from gamefactory.core.domain.models import utc_now_iso
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner

_INLINE_LOG_CHARS = 16_000
_MAX_CONTRACT_BYTES = 1024 * 1024
_MAX_REPORT_BYTES = 1024 * 1024


@dataclass(frozen=True)
class _OwnedFileIdentity:
    device: int
    inode: int
    size: int
    sha256: str


def _absolute_lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_linked_components(path: Path, label: str) -> None:
    current = path
    while True:
        is_junction = getattr(current, "is_junction", lambda: False)
        try:
            metadata = current.lstat()
            reparse = bool(
                getattr(metadata, "st_reparse_tag", 0)
                or getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        except OSError:
            reparse = False
        if current.is_symlink() or is_junction() or reparse:
            raise ValueError(f"{label} path traverses a symlink or junction: {current}")
        if current.parent == current:
            break
        current = current.parent


def _read_bounded_regular_file(path: Path, label: str, max_bytes: int) -> bytes:
    try:
        _reject_linked_components(path, label)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise DccFailedError(f"{label} must be a regular file: {path}")
        with path.open("rb") as stream:
            contents = stream.read(max_bytes + 1)
        after = path.lstat()
    except DccFailedError:
        raise
    except ValueError as exc:
        raise DccFailedError(f"{label} path is unsafe: {exc}") from exc
    except OSError as exc:
        raise DccFailedError(f"Cannot read {label}: {exc}") from exc
    if len(contents) > max_bytes:
        raise DccFailedError(f"{label} exceeds the {max_bytes}-byte limit")
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise DccFailedError(f"{label} changed during read")
    return contents


def _read_bounded_json_object(path: Path, label: str, max_bytes: int) -> dict[str, Any]:
    contents = _read_bounded_regular_file(path, label, max_bytes)
    try:
        value = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise DccFailedError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DccFailedError(f"{label} must contain a JSON object")
    return value


def _capture_owned_file(path: Path, expected_bytes: bytes) -> _OwnedFileIdentity:
    metadata = path.lstat()
    contents = _read_bounded_regular_file(path, "processing contract", _MAX_CONTRACT_BYTES)
    after = path.lstat()
    digest = hashlib.sha256(expected_bytes).hexdigest()
    if contents != expected_bytes:
        raise DccFailedError("Processing contract changed immediately after exclusive creation")
    if (metadata.st_dev, metadata.st_ino) != (
        after.st_dev,
        after.st_ino,
    ) or metadata.st_size != len(expected_bytes):
        raise DccFailedError(
            "Processing contract size changed immediately after exclusive creation"
        )
    return _OwnedFileIdentity(metadata.st_dev, metadata.st_ino, metadata.st_size, digest)


def _remove_owned_file_if_unchanged(path: Path, identity: _OwnedFileIdentity) -> None:
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            identity.device,
            identity.inode,
        ):
            return
        contents = _read_bounded_regular_file(
            path, "owned processing contract", _MAX_CONTRACT_BYTES
        )
        after = path.lstat()
        if (
            (after.st_dev, after.st_ino) == (identity.device, identity.inode)
            and len(contents) == identity.size
            and hashlib.sha256(contents).hexdigest() == identity.sha256
        ):
            path.unlink()
    except (DccFailedError, OSError):
        # If the artifact was replaced, mutated, or became a link, leave it for its owner.
        return


def _serialize_contract(payload: dict[str, Any]) -> bytes:
    encoder = json.JSONEncoder(sort_keys=True, indent=2)
    contents = bytearray()
    for text in encoder.iterencode(payload):
        encoded = text.encode("utf-8")
        if len(contents) + len(encoded) + 1 > _MAX_CONTRACT_BYTES:
            raise ValidationError(
                f"Serialized Blender processing contract exceeds {_MAX_CONTRACT_BYTES} bytes"
            )
        contents.extend(encoded)
    contents.extend(b"\n")
    return bytes(contents)


def _command_display(args: list[str]) -> str:
    return " ".join(shlex.quote(arg) for arg in args)


def _blender_version_line(stdout: str) -> str:
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Blender "):
            return stripped
    return "(not present in captured stdout)"


def _prefer_traceback(text: str) -> tuple[str, bool]:
    if not text:
        return "(empty)", False
    if len(text) <= _INLINE_LOG_CHARS:
        return text, False
    marker = text.find("Traceback (most recent call last):")
    note = "[inline excerpt; full captured text is in the diagnostics artifact]"
    if marker != -1:
        tail = text[marker:]
        if len(tail) <= _INLINE_LOG_CHARS:
            return f"...{note}...\n{tail}", True
        return f"{tail[:_INLINE_LOG_CHARS]}\n...{note}...\n", True
    head = 2_000
    tail = text[-(_INLINE_LOG_CHARS - head) :]
    return f"{text[:head]}\n...{note}...\n{tail}", True


def _artifact_note(path: Path) -> str:
    if path.is_file():
        return f"path={path} exists=true size={path.stat().st_size} absolute={path.is_absolute()}"
    return f"path={path} exists=false size=0 absolute={path.is_absolute()}"


def _environment_note(
    runner: ProcessRunner,
    request: CommandRequest,
    python_paths: Sequence[Path | str] = (),
) -> str:
    build_env = getattr(runner, "build_env", None)
    overrides = len(request.env_overrides)
    paths_str = os.pathsep.join(str(p) for p in python_paths) if python_paths else "(none)"
    if not callable(build_env):
        return (
            f"minimal_env={str(request.minimal_env).lower()}; "
            f"env_overrides_count={overrides}; keys=(unavailable); "
            f"blender_python_paths={paths_str}"
        )
    keys = ",".join(sorted(str(key) for key in build_env(request)))
    return (
        f"minimal_env={str(request.minimal_env).lower()}; "
        f"env_overrides_count={overrides}; keys={keys or '(none)'}; "
        f"blender_python_paths={paths_str}"
    )


def _write_diagnostics(path: Path, body: str) -> str:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            handle.write(body)
    except OSError as exc:
        return f"(diagnostics artifact not written: {exc})"
    return str(path)


def _blender_failure(
    summary: str,
    res: CommandResult,
    request: CommandRequest,
    runner: ProcessRunner,
    *,
    script_file: Path,
    raw_glb: Path,
    processed_glb: Path,
    contract_path: Path,
    diagnostics_path: Path,
    asset_id: str,
    python_paths: Sequence[Path | str] = (),
) -> DccFailedError:
    try:
        contract_text = _read_bounded_regular_file(
            contract_path, "processing contract diagnostics", _MAX_CONTRACT_BYTES
        ).decode("utf-8")
    except (DccFailedError, UnicodeDecodeError):
        contract_text = "(contract unavailable or over bounded diagnostic limit)"
    stdout_view, stdout_excerpted = _prefer_traceback(res.stdout)
    stderr_view, stderr_excerpted = _prefer_traceback(res.stderr)
    contract_view, contract_excerpted = _prefer_traceback(contract_text)
    parent = processed_glb.parent
    notes: list[str] = []
    if res.stdout_truncated:
        notes.append("stdout capture hit the process runner bound")
    if res.stderr_truncated:
        notes.append("stderr capture hit the process runner bound")
    if stdout_excerpted or stderr_excerpted or contract_excerpted:
        notes.append("inline message is an excerpt")
    capture = "; ".join(notes) if notes else "captured logs fit in this message"
    header = [
        summary,
        f"blender_version: {_blender_version_line(res.stdout)}",
        f"executable: {request.args[0] if request.args else ''}",
        f"command: {_command_display(list(request.args))}",
        f"cwd: {request.cwd}",
        f"exit_code: {res.exit_code}",
        f"processing_script: {script_file}",
        f"input_glb: {_artifact_note(raw_glb)}",
        f"output_glb: {_artifact_note(processed_glb)}",
        (
            f"output_parent: path={parent} exists={parent.is_dir()} "
            f"writable={os.access(parent, os.W_OK)}"
        ),
        f"processing_contract: {contract_path}",
        f"environment: {_environment_note(runner, request, python_paths)}",
        f"capture: {capture}",
    ]
    full_body = "\n".join(
        [
            *header,
            "--- processing contract ---",
            contract_text or "(empty)",
            "--- stdout ---",
            res.stdout or "(empty)",
            "--- stderr ---",
            res.stderr or "(empty)",
        ]
    )
    artifact = _write_diagnostics(diagnostics_path, full_body + "\n")
    message = "\n".join(
        [
            *header,
            "--- processing contract ---",
            contract_view,
            "--- stdout ---",
            stdout_view,
            "--- stderr ---",
            stderr_view,
            f"diagnostics_artifact: {artifact}",
        ]
    )
    return DccFailedError(
        message,
        exit_code=res.exit_code,
        stderr=res.stderr,
        details={
            "stdout": res.stdout,
            "asset_id": asset_id,
            "command": list(request.args),
            "cwd": str(request.cwd),
            "processing_script": str(script_file),
            "input_glb": str(raw_glb),
            "output_glb": str(processed_glb),
            "processing_contract": str(contract_path),
            "diagnostics_artifact": artifact,
            "stdout_truncated": res.stdout_truncated,
            "stderr_truncated": res.stderr_truncated,
            "blender_version": _blender_version_line(res.stdout),
            "blender_python_paths": [str(p) for p in python_paths],
        },
    )


def _prove_processed_assembly_glb(
    processed_glb_path: Path,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    verified_norm: VerifiedSourceNormalization,
    source_bytes: bytes,
    max_file_size_bytes: int = 50 * 1024 * 1024,
) -> dict[str, Any]:
    """Execute independent outside-Blender proof on processed GLB bytes.

    Validates:
    - glTF 2.0 structure, single scene, exact single identity ROOT node
    - Exact child topology and direct identity LOD0 mesh nodes
    - Per-part LOD1 (if required) under same parent PART node
    - Exact identity COL_{asset_id} root box collider spanning rest bounds
    - Exact LOD0 triangle soup match with source for each part (multiset with winding)
      (Detects shape/equal AABB tampering!)
    - Budgets: LOD0 triangles, LOD1 triangles, materials, texture dimensions
    - Decoded actual visual rest bounds match spec dimensions and origin policy
    """
    if max_file_size_bytes < 1:
        raise ValueError("maximum GLB size must be positive")
    try:
        with processed_glb_path.open("rb") as stream:
            proc_bytes = stream.read(max_file_size_bytes + 1)
    except OSError as exc:
        raise DccFailedError(f"Cannot read processed GLB: {exc}") from exc
    if len(proc_bytes) > max_file_size_bytes:
        raise DccFailedError(
            f"Processed GLB exceeds maximum allowed size of {max_file_size_bytes} bytes"
        )
    if hashlib.sha256(proc_bytes).hexdigest() != verified_norm.processed_sha256:
        raise DccFailedError("Processed GLB hash does not match the preservation observation")
    if (
        source_bytes != verified_norm.source_glb_bytes
        or hashlib.sha256(source_bytes).hexdigest() != verified_norm.source_sha256
    ):
        raise DccFailedError("Source GLB bytes do not match the preservation observation")
    try:
        proc_doc, proc_bin = _read_glb_bytes(proc_bytes, max_file_size_bytes)
        proc_meshes, _, _, proc_textures, _, _ = _inspect(proc_doc, proc_bin)
        src_doc, src_bin = _read_glb_bytes(source_bytes, max_file_size_bytes)
        src_meshes, _, _, _, _, _ = _inspect(src_doc, src_bin)
    except (OSError, _InvalidGLB, KeyError, TypeError, ValueError, IndexError) as exc:
        raise DccFailedError(f"Processed GLB structural decode failed: {exc}") from exc

    nodes = proc_doc.get("nodes", [])
    scenes = proc_doc.get("scenes", [])
    active_scene = proc_doc.get("scene", 0)
    if not scenes or not (0 <= active_scene < len(scenes)):
        raise DccFailedError("Processed GLB missing valid active scene")
    scene_nodes = scenes[active_scene].get("nodes", [])
    if len(scene_nodes) != 1:
        raise DccFailedError(
            f"Active scene must contain exactly 1 node (ROOT), found {len(scene_nodes)}"
        )
    root_idx = scene_nodes[0]
    root_node = nodes[root_idx]
    if root_node.get("name") != "ROOT" or not _matrix_is_identity(_node_matrix(root_node)):
        raise DccFailedError("Processed GLB active root node must be an identity 'ROOT' node")

    node_by_name = {str(n.get("name", "")): (i, n) for i, n in enumerate(nodes)}
    parent_by_index: dict[int, int] = {}
    for parent, n in enumerate(nodes):
        for child in n.get("children", []):
            parent_by_index[child] = parent

    spec_parts = {p.part_id: p for p in (spec.parts or [])}
    spec_sockets = {s.socket_id: s for s in (spec.sockets or [])}
    lod1_required = bool(
        profile.document.processing.lod1_required or spec.lod_policy == "lod0_lod1"
    )

    for part_id in spec_parts:
        lod1_name = f"SM_{spec.asset_id}_{part_id}_LOD1"
        entry = node_by_name.get(lod1_name)
        if lod1_required:
            part_entry = node_by_name.get(f"PART_{part_id}")
            if entry is None or part_entry is None:
                raise DccFailedError(f"Required per-part LOD1 '{lod1_name}' is missing")
            lod1_index, lod1_node = entry
            if parent_by_index.get(lod1_index) != part_entry[0]:
                raise DccFailedError(f"LOD1 '{lod1_name}' must be a direct child of its PART node")
            if not _matrix_is_identity(_node_matrix(lod1_node)):
                raise DccFailedError(f"LOD1 '{lod1_name}' local transform must be identity")
            lod1_mesh = next((mesh for mesh in proc_meshes if mesh.name == lod1_name), None)
            if lod1_mesh is None or lod1_mesh.triangle_count <= 0:
                raise DccFailedError(f"Required LOD1 '{lod1_name}' must contain triangles")
        elif entry is not None:
            raise DccFailedError(f"Unexpected LOD1 '{lod1_name}' for an LOD0-only specification")

    # Verify all sockets are present under declared parent parts
    for sid, s in spec_sockets.items():
        sname = f"SOCKET_{sid}"
        if sname not in node_by_name:
            raise DccFailedError(f"Required socket '{sname}' missing from processed GLB")
        s_idx, _ = node_by_name[sname]
        expected_parent_name = f"PART_{s.parent_part}"
        if expected_parent_name not in node_by_name:
            raise DccFailedError(f"Parent '{expected_parent_name}' for socket '{sname}' missing")
        expected_parent_idx, _ = node_by_name[expected_parent_name]
        if parent_by_index.get(s_idx) != expected_parent_idx:
            raise DccFailedError(
                f"Socket '{sname}' parent in processed GLB does not match declared parent"
            )

    # Verify each part's LOD0 triangle soup against source (preserving winding/shape)
    for pid in spec_parts:
        mesh_name = f"SM_{spec.asset_id}_{pid}_LOD0"
        src_soup = _triangle_soup(src_doc, src_bin, mesh_name)
        proc_soup = _triangle_soup(proc_doc, proc_bin, mesh_name)
        if not src_soup or not proc_soup:
            raise DccFailedError(f"Nonempty LOD0 triangle soup required for part '{pid}'")
        if not triangle_soups_equivalent(src_soup, proc_soup, tolerance=1e-5):
            raise DccFailedError(
                f"LOD0 triangle multiset mismatch for part '{pid}': source triangles were altered; "
                f"equal AABB is not sufficient proof of preservation"
            )

    # Verify collider COL_{asset_id}
    col_name = f"COL_{spec.asset_id}"
    if col_name not in node_by_name:
        raise DccFailedError(f"Required root box collider '{col_name}' missing from processed GLB")
    col_idx, col_node = node_by_name[col_name]
    if parent_by_index.get(col_idx) != root_idx:
        raise DccFailedError(f"Collider '{col_name}' parent must be ROOT")
    if not _matrix_is_identity(_node_matrix(col_node)):
        raise DccFailedError(f"Collider '{col_name}' local transform must be identity")

    col_mesh_info = next((m for m in proc_meshes if m.name == col_name), None)
    unique_corners = (
        {tuple(round(float(value), 6) for value in point) for point in col_mesh_info.points}
        if col_mesh_info is not None
        else set()
    )
    if col_mesh_info is None or len(unique_corners) != 8 or col_mesh_info.triangle_count != 12:
        raise DccFailedError(
            f"Collider '{col_name}' must have 8 distinct box corners and 12 triangles, "
            f"got {len(col_mesh_info.points) if col_mesh_info else 0} points, "
            f"{col_mesh_info.triangle_count if col_mesh_info else 0} triangles"
        )

    col_min: tuple[float, float, float] = tuple(
        min(point[axis] for point in col_mesh_info.points) for axis in range(3)
    )  # type: ignore[assignment]
    col_max: tuple[float, float, float] = tuple(
        max(point[axis] for point in col_mesh_info.points) for axis in range(3)
    )  # type: ignore[assignment]
    if not box_mesh_geometry_matches(
        proc_doc,
        proc_bin,
        col_name,
        col_mesh_info.points,
        col_min,
        col_max,
        1e-5,
    ):
        raise DccFailedError(f"Collider '{col_name}' is not a complete, nondegenerate box mesh")

    # Visual points and rest bounds
    visual_points = [
        p
        for m in proc_meshes
        if m.name.startswith(f"SM_{spec.asset_id}_") and m.name.endswith("_LOD0")
        for p in m.points
    ]
    if not visual_points:
        raise DccFailedError("No visual LOD0 points found in processed GLB")

    vmin = tuple(min(p[i] for p in visual_points) for i in range(3))
    vmax = tuple(max(p[i] for p in visual_points) for i in range(3))
    cmin = tuple(min(p[i] for p in col_mesh_info.points) for i in range(3))
    cmax = tuple(max(p[i] for p in col_mesh_info.points) for i in range(3))

    bounds_tolerance = profile.document.processing.dimension_tolerance_m
    if not all(
        abs(vmin[i] - cmin[i]) <= bounds_tolerance and abs(vmax[i] - cmax[i]) <= bounds_tolerance
        for i in range(3)
    ):
        raise DccFailedError(
            f"Root collider bounds mismatch: visual rest bounds ({vmin}, {vmax}), collider ({cmin}, {cmax})"
        )

    # Budget checks
    lod0_triangles = sum(
        m.triangle_count
        for m in proc_meshes
        if m.name.startswith(f"SM_{spec.asset_id}_") and m.name.endswith("_LOD0")
    )
    if lod0_triangles > spec.geometry_budget.max_triangles_lod0:
        raise DccFailedError(
            f"Processed LOD0 triangle count {lod0_triangles} exceeds budget {spec.geometry_budget.max_triangles_lod0}"
        )

    lod1_triangles = sum(
        m.triangle_count
        for m in proc_meshes
        if m.name.startswith(f"SM_{spec.asset_id}_") and m.name.endswith("_LOD1")
    )
    if lod1_required and lod1_triangles > spec.geometry_budget.max_triangles_lod1:
        raise DccFailedError(
            f"Processed LOD1 triangle count {lod1_triangles} exceeds budget {spec.geometry_budget.max_triangles_lod1}"
        )

    materials_count = len(proc_doc.get("materials", []))
    materials_limit = min(
        spec.material_budget.max_materials, profile.document.processing.max_materials
    )
    if materials_count > materials_limit:
        raise DccFailedError(
            f"Processed materials count {materials_count} exceeds budget {materials_limit}"
        )
    texture_max_dimension = max((max(width, height) for width, height in proc_textures), default=0)
    texture_limit = min(
        spec.texture_budget.max_dimension, profile.document.processing.max_texture_dimension
    )
    if texture_max_dimension > texture_limit:
        raise DccFailedError(
            f"Processed texture dimension {texture_max_dimension} exceeds budget {texture_limit}"
        )

    # Measure visual dimensions and origin
    vdim = tuple(vmax[i] - vmin[i] for i in range(3))
    tol = profile.document.processing.dimension_tolerance_m
    expected_dim = (spec.dimensions.width_m, spec.dimensions.height_m, spec.dimensions.depth_m)
    if not all(abs(vdim[i] - expected_dim[i]) <= tol for i in range(3)):
        raise DccFailedError(
            f"Processed visual dimensions {vdim} do not match specification {expected_dim} within {tol}m"
        )

    origin_ok = abs((vmin[0] + vmax[0]) / 2) <= tol and abs((vmin[2] + vmax[2]) / 2) <= tol
    if spec.origin_policy == "bottom_center":
        origin_ok = origin_ok and abs(vmin[1]) <= tol
    else:
        origin_ok = origin_ok and abs((vmin[1] + vmax[1]) / 2) <= tol
    if not origin_ok:
        raise DccFailedError(
            f"Processed visual origin does not match origin policy '{spec.origin_policy}': bounds=({vmin}, {vmax})"
        )

    return {
        "lod0_triangles": lod0_triangles,
        "lod1_triangles": lod1_triangles if lod1_required else None,
        "materials_count": materials_count,
        "texture_max_dimension": texture_max_dimension,
        "bounds_min": list(vmin),
        "bounds_max": list(vmax),
        "dimensions": list(vdim),
        "per_part_metrics": {
            part_id: {
                "lod0_triangles": next(
                    mesh.triangle_count
                    for mesh in proc_meshes
                    if mesh.name == f"SM_{spec.asset_id}_{part_id}_LOD0"
                ),
                "lod1_triangles": (
                    next(
                        mesh.triangle_count
                        for mesh in proc_meshes
                        if mesh.name == f"SM_{spec.asset_id}_{part_id}_LOD1"
                    )
                    if lod1_required
                    else None
                ),
            }
            for part_id in spec_parts
        },
    }


@dataclass(frozen=True)
class AssemblyProcessResult:
    """Immutable value object representing successful, verified V0.7 assembly processing.

    Anchors proof across pinned source provenance, Blender background execution,
    the outside-Blender preservation oracle, and the verified normalization observation.
    """

    status: Literal["SUCCESS"]
    exit_code: int
    duration_seconds: float
    blender_version: str
    script_sha256: str
    source_glb_sha256: str
    provenance_sha256: str
    processed_glb_sha256: str
    spec_fingerprint: str
    source_front: Literal["-Z", "+Z"]
    verified_normalization: VerifiedSourceNormalization
    processed_glb_path: Path
    report_path: Path
    report_data: dict[str, Any]
    part_metrics: dict[str, Any]
    processed_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        # Detach caller-visible nested report data from the retained proof result.
        object.__setattr__(self, "report_data", _freeze_json(self.report_data))
        object.__setattr__(self, "part_metrics", _freeze_json(self.part_metrics))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "blender_version": self.blender_version,
            "script_sha256": self.script_sha256,
            "source_glb_sha256": self.source_glb_sha256,
            "provenance_sha256": self.provenance_sha256,
            "processed_glb_sha256": self.processed_glb_sha256,
            "spec_fingerprint": self.spec_fingerprint,
            "source_front": self.source_front,
            "verified_normalization": {
                "source_sha256": self.verified_normalization.source_sha256,
                "processed_sha256": self.verified_normalization.processed_sha256,
                "source_front": self.verified_normalization.source_front,
                "normalization_applied": self.verified_normalization.normalization_applied,
                "root_rotation_xyzw": list(self.verified_normalization.root_rotation_xyzw),
            },
            "processed_glb_path": str(self.processed_glb_path),
            "report_path": str(self.report_path),
            "report_data": _thaw_json(self.report_data),
            "part_metrics": _thaw_json(self.part_metrics),
            "processed_at": self.processed_at,
        }


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        from types import MappingProxyType

        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: Any) -> Any:
    if isinstance(value, dict) or hasattr(value, "items"):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


class AssemblyProcessor:
    """Manages deterministic Blender background processing for V0.7 multi-part assemblies."""

    def __init__(
        self,
        blender_executable: Path | str | None = None,
        runner: ProcessRunner | None = None,
        python_paths: Sequence[Path | str] | None = None,
        dependency_preflight: bool = True,
    ) -> None:
        self.runner = runner or ProcessRunner(sanitize_output=True)
        if blender_executable:
            self.blender_exe = str(Path(blender_executable).resolve())
        else:
            detect = BlenderAdapter(self.runner).detect_tool()
            if not detect.available or not detect.executable_path:
                raise DccFailedError(
                    "Blender executable not available or detected on host",
                    details={"status": detect.status, "reason": detect.details.get("reason")},
                )
            self.blender_exe = detect.executable_path

        if python_paths is None:
            raw_env = os.environ.get(BLENDER_PYTHONPATH_ENV)
            self.python_paths = parse_blender_python_paths(raw_env)
        else:
            self.python_paths = parse_blender_python_paths(python_paths)

        self.dependency_preflight = dependency_preflight
        self.last_dependency_preflight: BlenderDependencyPreflight | None = None
        self._cached_preflight: BlenderDependencyPreflight | None = None

    @staticmethod
    def get_script_path() -> Path:
        """Resolve path to the embedded Blender assembly processing script."""
        pkg_file = files("gamefactory").joinpath("resources/blender/process_assembly.py")
        return Path(str(pkg_file)).resolve()

    def process_assembly(
        self,
        package: Path | str | AssemblyIngestResult,
        spec: AssetSpecificationV07,
        *,
        expected_provenance_sha256: str,
        processed_glb_path: Path | str,
        report_path: Path | str | None = None,
        lod1_ratio: float | None = None,
        timeout_seconds: float = 60.0,
    ) -> AssemblyProcessResult:
        """Run bounded Blender assembly processing and verify preservation.

        Requires mandatory expected_provenance_sha256 pin before processing.
        Rejects provider_generated, single_mesh, and non-V07 types before DCC.
        """
        # 1. Spec & Profile contract validation
        if not isinstance(spec, AssetSpecificationV07):
            raise ValidationError(
                f"Assembly processing requires typed AssetSpecificationV07, got {type(spec).__name__}"
            )
        if spec.source_kind != "local_operator_assembly":
            raise ValidationError(
                f"Assembly processor rejects non-assembly source_kind: '{spec.source_kind}'"
            )

        profile = spec.bound_profile()
        if not isinstance(profile, AssetProfileV07):
            raise ValidationError(
                f"Assembly processing requires typed AssetProfileV07, got {type(profile).__name__}"
            )
        if profile.geometry_mode != "assembly":
            raise ValidationError(
                f"Assembly processor rejects non-assembly profile geometry_mode: '{profile.geometry_mode}'"
            )

        # 2. Pinned provenance package authentication
        if isinstance(package, AssemblyIngestResult):
            package_dir = package.package_dir
        else:
            package_dir = Path(package)

        ingest_result = verify_retained_assembly(
            package_dir=package_dir,
            spec=spec,
            expected_provenance_sha256=expected_provenance_sha256,
        )

        provenance = ingest_result.provenance
        source_front = provenance.source_front
        retained_glb = _absolute_lexical(ingest_result.retained_glb_path)
        _reject_linked_components(retained_glb, "retained source GLB")
        if not retained_glb.is_file():
            raise DccFailedError(f"Retained source GLB does not exist: {retained_glb}")

        with retained_glb.open("rb") as source_stream:
            retained_source_bytes = source_stream.read(50 * 1024 * 1024 + 1)
        if len(retained_source_bytes) > 50 * 1024 * 1024:
            raise DccFailedError("Retained source GLB exceeds 50 MiB processing limit")
        source_hash_before = hashlib.sha256(retained_source_bytes).hexdigest()
        if source_hash_before.lower() != ingest_result.retained_glb_sha256.lower():
            raise DccFailedError(
                "Retained source GLB hash mismatch against authenticated ingest record"
            )

        # 3. Path safety & collision prevention
        processed_glb = _absolute_lexical(processed_glb_path)
        resolved_report = _absolute_lexical(
            report_path
            if report_path is not None
            else processed_glb.parent / f"{spec.asset_id}_blender_assembly_report.json"
        )
        raw_key = os.path.normcase(str(retained_glb))
        output_key = os.path.normcase(str(processed_glb))
        report_key = os.path.normcase(str(resolved_report))
        if len({raw_key, output_key, report_key}) != 3:
            raise ValueError("source GLB, processed GLB, and report paths must be distinct")

        for target, label, suffix in (
            (processed_glb, "processed GLB", ".glb"),
            (resolved_report, "processing report", ".json"),
        ):
            _reject_linked_components(target, label)
            if target.exists() or target.is_symlink():
                raise ValueError(f"refusing to overwrite existing {label}: {target}")
            if target.suffix.casefold() != suffix:
                raise ValueError(f"{label} must use the {suffix} extension")

        ratio = spec.geometry_budget.lod_ratio if lod1_ratio is None else lod1_ratio
        if not 0.05 <= ratio <= 0.95:
            raise ValueError("lod1_ratio must be between 0.05 and 0.95")

        # 4. Blender environment preflight
        if self.dependency_preflight:
            if self._cached_preflight is None or self._cached_preflight.status != "PASS":
                preflight = run_blender_dependency_preflight(
                    blender_executable=self.blender_exe,
                    runner=self.runner,
                    python_paths=self.python_paths,
                )
                self.last_dependency_preflight = preflight
                if preflight.status != "PASS":
                    msg = format_blender_preflight_failure_message(preflight)
                    raise DccFailedError(
                        msg,
                        exit_code=preflight.exit_code,
                        stderr=preflight.stderr_excerpt or "",
                        details={
                            "reason": preflight.reason_code or "BLENDER_DEPENDENCY_UNAVAILABLE",
                            "preflight": preflight.to_dict(),
                        },
                    )
                self._cached_preflight = preflight

        script_file = self.get_script_path()
        script_bytes = script_file.read_bytes()
        script_hash = hashlib.sha256(script_bytes).hexdigest()

        # 5. Contract serialization
        processed_glb.parent.mkdir(parents=True, exist_ok=True)
        resolved_report.parent.mkdir(parents=True, exist_ok=True)
        contract_path = resolved_report.with_suffix(".contract.json")
        if contract_path.exists() or contract_path.is_symlink():
            raise ValueError(f"refusing to overwrite existing processing contract: {contract_path}")

        contract_payload = {
            "asset_id": spec.asset_id,
            "spec_fingerprint": ingest_result.spec_fingerprint,
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "source_front": source_front,
            "provenance_sha256": ingest_result.retained_provenance_sha256,
            "source_glb_sha256": ingest_result.retained_glb_sha256,
            "lod_policy": spec.lod_policy,
            "lod1_required": profile.document.processing.lod1_required
            or spec.lod_policy == "lod0_lod1",
            "lod1_ratio": ratio,
            "collider_policy": spec.collider.policy,
            "dimension_tolerance_m": profile.document.processing.dimension_tolerance_m,
            "dimensions": spec.dimensions.model_dump(),
            "origin_policy": spec.origin_policy,
            "parts": [p.model_dump() for p in (spec.parts or [])],
            "sockets": [s.model_dump() for s in (spec.sockets or [])],
        }
        contract_bytes = _serialize_contract(contract_payload)
        with contract_path.open("xb") as contract_stream:
            contract_stream.write(contract_bytes)
        contract_identity = _capture_owned_file(contract_path, contract_bytes)

        try:
            # 6. Execute Blender in background
            cmd = [
                self.blender_exe,
                "--background",
                "--factory-startup",
                "--python-exit-code",
                str(BLENDER_PYTHON_FAILURE_EXIT_CODE),
                "--python",
                str(script_file),
                "--",
                "--input-glb",
                str(retained_glb),
                "--output-glb",
                str(processed_glb),
                "--report-path",
                str(resolved_report),
                "--contract",
                str(contract_path),
            ]

            req = CommandRequest(
                args=cmd,
                cwd=processed_glb.parent,
                env_overrides=blender_env_overrides(self.python_paths),
                timeout_seconds=timeout_seconds,
            )

            res = self.runner.run(req)

            diagnostics_path = resolved_report.with_suffix(".blender-diagnostics.txt")
            if res.exit_code != 0:
                raise _blender_failure(
                    f"Blender assembly processing failed with exit code {res.exit_code}",
                    res,
                    req,
                    self.runner,
                    script_file=script_file,
                    raw_glb=retained_glb,
                    processed_glb=processed_glb,
                    contract_path=contract_path,
                    diagnostics_path=diagnostics_path,
                    asset_id=spec.asset_id,
                    python_paths=self.python_paths,
                )

            if not processed_glb.is_file() or processed_glb.stat().st_size == 0:
                raise _blender_failure(
                    f"Blender succeeded but output processed GLB is missing or empty: {processed_glb}",
                    res,
                    req,
                    self.runner,
                    script_file=script_file,
                    raw_glb=retained_glb,
                    processed_glb=processed_glb,
                    contract_path=contract_path,
                    diagnostics_path=diagnostics_path,
                    asset_id=spec.asset_id,
                    python_paths=self.python_paths,
                )

            # Invariant check: retained source GLB must remain completely untouched
            with retained_glb.open("rb") as source_stream:
                source_after = source_stream.read(50 * 1024 * 1024 + 1)
            if len(source_after) > 50 * 1024 * 1024:
                raise DccFailedError(
                    "Retained source GLB exceeded 50 MiB during Blender processing"
                )
            if hashlib.sha256(source_after).hexdigest() != source_hash_before:
                raise DccFailedError("Retained source GLB was mutated during Blender processing!")

            with processed_glb.open("rb") as processed_stream:
                processed_bytes = processed_stream.read(50 * 1024 * 1024 + 1)
            if len(processed_bytes) > 50 * 1024 * 1024:
                raise DccFailedError("Processed GLB exceeds 50 MiB processing limit")
            processed_hash = hashlib.sha256(processed_bytes).hexdigest()

            # 7. Validate report artifact
            if not resolved_report.is_file():
                raise DccFailedError(
                    "Blender succeeded without writing its assembly processing report",
                    exit_code=res.exit_code,
                )
            report_data = _read_bounded_json_object(
                resolved_report,
                "Blender assembly processing report",
                _MAX_REPORT_BYTES,
            )

            if (
                report_data.get("schema_version") != "0.7.0"
                or report_data.get("status") != "SUCCESS"
                or report_data.get("exit_code") != 0
                or report_data.get("asset_id") != spec.asset_id
                or report_data.get("spec_fingerprint") != ingest_result.spec_fingerprint
                or report_data.get("profile_id") != profile.profile_id
                or report_data.get("profile_version") != profile.version
                or report_data.get("provenance_sha256") != ingest_result.retained_provenance_sha256
            ):
                raise DccFailedError(
                    "Blender assembly report does not bind the authenticated V0.7 source and specification",
                    exit_code=res.exit_code,
                )
            if report_data.get("source_glb_sha256") != source_hash_before:
                raise DccFailedError("Blender report source hash does not match immutable source")
            if report_data.get("output_glb_sha256") != processed_hash:
                raise DccFailedError("Blender report output hash does not match processed artifact")
            if report_data.get("processing_script_sha256") != script_hash:
                raise DccFailedError("Blender report script hash does not match executed script")

            # 8. Preservation ORACLE outside Blender report
            verified_norm = VerifiedSourceNormalization(
                source_sha256=source_hash_before,
                processed_sha256=processed_hash,
                source_front=source_front,
                normalization_applied=(source_front == "+Z"),
                root_rotation_xyzw=(0.0, 1.0, 0.0, 0.0)
                if source_front == "+Z"
                else (0.0, 0.0, 0.0, 1.0),
                source_glb_bytes=retained_source_bytes,
            )

            preservation = verify_source_to_processed_preservation(
                verified_norm, processed_glb, spec
            )
            if not preservation.passed:
                failed_msgs = [f.message for f in preservation.findings if not f.passed]
                raise DccFailedError(
                    f"Outside-Blender preservation check failed: {failed_msgs}",
                    details={"findings": [asdict(f) for f in preservation.findings]},
                )

            proof_metrics = _prove_processed_assembly_glb(
                processed_glb_path=processed_glb,
                spec=spec,
                profile=profile,
                verified_norm=verified_norm,
                source_bytes=retained_source_bytes,
            )

            report_normalization = report_data.get("normalization")
            if not isinstance(report_normalization, dict) or (
                report_normalization.get("source_front") != source_front
                or report_normalization.get("applied") != (source_front == "+Z")
            ):
                raise DccFailedError(
                    "Blender assembly report normalization does not match pinned source front"
                )
            report_bounds = report_data.get("assembly_bounds")
            if not isinstance(report_bounds, dict):
                raise DccFailedError("Blender assembly report is missing assembly bounds")
            report_tolerance = max(profile.document.processing.dimension_tolerance_m, 1e-5)
            for report_key, proof_key in (
                ("min", "bounds_min"),
                ("max", "bounds_max"),
                ("dimensions", "dimensions"),
            ):
                observed = report_bounds.get(report_key)
                expected = proof_metrics[proof_key]
                if (
                    not isinstance(observed, list)
                    or len(observed) != 3
                    or any(
                        not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or abs(float(value) - float(expected[index])) > report_tolerance
                        for index, value in enumerate(observed)
                    )
                ):
                    raise DccFailedError(
                        f"Blender report assembly {report_key} does not match decoded GLB"
                    )
            report_parts = report_data.get("per_part_metrics")
            if report_parts != proof_metrics["per_part_metrics"]:
                raise DccFailedError("Blender per-part metrics do not match decoded GLB geometry")

            return AssemblyProcessResult(
                status="SUCCESS",
                exit_code=res.exit_code,
                duration_seconds=res.duration_seconds,
                blender_version=report_data.get("blender_version", "unknown"),
                script_sha256=script_hash,
                source_glb_sha256=source_hash_before,
                provenance_sha256=ingest_result.retained_provenance_sha256,
                processed_glb_sha256=processed_hash,
                spec_fingerprint=ingest_result.spec_fingerprint,
                source_front=source_front,
                verified_normalization=verified_norm,
                processed_glb_path=processed_glb,
                report_path=resolved_report,
                report_data=report_data,
                part_metrics=report_data.get("per_part_metrics", {}),
            )

        finally:
            _remove_owned_file_if_unchanged(contract_path, contract_identity)
