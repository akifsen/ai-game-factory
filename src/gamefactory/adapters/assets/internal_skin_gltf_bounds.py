"""Strict embedded-GLB container checks for the internal skin subset (ADR 0019)."""

from __future__ import annotations

import math
from typing import Any

from gamefactory.adapters.assets.validation_rules import InvalidGLB

_MAX_ACCESSOR_ELEMENTS = 1_000_000
_COMPONENTS = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_TYPE_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


def _require_int(value: Any, label: str, *, min_value: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise InvalidGLB(f"{label} must be an integer")
    if min_value is not None and value < min_value:
        raise InvalidGLB(f"{label} must be >= {min_value}")
    return value


def _require_nonneg_int(value: Any, label: str) -> int:
    return _require_int(value, label, min_value=0)


def validate_embedded_glb_resources(document: dict[str, Any], binary: bytes) -> None:
    """Reject external URIs, out-of-range views/accessors, and forbidden glTF features."""
    if document.get("images"):
        raise InvalidGLB("images are unsupported in the internal skin subset")
    if document.get("textures") or document.get("materials"):
        raise InvalidGLB("materials and textures are unsupported in the internal skin subset")

    buffers = document.get("buffers")
    if not isinstance(buffers, list) or len(buffers) != 1:
        raise InvalidGLB("exactly one embedded buffer is required")
    buffer0 = buffers[0]
    if not isinstance(buffer0, dict):
        raise InvalidGLB("buffer entry is malformed")
    if buffer0.get("uri") is not None:
        raise InvalidGLB("external buffer URIs are forbidden")
    declared_len = _require_nonneg_int(buffer0.get("byteLength"), "buffers[0].byteLength")
    if declared_len != len(binary):
        raise InvalidGLB("buffer byteLength does not match GLB BIN chunk")

    views = document.get("bufferViews")
    if not isinstance(views, list):
        raise InvalidGLB("bufferViews must be an array")
    for vi, view in enumerate(views):
        if not isinstance(view, dict):
            raise InvalidGLB("bufferView entry is malformed")
        if _require_int(view.get("buffer"), f"bufferViews[{vi}].buffer", min_value=0) != 0:
            raise InvalidGLB("only buffer index 0 is supported")
        byte_offset = _require_nonneg_int(
            view.get("byteOffset", 0), f"bufferViews[{vi}].byteOffset"
        )
        byte_length = _require_nonneg_int(view.get("byteLength"), f"bufferViews[{vi}].byteLength")
        if byte_length == 0:
            raise InvalidGLB("bufferView byteLength must be positive")
        end = byte_offset + byte_length
        if end > declared_len or end > len(binary):
            raise InvalidGLB("bufferView extends past buffer bounds")
        stride = view.get("byteStride")
        if stride is not None:
            stride_i = _require_int(stride, f"bufferViews[{vi}].byteStride", min_value=1)

    accessors = document.get("accessors")
    if not isinstance(accessors, list):
        raise InvalidGLB("accessors must be an array")

    for ai, accessor in enumerate(accessors):
        if not isinstance(accessor, dict):
            raise InvalidGLB("accessor entry is malformed")
        if accessor.get("sparse") is not None:
            raise InvalidGLB("sparse accessors are unsupported")
        if accessor.get("normalized"):
            raise InvalidGLB("normalized accessors are unsupported")
        typ = accessor.get("type")
        component = accessor.get("componentType")
        if typ not in _TYPE_WIDTH or component not in _COMPONENTS:
            raise InvalidGLB("accessor type or componentType is unsupported")
        count = _require_int(accessor.get("count"), f"accessors[{ai}].count", min_value=1)
        if count > _MAX_ACCESSOR_ELEMENTS:
            raise InvalidGLB("accessor count exceeds safety limit")
        view_index = accessor.get("bufferView")
        if view_index is None:
            raise InvalidGLB("accessor bufferView is required in the internal subset")
        view_index = _require_int(view_index, f"accessors[{ai}].bufferView", min_value=0)
        if view_index >= len(views):
            raise InvalidGLB("accessor bufferView index out of range")
        view = views[view_index]
        view_start = _require_nonneg_int(view.get("byteOffset", 0), "bufferView byteOffset")
        view_len = _require_nonneg_int(view.get("byteLength"), "bufferView byteLength")
        acc_offset = _require_nonneg_int(
            accessor.get("byteOffset", 0), f"accessors[{ai}].byteOffset"
        )
        base = view_start + acc_offset
        if base < view_start or base > view_start + view_len:
            raise InvalidGLB("accessor byteOffset outside bufferView")
        width = _TYPE_WIDTH[typ]
        component_size = _COMPONENTS[component]
        packed = component_size * width
        stride = view.get("byteStride", packed)
        if stride is not None:
            stride_i = _require_int(
                stride, f"bufferViews[{view_index}].byteStride", min_value=packed
            )
        else:
            stride_i = packed
        end = base + (count - 1) * stride_i + packed
        if end > view_start + view_len:
            raise InvalidGLB("accessor data extends past bufferView bounds")


def validate_skinned_primitive_structure(
    document: dict[str, Any], primitive: dict[str, Any], joint_count: int
) -> None:
    if not isinstance(primitive, dict):
        raise InvalidGLB("mesh primitive is malformed")
    if primitive.get("targets"):
        raise InvalidGLB("morph targets are unsupported")
    if primitive.get("extensions"):
        raise InvalidGLB("glTF extensions are unsupported")
    mode = primitive.get("mode", 4)
    if mode != 4:
        raise InvalidGLB("only triangle primitives are supported")
    attrs = primitive.get("attributes")
    if not isinstance(attrs, dict):
        raise InvalidGLB("primitive attributes are missing or invalid")
    for key in attrs:
        if key.startswith("JOINTS_") and key != "JOINTS_0":
            raise InvalidGLB("only one joint influence set (JOINTS_0) is supported")
        if key.startswith("WEIGHTS_") and key != "WEIGHTS_0":
            raise InvalidGLB("only one weight set (WEIGHTS_0) is supported")
    accessors = document.get("accessors")
    if not isinstance(accessors, list):
        raise InvalidGLB("accessors must be an array")

    def _accessor_in_range(acc_idx: Any, label: str) -> int:
        idx = _require_int(acc_idx, label, min_value=0)
        if idx >= len(accessors):
            raise InvalidGLB(f"{label} accessor index out of range")
        if not isinstance(accessors[idx], dict):
            raise InvalidGLB(f"{label} accessor entry is malformed")
        return idx

    for key, acc_idx in attrs.items():
        _accessor_in_range(acc_idx, f"attribute {key}")

    pos_idx = attrs.get("POSITION")
    if pos_idx is None:
        raise InvalidGLB("POSITION attribute is required")
    pos_idx = _accessor_in_range(pos_idx, "POSITION")
    pos_count = _require_int(accessors[pos_idx].get("count"), "POSITION count", min_value=1)
    if "JOINTS_0" in attrs and "WEIGHTS_0" in attrs:
        j_idx = _accessor_in_range(attrs["JOINTS_0"], "JOINTS_0")
        w_idx = _accessor_in_range(attrs["WEIGHTS_0"], "WEIGHTS_0")
        if accessors[j_idx].get("count") != pos_count or accessors[w_idx].get("count") != pos_count:
            raise InvalidGLB("JOINTS_0 and WEIGHTS_0 accessor counts must match POSITION")

    indices_idx = primitive.get("indices")
    if indices_idx is not None:
        indices_idx = _require_int(indices_idx, "primitive.indices", min_value=0)
        if indices_idx >= len(accessors):
            raise InvalidGLB("indices accessor is invalid")
        idx_acc = accessors[indices_idx]
        idx_count = _require_int(idx_acc.get("count"), "indices count", min_value=3)
        if idx_count % 3 != 0:
            raise InvalidGLB("triangle index count must be a multiple of three")
        comp = idx_acc.get("componentType")
        if comp not in {5121, 5123, 5125}:
            raise InvalidGLB("indices component type is unsupported")
    else:
        if pos_count % 3 != 0:
            raise InvalidGLB("non-indexed triangle primitive requires a multiple of three vertices")


def read_index_triangles(
    document: dict[str, Any], binary: bytes, primitive: dict[str, Any], vertex_count: int
) -> None:
    """Validate index stream references and triangle corners."""
    indices_idx = primitive.get("indices")
    if indices_idx is None:
        return
    from gamefactory.adapters.assets.glb_validator import _accessor

    raw = _accessor(document, binary, indices_idx, "SCALAR")
    for row in raw:
        idx = int(row[0])
        if idx < 0 or idx >= vertex_count:
            raise InvalidGLB("triangle index references out-of-range vertex")


def validate_skin_joints(
    document: dict[str, Any], joint_nodes: list[int], skeleton_root: int
) -> None:
    nodes = document.get("nodes", [])
    if len(set(joint_nodes)) != len(joint_nodes):
        raise InvalidGLB("skin joints must be unique")
    for ji, j in enumerate(joint_nodes):
        idx = _require_int(j, f"skin.joints[{ji}]", min_value=0)
        if idx >= len(nodes):
            raise InvalidGLB("skin joint node index is invalid")
    reachable: set[int] = set()

    def walk(index: int) -> None:
        if index in reachable:
            return
        reachable.add(index)
        node = nodes[index]
        if not isinstance(node, dict):
            raise InvalidGLB("node entry is malformed")
        for child_raw in node.get("children", []):
            child = _require_int(child_raw, "node child index", min_value=0)
            if child >= len(nodes):
                raise InvalidGLB("invalid child node index")
            walk(child)

    walk(skeleton_root)
    missing = [j for j in joint_nodes if j not in reachable]
    if missing:
        raise InvalidGLB("skin joints must be reachable from skeleton root")
    if skeleton_root not in joint_nodes:
        raise InvalidGLB("skin skeleton root must be listed in joints")


def validate_active_scene(document: dict[str, Any], nodes: list[Any]) -> tuple[int, int, set[int]]:
    """Validate scenes/active scene and return (scene_index, sole root index, reachable nodes)."""
    scenes = document.get("scenes")
    if not isinstance(scenes, list) or not scenes:
        raise InvalidGLB("scenes must be a non-empty array")
    for si, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            raise InvalidGLB("scene entry is malformed")
        scene_nodes = scene.get("nodes", [])
        if not isinstance(scene_nodes, list):
            raise InvalidGLB("scene nodes must be an array")
        for ri, root in enumerate(scene_nodes):
            _require_int(root, f"scenes[{si}].nodes[{ri}]", min_value=0)

    scene_index = _require_int(document.get("scene", 0), "scene", min_value=0)
    if scene_index >= len(scenes):
        raise InvalidGLB("active scene index out of range")
    active = scenes[scene_index]
    if not isinstance(active, dict):
        raise InvalidGLB("active scene entry is malformed")
    roots = active.get("nodes", [])
    if not isinstance(roots, list) or len(roots) != 1:
        raise InvalidGLB("internal subset requires a single scene root")
    asset_root_index = _require_int(roots[0], "active scene root node index", min_value=0)
    if asset_root_index >= len(nodes):
        raise InvalidGLB("scene root node index out of range")

    parents: dict[int, int] = {}
    for parent, node in enumerate(nodes):
        if not isinstance(node, dict):
            raise InvalidGLB("node entry is malformed")
        for child_raw in node.get("children", []):
            child = _require_int(child_raw, f"nodes[{parent}].children entry", min_value=0)
            if child >= len(nodes) or child in parents:
                raise InvalidGLB("invalid or multiply-parented node")
            parents[child] = parent

    if asset_root_index in parents:
        raise InvalidGLB("scene root must not have a parent node")

    reachable: set[int] = set()

    def visit(index: int, trail: set[int]) -> None:
        if index in trail:
            raise InvalidGLB("node hierarchy cycle")
        if len(trail) >= 128:
            raise InvalidGLB("node hierarchy exceeds maximum depth 128")
        if index in reachable:
            return
        reachable.add(index)
        node = nodes[index]
        if not isinstance(node, dict):
            raise InvalidGLB("node entry is malformed")
        for child_raw in node.get("children", []):
            child = _require_int(child_raw, f"nodes[{index}].children entry", min_value=0)
            if child >= len(nodes):
                raise InvalidGLB("scene references invalid node")
            visit(child, trail | {index})

    visit(asset_root_index, set())
    if len(reachable) != len(nodes):
        raise InvalidGLB("all nodes must be reachable from the active scene root")
    return scene_index, asset_root_index, reachable


def validate_node_transforms_finite(document: dict[str, Any]) -> None:
    nodes = document.get("nodes", [])
    if not isinstance(nodes, list):
        raise InvalidGLB("nodes must be an array")
    for node in nodes:
        if not isinstance(node, dict):
            raise InvalidGLB("node entry is malformed")
        for key in ("translation", "scale", "rotation", "matrix"):
            if key not in node:
                continue
            values = node[key]
            if not isinstance(values, list):
                raise InvalidGLB(f"node {key} must be an array")
            for v in values:
                if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                    raise InvalidGLB(f"node {key} contains non-finite values")
