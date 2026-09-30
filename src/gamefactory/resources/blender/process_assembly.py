"""Deterministic Blender background processor for authored V0.7 assemblies.

Implements standalone V0.7 authored-assembly processing:
- Strictly standalone: NO gamefactory or external site-packages imports
- Validates distinct lexical target paths, rejecting symlinks and reparse points
- Refuses to overwrite existing output GLB or report files
- Preserves exact identity ROOT node and direct root PART child topology
- Normalizes root PART by Ry(+180Y) if and only if source_front == "+Z"
- Preserves all deeper PART and socket local transforms and LOD0 mesh vertices
- Generates per-part LOD1 (decimated copy) under each PART node when required
- Generates root COL_{asset_id} box collider as an identity child of ROOT enclosing rest bounds
- Preserves custom properties / extras on PART and SOCKET nodes upon glTF export
- Produces schema 0.7.0 processing report with full hash and metric bindings
"""

from __future__ import annotations

import argparse
import atexit
import dataclasses
import hashlib
import json
import math
import os
import stat
import struct
import sys
import tempfile
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any

MAX_GLB_BYTES = 50 * 1024 * 1024
MAX_CONTRACT_BYTES = 1024 * 1024
LOD_BOUNDS_TOLERANCE_M = 0.01


def _world_bounds(obj: Any) -> tuple[tuple[float, float, float], tuple[float, float, float]] | None:
    """Return actual world-space mesh bounds, or None for an empty mesh."""
    points = [obj.matrix_world @ vertex.co for vertex in obj.data.vertices]
    if not points:
        return None
    return (
        (
            min(float(point[0]) for point in points),
            min(float(point[1]) for point in points),
            min(float(point[2]) for point in points),
        ),
        (
            max(float(point[0]) for point in points),
            max(float(point[1]) for point in points),
            max(float(point[2]) for point in points),
        ),
    )


def _bounds_match(
    left: tuple[tuple[float, float, float], tuple[float, float, float]] | None,
    right: tuple[tuple[float, float, float], tuple[float, float, float]] | None,
    tolerance: float,
) -> bool:
    return (
        left is not None
        and right is not None
        and all(
            abs(left[side][axis] - right[side][axis]) <= tolerance
            for side in range(2)
            for axis in range(3)
        )
    )


def _require_lod1_fallback_budget(
    part_id: str, lod0_triangles: int, budget: int, already_used: int = 0
) -> None:
    if already_used + lod0_triangles > budget:
        raise RuntimeError(
            f"LOD1 decimation for '{part_id}' changed world bounds and preserving "
            f"LOD0 geometry ({lod0_triangles} triangles) with {already_used} already used "
            f"exceeds aggregate LOD1 budget {budget}"
        )


@dataclasses.dataclass(frozen=True)
class _StagedFileIdentity:
    device: int
    inode: int
    size: int
    sha256: str


def _args() -> argparse.Namespace:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-glb", required=True)
    parser.add_argument("--output-glb", required=True)
    parser.add_argument("--report-path", required=True)
    parser.add_argument("--contract", required=True)
    return parser.parse_args(values)


def _assert_no_links(path: Path, label: str) -> None:
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
            raise ValueError(f"{label} traverses symlink, junction, or reparse point: {current}")
        if current.parent == current:
            break
        current = current.parent


def _read_contract(path: Path) -> dict[str, Any]:
    _assert_no_links(path, "processing contract")
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("processing contract must be a regular file")
        with path.open("rb") as stream:
            contents = stream.read(MAX_CONTRACT_BYTES + 1)
        after = path.lstat()
    except OSError as exc:
        raise ValueError(f"cannot read processing contract: {exc}") from exc
    if len(contents) > MAX_CONTRACT_BYTES:
        raise ValueError(f"processing contract exceeds {MAX_CONTRACT_BYTES} bytes")
    if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("processing contract changed during read")
    try:
        contract = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise ValueError(f"processing contract is invalid UTF-8 JSON: {exc}") from exc
    if not isinstance(contract, dict):
        raise ValueError("processing contract must contain a JSON object")
    return contract


