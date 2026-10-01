#!/usr/bin/env python3
"""Cold, standard-library-only verification of internal rig evidence (V0.8-2)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import struct
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXCLUDED = {"manifest.json"}
_MAX_FILE_BYTES = 50_000_000
_MAX_BUNDLE_BYTES = 256_000_000
_MAX_JSON_BYTES = 4_000_000
_MAX_JSON_DEPTH = 64
_MAX_JSON_ARRAY_LEN = 100_000
_MAX_JSON_OBJECT_KEYS = 10_000
_MAX_RUNTIME_VERTICES = 4096
_WEIGHT_SUM_TOL = 1e-3
_IBM_TOL = 1e-3
_BASIS_TOL = 0.08

VALIDATOR_CONTRACT_VERSION = "humanoid_12bone_v1"
PINNED_CONTRACT_CANONICAL_SHA256 = (
    "670e55fc5ea867bb546ff29f03c10bd05e65a6707a79954eb3f1d3e81070245e"
)
PINNED_BLENDER_EXPORT_SCRIPT_SHA256 = (
    "43f321887b2ffba010e5876d3a5fd21d88fa04d8a0cec3e09bd5996f30cbe769"
)
PINNED_GODOT_HARNESS_REVIEWED_SHA256 = (
    "d4f406daded207808e5bbdb8d4501c23803136a7f7449a59dc9a47ae0fc1fcea"
)

_SINGLE_ROLES = {
    "skinned_glb",
    "rig_verification_contract",
    "rig_validation_report",
    "rig_runtime_request",
    "rig_runtime_observation",
    "reviewed_blender_export_script",
    "reviewed_godot_harness",
    "source_declaration",
}


class InvalidGLB(ValueError):
    pass


_COMPONENTS_GLB = {
    5120: (1, "b"),
    5121: (1, "B"),
    5122: (2, "h"),
    5123: (2, "H"),
    5125: (4, "I"),
    5126: (4, "f"),
}
_COMPONENTS = {5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4}
_TYPE_WIDTH = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}
_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
_MAX_ACCESSOR_ELEMENTS = 1_000_000


def invert_mat4(m: list[list[float]]) -> list[list[float]]:
    """Invert a 4x4 matrix with partial pivoting (row operations)."""
    a = [[float(m[r][c]) for c in range(4)] for r in range(4)]
    inv = [[0.0] * 4 for _ in range(4)]
    for i in range(4):
        inv[i][i] = 1.0
    for col in range(4):
        pivot_row = col
        pivot_val = abs(a[pivot_row][col])
        for row in range(col + 1, 4):
            if abs(a[row][col]) > pivot_val:
                pivot_row = row
                pivot_val = abs(a[row][col])
        if pivot_val < 1e-12:
            raise ValueError("singular matrix")
        if pivot_row != col:
            a[col], a[pivot_row] = a[pivot_row], a[col]
            inv[col], inv[pivot_row] = inv[pivot_row], inv[col]
        pivot = a[col][col]
        inv_p = 1.0 / pivot
        for j in range(4):
            a[col][j] *= inv_p
            inv[col][j] *= inv_p
        for row in range(4):
            if row == col:
                continue
            factor = a[row][col]
            if abs(factor) < 1e-15:
                continue
            for j in range(4):
                a[row][j] -= factor * a[col][j]
                inv[row][j] -= factor * inv[col][j]
    return inv


def mat3_basis_columns(
    world: list[list[float]],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """Return world +Y and -Z basis vectors from a joint world matrix (glTF column basis)."""
    y_axis = (world[0][1], world[1][1], world[2][1])
    neg_z = (-world[0][2], -world[1][2], -world[2][2])
    return y_axis, neg_z


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _read_glb(path: Path, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    size = path.stat().st_size
    if path.suffix.casefold() != ".glb":
        raise InvalidGLB("artifact extension must be .glb")
    if size < 20 or size > max_bytes:
        raise InvalidGLB(f"file size {size} is outside 20..{max_bytes} bytes")
    data = path.read_bytes()
    magic, version, total = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or total != len(data):
        raise InvalidGLB("invalid GLB magic, version, or declared total length")
    offset = 12
    json_chunk: bytes | None = None
    bin_chunk: bytes | None = None
    while offset < total:
        if offset + 8 > total:
            raise InvalidGLB("truncated GLB chunk header")
        length, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        end = offset + length
        if length % 4 or end > total:
            raise InvalidGLB("invalid or out-of-bounds GLB chunk length")
        chunk = data[offset:end]
        if kind == 0x4E4F534A:
            if json_chunk is not None or offset != 20:
                raise InvalidGLB("JSON chunk must be first and unique")
            json_chunk = chunk
        elif kind == 0x004E4942:
            if bin_chunk is not None:
                raise InvalidGLB("multiple BIN chunks are unsupported")
            bin_chunk = chunk
        else:
            raise InvalidGLB("unknown GLB chunk type")
        offset = end
    if json_chunk is None or bin_chunk is None:
        raise InvalidGLB("GLB must contain JSON and BIN chunks")
    try:
        document = json.loads(
            json_chunk.decode("utf-8").rstrip(" \t\r\n\x00"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid numeric token {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InvalidGLB(f"invalid JSON chunk: {exc}") from exc
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0":
        raise InvalidGLB("unsupported glTF document/version")
    return document, bin_chunk


def _accessor(
    document: dict[str, Any], binary: bytes, index: int, expected_type: str | None = None
) -> list[tuple[int | float, ...]]:
    accessors = document.get("accessors", [])
    views = document.get("bufferViews", [])
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        raise InvalidGLB("accessor index is invalid")
    accessor = accessors[index]
    if not isinstance(accessor, dict) or "sparse" in accessor:
        raise InvalidGLB("invalid or sparse accessor is unsupported")
    typ, component = accessor.get("type"), accessor.get("componentType")
    if (
        typ not in _WIDTHS
        or (expected_type and typ != expected_type)
        or component not in _COMPONENTS
    ):
        raise InvalidGLB("accessor type/component is unsupported")
    count = accessor.get("count")
    if not isinstance(count, int) or count <= 0 or count > _MAX_ACCESSOR_ELEMENTS:
        raise InvalidGLB("accessor count is invalid or exceeds safety limit")
    view_index = accessor.get("bufferView")
    if not isinstance(view_index, int) or not 0 <= view_index < len(views):
        raise InvalidGLB("accessor bufferView is missing or invalid")
    view = views[view_index]
    if not isinstance(view, dict) or view.get("buffer") != 0:
        raise InvalidGLB("only the single embedded GLB buffer is supported")
    component_size, code = _COMPONENTS_GLB[component]
    width = _WIDTHS[typ]
    packed_size = component_size * width
    stride = view.get("byteStride", packed_size)
    base = view.get("byteOffset", 0) + accessor.get("byteOffset", 0)
    view_start = view.get("byteOffset", 0)
    view_end = view_start + view.get("byteLength", -1)
    if (
        not all(isinstance(n, int) for n in (stride, base, view_start, view_end))
        or stride < packed_size
        or base < view_start
    ):
        raise InvalidGLB("invalid accessor stride or offsets")
    end = base + (count - 1) * stride + packed_size
    declared_buffer_length = document.get("buffers", [{}])[0].get("byteLength", -1)
    if (
        view_end > len(binary)
        or view_end > declared_buffer_length
        or end > view_end
        or view_end < view_start
    ):
        raise InvalidGLB("accessor reads outside its bufferView/BIN chunk")
    fmt = "<" + code * width
    result = [struct.unpack_from(fmt, binary, base + i * stride) for i in range(count)]
    if component == 5126 and any(not math.isfinite(v) for row in result for v in row):
        raise InvalidGLB("accessor contains non-finite floating-point values")
    return result


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _node_matrix(node: dict[str, Any]) -> list[list[float]]:
    if "matrix" in node:
        values = node["matrix"]
        if (
            not isinstance(values, list)
            or len(values) != 16
            or not all(math.isfinite(float(v)) for v in values)
        ):
            raise InvalidGLB("node matrix is malformed")
        return [[float(values[c * 4 + r]) for c in range(4)] for r in range(4)]
    t = node.get("translation", [0, 0, 0])
    s = node.get("scale", [1, 1, 1])
    q = node.get("rotation", [0, 0, 0, 1])
    if (
        len(t) != 3
        or len(s) != 3
        or len(q) != 4
        or not all(math.isfinite(float(x)) for x in (*t, *s, *q))
    ):
        raise InvalidGLB("node TRS is malformed")
    x, y, z, w = (float(v) for v in q)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-12:
        raise InvalidGLB("node quaternion is zero")
    x, y, z, w = (v / norm for v in (x, y, z, w))
    r = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    m = [[r[i][j] * float(s[j]) for j in range(3)] + [float(t[i])] for i in range(3)]
    return [*m, [0.0, 0.0, 0.0, 1.0]]


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
    # use module-level _accessor
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


def _decode_internal_skinned_glb(
    path: Path, *, max_file_size_bytes: int = 50 * 1024 * 1024
) -> DecodedInternalSkinnedGLB:
    import hashlib

    # use module-level _read_glb/_accessor
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


def _invert4(m: list[list[float]]) -> list[list[float]]:
    return invert_mat4(m)


def _mat4_from_flat(values: tuple[float, ...]) -> list[list[float]]:
    return [[values[c * 4 + r] for c in range(4)] for r in range(4)]


def _max_entry_diff(a: list[list[float]], b: list[list[float]]) -> float:
    return max(abs(a[r][c] - b[r][c]) for r in range(4) for c in range(4))


def _vec_angle_delta(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    import math

    la = math.sqrt(sum(c * c for c in a))
    lb = math.sqrt(sum(c * c for c in b))
    if la < 1e-9 or lb < 1e-9:
        return 1.0
    dot = sum(a[i] * b[i] for i in range(3)) / (la * lb)
    dot = max(-1.0, min(1.0, dot))
    return math.acos(dot)


def _blender_armature_wrapper_index(
    decoded: DecodedInternalSkinnedGLB, parents: dict[int, int]
) -> int | None:
    """Optional single armature object node between asset root and skeleton root (Blender export)."""
    nodes = decoded.document["nodes"]
    root_children = nodes[decoded.asset_root_index].get("children", [])
    mesh_idx = decoded.primitive.node_index
    candidates = [
        c for c in root_children if isinstance(c, int) and c != mesh_idx and 0 <= c < len(nodes)
    ]
    if len(candidates) != 1:
        return None
    wrap = candidates[0]
    skel = decoded.skeleton_root_index
    if parents.get(skel) == wrap and parents.get(wrap) == decoded.asset_root_index:
        return wrap
    return None


def _validation_finding(
    rule_id: str, ok: bool, expected: str, actual: str, message: str, artifact: str = ""
) -> dict[str, Any]:
    return {
        "rule_id": rule_id,
        "severity": "PASS" if ok else "FAIL",
        "expected": expected,
        "actual": actual,
        "artifact": artifact,
        "message": message,
    }


def validate_topology(
    decoded: DecodedInternalSkinnedGLB, contract: dict[str, Any]
) -> dict[str, Any]:
    nodes = decoded.document["nodes"]
    name_to_index = {str(n.get("name") or ""): i for i, n in enumerate(nodes)}
    if contract["asset_root_name"] not in name_to_index:
        return _validation_finding(
            "internal_skin.topology",
            False,
            f"asset root {contract['asset_root_name']}",
            "missing",
            "asset root node not found",
        )
    if name_to_index[contract["asset_root_name"]] != decoded.asset_root_index:
        return _validation_finding(
            "internal_skin.topology",
            False,
            "asset root is scene root",
            "mismatch",
            "scene root name does not match contract",
        )
    if len(decoded.joint_node_indices) > contract["joint_count_max"]:
        return _validation_finding(
            "internal_skin.topology",
            False,
            f"joint count <= {contract['joint_count_max']}",
            str(len(decoded.joint_node_indices)),
            "too many joints",
        )
    parents: dict[int, int] = {}
    for parent, node in enumerate(nodes):
        for child in node.get("children", []):
            if isinstance(child, int):
                parents[child] = parent
    armature_wrap = _blender_armature_wrapper_index(decoded, parents)
    wrong: list[str] = []
    for bone in contract["_bones"]:
        if bone["name"] not in name_to_index:
            wrong.append(f"missing bone {bone['name']}")
            continue
        idx = name_to_index[bone["name"]]
        if idx not in decoded.joint_node_indices:
            wrong.append(f"{bone['name']} is not in skin joints")
            continue
        parent_name = bone["parent"]
        if parent_name is None:
            wrong.append(f"{bone['name']} missing parent in contract")
            continue
        if parent_name not in name_to_index and parent_name != contract["asset_root_name"]:
            wrong.append(f"unknown parent {parent_name} for {bone['name']}")
            continue
        skel_name = str(nodes[decoded.skeleton_root_index].get("name") or "")
        if parent_name == contract["asset_root_name"]:
            if bone["name"] == skel_name and armature_wrap is not None:
                parent_idx = armature_wrap
            else:
                parent_idx = decoded.asset_root_index
        else:
            parent_idx = name_to_index[parent_name]
        parent_field = decoded.document["nodes"][parent_idx]
        if parent_idx not in {decoded.asset_root_index, *decoded.joint_node_indices} and (
            armature_wrap is None or parent_idx != armature_wrap
        ):
            wrong.append(f"parent {parent_name} not in skeleton")
        listed = parent_field.get("children", [])
        if idx not in listed:
            wrong.append(f"{bone['name']} not child of {parent_name}")
        actual_parent = parents.get(idx)
        if actual_parent != parent_idx:
            wrong.append(f"{bone['name']} parent index mismatch")
    return _validation_finding(
        "internal_skin.topology",
        not wrong,
        "declared bone map and parent chain",
        "; ".join(wrong) if wrong else "ok",
        "bone topology must match contract",
    )


def validate_rest_pose(
    decoded: DecodedInternalSkinnedGLB, contract: dict[str, Any]
) -> dict[str, Any]:
    nodes = decoded.document["nodes"]
    name_to_index = {str(n.get("name") or ""): i for i, n in enumerate(nodes)}
    tol = contract["rest_pose_tolerance"]
    issues: list[str] = []

    def bone_world(name: str) -> tuple[float, float, float]:
        idx = name_to_index.get(name)
        if idx is None or idx not in decoded.joint_world_rest:
            raise KeyError(name)
        m = decoded.joint_world_rest[idx]
        return (m[0][3], m[1][3], m[2][3])

    for bone_name, expected in contract["rest_joint_origins"].items():
        try:
            actual = bone_world(bone_name)
        except KeyError:
            issues.append(f"missing joint {bone_name}")
            continue
        for axis, label in enumerate("xyz"):
            if abs(actual[axis] - expected[axis]) > tol:
                issues.append(
                    f"{bone_name}.{label} expected {expected[axis]:.3f} got {actual[axis]:.3f}"
                )

    for bone_name, ref in contract["_rest_bases"].items():
        idx = name_to_index.get(bone_name)
        if idx is None or idx not in decoded.joint_world_rest:
            issues.append(f"missing joint basis for {bone_name}")
            continue
        y_axis, neg_z = mat3_basis_columns(decoded.joint_world_rest[idx])
        if _vec_angle_delta(y_axis, ref["y_axis"]) > _BASIS_TOL:
            issues.append(f"{bone_name} rest +Y basis mismatch")
        if _vec_angle_delta(neg_z, ref["neg_z_axis"]) > _BASIS_TOL:
            issues.append(f"{bone_name} rest -Z basis mismatch")

    try:
        chest = bone_world("Chest")
        lua = bone_world("LeftUpperArm")
        rua = bone_world("RightUpperArm")
        lle = bone_world("LeftLowerArm")
        lha = bone_world("LeftHand")
        if lua[0] >= chest[0] - 0.02:
            issues.append("left upper arm not on -X of chest")
        if rua[0] <= chest[0] + 0.02:
            issues.append("right upper arm not on +X of chest")
        if abs(lua[1] - chest[1]) > tol or abs(rua[1] - chest[1]) > tol:
            issues.append("upper arms not level with chest in Y")
        if lua[0] <= lle[0] or lle[0] <= lha[0]:
            issues.append("left arm chain does not extend -X")
        if chest[1] <= decoded.joint_world_rest[name_to_index["Hips"]][1][3]:
            issues.append("chest not above hips in +Y")
    except KeyError:
        issues.append("T-pose axis bones missing")

    return _validation_finding(
        "internal_skin.rest_pose",
        not issues,
        "T-pose joint origins and arm axes (+Y up, -Z front)",
        "; ".join(issues[:10]) if issues else "ok",
        "rest pose measured from joint globals against contract reference",
    )


def validate_weights(decoded: DecodedInternalSkinnedGLB) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    bad_sum: list[str] = []
    unweighted: list[str] = []
    too_many: list[str] = []
    for i, vw in enumerate(decoded.primitive.vertex_weights):
        active = [(j, w) for j, w in zip(vw.joints, vw.weights, strict=True) if w > 0]
        if len(active) > 4:
            too_many.append(str(i))
        total = sum(vw.weights)
        if total <= 1e-8:
            unweighted.append(str(i))
        elif abs(total - 1.0) > _WEIGHT_SUM_TOL:
            bad_sum.append(f"{i}:{total:.6f}")
    findings.append(
        _validation_finding(
            "internal_skin.weights.normalized",
            not bad_sum,
            f"weight sum within {_WEIGHT_SUM_TOL}",
            ", ".join(bad_sum[:8]) if bad_sum else "ok",
            "vertex weights must normalize",
        )
    )
    findings.append(
        _validation_finding(
            "internal_skin.weights.coverage",
            not unweighted,
            "every vertex has positive total weight",
            f"{len(unweighted)} unweighted" if unweighted else "ok",
            "unweighted vertices are forbidden",
        )
    )
    findings.append(
        _validation_finding(
            "internal_skin.weights.influence_count",
            not too_many,
            "at most four influences",
            ", ".join(too_many[:8]) if too_many else "ok",
            "too many influences per vertex",
        )
    )
    return findings


def validate_inverse_bind(decoded: DecodedInternalSkinnedGLB) -> dict[str, Any]:
    mismatches: list[str] = []
    for joint_i, node_index in enumerate(decoded.joint_node_indices):
        world = decoded.joint_world_rest[node_index]
        try:
            expected = _invert4(world)
        except ValueError:
            mismatches.append(f"joint {joint_i}: singular rest matrix")
            continue
        actual = _mat4_from_flat(decoded.inverse_bind_matrices[joint_i])
        diff = _max_entry_diff(expected, actual)
        if diff > _IBM_TOL:
            mismatches.append(f"joint {joint_i}: max entry diff {diff:.6f}")
    return _validation_finding(
        "internal_skin.inverse_bind",
        not mismatches,
        f"inverse bind within {_IBM_TOL} of rest inverse",
        "; ".join(mismatches[:6]) if mismatches else "ok",
        "inverse bind matrices must match rest pose",
    )


def validate_animation_absent(decoded: DecodedInternalSkinnedGLB) -> dict[str, Any]:
    has_anim = bool(decoded.document.get("animations"))
    return _validation_finding(
        "internal_skin.animation_forbidden",
        not has_anim,
        "no animations",
        "present" if has_anim else "absent",
        "animations are forbidden in internal subset",
    )


def run_internal_skin_checks(
    decoded: DecodedInternalSkinnedGLB, contract: dict[str, Any]
) -> list[dict[str, Any]]:
    return [
        validate_animation_absent(decoded),
        validate_topology(decoded, contract),
        validate_rest_pose(decoded, contract),
        *validate_weights(decoded),
        validate_inverse_bind(decoded),
    ]


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_json_digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    return _sha256(raw)


def _reviewed_text_sha256(raw: bytes, suffix: str) -> tuple[str, str]:
    raw_digest = _sha256(raw)
    if suffix.casefold() in {".py", ".gd"}:
        text = raw.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        return _sha256(text.encode("utf-8")), raw_digest
    return raw_digest, raw_digest


def _strict_int(value: Any, field: str) -> int:
    if type(value) is bool or not isinstance(value, int):
        raise ValueError(f"{field} must be a strict integer")
    return value


def _strict_float(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{field} must be finite")
    return result


def _strict_nonempty_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


def _strict_sha256_field(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase sha256 hex digest")
    return value


_MANIFEST_FILE_ENTRY_KEYS = frozenset({"path", "role", "size", "sha256"})


def _validate_json_value(value: Any, *, path: str, depth: int) -> None:
    if depth > _MAX_JSON_DEPTH:
        raise ValueError(f"JSON depth limit exceeded at {path}")
    if isinstance(value, dict):
        if len(value) > _MAX_JSON_OBJECT_KEYS:
            raise ValueError(f"JSON object too large at {path}")
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError(f"JSON object key must be string at {path}")
            _validate_json_value(child, path=f"{path}.{key}", depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_JSON_ARRAY_LEN:
            raise ValueError(f"JSON array too large at {path}")
        for index, child in enumerate(value):
            _validate_json_value(child, path=f"{path}[{index}]", depth=depth + 1)
        return
    if value is None or isinstance(value, (bool, str)):
        return
    if type(value) is bool:
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"non-finite JSON number at {path}")
        return
    raise ValueError(f"unsupported JSON value at {path}")


def _strict_region_box(box: Any, field: str) -> dict[str, list[float]]:
    if not isinstance(box, dict):
        raise ValueError(f"{field} must be an object")
    mn = box.get("min")
    mx = box.get("max")
    if not isinstance(mn, list) or not isinstance(mx, list) or len(mn) != 3 or len(mx) != 3:
        raise ValueError(f"{field} min/max invalid")
    return {
        "min": [_strict_float(mn[i], f"{field}.min[{i}]") for i in range(3)],
        "max": [_strict_float(mx[i], f"{field}.max[{i}]") for i in range(3)],
    }


def _bundle_path_crosses_link(path: Path) -> bool:
    for current in [path, *path.parents]:
        if _linked(current):
            return True
        if current.parent == current:
            break
    return False


def _path(value: Any) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError(f"unsafe bundle path: {value!r}")
    pure = PurePosixPath(value)
    if (
        pure.is_absolute()
        or pure.as_posix() != value
        or any(part in {"", ".", ".."} or ":" in part for part in value.split("/"))
    ):
        raise ValueError(f"unsafe bundle path: {value!r}")
    return value


def _linked(path: Path) -> bool:
    junction = getattr(path, "is_junction", None)
    return path.is_symlink() or bool(junction and junction())


def _resolve_under_root(root: Path, rel: str) -> Path:
    current = root
    for part in PurePosixPath(rel).parts:
        current = current / part
        if _linked(current):
            raise ValueError(f"symlink or junction in bundle path: {rel}")
    return current


def _read_bounded_bytes(path: Path, declared_size: int) -> bytes:
    if _linked(path):
        raise ValueError(f"symlink or junction file: {path.name}")
    if type(declared_size) is not int or declared_size < 0:
        raise ValueError(f"invalid declared size for {path.name}")
    try:
        stat = path.stat()
    except OSError as exc:
        raise ValueError(f"cannot stat bundle file: {path.name}") from exc
    if not path.is_file():
        raise ValueError(f"bundle path is not a regular file: {path.name}")
    actual = stat.st_size
    if actual > _MAX_FILE_BYTES:
        raise ValueError(f"bundle file exceeds size limit: {path.name}")
    if actual != declared_size:
        raise ValueError(f"declared size {declared_size} != actual {actual} for {path.name}")
    with path.open("rb") as handle:
        return handle.read(actual)


def _object_bytes(raw: bytes, name: str) -> dict[str, Any]:
    if len(raw) > _MAX_JSON_BYTES:
        raise ValueError(f"JSON file exceeds size limit: {name}")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    value = json.loads(
        raw.decode("utf-8"),
        object_pairs_hook=unique_pairs,
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(token)),
    )
    if not isinstance(value, dict):
        raise ValueError(f"JSON object required: {name}")
    _validate_json_value(value, path=name, depth=0)
    return value


def _object(path: Path, declared_size: int) -> dict[str, Any]:
    return _object_bytes(_read_bounded_bytes(path, declared_size), path.name)


def _parse_contract(data: dict[str, Any]) -> dict[str, Any]:
    bones = data.get("bones")
    if not isinstance(bones, list):
        raise ValueError("contract bones missing")
    bases_raw = data.get("rest_joint_bases")
    if not isinstance(bases_raw, dict):
        raise ValueError("contract rest_joint_bases missing")
    rest_bases = {
        k: {"y_axis": v["y_axis"], "neg_z_axis": v["neg_z_axis"]}
        for k, v in bases_raw.items()
        if isinstance(v, dict)
    }
    return {
        **data,
        "_bones": bones,
        "_rest_bases": rest_bases,
        "rest_joint_origins": data.get("rest_joint_origins", {}),
    }


def _finding_dicts(findings: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError("validation finding must be an object")
        out.append(item)
    return out


def _finding_key(finding: dict[str, Any]) -> tuple[str, str, str, str, str, str]:
    return (
        str(finding.get("rule_id", "")),
        str(finding.get("severity", "")),
        str(finding.get("expected", "")),
        str(finding.get("actual", "")),
        str(finding.get("artifact", "")),
        str(finding.get("message", "")),
    )


def _recomputed_validation_status(findings: list[dict[str, Any]]) -> str:
    if any(f.get("severity") == "FAIL" for f in findings):
        return "FAIL"
    return "PASS"


_REGION_BOUNDARY_TOLERANCE = 1e-6
_RUNTIME_REQUEST_STRICT_INT_FIELDS = frozenset(
    {
        "vertex_count",
        "affected_vertex_count",
        "unaffected_vertex_count",
    }
)


def _strict_runtime_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{field} must be a strict integer")
    as_float = float(value)
    if not math.isfinite(as_float) or as_float != math.trunc(as_float):
        raise ValueError(f"{field} must be a strict integer")
    iv = int(as_float)
    if iv < 0:
        raise ValueError(f"{field} must be non-negative")
    return iv


def _strict_region_boundary_tolerance(value: Any) -> float:
    tol = _strict_float(value, "region_boundary_tolerance")
    if not math.isclose(tol, _REGION_BOUNDARY_TOLERANCE, rel_tol=0.0, abs_tol=0.0):
        raise ValueError("region_boundary_tolerance must match pinned REGION_BOUNDARY_TOLERANCE")
    return float(f"{_REGION_BOUNDARY_TOLERANCE:.6f}")


def _canonicalize_runtime_request_for_digest(request: dict[str, Any]) -> dict[str, Any]:
    payload = {
        k: v for k, v in request.items() if k not in {"output_path", "request_digest", "glb"}
    }
    out: dict[str, Any] = {}
    for key in sorted(payload):
        value = payload[key]
        if key in _RUNTIME_REQUEST_STRICT_INT_FIELDS:
            out[key] = _strict_runtime_int(value, key)
        elif key == "region_boundary_tolerance":
            out[key] = _strict_region_boundary_tolerance(value)
        else:
            out[key] = value
    return out


def _godot_compatible_runtime_json_bytes(payload: dict[str, Any]) -> bytes:
    parts: list[str] = []
    for key in sorted(payload):
        value = payload[key]
        if key == "region_boundary_tolerance":
            value_json = "0.000001"
        else:
            value_json = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
        parts.append(f"{json.dumps(key)}:{value_json}")
    return ("{" + ",".join(parts) + "}").encode("utf-8")


def _runtime_request_digest(request: dict[str, Any]) -> str:
    payload = _canonicalize_runtime_request_for_digest(request)
    return _sha256(_godot_compatible_runtime_json_bytes(payload))


def _axis_angle_basis(axis: list[Any], degrees: float) -> list[list[float]]:
    ax = _strict_float(axis[0], "rotation_axis[0]")
    ay = _strict_float(axis[1], "rotation_axis[1]")
    az = _strict_float(axis[2], "rotation_axis[2]")
    length = math.sqrt(ax * ax + ay * ay + az * az)
    if length < 1e-12:
        raise ValueError("rotation_axis is zero")
    ax, ay, az = ax / length, ay / length, az / length
    rad = math.radians(degrees)
    c, s = math.cos(rad), math.sin(rad)
    t = 1.0 - c
    return [
        [t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay],
        [t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax],
        [t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c],
    ]


def _basis_close(expected: list[list[float]], observed: list[list[float]], tol: float) -> bool:
    for row in range(3):
        for col in range(3):
            if abs(expected[row][col] - observed[row][col]) > tol:
                return False
    return True


def _inside(
    p: tuple[float, float, float],
    box: dict[str, Any],
    *,
    boundary_tolerance: float = _REGION_BOUNDARY_TOLERANCE,
) -> bool:
    mn, mx = box["min"], box["max"]
    eps = boundary_tolerance
    return (
        (p[0] - float(mx[0])) <= eps
        and (float(mn[0]) - p[0]) <= eps
        and (p[1] - float(mx[1])) <= eps
        and (float(mn[1]) - p[1]) <= eps
        and (p[2] - float(mx[2])) <= eps
        and (float(mn[2]) - p[2]) <= eps
    )


def _match_rest_to_glb(
    samples: list[dict[str, Any]],
    positions: list[Any] | tuple[Any, ...],
    *,
    rest_tol: float,
) -> dict[int, int]:
    vertex_count = len(positions)
    if vertex_count > _MAX_RUNTIME_VERTICES:
        raise ValueError("vertex count exceeds cold runtime matching limit")
    godot_indices: set[int] = set()
    for entry in samples:
        idx = _strict_int(entry.get("vertex_index"), "vertex_index")
        if idx in godot_indices or idx < 0 or idx >= vertex_count:
            raise ValueError("vertex_index coverage or uniqueness violation")
        godot_indices.add(idx)
    if godot_indices != set(range(vertex_count)):
        raise ValueError("vertex_samples do not cover all vertex indices")
    glb_pool = list(range(vertex_count))
    mapping: dict[int, int] = {}
    for entry in sorted(
        samples, key=lambda item: _strict_int(item.get("vertex_index"), "vertex_index")
    ):
        godot_idx = _strict_int(entry.get("vertex_index"), "vertex_index")
        rest = entry.get("rest_position")
        if not isinstance(rest, list) or len(rest) != 3:
            raise ValueError("vertex sample rest_position invalid")
        rp = (
            _strict_float(rest[0], "rest_position[0]"),
            _strict_float(rest[1], "rest_position[1]"),
            _strict_float(rest[2], "rest_position[2]"),
        )
        best_glb = -1
        best_dist = float("inf")
        for glb_idx in glb_pool:
            glb_rest = positions[glb_idx]
            dist = math.sqrt(sum((rp[axis] - float(glb_rest[axis])) ** 2 for axis in range(3)))
            if dist < best_dist:
                best_dist = dist
                best_glb = glb_idx
        if best_glb < 0 or best_dist > rest_tol:
            raise ValueError("rest_position does not match any unused GLB vertex")
        glb_pool.remove(best_glb)
        mapping[godot_idx] = best_glb
    if glb_pool:
        raise ValueError("vertex_samples do not account for full GLB geometry")
    return mapping


def _recompute_runtime(
    payload: dict[str, Any],
    decoded: DecodedInternalSkinnedGLB,
    contract: dict[str, Any],
    *,
    boundary_tolerance: float = _REGION_BOUNDARY_TOLERANCE,
) -> tuple[float, float, int, int]:
    oracle = contract["deformation_oracle"]
    affected = oracle["affected_region"]
    unaffected = oracle["unaffected_region"]
    samples = payload.get("vertex_samples")
    if not isinstance(samples, list):
        raise ValueError("vertex_samples missing")
    positions = decoded.primitive.positions
    vertex_count = _strict_int(payload.get("vertex_count"), "vertex_count")
    if vertex_count != len(positions):
        raise ValueError("vertex_count does not match decoded GLB")
    if len(samples) != vertex_count:
        raise ValueError("vertex_samples must include every vertex")
    _match_rest_to_glb(samples, positions, rest_tol=1e-3)
    max_aff = 0.0
    max_unaff = 0.0
    aff = 0
    unaff = 0
    for entry in samples:
        if not isinstance(entry, dict):
            raise ValueError("vertex sample must be an object")
        rest = entry.get("rest_position")
        posed = entry.get("posed_position")
        if (
            not isinstance(rest, list)
            or not isinstance(posed, list)
            or len(rest) != 3
            or len(posed) != 3
        ):
            raise ValueError("vertex sample positions must be length-3 arrays")
        rp = (
            _strict_float(rest[0], "rest_position[0]"),
            _strict_float(rest[1], "rest_position[1]"),
            _strict_float(rest[2], "rest_position[2]"),
        )
        pp = (
            _strict_float(posed[0], "posed_position[0]"),
            _strict_float(posed[1], "posed_position[1]"),
            _strict_float(posed[2], "posed_position[2]"),
        )
        delta = math.sqrt(sum((pp[i] - rp[i]) ** 2 for i in range(3)))
        if _inside(rp, affected, boundary_tolerance=boundary_tolerance):
            aff += 1
            max_aff = max(max_aff, delta)
        if _inside(rp, unaffected, boundary_tolerance=boundary_tolerance):
            unaff += 1
            max_unaff = max(max_unaff, delta)
    return max_aff, max_unaff, aff, unaff


def _population_from_glb(
    decoded: DecodedInternalSkinnedGLB,
    contract: dict[str, Any],
    *,
    boundary_tolerance: float = _REGION_BOUNDARY_TOLERANCE,
) -> tuple[int, int]:
    oracle = contract["deformation_oracle"]
    affected = oracle["affected_region"]
    unaffected = oracle["unaffected_region"]
    aff = 0
    unaff = 0
    for pos in decoded.primitive.positions:
        rp = (float(pos[0]), float(pos[1]), float(pos[2]))
        if _inside(rp, affected, boundary_tolerance=boundary_tolerance):
            aff += 1
        if _inside(rp, unaffected, boundary_tolerance=boundary_tolerance):
            unaff += 1
    return aff, unaff


def _validate_source_declaration(
    source_decl: dict[str, Any],
    *,
    contract_bytes_hash: str,
    contract_canonical_hash: str,
) -> None:
    if source_decl.get("schema_version") != "internal-rig-source-declaration-0.8.0":
        raise ValueError("source_declaration schema_version mismatch")
    if source_decl.get("validator_contract_version") != VALIDATOR_CONTRACT_VERSION:
        raise ValueError("source_declaration validator_contract_version mismatch")
    if source_decl.get("contract_bytes_sha256") != contract_bytes_hash:
        raise ValueError("source_declaration contract_bytes_sha256 mismatch")
    if source_decl.get("contract_canonical_sha256") != contract_canonical_hash:
        raise ValueError("source_declaration contract_canonical_sha256 mismatch")
    pin = source_decl.get("positive_fixture_glb_sha256")
    if not isinstance(pin, str) or not _SHA256.fullmatch(pin):
        raise ValueError("source_declaration positive_fixture_glb_sha256 invalid")


def _enforce_runtime_request(
    request: dict[str, Any],
    contract: dict[str, Any],
    harness_lf: str,
    *,
    vertex_count: int,
    affected_count: int,
    unaffected_count: int,
) -> None:
    oracle = contract["deformation_oracle"]
    _strict_nonempty_string(request.get("godot_version"), "runtime request godot_version")
    if request.get("pose_bone") != oracle.get("pose_bone"):
        raise ValueError("runtime request pose_bone does not match contract")
    axis = request.get("rotation_axis")
    if not isinstance(axis, list) or len(axis) != 3:
        raise ValueError("runtime request rotation_axis invalid")
    req_axis = [_strict_float(axis[i], f"rotation_axis[{i}]") for i in range(3)]
    oracle_axis = oracle.get("rotation_axis", [])
    if not isinstance(oracle_axis, list) or len(oracle_axis) != 3:
        raise ValueError("contract rotation_axis invalid")
    if req_axis != [_strict_float(oracle_axis[i], "contract.rotation_axis") for i in range(3)]:
        raise ValueError("runtime request rotation_axis does not match contract")
    if _strict_float(request.get("rotation_degrees"), "rotation_degrees") != float(
        oracle.get("rotation_degrees")
    ):
        raise ValueError("runtime request rotation_degrees does not match contract")
    for key in ("affected", "unaffected"):
        contract_key = "affected_region" if key == "affected" else "unaffected_region"
        contract_box = oracle.get(contract_key)
        if not isinstance(contract_box, dict):
            raise ValueError(f"contract {contract_key} missing")
        if _strict_region_box(request.get(key), key) != _strict_region_box(
            contract_box, contract_key
        ):
            raise ValueError(f"runtime request {key} does not match contract")
    if _strict_int(request.get("vertex_count"), "vertex_count") != vertex_count:
        raise ValueError("runtime request vertex_count does not match decoded GLB")
    if _strict_int(request.get("affected_vertex_count"), "affected_vertex_count") != affected_count:
        raise ValueError("runtime request affected_vertex_count does not match decoded GLB")
    if (
        _strict_int(request.get("unaffected_vertex_count"), "unaffected_vertex_count")
        != unaffected_count
    ):
        raise ValueError("runtime request unaffected_vertex_count does not match decoded GLB")
    if _strict_float(
        request.get("min_affected_displacement"), "min_affected_displacement"
    ) != float(oracle.get("min_affected_displacement")):
        raise ValueError("runtime request min_affected_displacement mismatch")
    if _strict_float(
        request.get("max_unaffected_displacement"), "max_unaffected_displacement"
    ) != float(oracle.get("max_unaffected_displacement")):
        raise ValueError("runtime request max_unaffected_displacement mismatch")
    if _strict_float(
        request.get("max_affected_displacement"), "max_affected_displacement"
    ) != float(oracle.get("max_affected_displacement")):
        raise ValueError("runtime request max_affected_displacement mismatch")
    _strict_region_boundary_tolerance(request.get("region_boundary_tolerance"))
    if request.get("harness_sha256") != harness_lf:
        raise ValueError("runtime request harness_sha256 does not match reviewed harness pin")


def _validate_observation_pose(request: dict[str, Any], observation: dict[str, Any]) -> None:
    transform = observation.get("observed_bone_transform")
    if not isinstance(transform, dict) or transform.get("kind") != "basis":
        raise ValueError("observed_bone_transform must be a basis object")
    basis = transform.get("basis")
    if not isinstance(basis, list) or len(basis) != 3:
        raise ValueError("observed_bone_transform basis invalid")
    observed: list[list[float]] = []
    for row_index, row in enumerate(basis):
        if not isinstance(row, list) or len(row) != 3:
            raise ValueError("observed_bone_transform basis row invalid")
        observed.append([_strict_float(row[c], f"basis[{row_index}][{c}]") for c in range(3)])
    expected = _axis_angle_basis(
        list(request.get("rotation_axis", [])),
        _strict_float(request.get("rotation_degrees"), "rotation_degrees"),
    )
    if not _basis_close(expected, observed, _BASIS_TOL):
        raise ValueError("observed bone rotation does not match requested pose")


def verify_bundle(bundle: Path) -> dict[str, Any]:
    bundle = Path(bundle)
    if _bundle_path_crosses_link(bundle):
        raise ValueError("bundle path crosses a symlink or junction")
    if _linked(bundle):
        raise ValueError("bundle root is a symlink or junction")
    root = bundle.resolve(strict=True)
    manifest_path = _resolve_under_root(root, "manifest.json")
    manifest_entry_size = manifest_path.stat().st_size
    if manifest_entry_size > _MAX_JSON_BYTES:
        raise ValueError("manifest.json exceeds size limit")
    manifest = _object(manifest_path, manifest_entry_size)
    if manifest.get("schema_version") != "rig-evidence-0.8.0":
        raise ValueError("unsupported manifest schema")
    _strict_nonempty_string(manifest.get("bundle_id"), "manifest.bundle_id")
    _strict_sha256_field(
        manifest.get("harness_reviewed_sha256"), "manifest.harness_reviewed_sha256"
    )
    _strict_sha256_field(
        manifest.get("blender_script_reviewed_sha256"),
        "manifest.blender_script_reviewed_sha256",
    )
    manifest_runtime_status = manifest.get("runtime_status")
    if manifest_runtime_status is None:
        raise ValueError("manifest runtime_status required")
    if manifest_runtime_status != "PASS":
        raise ValueError("manifest runtime_status must be PASS")
    files = manifest.get("files")
    if not isinstance(files, list):
        raise ValueError("manifest files must be an array")
    roles: dict[str, list[dict[str, Any]]] = {}
    listed: set[str] = set()
    total_size = 0
    file_raw: dict[str, bytes] = {}
    for item in files:
        if not isinstance(item, dict):
            raise ValueError("manifest file entry must be an object")
        unknown_keys = set(item.keys()) - _MANIFEST_FILE_ENTRY_KEYS
        if unknown_keys:
            raise ValueError("manifest file entry has unknown keys")
        rel = _path(item.get("path"))
        role = item.get("role")
        if rel in listed or role not in _SINGLE_ROLES:
            raise ValueError(f"duplicate or unknown bundle entry: {rel}")
        digest, size = item.get("sha256"), item.get("size")
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or type(size) is not int:
            raise ValueError(f"invalid hash or size: {rel}")
        if size < 0:
            raise ValueError(f"invalid declared size: {rel}")
        total_size += size
        if size > _MAX_FILE_BYTES or total_size > _MAX_BUNDLE_BYTES:
            raise ValueError("bundle exceeds size limits")
        target = _resolve_under_root(root, rel)
        if not target.is_file():
            raise ValueError(f"missing bundle file: {rel}")
        raw = _read_bounded_bytes(target, size)
        if _sha256(raw) != digest:
            raise ValueError(f"hash mismatch: {rel}")
        listed.add(rel)
        file_raw[rel] = raw
        roles.setdefault(str(role), []).append(item)
    for role in _SINGLE_ROLES:
        if len(roles.get(role, [])) != 1:
            raise ValueError(f"bundle requires exactly one {role}")
    on_disk: set[str] = set()
    for current, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        base = Path(current)
        if _linked(base):
            rel = base.relative_to(root).as_posix() or "."
            raise ValueError(f"symlink or junction in bundle inventory: {rel}")
        for name in list(dirnames) + filenames:
            child = base / name
            if _linked(child):
                rel = child.relative_to(root).as_posix()
                raise ValueError(f"symlink or junction in bundle inventory: {rel}")
        for name in filenames:
            on_disk.add((base / name).relative_to(root).as_posix())
    allowed_extra = _EXCLUDED
    if on_disk - allowed_extra != listed:
        raise ValueError("unlisted or missing manifest files")

    def one(role: str) -> dict[str, Any]:
        return roles[role][0]

    glb_entry = one("skinned_glb")
    glb_path = _resolve_under_root(root, glb_entry["path"])
    contract_entry = one("rig_verification_contract")
    contract_raw = file_raw[contract_entry["path"]]
    contract_data = _parse_contract(_object_bytes(contract_raw, contract_entry["path"]))
    contract_bytes_hash = _sha256(contract_raw)
    contract_canonical_hash = _canonical_json_digest(
        _object_bytes(contract_raw, contract_entry["path"])
    )

    reviewed_blender = file_raw[one("reviewed_blender_export_script")["path"]]
    reviewed_harness = file_raw[one("reviewed_godot_harness")["path"]]
    blender_lf, _ = _reviewed_text_sha256(reviewed_blender, ".py")
    harness_lf, _harness_raw = _reviewed_text_sha256(reviewed_harness, ".gd")

    if contract_canonical_hash != PINNED_CONTRACT_CANONICAL_SHA256:
        raise ValueError("contract canonical digest does not match pinned reviewed contract")
    if blender_lf != PINNED_BLENDER_EXPORT_SCRIPT_SHA256:
        raise ValueError("reviewed blender export script does not match pin")
    if harness_lf != PINNED_GODOT_HARNESS_REVIEWED_SHA256:
        raise ValueError("reviewed godot harness LF digest does not match pin")

    if manifest.get("contract_id") != contract_data.get("contract_id"):
        raise ValueError("manifest contract_id does not match bundled contract")
    if manifest.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("manifest glb_sha256 mismatch")
    if manifest.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("manifest contract_sha256 mismatch")
    if manifest.get("harness_reviewed_sha256") != harness_lf:
        raise ValueError("manifest harness_reviewed_sha256 mismatch")
    if manifest.get("blender_script_reviewed_sha256") != blender_lf:
        raise ValueError("manifest blender_script_reviewed_sha256 mismatch")

    decoded = _decode_internal_skinned_glb(glb_path)
    if decoded.sha256 != glb_entry["sha256"]:
        raise ValueError("skinned_glb digest mismatch after decode")
    findings = run_internal_skin_checks(decoded, contract_data)
    finding_dicts = _finding_dicts(findings)
    recomputed_status = _recomputed_validation_status(finding_dicts)
    report = _object_bytes(
        file_raw[one("rig_validation_report")["path"]], "rig_validation_report.json"
    )
    if report.get("schema_version") != "rig-validation-report-0.8.0":
        raise ValueError("validation report schema mismatch")
    if report.get("validator_contract_version") != VALIDATOR_CONTRACT_VERSION:
        raise ValueError("validation report validator_contract_version mismatch")
    if report.get("contract_canonical_sha256") != contract_canonical_hash:
        raise ValueError("validation report contract_canonical_sha256 mismatch")
    if report.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("validation report glb_sha256 mismatch")
    if report.get("contract_id") != contract_data.get("contract_id"):
        raise ValueError("validation report contract_id mismatch")
    if report.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("validation report contract_sha256 mismatch")
    bundled_findings = report.get("findings")
    if not isinstance(bundled_findings, list):
        raise ValueError("validation report findings missing")
    if len(bundled_findings) != len(finding_dicts):
        raise ValueError("validation report findings length mismatch")
    for finding in bundled_findings:
        if not isinstance(finding, dict):
            raise ValueError("validation report finding must be an object")
    bundled_keys = [_finding_key(f) for f in bundled_findings]
    recomputed_keys = [_finding_key(f) for f in finding_dicts]
    if bundled_keys != recomputed_keys:
        raise ValueError("validation findings mismatch with recomputed static checks")
    if report.get("status") != recomputed_status:
        raise ValueError("validation report status does not match recomputed findings")
    if "passed" in report:
        report_passed = report["passed"]
        if type(report_passed) is not bool:
            raise ValueError("validation report passed must be a boolean")
        if report_passed != (recomputed_status == "PASS"):
            raise ValueError("validation report passed does not match recomputed status")
    if "summary" in report:
        if not isinstance(report["summary"], str):
            raise ValueError("validation report summary must be a string")
    if recomputed_status != "PASS":
        raise ValueError("static validation FAIL rejected")
    if manifest.get("validation_status") != recomputed_status:
        raise ValueError("manifest validation_status mismatch")

    request = _object_bytes(
        file_raw[one("rig_runtime_request")["path"]], "rig_runtime_request.json"
    )
    observation = _object_bytes(
        file_raw[one("rig_runtime_observation")["path"]], "rig_runtime_observation.json"
    )
    if request.get("schema_version") != "rig-runtime-request-0.8.0":
        raise ValueError("runtime request schema mismatch")
    if observation.get("schema_version") != "rig-runtime-observation-0.8.0":
        raise ValueError("runtime observation schema mismatch")
    digest = _runtime_request_digest(request)
    if request.get("request_digest") != digest:
        raise ValueError("runtime request request_digest mismatch")
    if observation.get("request_digest") != digest:
        raise ValueError("runtime observation request_digest mismatch")
    manifest_digest = manifest.get("runtime_request_digest")
    if manifest_digest != digest:
        raise ValueError("manifest runtime_request_digest mismatch")
    glb_vertex_count = len(decoded.primitive.positions)
    glb_aff_expected, glb_unaff_expected = _population_from_glb(decoded, contract_data)
    _enforce_runtime_request(
        request,
        contract_data,
        harness_lf,
        vertex_count=glb_vertex_count,
        affected_count=glb_aff_expected,
        unaffected_count=glb_unaff_expected,
    )
    if observation.get("method") != "bake_mesh_from_current_skeleton_pose":
        raise ValueError("runtime observation method mismatch")
    if observation.get("pose_bone") != request.get("pose_bone"):
        raise ValueError("runtime observation pose_bone mismatch")
    obs_godot = _strict_nonempty_string(
        observation.get("godot_version"), "runtime observation godot_version"
    )
    req_godot = _strict_nonempty_string(
        request.get("godot_version"), "runtime request godot_version"
    )
    if obs_godot != req_godot:
        raise ValueError("runtime observation godot_version mismatch")
    for key in ("glb_sha256", "contract_sha256", "harness_sha256"):
        if observation.get(key) != request.get(key):
            raise ValueError(f"runtime binding mismatch on {key}")
    if observation.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("observation glb_sha256 does not match bundled GLB")
    if observation.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("observation contract_sha256 does not match bundled contract")
    if request.get("glb_sha256") != glb_entry["sha256"]:
        raise ValueError("request glb_sha256 does not match bundled GLB")
    if request.get("contract_sha256") != contract_entry["sha256"]:
        raise ValueError("request contract_sha256 does not match bundled contract")

    _validate_observation_pose(request, observation)

    region_tol = _strict_region_boundary_tolerance(request.get("region_boundary_tolerance"))
    max_aff, max_unaff, aff, unaff = _recompute_runtime(
        observation, decoded, contract_data, boundary_tolerance=region_tol
    )
    if (
        abs(
            _strict_float(observation["max_affected_displacement"], "max_affected_displacement")
            - max_aff
        )
        > 1e-5
    ):
        raise ValueError("max_affected_displacement mismatch")
    if (
        abs(
            _strict_float(observation["max_unaffected_displacement"], "max_unaffected_displacement")
            - max_unaff
        )
        > 1e-5
    ):
        raise ValueError("max_unaffected_displacement mismatch")
    glb_aff, glb_unaff = _population_from_glb(decoded, contract_data)
    if (
        _strict_int(observation.get("affected_vertex_count"), "affected_vertex_count") != aff
        or aff != glb_aff
    ):
        raise ValueError("affected_vertex_count inconsistent with GLB regions")
    if (
        _strict_int(observation.get("unaffected_vertex_count"), "unaffected_vertex_count") != unaff
        or unaff != glb_unaff
    ):
        raise ValueError("unaffected_vertex_count inconsistent with GLB regions")

    oracle = contract_data["deformation_oracle"]
    status = observation.get("status")
    measured_pass = (
        max_aff >= float(oracle["min_affected_displacement"])
        and max_aff <= float(oracle["max_affected_displacement"])
        and max_unaff <= float(oracle["max_unaffected_displacement"])
    )
    if measured_pass != (status == "PASS"):
        raise ValueError("observation status does not match recomputed displacements")
    if status != "PASS":
        raise ValueError("runtime observation must report PASS for verified bundle")
    if manifest.get("runtime_status") != status:
        raise ValueError("manifest runtime_status mismatch")

    source_decl = _object_bytes(
        file_raw[one("source_declaration")["path"]], "source_declaration.json"
    )
    _validate_source_declaration(
        source_decl,
        contract_bytes_hash=contract_bytes_hash,
        contract_canonical_hash=contract_canonical_hash,
    )
    fixture_pin = source_decl.get("positive_fixture_glb_sha256")
    source_claim_matches = isinstance(fixture_pin, str) and fixture_pin == glb_entry["sha256"]

    return {
        "outcome": "CONSISTENT_BUT_UNAUTHENTICATED",
        "integrity_outcome": "VERIFIED",
        "execution_provenance": "CONSISTENT_BUT_UNAUTHENTICATED",
        "reviewed_pins_match": True,
        "source_declaration_fixture_claim_matches_glb": source_claim_matches,
        "validator_contract_version": VALIDATOR_CONTRACT_VERSION,
        "glb_sha256": glb_entry["sha256"],
        "contract_sha256": contract_entry["sha256"],
        "validation_status": recomputed_status,
        "runtime_status": status,
        "verified_files": len(files),
    }


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: python -I verify_rig_bundle.py BUNDLE", file=sys.stderr)
        return 2
    try:
        result = verify_bundle(Path(args[0]))
    except (
        OSError,
        ValueError,
        json.JSONDecodeError,
        UnicodeError,
        InvalidGLB,
        TypeError,
        KeyError,
        IndexError,
        OverflowError,
        RecursionError,
    ) as exc:
        print("FAILED")
        print(json.dumps({"outcome": "FAILED", "error": str(exc)}, ensure_ascii=False))
        return 1
    label = str(result.get("outcome", "FAILED"))
    print(label)
    print(json.dumps(result, sort_keys=True))
    return 0 if label in {"VERIFIED", "CONSISTENT_BUT_UNAUTHENTICATED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
