"""Deterministic glTF 2.0 Binary (GLB) generator for fixtures and test assets.

Generates completely valid, spec-compliant binary GLB files containing:
- Measured 3D box or prop mesh with real vertex buffer and index buffer
- Optional LOD0 and LOD1 meshes
- Optional collider mesh (COL_...)
- Materials and optional embedded textures
- Real accessor min/max bounds for geometry verification
"""

from __future__ import annotations

import io
import json
import struct
from pathlib import Path
from typing import Any

from PIL import Image


def _create_solid_texture_png(
    color: tuple[int, int, int] = (60, 120, 200), size: tuple[int, int] = (64, 64)
) -> bytes:
    img = Image.new("RGBA", size, color=(color[0], color[1], color[2], 255))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def create_box_glb(
    width_m: float = 1.2,
    depth_m: float = 1.0,
    height_m: float = 1.0,
    mesh_name: str = "SM_Prop_EnergyCrate_A",
    include_lod1: bool = False,
    include_collider: bool = False,
    collider_name: str = "COL_Prop_EnergyCrate_A",
    num_materials: int = 1,
    include_texture: bool = False,
    texture_size: tuple[int, int] = (64, 64),
    origin: str = "bottom_center",  # "bottom_center" or "center"
    output_path: Path | None = None,
) -> bytes:
    """Generate a standard glTF 2.0 binary GLB file with real buffers and geometry."""
    hw = width_m / 2.0
    hd = depth_m / 2.0
    y_min = 0.0 if origin == "bottom_center" else -height_m / 2.0
    y_max = height_m if origin == "bottom_center" else height_m / 2.0

    # 8 vertices for a box
    # Godot / glTF coordinate convention: +Y is UP, +Z is back, -Z is front, +X is right
    vertices = [
        [-hw, y_min, -hd],
        [hw, y_min, -hd],
        [hw, y_min, hd],
        [-hw, y_min, hd],
        [-hw, y_max, -hd],
        [hw, y_max, -hd],
        [hw, y_max, hd],
        [-hw, y_max, hd],
    ]
    # 12 triangles (36 indices)
    indices = [
        0,
        1,
        2,
        0,
        2,
        3,  # bottom
        4,
        6,
        5,
        4,
        7,
        6,  # top
        0,
        4,
        5,
        0,
        5,
        1,  # front (-Z)
        2,
        6,
        7,
        2,
        7,
        3,  # back (+Z)
        0,
        3,
        7,
        0,
        7,
        4,  # left (-X)
        1,
        5,
        6,
        1,
        6,
        2,  # right (+X)
    ]

    bin_chunks: list[bytes] = []
    buffer_views: list[dict[str, Any]] = []
    accessors: list[dict[str, Any]] = []

    def add_buffer_data(data: bytes, target: int | None = None) -> int:
        offset = sum(len(c) for c in bin_chunks)
        # Pad offset to 4-byte boundary
        pad = (4 - (offset % 4)) % 4
        if pad:
            bin_chunks.append(b"\x00" * pad)
            offset += pad
        bin_chunks.append(data)
        view_idx = len(buffer_views)
        view_entry: dict[str, Any] = {"buffer": 0, "byteOffset": offset, "byteLength": len(data)}
        if target is not None:
            view_entry["target"] = target
        buffer_views.append(view_entry)
        return view_idx

    pos_bytes = b"".join(struct.pack("<fff", *v) for v in vertices)
    pos_view_idx = add_buffer_data(pos_bytes, target=34962)  # ARRAY_BUFFER

    min_pos = [-hw, y_min, -hd]
    max_pos = [hw, y_max, hd]
    pos_accessor_idx = len(accessors)
    accessors.append(
        {
            "bufferView": pos_view_idx,
            "byteOffset": 0,
            "componentType": 5126,  # FLOAT
            "count": 8,
            "type": "VEC3",
            "min": min_pos,
            "max": max_pos,
        }
    )

    uv_bytes = b"".join(
        struct.pack("<ff", *uv)
        for uv in [(0, 0), (1, 0), (1, 1), (0, 1), (0, 0), (1, 0), (1, 1), (0, 1)]
    )
    uv_view_idx = add_buffer_data(uv_bytes, target=34962)
    uv_accessor_idx = len(accessors)
    accessors.append(
        {
            "bufferView": uv_view_idx,
            "byteOffset": 0,
            "componentType": 5126,
            "count": 8,
            "type": "VEC2",
        }
    )

    idx_bytes = b"".join(struct.pack("<H", i) for i in indices)
    idx_view_idx = add_buffer_data(idx_bytes, target=34963)  # ELEMENT_ARRAY_BUFFER
    idx_accessor_idx = len(accessors)
    accessors.append(
        {
            "bufferView": idx_view_idx,
            "byteOffset": 0,
            "componentType": 5123,  # UNSIGNED_SHORT
            "count": 36,
            "type": "SCALAR",
            "min": [0],
            "max": [7],
        }
    )

    materials: list[dict[str, Any]] = []
    images: list[dict[str, Any]] = []
    textures: list[dict[str, Any]] = []

    texture_idx = None
    if include_texture:
        png_bytes = _create_solid_texture_png(size=texture_size)
        tex_view_idx = add_buffer_data(png_bytes)
        img_idx = len(images)
        images.append({"bufferView": tex_view_idx, "mimeType": "image/png"})
        texture_idx = len(textures)
        textures.append({"source": img_idx})

    for m in range(max(1, num_materials)):
        mat_entry: dict[str, Any] = {
            "name": f"M_Material_{m + 1}",
            "pbrMetallicRoughness": {
                "baseColorFactor": [min(0.3 * (m + 1), 1.0), 0.5, 0.8, 1.0],
                "metallicFactor": 0.1,
                "roughnessFactor": 0.8,
            },
        }
        if texture_idx is not None and m == 0:
            mat_entry["pbrMetallicRoughness"]["baseColorTexture"] = {"index": texture_idx}
        materials.append(mat_entry)

    meshes: list[dict[str, Any]] = []
    nodes: list[dict[str, Any]] = []

    # Node 0: LOD0 mesh
    lod0_mesh_idx = len(meshes)
    meshes.append(
        {
            "name": f"{mesh_name}_LOD0",
            "primitives": [
                {
                    "attributes": {"POSITION": pos_accessor_idx, "TEXCOORD_0": uv_accessor_idx},
                    "indices": idx_accessor_idx,
                    "material": m,
                }
                for m in range(max(1, num_materials))
            ],
        }
    )
    nodes.append({"name": f"{mesh_name}_LOD0", "mesh": lod0_mesh_idx})

    # Optional LOD1 node
    if include_lod1:
        # For a box fixture, LOD1 can use 8 triangles (24 indices) or same geometry with fewer tris
        # Let's create an 8-triangle index list (decimated box representation)
        lod1_indices = indices[:24]  # 8 triangles
        lod1_idx_bytes = b"".join(struct.pack("<H", i) for i in lod1_indices)
        lod1_idx_view = add_buffer_data(lod1_idx_bytes, target=34963)
        lod1_acc_idx = len(accessors)
        accessors.append(
            {
                "bufferView": lod1_idx_view,
                "byteOffset": 0,
                "componentType": 5123,
                "count": 24,
                "type": "SCALAR",
                "min": [0],
                "max": [7],
            }
        )
        lod1_mesh_idx = len(meshes)
        meshes.append(
            {
                "name": f"{mesh_name}_LOD1",
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_accessor_idx, "TEXCOORD_0": uv_accessor_idx},
                        "indices": lod1_acc_idx,
                        "material": 0,
                    }
                ],
            }
        )
        nodes.append({"name": f"{mesh_name}_LOD1", "mesh": lod1_mesh_idx})

    # Optional Collider node
    if include_collider:
        col_mesh_idx = len(meshes)
        meshes.append(
            {
                "name": collider_name,
                "primitives": [
                    {
                        "attributes": {"POSITION": pos_accessor_idx, "TEXCOORD_0": uv_accessor_idx},
                        "indices": idx_accessor_idx,
                    }
                ],
            }
        )
        nodes.append({"name": collider_name, "mesh": col_mesh_idx})

    total_bin = b"".join(bin_chunks)
    # Ensure final buffer length is padded to 4 bytes
    pad_bin = (4 - (len(total_bin) % 4)) % 4
    if pad_bin:
        total_bin += b"\x00" * pad_bin

    gltf_dict: dict[str, Any] = {
        "asset": {"version": "2.0", "generator": "AI Game Factory Asset Generator"},
        "scenes": [{"nodes": list(range(len(nodes)))}],
        "nodes": nodes,
        "meshes": meshes,
        "materials": materials,
        "accessors": accessors,
        "bufferViews": buffer_views,
        "buffers": [{"byteLength": len(total_bin)}],
    }
    if textures:
        gltf_dict["textures"] = textures
    if images:
        gltf_dict["images"] = images

    json_bytes = json.dumps(gltf_dict, separators=(",", ":")).encode("utf-8")
    json_pad = (4 - (len(json_bytes) % 4)) % 4
    if json_pad:
        json_bytes += b" " * json_pad

    total_length = 12 + 8 + len(json_bytes) + 8 + len(total_bin)
    header = struct.pack("<4sII", b"glTF", 2, total_length)
    chunk0 = struct.pack("<II", len(json_bytes), 0x4E4F534A) + json_bytes
    chunk1 = struct.pack("<II", len(total_bin), 0x004E4942) + total_bin
    glb_data = header + chunk0 + chunk1

    if output_path is not None:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(glb_data)

    return glb_data
