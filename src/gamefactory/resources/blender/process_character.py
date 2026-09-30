"""Standalone Blender worker for pinned, static V0.7 character GLBs.

This file intentionally depends only on Blender's Python and the standard
library. The calling adapter performs strict GLB preflight and independently
validates the exported artifact.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

MAX_GLB_BYTES = 50 * 1024 * 1024
MAX_JSON_BYTES = 1024 * 1024


def _args() -> argparse.Namespace:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-glb", required=True)
    parser.add_argument("--output-glb", required=True)
    parser.add_argument("--report-path", required=True)
    parser.add_argument("--contract", required=True)
    return parser.parse_args(values)


def _read_regular(path: Path, limit: int, label: str) -> bytes:
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or path.is_symlink():
        raise RuntimeError(f"{label} must be a regular non-link file: {path}")
    with path.open("rb") as stream:
        content = stream.read(limit + 1)
    after = path.lstat()
    if len(content) > limit:
        raise RuntimeError(f"{label} exceeds the {limit}-byte limit")
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError(f"{label} changed during read")
    return content


def _read_contract(path: Path) -> dict[str, Any]:
    raw = _read_regular(path, MAX_JSON_BYTES, "processing contract")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise RuntimeError(f"processing contract is invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("processing contract must contain a JSON object")

    def ensure_finite(item: Any) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise RuntimeError("processing contract contains a non-finite number")
        if isinstance(item, dict):
            for child in item.values():
                ensure_finite(child)
        elif isinstance(item, list):
            for child in item:
                ensure_finite(child)

    ensure_finite(value)
    return value


def _file_identity(path: Path, raw: bytes) -> tuple[int, int, int, str]:
    before = path.lstat()
    current = _read_regular(path, max(len(raw), 1), "owned temporary file")
    after = path.lstat()
    if current != raw or (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise RuntimeError("temporary file changed while ownership was captured")
    return after.st_dev, after.st_ino, len(raw), hashlib.sha256(raw).hexdigest()


def _unlink_owned(path: Path, identity: tuple[int, int, int, str] | None) -> None:
    if identity is None:
        return
    try:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or (metadata.st_dev, metadata.st_ino) != identity[:2]:
            return
        raw = _read_regular(path, max(identity[2], 1), "owned temporary artifact")
        current = path.lstat()
        if (
            (current.st_dev, current.st_ino) == identity[:2]
            and len(raw) == identity[2]
            and hashlib.sha256(raw).hexdigest() == identity[3]
        ):
            path.unlink()
    except OSError:
        return


def _write_report(path: Path, report: dict[str, Any]) -> tuple[bytes, tuple[int, int, int, str]]:
    encoder = json.JSONEncoder(sort_keys=True, indent=2, allow_nan=False)
    content = bytearray()
    for piece in encoder.iterencode(report):
        encoded = piece.encode("utf-8")
        if len(content) + len(encoded) + 1 > MAX_JSON_BYTES:
            raise RuntimeError(f"processing report exceeds {MAX_JSON_BYTES}-byte limit")
        content.extend(encoded)
    content.extend(b"\n")
    raw = bytes(content)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    return raw, _file_identity(path, raw)


def _safe_targets(source: Path, output: Path, report: Path) -> None:
    paths = [Path(os.path.abspath(item)) for item in (source, output, report)]
    if len({os.path.normcase(str(item)) for item in paths}) != 3:
        raise RuntimeError("input, output, and report paths must be distinct")
    for path in paths:
        current = path
        while True:
            if current.is_symlink() or getattr(current, "is_junction", lambda: False)():
                raise RuntimeError(f"path traverses a link or junction: {current}")
            if current.parent == current:
                break
            current = current.parent
    if not paths[0].is_file() or paths[0].suffix.casefold() != ".glb":
        raise RuntimeError("input must be an existing GLB file")
    for target, extension in ((paths[1], ".glb"), (paths[2], ".json")):
        if target.exists() or target.is_symlink():
            raise RuntimeError(f"refusing to overwrite existing artifact: {target}")
        if target.suffix.casefold() != extension:
            raise RuntimeError(f"artifact must use {extension}: {target}")


def _bounds(objects: list[Any], Vector: Any) -> tuple[Any, Any]:
    corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
    return Vector(tuple(min(point[index] for point in corners) for index in range(3))), Vector(
        tuple(max(point[index] for point in corners) for index in range(3))
    )


def main() -> None:
    started = time.monotonic()
    args = _args()
    source, output, report_path, contract_path = map(
        Path, (args.input_glb, args.output_glb, args.report_path, args.contract)
    )
    _safe_targets(source, output, report_path)
    contract = _read_contract(contract_path)
    if contract.get("contract_version") != "character-processing-0.7.0":
        raise RuntimeError("unsupported processing contract")
    actual_script_sha = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if actual_script_sha != contract.get("script_sha256"):
        raise RuntimeError("processing script differs from pinned contract digest")
    raw_bytes = _read_regular(source, MAX_GLB_BYTES, "raw character source")
    raw_sha = hashlib.sha256(raw_bytes).hexdigest()
    if raw_sha != contract.get("source_sha256"):
        raise RuntimeError("raw character source differs from pinned contract digest")

    import bpy  # type: ignore[import-not-found]
    from mathutils import Vector  # type: ignore[import-not-found]

    stage_path: Path | None = None
    stage_identity: tuple[int, int, int, str] | None = None
    report_identity: tuple[int, int, int, str] | None = None
    output_identity: tuple[int, int, int, str] | None = None
    succeeded = False
    try:
        # Blender imports only this exact bounded, hash-pinned snapshot.
        fd, stage_name = tempfile.mkstemp(
            prefix="gf-character-source-", suffix=".glb", dir=output.parent
        )
        stage_path = Path(stage_name)
        with os.fdopen(fd, "wb") as stage_file:
            stage_file.write(raw_bytes)
            stage_file.flush()
            os.fsync(stage_file.fileno())
        stage_identity = _file_identity(stage_path, raw_bytes)

        bpy.ops.wm.read_factory_settings(use_empty=True)
        imported = bpy.ops.import_scene.gltf(filepath=str(stage_path))
        if "FINISHED" not in imported:
            raise RuntimeError("Blender GLB importer did not finish")
        scene_objects = list(bpy.context.scene.objects)
        if any(obj.type not in {"MESH", "EMPTY"} for obj in scene_objects):
            raise RuntimeError("character source contains a non-static Blender object")
        visuals = [obj for obj in scene_objects if obj.type == "MESH"]
        if not visuals:
            raise RuntimeError("character source contains no mesh objects")
        raw_low, raw_high = _bounds(visuals, Vector)
        raw_dims = raw_high - raw_low
        if min(raw_dims) <= 1e-8 or not all(math.isfinite(float(value)) for value in raw_dims):
            raise RuntimeError("character source has empty or non-finite bounds")
        target = contract["dimensions"]
        height = float(target["height_m"])
        uniform_scale = height / raw_dims.z
        scaled_dims = (
            raw_dims.x * uniform_scale,
            raw_dims.y * uniform_scale,
            raw_dims.z * uniform_scale,
        )
        tolerance = float(contract["dimension_tolerance_m"])
        expected_dims = (float(target["width_m"]), float(target["depth_m"]), height)
        if any(
            abs(float(actual) - expected) > tolerance
            for actual, expected in zip(scaled_dims, expected_dims, strict=True)
        ):
            raise RuntimeError("dimensions do not fit target using uniform height scaling")

        raw_metrics = {
            "coordinate_space": "Blender internal Z-up: X width, Y depth, Z height",
            "bounds_min": [float(value) for value in raw_low],
            "bounds_max": [float(value) for value in raw_high],
            "dimensions": [float(value) for value in raw_dims],
            "mesh_objects": len(visuals),
            "lod0_triangles": sum(
                sum(max(1, len(poly.vertices) - 2) for poly in obj.data.polygons) for obj in visuals
            ),
            "materials": len({mat.name for obj in visuals for mat in obj.data.materials if mat}),
        }
        raw_metrics["lod0_triangles"] = sum(
            sum(max(1, len(poly.vertices) - 2) for poly in obj.data.polygons) for obj in visuals
        )

        # Bake every source object's world transform before joining, preserving
        # static translated pieces while eliminating non-identity visual nodes.
        for obj in visuals:
            world = obj.matrix_world.copy()
            obj.parent = None
            obj.data = obj.data.copy()
            obj.data.transform(world)
            obj.location = (0.0, 0.0, 0.0)
            obj.rotation_euler = (0.0, 0.0, 0.0)
            obj.scale = (1.0, 1.0, 1.0)

        bpy.ops.object.select_all(action="DESELECT")
        for obj in visuals:
            obj.select_set(True)
        bpy.context.view_layer.objects.active = visuals[0]
        if len(visuals) > 1:
            bpy.ops.object.join()
        lod0 = bpy.context.view_layer.objects.active
        if lod0 is None or lod0.type != "MESH":
            raise RuntimeError("joining static meshes failed")
        lod0.name = f"SM_{contract['asset_id']}_LOD0"
        lod0.data.name = f"SM_{contract['asset_id']}_LOD0_Mesh"
        for vertex in lod0.data.vertices:
            vertex.co *= uniform_scale
        scaled_low, scaled_high = _bounds([lod0], Vector)
        policy = contract["origin_policy"]
        offset = Vector(
            (
                -(scaled_low.x + scaled_high.x) / 2,
                -(scaled_low.y + scaled_high.y) / 2,
                -scaled_low.z if policy == "bottom_center" else -(scaled_low.z + scaled_high.z) / 2,
            )
        )
        for vertex in lod0.data.vertices:
            vertex.co += offset
        lod0.location = (0.0, 0.0, 0.0)
        lod0.rotation_euler = (0.0, 0.0, 0.0)
        lod0.scale = (1.0, 1.0, 1.0)
        lod0.data.update()
        lod0_triangles = sum(max(1, len(poly.vertices) - 2) for poly in lod0.data.polygons)
        if lod0_triangles > int(contract["geometry_budget"]["max_triangles_lod0"]):
            raise RuntimeError("processed LOD0 exceeds its triangle budget")

        lod1 = lod0.copy()
        lod1.data = lod0.data.copy()
        lod1.name = f"SM_{contract['asset_id']}_LOD1"
        lod1.data.name = f"SM_{contract['asset_id']}_LOD1_Mesh"
        bpy.context.scene.collection.objects.link(lod1)
        decimator = lod1.modifiers.new("LOD1_Decimate", "DECIMATE")
        decimator.ratio = float(contract["lod1_ratio"])
        bpy.context.view_layer.objects.active = lod1
        lod1.select_set(True)
        bpy.ops.object.modifier_apply(modifier=decimator.name)
        lod1_triangles = sum(max(1, len(poly.vertices) - 2) for poly in lod1.data.polygons)
        if not 0 < lod1_triangles <= int(contract["geometry_budget"]["max_triangles_lod1"]):
            raise RuntimeError("processed LOD1 is empty or exceeds its triangle budget")
        lod1_low, lod1_high = _bounds([lod1], Vector)
        lod0_low, lod0_high = _bounds([lod0], Vector)
        lod1_bounds_match = not any(
            abs(float(lod1_low[index] - lod0_low[index])) > tolerance
            or abs(float(lod1_high[index] - lod0_high[index])) > tolerance
            for index in range(3)
        )
        lod1_method = "decimated"
        if not lod1_bounds_match:
            # Keep the required LOD1 node and be honest about the fallback. The
            # fallback is permitted only when unchanged geometry fits its budget.
            lod1.data = lod0.data.copy()
            lod1_triangles = lod0_triangles
            if lod1_triangles > int(contract["geometry_budget"]["max_triangles_lod1"]):
                raise RuntimeError(
                    "decimated LOD1 loses the required bounds and unchanged fallback exceeds its budget"
                )
            lod1_method = "unchanged_fallback"

        _safe_targets(source, output, report_path)
        bpy.ops.object.select_all(action="DESELECT")
        lod0.select_set(True)
        lod1.select_set(True)
        bpy.context.view_layer.objects.active = lod0
        export = bpy.ops.export_scene.gltf(
            filepath=str(output), export_format="GLB", use_selection=True
        )
        if set(export) != {"FINISHED"} or not output.is_file() or output.stat().st_size == 0:
            raise RuntimeError(f"GLB export did not finish: {export}")
        output_bytes = _read_regular(output, MAX_GLB_BYTES, "staged character output")
        output_identity = _file_identity(output, output_bytes)
        output_sha = hashlib.sha256(output_bytes).hexdigest()
        if (
            hashlib.sha256(
                _read_regular(stage_path, MAX_GLB_BYTES, "pinned source staging")
            ).hexdigest()
            != raw_sha
        ):
            raise RuntimeError("pinned source staging changed during processing")
        out_low, out_high = _bounds([lod0], Vector)
        out_dims = out_high - out_low
        # Blender internal Y is GLB Z; internal Z is GLB Y.
        translation_gltf = [float(offset.x), float(offset.z), float(offset.y)]
        capsule = contract["capsule"]
        used_materials = {
            material for obj in (lod0, lod1) for material in obj.data.materials if material
        }
        used_images = {
            node.image
            for material in used_materials
            if material.use_nodes and material.node_tree is not None
            for node in material.node_tree.nodes
            if node.type == "TEX_IMAGE" and node.image is not None
        }
        texture_max_dimension = max(
            (
                max(int(image.size[0]), int(image.size[1]))
                for image in used_images
                if len(image.size) >= 2
            ),
            default=0,
        )
        report = {
            "status": "SUCCESS",
            "exit_code": 0,
            "contract_version": contract["contract_version"],
            "attempt_id": contract["attempt_id"],
            "asset_id": contract["asset_id"],
            "source_sha256": raw_sha,
            "output_sha256": output_sha,
            "spec_sha256": contract["spec_sha256"],
            "profile_id": contract["profile_id"],
            "profile_version": contract["profile_version"],
            "profile_sha256": contract["profile_sha256"],
            "script_sha256": contract["script_sha256"],
            "blender_version": f"Blender {bpy.app.version_string}",
            "transform": {
                "scale_mode": "uniform_height_fit",
                "uniform_scale": float(uniform_scale),
                "translation_gltf_m": translation_gltf,
                "origin_policy": policy,
            },
            "runtime_collider": {"policy": "capsule", **capsule},
            "raw_metrics": raw_metrics,
            "processed_metrics": {
                "coordinate_space": "glTF Y-up: X width, Y height, Z depth",
                "bounds_min": [float(out_low.x), float(out_low.z), float(out_low.y)],
                "bounds_max": [float(out_high.x), float(out_high.z), float(out_high.y)],
                "dimensions": [float(out_dims.x), float(out_dims.z), float(out_dims.y)],
                "lod0_triangles": lod0_triangles,
                "lod1_triangles": lod1_triangles,
                "lod1_method": lod1_method,
                "lod1_requested_ratio": float(contract["lod1_ratio"]),
                "lod1_actual_triangle_ratio": lod1_triangles / lod0_triangles,
                "materials": len(used_materials),
                "texture_max_dimension": texture_max_dimension,
                "mesh_nodes": [lod0.name, lod1.name],
                "collider_meshes": [],
                "capsule": {"radius_m": capsule["radius_m"], "height_m": capsule["height_m"]},
            },
            "duration_seconds": time.monotonic() - started,
        }
        report_bytes, report_identity = _write_report(report_path, report)
        succeeded = True
    finally:
        if stage_path is not None:
            _unlink_owned(stage_path, stage_identity)
        if not succeeded:
            _unlink_owned(output, output_identity)
            _unlink_owned(report_path, report_identity)


if __name__ == "__main__":
    main()
