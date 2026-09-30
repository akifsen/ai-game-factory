"""Author a V0.7 assembly in real Blender and export it with Blender's glTF exporter.

Run: blender --background --factory-startup --python blender_author_assembly.py -- \
        --design design.json --output source.glb

The design JSON is produced by gamefactory's deterministic fixture generator in
the canonical glTF frame (+Y up, -Z front). The model is built facing Blender's
front view (-Y), exactly as an operator would, so the default exporter (Z-up to
Y-up) writes a source that faces glTF +Z. Parts are Empties named PART_<id>
with gf_motion/gf_axis custom properties (exported as node extras); meshes are
children with identity local transforms; sockets are Empties; the collider is a
box under ROOT. Nothing is normalized here: that is the factory's job.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path


def _args() -> tuple[Path, Path]:
    values = sys.argv[sys.argv.index("--") + 1 :] if "--" in sys.argv else []
    design = Path(values[values.index("--design") + 1])
    output = Path(values[values.index("--output") + 1])
    return design, output


def g2b(v: list[float]) -> tuple[float, float, float]:
    """glTF (+Y up) vector -> Blender (+Z up) vector, the inverse of the exporter's axis change."""
    return (float(v[0]), -float(v[2]), float(v[1]))


def main() -> None:
    import bmesh  # type: ignore[import-not-found]
    import bpy  # type: ignore[import-not-found]

    design_path, output = _args()
    design = json.loads(design_path.read_text(encoding="utf-8"))
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene

    def empty(name: str, parent: object | None) -> object:
        obj = bpy.data.objects.new(name, None)
        scene.collection.objects.link(obj)
        if parent is not None:
            obj.parent = parent
        return obj

    def box(name: str, parent: object, box_min: list[float], box_max: list[float]) -> object:
        corners = [
            g2b([x, y, z])
            for x in (box_min[0], box_max[0])
            for y in (box_min[1], box_max[1])
            for z in (box_min[2], box_max[2])
        ]
        lo = [min(c[i] for c in corners) for i in range(3)]
        hi = [max(c[i] for c in corners) for i in range(3)]
        mesh = bpy.data.meshes.new(name + "_mesh")
        bm = bmesh.new()
        bmesh.ops.create_cube(bm, size=1.0)
        for vert in bm.verts:
            vert.co.x = lo[0] if vert.co.x < 0 else hi[0]
            vert.co.y = lo[1] if vert.co.y < 0 else hi[1]
            vert.co.z = lo[2] if vert.co.z < 0 else hi[2]
        bmesh.ops.triangulate(bm, faces=bm.faces[:])
        bm.to_mesh(mesh)
        bm.free()
        obj = bpy.data.objects.new(name, mesh)
        scene.collection.objects.link(obj)
        obj.parent = parent
        return obj

    root = empty("ROOT", None)
    parts: dict[str, object] = {}
    for part in design["parts"]:
        parent = root if part["parent"] == "root" else parts[part["parent"]]
        node = empty(f"PART_{part['part_id']}", parent)
        node.location = g2b(part["position"])
        if part["parent"] == "root":
            # Facing Blender's front (-Y): the operator turns the root part half a turn.
            node.rotation_euler = (0.0, 0.0, math.pi)
        node["gf_motion"] = part["motion"]
        if part["motion"] != "fixed":
            node["gf_axis"] = [float(v) for v in part["axis"]]
        parts[part["part_id"]] = node
        box(
            f"SM_{design['asset_id']}_{part['part_id']}_LOD0",
            node,
            part["box_min"],
            part["box_max"],
        )
    for socket in design["sockets"]:
        node = empty(f"SOCKET_{socket['socket_id']}", parts[socket["parent_part"]])
        node.location = g2b(socket["translation"])
    collider = design["collider"]
    box(f"COL_{design['asset_id']}", root, collider["min"], collider["max"])
    bpy.context.view_layer.update()
    output.parent.mkdir(parents=True, exist_ok=True)
    result = bpy.ops.export_scene.gltf(
        filepath=str(output),
        export_format="GLB",
        export_yup=True,
        export_extras=True,
        export_apply=False,
        export_materials="NONE",
        export_cameras=False,
        export_lights=False,
        use_selection=False,
    )
    if "FINISHED" not in result or not output.is_file():
        raise RuntimeError(f"glTF export did not finish: {result}")
    print(
        json.dumps(
            {
                "blender_version": bpy.app.version_string,
                "output": str(output),
                "bytes": output.stat().st_size,
            }
        )
    )


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"AUTHORING_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        raise