def _serialize_report(report: dict[str, Any]) -> bytes:
    encoder = json.JSONEncoder(sort_keys=True, indent=2)
    contents = bytearray()
    for text in encoder.iterencode(report):
        encoded = text.encode("utf-8")
        if len(contents) + len(encoded) > MAX_CONTRACT_BYTES:
            raise ValueError(f"processing report exceeds {MAX_CONTRACT_BYTES} bytes")
        contents.extend(encoded)
    return bytes(contents)


def _capture_staged_file(path: Path, max_bytes: int) -> _StagedFileIdentity:
    _assert_no_links(path, "staged Blender artifact")
    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError(f"staged artifact must be a regular file: {path}")
    with path.open("rb") as stream:
        contents = stream.read(max_bytes + 1)
    after = path.lstat()
    if len(contents) > max_bytes:
        raise ValueError(f"staged artifact exceeds {max_bytes} bytes")
    if (metadata.st_dev, metadata.st_ino) != (after.st_dev, after.st_ino):
        raise ValueError("staged artifact changed during ownership capture")
    return _StagedFileIdentity(
        metadata.st_dev,
        metadata.st_ino,
        len(contents),
        hashlib.sha256(contents).hexdigest(),
    )


def _remove_staged_if_unchanged(path: Path, expected: _StagedFileIdentity, max_bytes: int) -> None:
    try:
        _assert_no_links(path, "staged Blender artifact")
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            expected.device,
            expected.inode,
        ):
            return
        with path.open("rb") as stream:
            contents = stream.read(max_bytes + 1)
        after = path.lstat()
        if (
            len(contents) <= max_bytes
            and len(contents) == expected.size
            and hashlib.sha256(contents).hexdigest() == expected.sha256
            and (after.st_dev, after.st_ino) == (expected.device, expected.inode)
        ):
            path.unlink()
    except (OSError, ValueError):
        return


def _assert_new_distinct_targets(source: Path, output: Path, report: Path) -> None:
    source_abs = Path(os.path.abspath(source))
    output_abs = Path(os.path.abspath(output))
    report_abs = Path(os.path.abspath(report))

    keys = {os.path.normcase(str(p)) for p in (source_abs, output_abs, report_abs)}
    if len(keys) != 3:
        raise ValueError("input GLB, output GLB, and report paths must be distinct")

    for p, label in (
        (source_abs, "input GLB"),
        (output_abs, "output GLB"),
        (report_abs, "report"),
    ):
        _assert_no_links(p, label)

    if not source_abs.is_file() or source_abs.suffix.casefold() != ".glb":
        raise ValueError(f"input GLB must be an existing .glb file: {source_abs}")

    for p, label, ext in ((output_abs, "output GLB", ".glb"), (report_abs, "report", ".json")):
        if p.exists() or p.is_symlink():
            raise ValueError(f"refusing to overwrite existing {label}: {p}")
        if p.suffix.casefold() != ext:
            raise ValueError(f"{label} path must use {ext}: {p}")


def _require_finished_export(operator_result: Iterable[object], output: Path) -> None:
    if isinstance(operator_result, (str, bytes)):
        raise RuntimeError(
            f"glTF export operator returned text instead of a status set: {operator_result!r}"
        )
    statuses = {str(item) for item in operator_result}
    if statuses != {"FINISHED"}:
        actual = ", ".join(sorted(statuses)) or "(empty)"
        raise RuntimeError(f"glTF export operator expected FINISHED, actual {actual}")
    if not output.is_file():
        raise RuntimeError(f"glTF export reported FINISHED but output is missing: {output}")
    size = output.stat().st_size
    if size <= 0:
        raise RuntimeError(
            f"glTF export reported FINISHED but output is empty: {output} ({size} bytes)"
        )


def _source_has_normals(source_path: Path) -> bool:
    """Check if any mesh primitive in source GLB declares a NORMAL attribute."""
    try:
        with source_path.open("rb") as source_stream:
            raw = source_stream.read(MAX_GLB_BYTES + 1)
        if len(raw) > MAX_GLB_BYTES:
            raise ValueError("source GLB exceeds processing size bound")
        if len(raw) < 20:
            return False
        json_len = struct.unpack_from("<I", raw, 12)[0]
        doc = json.loads(raw[20 : 20 + json_len].decode("utf-8").rstrip())
        for mesh in doc.get("meshes", []):
            for prim in mesh.get("primitives", []):
                if "NORMAL" in prim.get("attributes", {}):
                    return True
    except Exception:
        pass
    return False


