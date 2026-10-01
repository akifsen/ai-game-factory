"""Test-only GLB builders for V0.8 candidate triangle budget validation."""

from __future__ import annotations

import copy
import hashlib
import struct
from typing import Any, Literal

from gamefactory.adapters.assets.glb_io import read_glb, write_glb
from gamefactory.adapters.assets.glb_validator import _accessor
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetSpecificationV08Candidate,
    load_packaged_candidate_profile,
    load_packaged_candidate_specification,
    profile_document_hash,
)

BudgetMode = Literal["indexed_dual_mesh", "nonindexed_dual_mesh"]
NORMATIVE_MIN_TRIANGLE_BUDGET = 100
TARGET_TRIANGLE_COUNT = 101


def _pack_f32(values: list[float]) -> bytes:
    return struct.pack(f"<{len(values)}f", *values)


def _pack_u8(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}B", *values)


def _pack_u16(values: list[int]) -> bytes:
    return struct.pack(f"<{len(values)}H", *values)


def glb_sha256(glb: bytes) -> str:
    return hashlib.sha256(glb).hexdigest()


def _insert_unused_mesh0(document: dict[str, Any]) -> None:
    meshes = document.setdefault("meshes", [])
    if len(meshes) != 1:
        raise ValueError("expected single-mesh positive fixture")
    skin_mesh = copy.deepcopy(meshes[0])
    document["meshes"] = [{"name": "UnusedDecoyMesh", "primitives": []}, skin_mesh]
    for node in document.get("nodes", []):
        if isinstance(node, dict) and node.get("name") == "SM_HumanoidSkin":
            node["mesh"] = 1
            return
    raise ValueError("SM_HumanoidSkin node missing")


def _splice_buffer_view_segment(
    document: dict[str, Any],
    binary: bytes,
    view_idx: int,
    new_segment: bytes,
) -> tuple[dict[str, Any], bytes]:
    """Replace one bufferView's bytes and rebase every later bufferView byteOffset."""
    document = copy.deepcopy(document)
    view = document["bufferViews"][view_idx]
    offset = int(view["byteOffset"])
    old_len = int(view["byteLength"])
    new_binary = binary[:offset] + new_segment + binary[offset + old_len :]
    document["bufferViews"][view_idx]["byteLength"] = len(new_segment)
    delta = len(new_segment) - old_len
    if delta:
        splice_end = offset + old_len
        for buffer_view in document["bufferViews"]:
            byte_offset = int(buffer_view["byteOffset"])
            if byte_offset >= splice_end:
                buffer_view["byteOffset"] = byte_offset + delta
    return document, new_binary


def _repeat_indices_to_triangle_count(
    document: dict[str, Any],
    binary: bytes,
    primitive: dict[str, Any],
    *,
    target_triangles: int,
) -> tuple[dict[str, Any], bytes]:
    idx_acc = int(primitive["indices"])
    accessors = document["accessors"]
    idx_view = int(accessors[idx_acc]["bufferView"])
    flat_indices = [int(row[0]) for row in _accessor(document, binary, idx_acc, "SCALAR")]
    base_triangles = len(flat_indices) // 3
    if base_triangles <= 0:
        raise ValueError("fixture has no triangles")
    repeats = (target_triangles + base_triangles - 1) // base_triangles
    expanded = flat_indices * repeats
    triangle_count = len(expanded) // 3
    if triangle_count < target_triangles:
        raise ValueError("failed to expand triangle count")
    new_index_bytes = _pack_u16(expanded)
    document, new_binary = _splice_buffer_view_segment(document, binary, idx_view, new_index_bytes)
    document["accessors"][idx_acc]["count"] = len(expanded)
    return document, new_binary


