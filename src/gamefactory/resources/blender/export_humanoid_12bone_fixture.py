"""Blender entry: build and export the 12-bone humanoid skin fixture (ADR 0019).

Usage:
  blender --background --python export_humanoid_12bone_fixture.py -- <output.glb>

Godot/glTF joint space is +Y up, -Z forward. Blender edit/armature space uses
``(x, -z, y)`` from that frame before export (Blender glTF exporter emits Y-up).
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

try:
    import bpy  # type: ignore[import-not-found]
    from mathutils import Vector  # type: ignore[import-not-found]
except ImportError:
    raise SystemExit("this script must be run inside Blender (bpy missing)") from None

# Local translations in Godot/glTF space (same as humanoid_skin_fixture / contract).
_BONE_LOCALS: tuple[tuple[str, str, tuple[float, float, float]], ...] = (
    ("Hips", "HumanoidRoot", (0.0, 1.0, 0.0)),
    ("Spine", "Hips", (0.0, 0.2, 0.0)),
    ("Chest", "Spine", (0.0, 0.2, 0.0)),
    ("Neck", "Chest", (0.0, 0.15, 0.0)),
    ("Head", "Neck", (0.0, 0.12, 0.0)),
    ("LeftUpperArm", "Chest", (-0.12, 0.0, 0.0)),
    ("LeftLowerArm", "LeftUpperArm", (-0.28, 0.0, 0.0)),
    ("LeftHand", "LeftLowerArm", (-0.22, 0.0, 0.0)),
    ("RightUpperArm", "Chest", (0.12, 0.0, 0.0)),
    ("RightLowerArm", "RightUpperArm", (0.28, 0.0, 0.0)),
    ("RightHand", "RightLowerArm", (0.22, 0.0, 0.0)),
    ("LeftUpperLeg", "Hips", (-0.08, -0.05, 0.0)),
)

# Cumulative joint origins in Godot/glTF space (matches humanoid_12bone_contract.json).
_GODOT_GLOBALS: dict[str, tuple[float, float, float]] = {
    "Hips": (0.0, 1.0, 0.0),
    "Spine": (0.0, 1.2, 0.0),
    "Chest": (0.0, 1.4, 0.0),
    "Neck": (0.0, 1.55, 0.0),
    "Head": (0.0, 1.67, 0.0),
    "LeftUpperArm": (-0.12, 1.4, 0.0),
    "LeftLowerArm": (-0.4, 1.4, 0.0),
    "LeftHand": (-0.62, 1.4, 0.0),
    "RightUpperArm": (0.12, 1.4, 0.0),
    "RightLowerArm": (0.4, 1.4, 0.0),
    "RightHand": (0.62, 1.4, 0.0),
    "LeftUpperLeg": (-0.08, 0.95, 0.0),
}


def _godot_to_blender(vec: tuple[float, float, float]) -> tuple[float, float, float]:
    x, y, z = vec
    return (x, -z, y)


def _clear_scene() -> None:
    bpy.ops.object.select_all(action="SELECT")
    bpy.ops.object.delete(use_global=False)


def _box_godot(
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    zmin: float,
    zmax: float,
) -> Any:
    cx = (xmin + xmax) / 2
    cy = (ymin + ymax) / 2
    cz = (zmin + zmax) / 2
    sx = xmax - xmin
    sy = ymax - ymin
    sz = zmax - zmin
    loc = _godot_to_blender((cx, cy, cz))
    bpy.ops.mesh.primitive_cube_add(size=1.0, location=loc)
    obj = bpy.context.active_object
    obj.scale = (sx / 2, sz / 2, sy / 2)
    bpy.ops.object.transform_apply(scale=True)
    return obj


def _build_rig(root: Any) -> Any:
    bpy.ops.object.armature_add(location=(0.0, 0.0, 0.0))
    arm_obj = bpy.context.active_object
    arm_obj.name = "HumanoidArmature"
    arm_obj.parent = root
    bpy.ops.object.mode_set(mode="EDIT")
    edit = arm_obj.data.edit_bones
    for bone in list(edit):
        edit.remove(bone)
    bone_map: dict[str, Any] = {}
    for name, _parent, _local in _BONE_LOCALS:
        bone = edit.new(name)
        head_g = _GODOT_GLOBALS[name]
        head = Vector(_godot_to_blender(head_g))
        bone.head = head
        bone.tail = head + Vector((0.0, 0.0, 0.08))
        bone_map[name] = bone
    for name, parent, _ in _BONE_LOCALS:
        if parent != "HumanoidRoot":
            bone_map[name].parent = bone_map[parent]
    bpy.ops.object.mode_set(mode="OBJECT")
    return arm_obj


def _skin_mesh(mesh_obj: Any, arm_obj: Any) -> None:
    bpy.context.view_layer.objects.active = mesh_obj
    mesh_obj.select_set(True)
    spine = mesh_obj.vertex_groups.new(name="Spine")
    lua = mesh_obj.vertex_groups.new(name="LeftUpperArm")
    for vert in mesh_obj.data.vertices:
        gx, gy = vert.co.x, vert.co.z
        if gx < -0.15 and gy > 1.2:
            lua.add([vert.index], 1.0, "REPLACE")
        else:
            spine.add([vert.index], 1.0, "REPLACE")
    mod = mesh_obj.modifiers.new("Armature", "ARMATURE")
    mod.object = arm_obj


def _select_hierarchy(obj: Any) -> None:
    obj.select_set(True)
    for child in obj.children:
        _select_hierarchy(child)


def export_humanoid_glb(output_path: Path) -> None:
    _clear_scene()
    root = bpy.data.objects.new("HumanoidRoot", None)
    bpy.context.collection.objects.link(root)
    arm = _build_rig(root)
    torso = _box_godot(-0.1, 0.1, 0.9, 1.15, -0.1, 0.1)
    arm_mesh = _box_godot(-0.62, -0.18, 1.25, 1.55, -0.12, 0.12)
    bpy.ops.object.select_all(action="DESELECT")
    torso.select_set(True)
    arm_mesh.select_set(True)
    bpy.context.view_layer.objects.active = torso
    bpy.ops.object.join()
    mesh_obj = bpy.context.active_object
    mesh_obj.name = "SM_HumanoidSkin"
    mesh_obj.parent = root
    _skin_mesh(mesh_obj, arm)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    bpy.ops.object.select_all(action="DESELECT")
    _select_hierarchy(root)
    bpy.context.view_layer.objects.active = root
    bpy.ops.export_scene.gltf(
        filepath=str(output_path),
        export_format="GLB",
        use_selection=True,
        export_apply=True,
        export_skins=True,
        export_morph=False,
        export_materials="NONE",
        export_texcoords=False,
        export_normals=False,
        export_tangents=False,
    )


def main() -> None:
    argv = sys.argv
    if "--" not in argv:
        raise SystemExit(
            "usage: blender ... --python export_humanoid_12bone_fixture.py -- <out.glb>"
        )
    out = Path(argv[argv.index("--") + 1])
    export_humanoid_glb(out)


if __name__ == "__main__":
    main()
