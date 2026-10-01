"""Deterministic 12-bone skinned humanoid GLB for internal skin tests (ADR 0019)."""

from __future__ import annotations

import copy
import struct
from dataclasses import dataclass
from typing import Any, Literal

from gamefactory.adapters.assets.glb_io import read_glb, write_glb
from gamefactory.adapters.assets.internal_skin_math import invert_mat4
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _node_matrix(node: dict[str, Any]) -> list[list[float]]:
    if "matrix" in node:
        values = node["matrix"]
        return [[float(values[c * 4 + r]) for c in range(4)] for r in range(4)]
    t = node.get("translation", [0, 0, 0])
    s = node.get("scale", [1, 1, 1])
    q = node.get("rotation", [0, 0, 0, 1])
    x, y, z, w = (float(v) for v in q)
    norm = (x * x + y * y + z * z + w * w) ** 0.5
    x, y, z, w = (v / norm for v in (x, y, z, w))
    r = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    m = [[r[i][j] * float(s[j]) for j in range(3)] + [float(t[i])] for i in range(3)]
    return [*m, [0.0, 0.0, 0.0, 1.0]]


Variant = Literal[
    "positive",
    "weight_sum",
    "five_influences",
    "missing_bone",
    "wrong_parent",
    "inverse_bind_mismatch",
    "rest_mismatch",
    "rotated_leaf_rest",
    "invalid_joint_reference",
    "unweighted_vertex",
    "negative_weight",
    "nonfinite_weight",
    "malformed_accessor",
    "animation_present",
    "false_deformation_oracle",
]


@dataclass(frozen=True)
class _BoneDef:
    name: str
    parent: str
    translation: tuple[float, float, float]


_BONES: tuple[_BoneDef, ...] = (
    _BoneDef("Hips", "HumanoidRoot", (0.0, 1.0, 0.0)),
    _BoneDef("Spine", "Hips", (0.0, 0.2, 0.0)),
    _BoneDef("Chest", "Spine", (0.0, 0.2, 0.0)),
    _BoneDef("Neck", "Chest", (0.0, 0.15, 0.0)),
    _BoneDef("Head", "Neck", (0.0, 0.12, 0.0)),
    _BoneDef("LeftUpperArm", "Chest", (-0.12, 0.0, 0.0)),
    _BoneDef("LeftLowerArm", "LeftUpperArm", (-0.28, 0.0, 0.0)),
    _BoneDef("LeftHand", "LeftLowerArm", (-0.22, 0.0, 0.0)),
    _BoneDef("RightUpperArm", "Chest", (0.12, 0.0, 0.0)),
    _BoneDef("RightLowerArm", "RightUpperArm", (0.28, 0.0, 0.0)),
    _BoneDef("RightHand", "RightLowerArm", (0.22, 0.0, 0.0)),
    _BoneDef("LeftUpperLeg", "Hips", (-0.08, -0.05, 0.0)),
)


def _box_vertices(
    xmin: float,
    xmax: float,
    ymin: float,
    ymax: float,
    zmin: float,
    zmax: float,
) -> list[tuple[float, float, float]]:
    return [
        (xmin, ymin, zmin),
        (xmax, ymin, zmin),
        (xmax, ymin, zmax),
        (xmin, ymin, zmax),
        (xmin, ymax, zmin),
        (xmax, ymax, zmin),
        (xmax, ymax, zmax),
        (xmin, ymax, zmax),
    ]


def _invert4(m: list[list[float]]) -> list[list[float]]:
    return invert_mat4(m)


