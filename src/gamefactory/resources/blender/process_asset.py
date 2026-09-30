"""Deterministic Blender background processor for the static_prop GLB subset."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Iterable
from pathlib import Path


def _args() -> argparse.Namespace:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-glb", required=True)
    parser.add_argument("--output-glb", required=True)
    parser.add_argument("--report-path", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--target-width", type=float, required=True)
    parser.add_argument("--target-depth", type=float, required=True)
    parser.add_argument("--target-height", type=float, required=True)
    parser.add_argument("--lod1-ratio", type=float, default=0.5)
    parser.add_argument(
        "--origin-policy", choices=("bottom_center", "center"), default="bottom_center"
    )
    parser.add_argument("--lod-policy", choices=("lod0_lod1", "lod0_only"), default="lod0_lod1")
    parser.add_argument("--contract", default="")
    return parser.parse_args(values)


def _assert_new_distinct_targets(source: Path, output: Path, report: Path) -> None:
    source_abs = Path(os.path.abspath(source))
    output_abs = Path(os.path.abspath(output))
    report_abs = Path(os.path.abspath(report))
    keys = {os.path.normcase(str(p)) for p in (source_abs, output_abs, report_abs)}
    if len(keys) != 3:
        raise ValueError("raw input, processed output, and report paths must be distinct")
    for path in (source_abs, output_abs, report_abs):
        current = path
        while True:
            is_junction = getattr(current, "is_junction", lambda: False)
            if current.is_symlink() or is_junction():
                raise ValueError(f"path traverses symlink/junction: {current}")
            if current.parent == current:
                break
            current = current.parent
    if not source_abs.is_file() or source_abs.suffix.casefold() != ".glb":
        raise ValueError("raw input must be an existing .glb file")
    for path, suffix in ((output_abs, ".glb"), (report_abs, ".json")):
        if path.exists() or path.is_symlink():
            raise ValueError(f"refusing to overwrite existing artifact: {path}")
        if path.suffix.casefold() != suffix:
            raise ValueError(f"output path must use {suffix}: {path}")


def _require_finished_export(operator_result: Iterable[object], output: Path) -> None:
    """Fail unless glTF export finished and the output file is already nonempty."""
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


def main() -> None:
    started = time.monotonic()
    args = _args()
    source, output, report_path = map(Path, (args.input_glb, args.output_glb, args.report_path))
    _assert_new_distinct_targets(source, output, report_path)
    if not 0.05 <= args.lod1_ratio <= 0.95:
        raise ValueError("lod1-ratio must be between 0.05 and 0.95")
    contract: dict = {}
    if args.contract:
        contract = json.loads(Path(args.contract).read_text(encoding="utf-8"))
        if not isinstance(contract, dict):
            raise ValueError("processing contract must be a JSON object")
    lod1_required = bool(contract.get("lod1_required", args.lod_policy == "lod0_lod1"))
    if args.lod_policy == "lod0_only" and lod1_required:
        raise ValueError("processing contract requires LOD1")
    if args.lod_policy not in {"lod0_lod1", "lod0_only"}:
        raise ValueError("unsupported lod policy")
    collider_policy = contract.get("collider_policy", "box") if contract else "box"
    if collider_policy not in {"box", "capsule"}:
        raise ValueError("this processor implements box and capsule colliders only")
    if contract and contract.get("geometry_mode", "single_mesh") != "single_mesh":
        raise ValueError("this processor handles single-mesh assets only")
    raw_hash = hashlib.sha256(source.read_bytes()).hexdigest()
    import bpy  # type: ignore[import-not-found]
    from mathutils import Vector  # type: ignore[import-not-found]

    bpy.ops.wm.read_factory_settings(use_empty=True)
    result = bpy.ops.import_scene.gltf(filepath=str(source))
    if "FINISHED" not in result:
        raise RuntimeError("Blender GLB importer did not finish")
    visual = [obj for obj in bpy.context.scene.objects if obj.type == "MESH"]
    if not visual:
        raise RuntimeError("input contains no mesh")
    if any(obj.parent for obj in visual):
        raise RuntimeError("nested mesh transforms are unsupported by this processor")
    if any(obj.type not in {"MESH", "EMPTY"} for obj in bpy.context.scene.objects):
        raise RuntimeError("rigged or non-static scene objects are unsupported")

    def bounds(objects: list) -> tuple[Vector, Vector]:
        corners = [obj.matrix_world @ Vector(corner) for obj in objects for corner in obj.bound_box]
        return Vector(tuple(min(v[i] for v in corners) for i in range(3))), Vector(
            tuple(max(v[i] for v in corners) for i in range(3))
        )

    raw_min, raw_max = bounds(visual)
    raw_dim = raw_max - raw_min
    if min(raw_dim) <= 1e-7 or not all(math.isfinite(v) for v in raw_dim):
        raise RuntimeError("input geometry has empty or non-finite bounds")
    raw_metrics = {
        "coordinate_space": "Blender internal Z-up (X width, Y depth, Z height)",
        "triangles": sum(
            sum(max(1, len(poly.vertices) - 2) for poly in obj.data.polygons) for obj in visual
        ),
        "materials": len({mat.name for obj in visual for mat in obj.data.materials if mat}),
        "dimensions": {"width_m": raw_dim.x, "depth_m": raw_dim.y, "height_m": raw_dim.z},
    }
    # Keep objects and material assignments intact; join only when multiple visual meshes exist.
    bpy.ops.object.select_all(action="DESELECT")
    for obj in visual:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = visual[0]
    if len(visual) > 1:
        bpy.ops.object.join()
    lod0 = bpy.context.view_layer.objects.active
    lod0.name = f"SM_{args.asset_id}_LOD0"
    lod0.data.name = f"SM_{args.asset_id}_LOD0_Mesh"
    dims = lod0.dimensions
    factors = (args.target_width / dims.x, args.target_depth / dims.y, args.target_height / dims.z)
    if not all(math.isfinite(value) and value > 0 for value in factors):
        raise RuntimeError("could not derive safe positive scale normalization")
    # Blender internal axes are X width, Y depth, Z height; the glTF exporter
    # performs the Z-up to Y-up conversion for Godot.
    lod0.scale = (
        lod0.scale.x * factors[0],
        lod0.scale.y * factors[1],
        lod0.scale.z * factors[2],
    )
    bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)
    low, high = bounds([lod0])
    lod0.location.x -= (low.x + high.x) / 2
    lod0.location.y -= (low.y + high.y) / 2
    lod0.location.z -= low.z if args.origin_policy == "bottom_center" else (low.z + high.z) / 2
    bpy.ops.object.transform_apply(location=True, rotation=False, scale=False)
    lod0_triangles = sum(max(1, len(poly.vertices) - 2) for poly in lod0.data.polygons)

    lod1 = None
    lod1_triangles = None
    if lod1_required:
        lod1 = lod0.copy()
        lod1.data = lod0.data.copy()
        lod1.name = f"SM_{args.asset_id}_LOD1"
        lod1.data.name = f"SM_{args.asset_id}_LOD1_Mesh"
        bpy.context.scene.collection.objects.link(lod1)
        decimate = lod1.modifiers.new("LOD1_Decimate", "DECIMATE")
        decimate.ratio = args.lod1_ratio
        bpy.context.view_layer.objects.active = lod1
        lod1.select_set(True)
        bpy.ops.object.modifier_apply(modifier=decimate.name)
        lod1_triangles = sum(max(1, len(poly.vertices) - 2) for poly in lod1.data.polygons)

    low, high = bounds([lod0])
    collider = None
    collider_triangles = None
    if collider_policy == "box":
        bpy.ops.mesh.primitive_cube_add(
            size=1.0, location=((low.x + high.x) / 2, (low.y + high.y) / 2, (low.z + high.z) / 2)
        )
        collider = bpy.context.object
        collider.name = f"COL_{args.asset_id}"
        collider.dimensions = high - low
        bpy.ops.object.transform_apply(location=False, rotation=False, scale=True)
        collider_triangles = sum(max(1, len(poly.vertices) - 2) for poly in collider.data.polygons)
    # A capsule collider carries no mesh; Godot builds CapsuleShape3D from the contract.
    selected_objects = [obj for obj in (lod0, lod1, collider) if obj is not None]
    for obj in bpy.context.scene.objects:
        obj.select_set(obj in selected_objects)
    bpy.context.view_layer.objects.active = lod0
    _assert_new_distinct_targets(source, output, report_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    export = bpy.ops.export_scene.gltf(
        filepath=str(output), export_format="GLB", use_selection=True
    )
    _require_finished_export(export, output)
    if hashlib.sha256(source.read_bytes()).hexdigest() != raw_hash:
        raise RuntimeError("raw GLB changed during processing")
    low, high = bounds([lod0])
    dimensions = high - low
    payload = {
        "status": "SUCCESS",
        "asset_id": args.asset_id,
        "profile_id": contract.get("profile_id"),
        "profile_version": contract.get("profile_version"),
        "processing_contract": contract or None,
        "blender_version": bpy.app.version_string,
        "input_raw_glb_sha256": raw_hash,
        "processing_script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "raw_metrics": raw_metrics,
        "processed_metrics": {
            "coordinate_space": "Blender internal Z-up (X width, Y depth, Z height)",
            "exported_gltf_coordinate_space": "Godot/glTF Y-up (X width, Y height, Z depth)",
            "lod0_triangles": lod0_triangles,
            "lod1_triangles": lod1_triangles,
            "collider_triangles": collider_triangles,
            "dimensions": {
                "width_m": dimensions.x,
                "depth_m": dimensions.y,
                "height_m": dimensions.z,
            },
            "bounds_min": list(low),
            "bounds_max": list(high),
            "lod1_ratio": args.lod1_ratio,
            "nodes": [item.name for item in selected_objects],
        },
        "duration_seconds": time.monotonic() - started,
        "exit_code": 0,
        "output_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    with report_path.open("x", encoding="utf-8") as report_file:
        report_file.write(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"PROCESSING_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