def _publish_no_clobber(
    staged_output: Path,
    staged_report: Path,
    output_target: Path,
    report_target: Path,
) -> None:
    """Publish complete same-volume files without replacing concurrent targets."""
    published_output = False
    published_report = False
    try:
        os.link(staged_output, output_target)
        published_output = True
        os.link(staged_report, report_target)
        published_report = True
    except BaseException:
        if published_report:
            try:
                if report_target.samefile(staged_report):
                    report_target.unlink()
            except OSError:
                pass
        if published_output:
            try:
                if output_target.samefile(staged_output):
                    output_target.unlink()
            except OSError:
                pass
        raise


def main() -> None:
    started = time.monotonic()
    args = _args()

    source = Path(os.path.abspath(args.input_glb))
    output_target = Path(os.path.abspath(args.output_glb))
    report_target = Path(os.path.abspath(args.report_path))
    output = output_target
    report_path = report_target
    contract_file = Path(os.path.abspath(args.contract))

    _assert_no_links(contract_file, "processing contract")
    if not contract_file.is_file():
        raise ValueError(f"processing contract file does not exist: {contract_file}")

    contract = _read_contract(contract_file)

    _assert_new_distinct_targets(source, output_target, report_target)

    output_fd, staged_output_name = tempfile.mkstemp(
        prefix=f".{output_target.stem}.", suffix=".stage.glb", dir=output_target.parent
    )
    os.close(output_fd)
    output = Path(staged_output_name)
    report_path = report_target.with_name(f".{report_target.stem}.{uuid.uuid4().hex}.stage.json")
    staging_output_identity = _capture_staged_file(output, MAX_GLB_BYTES)
    staging_report_identity: _StagedFileIdentity | None = None

    def cleanup_staging() -> None:
        _remove_staged_if_unchanged(output, staging_output_identity, MAX_GLB_BYTES)
        if staging_report_identity is not None:
            _remove_staged_if_unchanged(report_path, staging_report_identity, MAX_CONTRACT_BYTES)

    atexit.register(cleanup_staging)

    with source.open("rb") as source_stream:
        raw_bytes = source_stream.read(MAX_GLB_BYTES + 1)
    if len(raw_bytes) > MAX_GLB_BYTES:
        raise ValueError("source GLB exceeds 50 MiB processing limit")
    raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    expected_source_hash = contract.get("source_glb_sha256")
    if (
        not isinstance(expected_source_hash, str)
        or len(expected_source_hash) != 64
        or any(char not in "0123456789abcdefABCDEF" for char in expected_source_hash)
    ):
        raise ValueError("processing contract must pin the source GLB SHA-256")
    if raw_hash.lower() != expected_source_hash.lower():
        raise RuntimeError(
            f"Source GLB SHA-256 mismatch: expected {expected_source_hash}, actual {raw_hash}"
        )

    asset_id = str(contract["asset_id"])
    source_front = str(contract["source_front"])
    if source_front not in ("-Z", "+Z"):
        raise ValueError(f"Invalid source_front in contract: {source_front!r}")

    lod_policy = str(contract.get("lod_policy", "lod0_only"))
    lod1_required = bool(contract.get("lod1_required", lod_policy == "lod0_lod1"))
    lod1_ratio = float(contract.get("lod1_ratio", 0.5))
    if not 0.05 <= lod1_ratio <= 0.95:
        raise ValueError("lod1_ratio must be between 0.05 and 0.95")
    max_triangles_lod1 = contract.get("max_triangles_lod1")
    if lod1_required and (
        not isinstance(max_triangles_lod1, int)
        or isinstance(max_triangles_lod1, bool)
        or max_triangles_lod1 <= 0
    ):
        raise ValueError("required LOD1 needs a positive max_triangles_lod1 budget")
    lod_bounds_tolerance = contract.get("lod_bounds_tolerance_m", LOD_BOUNDS_TOLERANCE_M)
    if (
        not isinstance(lod_bounds_tolerance, (int, float))
        or isinstance(lod_bounds_tolerance, bool)
        or not math.isfinite(lod_bounds_tolerance)
        or lod_bounds_tolerance <= 0
    ):
        raise ValueError("lod_bounds_tolerance_m must be a finite positive number")

    spec_parts = contract.get("parts", [])
    spec_sockets = contract.get("sockets", [])
    if not spec_parts:
        raise ValueError("processing contract specifies no assembly parts")

    root_parts = [p for p in spec_parts if p.get("parent") == "root"]
    if len(root_parts) != 1:
        raise ValueError(f"Contract must specify exactly one root part, found {len(root_parts)}")
    root_part_spec = root_parts[0]
    root_part_id = root_part_spec["part_id"]
    root_part_name = f"PART_{root_part_id}"

    # Import Blender modules safely inside Blender environment
    import bpy  # type: ignore[import-not-found]
    from mathutils import Matrix, Vector  # type: ignore[import-not-found]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    import_result = bpy.ops.import_scene.gltf(filepath=str(source))
    if "FINISHED" not in import_result:
        raise RuntimeError("Blender GLB importer did not finish successfully")

    # 1. ROOT node verification
    root_candidates = [
        obj for obj in bpy.context.scene.objects if obj.name == "ROOT" and obj.type == "EMPTY"
    ]
    if len(root_candidates) != 1:
        raise RuntimeError(
            f"Scene must contain exactly one EMPTY node named 'ROOT', found {len(root_candidates)}"
        )
    root_obj = root_candidates[0]
    if root_obj.parent is not None:
        raise RuntimeError("ROOT node must not have a parent")

    # Check ROOT matrix is identity
    for r in range(4):
        for c in range(4):
            exp_val = 1.0 if r == c else 0.0
            if abs(root_obj.matrix_local[r][c] - exp_val) > 1e-5:
                raise RuntimeError("ROOT node transform in Blender is not identity")

    # 2. Verify all PART nodes and their direct LOD0 mesh children
    part_objs: dict[str, Any] = {}
    lod0_objs: dict[str, Any] = {}

    for p in spec_parts:
        pid = p["part_id"]
        pname = f"PART_{pid}"
        obj = bpy.data.objects.get(pname)
        if obj is None:
            raise RuntimeError(f"Required part object '{pname}' is missing from imported scene")
        part_objs[pid] = obj

        # Direct LOD0 mesh child check
        lod0_name = f"SM_{asset_id}_{pid}_LOD0"
        lod0 = bpy.data.objects.get(lod0_name)
        if lod0 is None or lod0.type != "MESH":
            raise RuntimeError(f"Required direct LOD0 mesh '{lod0_name}' is missing from scene")
        if lod0.parent != obj:
            raise RuntimeError(
                f"LOD0 mesh '{lod0_name}' parent must be '{pname}', got {lod0.parent.name if lod0.parent else None}"
            )
        # Check LOD0 transform is identity
        for r in range(4):
            for c in range(4):
                exp_v = 1.0 if r == c else 0.0
                if abs(lod0.matrix_local[r][c] - exp_v) > 1e-5:
                    raise RuntimeError(f"LOD0 mesh '{lod0_name}' local transform must be identity")
        lod0_objs[pid] = lod0

    # Verify parent topology
    for p in spec_parts:
        pid = p["part_id"]
        pname = f"PART_{pid}"
        obj = part_objs[pid]
        expected_parent_name = "ROOT" if p["parent"] == "root" else f"PART_{p['parent']}"
        actual_parent_name = obj.parent.name if obj.parent else None
        if actual_parent_name != expected_parent_name:
            raise RuntimeError(
                f"Part '{pname}' parent mismatch: expected '{expected_parent_name}', got '{actual_parent_name}'"
            )

    # ROOT direct child must be the root PART
    root_part_objs = [obj for obj in root_obj.children if obj.name.startswith("PART_")]
    if len(root_part_objs) != 1 or root_part_objs[0].name != root_part_name:
        raise RuntimeError(
            f"ROOT direct child must be '{root_part_name}', found {[o.name for o in root_part_objs]}"
        )

    # 3. Verify all SOCKET nodes
    socket_objs: dict[str, Any] = {}
    for s in spec_sockets:
        sid = s["socket_id"]
        sname = f"SOCKET_{sid}"
        obj = bpy.data.objects.get(sname)
        if obj is None:
            raise RuntimeError(f"Required socket object '{sname}' is missing from imported scene")
        if obj.type != "EMPTY":
            raise RuntimeError(f"Socket '{sname}' must be an EMPTY object, got {obj.type}")
        if obj.children:
            raise RuntimeError(f"Socket '{sname}' must be a leaf without children")
        expected_parent_name = f"PART_{s['parent_part']}"
        actual_parent_name = obj.parent.name if obj.parent else None
        if actual_parent_name != expected_parent_name:
            raise RuntimeError(
                f"Socket '{sname}' parent mismatch: expected '{expected_parent_name}', got '{actual_parent_name}'"
            )
        # Check scale is identity
        if any(abs(scale_v - 1.0) > 1e-5 for scale_v in obj.scale):
            raise RuntimeError(f"Socket '{sname}' scale must be identity, got {list(obj.scale)}")
        socket_objs[sid] = obj

    # 4. Measure initial visual geometry bounds and check dimensions & origin policy
    # No stretching / rescaling / repivoting / recentering allowed!
    bpy.context.view_layer.update()
    all_lod0_pts = [
        obj.matrix_world @ v.co for obj in lod0_objs.values() for v in obj.data.vertices
    ]
    if not all_lod0_pts:
        raise RuntimeError("Assembly contains no visual LOD0 vertices")

    raw_min = Vector(tuple(min(p[i] for p in all_lod0_pts) for i in range(3)))
    raw_max = Vector(tuple(max(p[i] for p in all_lod0_pts) for i in range(3)))
    raw_dim = raw_max - raw_min

    target_dims = contract.get("dimensions", {})
    target_w = float(target_dims.get("width_m", raw_dim.x))
    target_d = float(target_dims.get("depth_m", raw_dim.y))
    target_h = float(target_dims.get("height_m", raw_dim.z))
    tol = float(contract.get("dimension_tolerance_m", 0.02))

    # In Blender coordinate space: X is width, Y is depth, Z is height
    if (
        abs(raw_dim.x - target_w) > tol
        or abs(raw_dim.y - target_d) > tol
        or abs(raw_dim.z - target_h) > tol
    ):
        raise RuntimeError(
            f"Assembly dimensions mismatch: target width={target_w} depth={target_d} height={target_h}, "
            f"actual width={raw_dim.x:.4f} depth={raw_dim.y:.4f} height={raw_dim.z:.4f} (tol={tol}); "
            f"stretching or rescaling is forbidden"
        )

    origin_policy = contract.get("origin_policy", "center")
    origin_ok = abs((raw_min.x + raw_max.x) / 2) <= tol and abs((raw_min.y + raw_max.y) / 2) <= tol
    if origin_policy == "bottom_center":
        origin_ok = origin_ok and abs(raw_min.z) <= tol
    else:
        origin_ok = origin_ok and abs((raw_min.z + raw_max.z) / 2) <= tol

    if not origin_ok:
        raise RuntimeError(
            f"Assembly origin mismatch for declared origin_policy '{origin_policy}': "
            f"bounds min={list(raw_min)} max={list(raw_max)}; recentering is forbidden"
        )

    # 5. One allowed normalization: canonical = Ry(+180Y) * raw logical root PART if source_front == "+Z"
    root_part_obj = part_objs[root_part_id]
    normalization_applied = False
    if source_front == "+Z":
        # Blender Z axis is glTF Y axis (Up). Rotating 180° around Blender Z
        # corresponds precisely to Ry(+180Y) in glTF normative coordinates.
        rot_z_180 = Matrix.Rotation(math.pi, 4, "Z")
        root_part_obj.matrix_local = rot_z_180 @ root_part_obj.matrix_local
        normalization_applied = True
        bpy.context.view_layer.update()
    elif source_front == "-Z":
        # -Z requires no normalization; root part local transform stays unchanged
        pass

    # 6. Generate per-part LOD1 (decimated copy) under each PART node if required
    lod1_objs: dict[str, Any] = {}
    part_metrics: dict[str, Any] = {}
    lod1_triangles_total = 0

    for pid in [p["part_id"] for p in spec_parts]:
        lod0 = lod0_objs[pid]
        lod0_triangles = sum(max(1, len(poly.vertices) - 2) for poly in lod0.data.polygons)
        lod1_triangles: int | None = None

        if lod1_required:
            if (
                not isinstance(max_triangles_lod1, int)
                or isinstance(max_triangles_lod1, bool)
                or max_triangles_lod1 <= 0
            ):
                raise ValueError("required LOD1 needs a positive max_triangles_lod1 budget")
            lod1_budget = max_triangles_lod1
            lod1 = lod0.copy()
            lod1.data = lod0.data.copy()
            lod1.name = f"SM_{asset_id}_{pid}_LOD1"
            lod1.data.name = f"SM_{asset_id}_{pid}_LOD1_Mesh"
            bpy.context.scene.collection.objects.link(lod1)
            lod1.parent = lod0.parent
            lod1.matrix_local = Matrix.Identity(4)

            decimate = lod1.modifiers.new("LOD1_Decimate", "DECIMATE")
            decimate.ratio = lod1_ratio
            bpy.context.view_layer.objects.active = lod1
            lod1.select_set(True)
            bpy.ops.object.modifier_apply(modifier=decimate.name)
            lod1.select_set(False)
            bpy.context.view_layer.update()

            if not _bounds_match(
                _world_bounds(lod0),
                _world_bounds(lod1),
                float(lod_bounds_tolerance),
            ):
                # Decimation is allowed to miss extrema only when a conservative
                # unchanged-geometry fallback still fits the declared LOD1 budget.
                _require_lod1_fallback_budget(
                    pid,
                    lod0_triangles,
                    lod1_budget,
                    already_used=lod1_triangles_total,
                )
                discarded_mesh = lod1.data
                lod1.data = lod0.data.copy()
                lod1.data.name = f"SM_{asset_id}_{pid}_LOD1_Mesh"
                if discarded_mesh.users == 0:
                    bpy.data.meshes.remove(discarded_mesh)
                bpy.context.view_layer.update()

            lod1_triangles = sum(max(1, len(poly.vertices) - 2) for poly in lod1.data.polygons)
            if lod1_triangles > lod1_budget:
                raise RuntimeError(
                    f"LOD1 '{pid}' has {lod1_triangles} triangles, exceeding budget {lod1_budget}"
                )
            lod1_triangles_total += lod1_triangles
            if lod1_triangles_total > lod1_budget:
                raise RuntimeError(
                    f"Aggregate LOD1 has {lod1_triangles_total} triangles, exceeding "
                    f"budget {lod1_budget}"
                )
            if not _bounds_match(
                _world_bounds(lod0),
                _world_bounds(lod1),
                float(lod_bounds_tolerance),
            ):
                raise RuntimeError(f"LOD1 bounds for '{pid}' do not preserve LOD0 bounds")
            lod1_objs[pid] = lod1

        part_metrics[pid] = {
            "lod0_triangles": lod0_triangles,
            "lod1_triangles": lod1_triangles,
        }

    # 7. Generate root box collider COL_{asset_id} from canonical assembly rest bounds
    bpy.context.view_layer.update()
    canonical_lod0_pts = [
        obj.matrix_world @ v.co for obj in lod0_objs.values() for v in obj.data.vertices
    ]
    cmin = Vector(tuple(min(p[i] for p in canonical_lod0_pts) for i in range(3)))
    cmax = Vector(tuple(max(p[i] for p in canonical_lod0_pts) for i in range(3)))

    box_verts = [
        (cmin.x, cmin.y, cmin.z),
        (cmax.x, cmin.y, cmin.z),
        (cmax.x, cmax.y, cmin.z),
        (cmin.x, cmax.y, cmin.z),
        (cmin.x, cmin.y, cmax.z),
        (cmax.x, cmin.y, cmax.z),
        (cmax.x, cmax.y, cmax.z),
        (cmin.x, cmax.y, cmax.z),
    ]
    box_faces = [
        (0, 1, 2, 3),  # bottom
        (4, 7, 6, 5),  # top
        (0, 4, 5, 1),  # front
        (1, 5, 6, 2),  # right
        (2, 6, 7, 3),  # back
        (3, 7, 4, 0),  # left
    ]

    col_mesh = bpy.data.meshes.new(f"COL_{asset_id}_Mesh")
    col_mesh.from_pydata(box_verts, [], box_faces)
    col_mesh.update()
    col_obj = bpy.data.objects.new(f"COL_{asset_id}", col_mesh)
    bpy.context.scene.collection.objects.link(col_obj)
    col_obj.parent = root_obj
    col_obj.matrix_local = Matrix.Identity(4)

    # 8. Ensure extras / custom properties on PART and SOCKET nodes are preserved
    for p in spec_parts:
        pid = p["part_id"]
        p_obj = part_objs[pid]
        motion = p.get("pivot", {}).get("motion", {})
        kind = motion.get("kind", "fixed")
        p_obj["gf_motion"] = kind
        if kind != "fixed":
            axis = motion.get("axis")
            if axis is not None:
                p_obj["gf_axis"] = [float(v) for v in axis]

    # 9. Selection and export: select ONLY approved assembly nodes
    allowed_names = {"ROOT", f"COL_{asset_id}"}
    for pid in [p["part_id"] for p in spec_parts]:
        allowed_names.add(f"PART_{pid}")
        allowed_names.add(f"SM_{asset_id}_{pid}_LOD0")
        if lod1_required:
            allowed_names.add(f"SM_{asset_id}_{pid}_LOD1")
    for s in spec_sockets:
        allowed_names.add(f"SOCKET_{s['socket_id']}")

    for obj in bpy.context.scene.objects:
        obj.select_set(obj.name in allowed_names)

    bpy.context.view_layer.objects.active = root_obj

    # Check if source mesh had normals declared
    source_had_normals = _source_has_normals(source)

    output.parent.mkdir(parents=True, exist_ok=True)
    export_res = bpy.ops.export_scene.gltf(
        filepath=str(output),
        export_format="GLB",
        use_selection=True,
        export_normals=source_had_normals,
        export_extras=True,
    )
    _require_finished_export(export_res, output)
    staging_output_identity = _capture_staged_file(output, MAX_GLB_BYTES)

    # Invariant: source GLB must not have been mutated
    with source.open("rb") as source_stream:
        source_after = source_stream.read(MAX_GLB_BYTES + 1)
    if len(source_after) > MAX_GLB_BYTES or hashlib.sha256(source_after).hexdigest() != raw_hash:
        raise RuntimeError("Source GLB changed during Blender processing!")

    with output.open("rb") as output_stream:
        output_bytes = output_stream.read(MAX_GLB_BYTES + 1)
    if len(output_bytes) > MAX_GLB_BYTES:
        raise RuntimeError("Processed GLB exceeds 50 MiB processing limit")
    output_hash = hashlib.sha256(output_bytes).hexdigest()
    script_bytes = Path(__file__).read_bytes()
    script_hash = hashlib.sha256(script_bytes).hexdigest()

    # 10. Write Schema 0.7.0 processing report
    report_data = {
        "schema_version": "0.7.0",
        "status": "SUCCESS",
        "asset_id": asset_id,
        "spec_fingerprint": contract.get("spec_fingerprint"),
        "profile_id": contract.get("profile_id"),
        "profile_version": contract.get("profile_version"),
        "source_glb_sha256": raw_hash,
        "provenance_sha256": contract.get("provenance_sha256"),
        "output_glb_sha256": output_hash,
        "processing_script_sha256": script_hash,
        "normalization": {
            "source_front": source_front,
            "applied": normalization_applied,
            "root_rotation_xyzw": (
                [0.0, 1.0, 0.0, 0.0] if normalization_applied else [0.0, 0.0, 0.0, 1.0]
            ),
        },
        "per_part_metrics": part_metrics,
        "assembly_bounds": {
            # Blender is Z-up; report dimensions in the canonical glTF/Godot Y-up frame.
            "min": [cmin.x, cmin.z, -cmax.y],
            "max": [cmax.x, cmax.z, -cmin.y],
            "dimensions": [cmax.x - cmin.x, cmax.z - cmin.z, cmax.y - cmin.y],
        },
        "collider": {
            "name": f"COL_{asset_id}",
            "parent": "ROOT",
            "bounds_min": [cmin.x, cmin.z, -cmax.y],
            "bounds_max": [cmax.x, cmax.z, -cmin.y],
        },
        "blender_version": bpy.app.version_string,
        "duration_seconds": time.monotonic() - started,
        "exit_code": 0,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_bytes = _serialize_report(report_data)
    with report_path.open("xb") as rf:
        rf.write(report_bytes)
    staging_report_identity = _capture_staged_file(report_path, MAX_CONTRACT_BYTES)

    # Same-volume hard links make publication all-or-nothing per target and never replace.
    _publish_no_clobber(output, report_path, output_target, report_target)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(
            f"ASSEMBLY_PROCESSING_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True
        )
        raise
