"""Fail-closed decode of the bounded internal skinned GLB subset (ADR 0019)."""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.internal_skin_gltf_bounds import (
    _require_int,
    read_index_triangles,
    validate_active_scene,
    validate_embedded_glb_resources,
    validate_node_transforms_finite,
    validate_skin_joints,
    validate_skinned_primitive_structure,
)
from gamefactory.adapters.assets.validation_rules import InvalidGLB

InvalidInternalSkinGLB = InvalidGLB


@dataclass(frozen=True)
class SkinVertexWeights:
    joints: tuple[int, int, int, int]
    weights: tuple[float, float, float, float]


@dataclass(frozen=True)
class SkinnedPrimitive:
    node_index: int
    node_name: str
    positions: tuple[tuple[float, float, float], ...]
    vertex_weights: tuple[SkinVertexWeights, ...]
    skin_index: int


@dataclass(frozen=True)
class DecodedInternalSkinnedGLB:
    document: dict[str, Any]
    binary: bytes
    sha256: str
    asset_root_index: int
    skin_index: int
    skeleton_root_index: int
    joint_node_indices: tuple[int, ...]
    inverse_bind_matrices: tuple[tuple[float, ...], ...]
    joint_world_rest: dict[int, list[list[float]]]
    primitive: SkinnedPrimitive


def _glb_io() -> tuple[Any, Any, Any, Any]:
    from gamefactory.adapters.assets.glb_validator import (
        _accessor,
        _mat_mul,
        _node_matrix,
        _read_glb,
    )

    return _read_glb, _accessor, _mat_mul, _node_matrix


def _node_transform_is_identity(node: dict[str, Any]) -> bool:
    if "matrix" in node:
        return False
    t = node.get("translation", [0, 0, 0])
    r = node.get("rotation", [0, 0, 0, 1])
    s = node.get("scale", [1, 1, 1])
    if not all(abs(float(v)) < 1e-6 for v in t):
        return False
    if not (
        abs(float(r[0])) < 1e-6
        and abs(float(r[1])) < 1e-6
        and abs(float(r[2])) < 1e-6
        and abs(float(r[3]) - 1.0) < 1e-6
    ):
        return False
    return all(abs(float(v) - 1.0) < 1e-6 for v in s)


def _reject_extensions(document: dict[str, Any]) -> None:
    if document.get("extensionsRequired") or document.get("extensionsUsed"):
        raise InvalidGLB("glTF extensions are unsupported")
    if document.get("animations"):
        raise InvalidGLB("animations are forbidden in the internal skin subset")

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("extensions"):
                raise InvalidGLB("glTF extensions are unsupported")
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(document)


def _world_matrices(
    document: dict[str, Any], binary: bytes
) -> tuple[dict[int, int], list[list[list[float]]], dict[int, list[list[float]]]]:
    _, _, _mat_mul, _node_matrix = _glb_io()
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise InvalidGLB("nodes must be an array")
    parents: dict[int, int] = {}
    for parent, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise InvalidGLB("node entry is malformed")
        for child_raw in node.get("children", []):
            child = _require_int(child_raw, f"nodes[{parent}].children entry", min_value=0)
            if child >= len(nodes) or child in parents:
                raise InvalidGLB("invalid or multiply-parented node")
            parents[child] = parent
    local = [_node_matrix(node) for node in nodes]
    world: dict[int, list[list[float]]] = {}

    def world_of(index: int, trail: set[int]) -> list[list[float]]:
        if index in world:
            return world[index]
        if index in trail:
            raise InvalidGLB("node hierarchy cycle")
        if len(trail) >= 128:
            raise InvalidGLB("node hierarchy exceeds maximum depth 128")
        val = (
            local[index]
            if index not in parents
            else _mat_mul(world_of(parents[index], trail | {index}), local[index])
        )
        typed: list[list[float]] = val
        world[index] = typed
        return typed

    for i in range(len(nodes)):
        world_of(i, set())
    return parents, local, world