def _pack_f32(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _pack_u8(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}B", *values)


def _pack_u16(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}H", *values)


def _cube_triangle_indices(vertex_offset: int) -> list[int]:
    faces = (
        (0, 1, 2),
        (2, 3, 0),
        (4, 5, 6),
        (6, 7, 4),
        (0, 4, 7),
        (7, 3, 0),
        (1, 5, 6),
        (6, 2, 1),
        (0, 1, 5),
        (5, 4, 0),
        (3, 2, 6),
        (6, 7, 3),
    )
    return [vertex_offset + i for tri in faces for i in tri]


def build_humanoid_skinned_glb(variant: Variant = "positive") -> bytes:
    contract = load_internal_skin_contract()
    nodes: list[dict[str, Any]] = [{"name": contract.asset_root_name, "children": []}]
    name_to_index = {contract.asset_root_name: 0}
    for bone in _BONES:
        name_to_index[bone.name] = len(nodes)
        nodes.append(
            {
                "name": bone.name,
                "translation": list(bone.translation),
                "children": [],
            }
        )
    for bone in _BONES:
        parent_idx = name_to_index[bone.parent]
        child_idx = name_to_index[bone.name]
        nodes[parent_idx].setdefault("children", []).append(child_idx)

    torso = _box_vertices(-0.1, 0.1, 0.9, 1.15, -0.1, 0.1)
    arm = _box_vertices(-0.62, -0.18, 1.25, 1.55, -0.12, 0.12)
    positions = torso + arm
    spine_joint = 1  # index into skin.joints (Hips=0, Spine=1)
    lua_joint = 5
    joints_attr: list[tuple[int, int, int, int]] = []
    weights_attr: list[tuple[float, float, float, float]] = []
    for i in range(len(positions)):
        if i < len(torso):
            joints_attr.append((spine_joint, 0, 0, 0))
            weights_attr.append((1.0, 0.0, 0.0, 0.0))
        else:
            if variant == "false_deformation_oracle":
                joints_attr.append((spine_joint, 0, 0, 0))
                weights_attr.append((1.0, 0.0, 0.0, 0.0))
            else:
                joints_attr.append((lua_joint, 0, 0, 0))
                weights_attr.append((1.0, 0.0, 0.0, 0.0))

    if variant == "weight_sum":
        weights_attr[0] = (0.5, 0.3, 0.0, 0.0)
        joints_attr[0] = (spine_joint, lua_joint, 0, 0)
    elif variant == "five_influences":
        pass  # extra joint set added to primitive below
    elif variant == "unweighted_vertex":
        weights_attr[0] = (0.0, 0.0, 0.0, 0.0)
    elif variant == "negative_weight":
        weights_attr[0] = (1.1, -0.1, 0.0, 0.0)
        joints_attr[0] = (spine_joint, lua_joint, 0, 0)
    elif variant == "nonfinite_weight":
        weights_attr[0] = (float("nan"), 0.0, 0.0, 0.0)
    elif variant == "invalid_joint_reference":
        joints_attr[0] = (99, 0, 0, 0)

    indices = _cube_triangle_indices(0) + _cube_triangle_indices(len(torso))

    pos_bytes = _pack_f32([c for v in positions for c in v])
    joint_bytes = _pack_u8([c for quad in joints_attr for c in quad])
    weight_bytes = _pack_f32([c for quad in weights_attr for c in quad])
    index_bytes = _pack_u16(indices)
    normal_bytes = _pack_f32([0.0, 1.0, 0.0] * len(positions))

    blob = pos_bytes + joint_bytes + weight_bytes + index_bytes + normal_bytes
    views = [
        {"buffer": 0, "byteOffset": 0, "byteLength": len(pos_bytes)},
        {
            "buffer": 0,
            "byteOffset": len(pos_bytes),
            "byteLength": len(joint_bytes),
        },
        {
            "buffer": 0,
            "byteOffset": len(pos_bytes) + len(joint_bytes),
            "byteLength": len(weight_bytes),
        },
        {
            "buffer": 0,
            "byteOffset": len(pos_bytes) + len(joint_bytes) + len(weight_bytes),
            "byteLength": len(index_bytes),
        },
        {
            "buffer": 0,
            "byteOffset": len(pos_bytes) + len(joint_bytes) + len(weight_bytes) + len(index_bytes),
            "byteLength": len(normal_bytes),
        },
    ]
    accessors = [
        {
            "bufferView": 0,
            "componentType": 5126,
            "count": len(positions),
            "type": "VEC3",
            "min": [min(p[i] for p in positions) for i in range(3)],
            "max": [max(p[i] for p in positions) for i in range(3)],
        },
        {
            "bufferView": 1,
            "componentType": 5121,
            "count": len(positions),
            "type": "VEC4",
        },
        {
            "bufferView": 2,
            "componentType": 5126,
            "count": len(positions),
            "type": "VEC4",
        },
        {
            "bufferView": 3,
            "componentType": 5123,
            "count": len(indices),
            "type": "SCALAR",
        },
        {
            "bufferView": 4,
            "componentType": 5126,
            "count": len(positions),
            "type": "VEC3",
        },
    ]
    if variant == "malformed_accessor":
        accessors[0]["count"] = len(positions) + 5

    mesh_index = len(nodes)
    nodes.append(
        {
            "name": "SM_HumanoidSkin",
            "mesh": 0,
            "skin": 0,
        }
    )
    nodes[0]["children"] = [mesh_index, name_to_index["Hips"]]

    if variant == "missing_bone":
        nodes[name_to_index["LeftHand"]]["name"] = "LeftHandMissing"
    if variant == "wrong_parent":
        nodes[name_to_index["Chest"]]["children"].remove(name_to_index["LeftUpperArm"])
        nodes[name_to_index["Hips"]]["children"].append(name_to_index["LeftUpperArm"])
    if variant == "rest_mismatch":
        nodes[name_to_index["LeftUpperArm"]]["translation"] = [0.5, 0.0, 0.0]
    if variant == "rotated_leaf_rest":
        nodes[name_to_index["LeftUpperLeg"]]["rotation"] = [0.0, 1.0, 0.0, 0.0]

    joint_nodes = [name_to_index[b.name] for b in _BONES]
    parents: dict[int, int] = {}
    for parent, node in enumerate(nodes):
        for child in node.get("children", []):
            parents[child] = parent
    local = [_node_matrix(n) for n in nodes]
    world: dict[int, list[list[float]]] = {}

    def world_of(index: int, depth: int = 0) -> list[list[float]]:
        if index in world:
            return world[index]
        if depth > 128:
            raise ValueError("depth")
        val = (
            local[index]
            if index not in parents
            else _mat_mul(world_of(parents[index], depth + 1), local[index])
        )
        world[index] = val
        return val

    for idx in joint_nodes:
        world_of(idx)
    ibm_flat: list[float] = []
    for idx in joint_nodes:
        inv = _invert4(world[idx])
        ibm_flat.extend(inv[c][r] for r in range(4) for c in range(4))

    if variant == "inverse_bind_mismatch":
        ibm_flat[0] += 0.5

    ibm_offset = len(blob)
    blob += _pack_f32(ibm_flat)
    views.append(
        {
            "buffer": 0,
            "byteOffset": ibm_offset,
            "byteLength": len(ibm_flat) * 4,
        }
    )
    ibm_accessor_index = len(accessors)
    accessors.append(
        {
            "bufferView": len(views) - 1,
            "componentType": 5126,
            "count": len(joint_nodes),
            "type": "MAT4",
        }
    )

    primitive_attrs: dict[str, int] = {
        "POSITION": 0,
        "NORMAL": 4,
        "JOINTS_0": 1,
        "WEIGHTS_0": 2,
    }
    if variant == "five_influences":
        primitive_attrs["JOINTS_1"] = 1
        primitive_attrs["WEIGHTS_1"] = 2

    document: dict[str, Any] = {
        "asset": {"version": "2.0"},
        "scene": 0,
        "scenes": [{"nodes": [0]}],
        "nodes": nodes,
        "meshes": [
            {
                "name": "HumanoidMesh",
                "primitives": [
                    {"attributes": primitive_attrs, "indices": 3, "mode": 4},
                ],
            }
        ],
        "skins": [
            {
                "inverseBindMatrices": ibm_accessor_index,
                "joints": joint_nodes,
                "skeleton": name_to_index["Hips"],
            }
        ],
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": views,
        "accessors": accessors,
    }
    if variant == "animation_present":
        document["animations"] = [{"channels": [], "samplers": []}]

    return write_glb(document, blob)


def humanoid_fixture_sha256() -> str:
    import hashlib

    return hashlib.sha256(build_humanoid_skinned_glb("positive")).hexdigest()


def mutate_glb_document(glb: bytes, mutator: Any) -> bytes:
    document, binary = read_glb(glb)
    document = copy.deepcopy(document)
    mutator(document)
    return write_glb(document, binary)