def _expand_nonindexed_primitive(
    document: dict[str, Any],
    binary: bytes,
    primitive: dict[str, Any],
    *,
    target_triangles: int,
) -> tuple[dict[str, Any], bytes]:
    attrs = primitive["attributes"]
    positions = _accessor(document, binary, attrs["POSITION"], "VEC3")
    joints = _accessor(document, binary, attrs["JOINTS_0"], "VEC4")
    weights = _accessor(document, binary, attrs["WEIGHTS_0"], "VEC4")
    normals = _accessor(document, binary, attrs["NORMAL"], "VEC3")
    flat_indices = [
        int(row[0]) for row in _accessor(document, binary, primitive["indices"], "SCALAR")
    ]
    base_triangles = len(flat_indices) // 3
    repeats = (target_triangles + base_triangles - 1) // base_triangles
    expanded_indices = flat_indices * repeats
    new_positions: list[tuple[float, float, float]] = []
    new_joints: list[tuple[int, int, int, int]] = []
    new_weights: list[tuple[float, float, float, float]] = []
    new_normals: list[tuple[float, float, float]] = []
    for idx in expanded_indices:
        row = positions[idx]
        new_positions.append((float(row[0]), float(row[1]), float(row[2])))
        jr = joints[idx]
        new_joints.append((int(jr[0]), int(jr[1]), int(jr[2]), int(jr[3])))
        wr = weights[idx]
        new_weights.append((float(wr[0]), float(wr[1]), float(wr[2]), float(wr[3])))
        nr = normals[idx]
        new_normals.append((float(nr[0]), float(nr[1]), float(nr[2])))

    pos_bytes = _pack_f32([c for v in new_positions for c in v])
    joint_bytes = _pack_u8([c for quad in new_joints for c in quad])
    weight_bytes = _pack_f32([c for quad in new_weights for c in quad])
    normal_bytes = _pack_f32([c for v in new_normals for c in v])
    ibm_view_idx = int(document["accessors"][-1]["bufferView"])
    ibm_off = int(document["bufferViews"][ibm_view_idx]["byteOffset"])
    ibm_len = int(document["bufferViews"][ibm_view_idx]["byteLength"])
    ibm_bytes = binary[ibm_off : ibm_off + ibm_len]

    new_binary = pos_bytes + joint_bytes + weight_bytes + normal_bytes + ibm_bytes
    document = copy.deepcopy(document)
    segment_by_view = {
        0: len(pos_bytes),
        1: len(joint_bytes),
        2: len(weight_bytes),
        4: len(normal_bytes),
    }
    running = 0
    for view_idx in (0, 1, 2, 4):
        length = segment_by_view[view_idx]
        document["bufferViews"][view_idx]["byteOffset"] = running
        document["bufferViews"][view_idx]["byteLength"] = length
        running += length
    ibm_view_idx = int(document["accessors"][-1]["bufferView"])
    document["bufferViews"][ibm_view_idx]["byteOffset"] = running
    document["bufferViews"][ibm_view_idx]["byteLength"] = len(ibm_bytes)
    vertex_count = len(new_positions)
    document["accessors"][attrs["POSITION"]]["count"] = vertex_count
    document["accessors"][attrs["JOINTS_0"]]["count"] = vertex_count
    document["accessors"][attrs["WEIGHTS_0"]]["count"] = vertex_count
    document["accessors"][attrs["NORMAL"]]["count"] = vertex_count
    skin_mesh = document["meshes"][1]
    skin_primitive = skin_mesh["primitives"][0]
    skin_primitive.pop("indices", None)
    return document, new_binary


def build_candidate_budget_glb(
    mode: BudgetMode = "indexed_dual_mesh",
    *,
    target_triangles: int = TARGET_TRIANGLE_COUNT,
) -> bytes:
    if target_triangles <= NORMATIVE_MIN_TRIANGLE_BUDGET:
        raise ValueError("target_triangles must exceed normative minimum budget")
    document, binary = read_glb(build_humanoid_skinned_glb("positive"))
    document = copy.deepcopy(document)
    _insert_unused_mesh0(document)
    primitive = document["meshes"][1]["primitives"][0]
    if mode == "indexed_dual_mesh":
        document, binary = _repeat_indices_to_triangle_count(
            document, binary, primitive, target_triangles=target_triangles
        )
    elif mode == "nonindexed_dual_mesh":
        document, binary = _expand_nonindexed_primitive(
            document, binary, primitive, target_triangles=target_triangles
        )
    else:
        raise ValueError(f"unsupported mode {mode!r}")
    document["buffers"][0]["byteLength"] = len(binary)
    return write_glb(document, binary)


def spec_bound_to_glb(
    glb: bytes,
    *,
    max_triangles_lod0: int,
) -> AssetSpecificationV08Candidate:
    from gamefactory.core.domain.v08_candidate_contracts import (
        parse_asset_specification_v08_candidate,
    )

    data = load_packaged_candidate_specification().model_dump(mode="json")
    data["processed_glb_sha256"] = glb_sha256(glb)
    data["geometry_budget"]["max_triangles_lod0"] = max_triangles_lod0
    if max_triangles_lod0 <= data["geometry_budget"]["max_triangles_lod1"]:
        data["geometry_budget"]["max_triangles_lod1"] = max(1, max_triangles_lod0 // 2)
    data["profile_document_hash"] = profile_document_hash(
        load_packaged_candidate_profile().document
    )
    return parse_asset_specification_v08_candidate(data)