def _read_mat4_accessor(
    document: dict[str, Any], binary: bytes, index: int
) -> list[tuple[float, ...]]:
    accessors_list = document.get("accessors", [])
    index = _require_int(index, "inverse bind accessor index", min_value=0)
    if index >= len(accessors_list):
        raise InvalidGLB("inverse bind accessor index is invalid")
    accessor = accessors_list[index]
    if not isinstance(accessor, dict):
        raise InvalidGLB("inverse bind accessor is malformed")
    if accessor.get("type") != "MAT4" or accessor.get("componentType") != 5126:
        raise InvalidGLB("inverse bind accessor must be float MAT4")
    count = accessor.get("count")
    if not isinstance(count, int) or count <= 0:
        raise InvalidGLB("inverse bind accessor count is invalid")
    view_index = accessor.get("bufferView")
    if not isinstance(view_index, int) or view_index < 0:
        raise InvalidGLB("inverse bind accessor bufferView is missing")
    views = document.get("bufferViews", [])
    if view_index >= len(views):
        raise InvalidGLB("inverse bind accessor bufferView out of range")
    view = views[view_index]
    if not isinstance(view, dict):
        raise InvalidGLB("inverse bind bufferView is malformed")
    view_start = view.get("byteOffset", 0)
    view_len = view.get("byteLength", 0)
    if not isinstance(view_start, int) or view_start < 0:
        raise InvalidGLB("inverse bind bufferView byteOffset invalid")
    if not isinstance(view_len, int) or view_len < 64:
        raise InvalidGLB("inverse bind bufferView byteLength invalid")
    base = view_start + accessor.get("byteOffset", 0)
    if not isinstance(base, int) or base < view_start:
        raise InvalidGLB("inverse bind accessor byteOffset invalid")
    stride = view.get("byteStride", 64)
    if not isinstance(stride, int) or stride < 64:
        raise InvalidGLB("inverse bind bufferView byteStride invalid")
    end = base + (count - 1) * stride + 64
    if end > view_start + view_len or end > len(binary):
        raise InvalidGLB("MAT4 accessor out of bounds")
    matrices: list[tuple[float, ...]] = []
    for i in range(count):
        offset = base + i * stride
        values = struct.unpack_from("<16f", binary, offset)
        if any(not math.isfinite(v) for v in values):
            raise InvalidGLB("inverse bind matrix contains non-finite values")
        matrices.append(tuple(float(v) for v in values))
    return matrices


def _infer_skeleton_root_index(
    joint_node_indices: tuple[int, ...], parents: dict[int, int], asset_root_index: int
) -> int:
    """Resolve skeleton root when exporters omit ``skin.skeleton`` (Blender glTF)."""
    joint_set = set(joint_node_indices)
    roots: list[int] = []
    for joint in joint_node_indices:
        parent = parents.get(joint)
        while parent is not None and parent not in joint_set:
            if parent == asset_root_index:
                roots.append(joint)
                break
            parent = parents.get(parent)
        else:
            if parent is None:
                roots.append(joint)
    if len(roots) != 1:
        raise InvalidGLB("could not infer unique skeleton root from skin joints")
    return roots[0]


def _parse_joints_weights(
    document: dict[str, Any], binary: bytes, attrs: dict[str, Any], joint_count: int
) -> tuple[tuple[tuple[int, int, int, int], ...], tuple[tuple[float, float, float, float], ...]]:
    _, _accessor, _, _ = _glb_io()
    if "JOINTS_0" not in attrs or "WEIGHTS_0" not in attrs:
        raise InvalidGLB("skinned primitive requires JOINTS_0 and WEIGHTS_0")
    joints_accessor = document["accessors"][attrs["JOINTS_0"]]
    weights_accessor = document["accessors"][attrs["WEIGHTS_0"]]
    joint_component = joints_accessor.get("componentType")
    if joint_component not in {5121, 5123}:
        raise InvalidGLB("JOINTS_0 must be UNSIGNED_BYTE or UNSIGNED_SHORT")
    if weights_accessor.get("componentType") != 5126:
        raise InvalidGLB("WEIGHTS_0 must be float32")
    joints_raw = _accessor(document, binary, attrs["JOINTS_0"], "VEC4")
    weights_raw = _accessor(document, binary, attrs["WEIGHTS_0"], "VEC4")
    if len(joints_raw) != len(weights_raw):
        raise InvalidGLB("JOINTS_0 and WEIGHTS_0 counts differ")
    joint_rows: list[tuple[int, int, int, int]] = []
    weight_rows: list[tuple[float, float, float, float]] = []
    for jrow, wrow in zip(joints_raw, weights_raw, strict=True):
        joints = tuple(int(v) for v in jrow)
        weights = tuple(float(v) for v in wrow)
        if any(j < 0 or j >= joint_count for j in joints):
            raise InvalidGLB("JOINTS_0 references invalid joint index")
        if any(w < 0 or not math.isfinite(w) for w in weights):
            raise InvalidGLB("WEIGHTS_0 contains negative or non-finite values")
        joint_rows.append((int(joints[0]), int(joints[1]), int(joints[2]), int(joints[3])))
        weight_rows.append(
            (float(weights[0]), float(weights[1]), float(weights[2]), float(weights[3]))
        )
    return tuple(joint_rows), tuple(weight_rows)


def decode_internal_skinned_glb(
    path: Path, *, max_file_size_bytes: int = 50 * 1024 * 1024
) -> DecodedInternalSkinnedGLB:
    import hashlib

    _read_glb, _accessor, _, _ = _glb_io()
    doc, binary = _read_glb(path, max_file_size_bytes)
    validate_embedded_glb_resources(doc, binary)
    validate_node_transforms_finite(doc)
    _reject_extensions(doc)
    if doc.get("skins") is None or not isinstance(doc["skins"], list) or len(doc["skins"]) != 1:
        raise InvalidGLB("exactly one skin is required")
    skin = doc["skins"][0]
    if not isinstance(skin, dict):
        raise InvalidGLB("skin entry is malformed")
    joint_nodes = skin.get("joints")
    if not isinstance(joint_nodes, list) or not joint_nodes:
        raise InvalidGLB("skin joints are missing or invalid")
    nodes = doc["nodes"]
    if not isinstance(nodes, list):
        raise InvalidGLB("nodes must be an array")
    joint_parsed: list[int] = []
    for ji, entry in enumerate(joint_nodes):
        idx = _require_int(entry, f"skins[0].joints[{ji}]", min_value=0)
        if idx >= len(nodes):
            raise InvalidGLB("skin joint node index out of range")
        joint_parsed.append(idx)
    joint_node_indices = tuple(joint_parsed)
    parents, _, world = _world_matrices(doc, binary)

    _, asset_root_index, scene_reachable = validate_active_scene(doc, nodes)
    skeleton_raw = skin.get("skeleton")
    if skeleton_raw is None:
        skeleton_root = _infer_skeleton_root_index(joint_node_indices, parents, asset_root_index)
    else:
        skeleton_root = _require_int(skeleton_raw, "skin.skeleton", min_value=0)
        if skeleton_root >= len(nodes):
            raise InvalidGLB("skin skeleton root node index out of range")
        if skeleton_root not in joint_node_indices:
            raise InvalidGLB("skin skeleton root must be listed in joints")
    skel_parent = parents.get(skeleton_root)
    if skel_parent != asset_root_index:
        if skel_parent is None or parents.get(skel_parent) != asset_root_index:
            raise InvalidGLB(
                "skeleton root must be a direct child of the asset root "
                "(or of a single Blender armature wrapper under the asset root)"
            )
        if not _node_transform_is_identity(nodes[skel_parent]):
            raise InvalidGLB("armature wrapper node must have identity transform")
    validate_skin_joints(doc, list(joint_node_indices), skeleton_root)

    ibm_accessor = _require_int(
        skin.get("inverseBindMatrices"), "skin.inverseBindMatrices", min_value=0
    )
    matrices = _read_mat4_accessor(doc, binary, ibm_accessor)
    if len(matrices) != len(joint_node_indices):
        raise InvalidGLB("inverse bind matrix count does not match joint count")

    skinned_nodes: list[tuple[int, dict[str, Any]]] = []
    meshes = doc.get("meshes", [])
    for index, node in enumerate(nodes):
        mesh_index = node.get("mesh")
        if mesh_index is None:
            continue
        mesh_index = _require_int(mesh_index, f"nodes[{index}].mesh", min_value=0)
        if mesh_index >= len(meshes):
            raise InvalidGLB("node mesh index invalid")
        if index not in scene_reachable:
            raise InvalidGLB("skinned mesh node is not reachable from the active scene")
        mesh = meshes[mesh_index]
        for primitive in mesh.get("primitives", []):
            validate_skinned_primitive_structure(doc, primitive, len(joint_node_indices))
            skinned_nodes.append((index, primitive))

    if len(skinned_nodes) != 1:
        raise InvalidGLB("exactly one skinned mesh primitive is required")
    mesh_node_index, primitive = skinned_nodes[0]
    if parents.get(mesh_node_index) != asset_root_index:
        raise InvalidGLB("skinned mesh must be a direct child of the asset root")
    mesh_node = nodes[mesh_node_index]
    if not _node_transform_is_identity(mesh_node):
        raise InvalidGLB("skinned mesh node must have identity transform in internal subset")
    if not _node_transform_is_identity(nodes[asset_root_index]):
        raise InvalidGLB("asset root must have identity transform in internal subset")
    attrs = primitive["attributes"]
    positions = _accessor(doc, binary, attrs["POSITION"], "VEC3")
    read_index_triangles(doc, binary, primitive, len(positions))
    pos_tuple = tuple((float(r[0]), float(r[1]), float(r[2])) for r in positions)
    if (
        _require_int(nodes[mesh_node_index].get("skin"), "skinned node skin index", min_value=0)
        != 0
    ):
        raise InvalidGLB("skinned mesh node must reference skin index 0")
    joint_rows, weight_rows = _parse_joints_weights(doc, binary, attrs, len(joint_node_indices))
    vertex_weights = tuple(
        SkinVertexWeights(joints=j, weights=w) for j, w in zip(joint_rows, weight_rows, strict=True)
    )

    ibm_tuple: list[tuple[float, ...]] = []
    for row in matrices:
        if len(row) != 16:
            raise InvalidGLB("inverse bind matrix must have 16 components")
        ibm_tuple.append(tuple(float(v) for v in row))

    return DecodedInternalSkinnedGLB(
        document=doc,
        binary=binary,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        asset_root_index=asset_root_index,
        skin_index=0,
        skeleton_root_index=skeleton_root,
        joint_node_indices=joint_node_indices,
        inverse_bind_matrices=tuple(ibm_tuple),
        joint_world_rest={i: world[i] for i in joint_node_indices},
        primitive=SkinnedPrimitive(
            node_index=mesh_node_index,
            node_name=str(nodes[mesh_node_index].get("name") or ""),
            positions=pos_tuple,
            vertex_weights=vertex_weights,
            skin_index=0,
        ),
    )
