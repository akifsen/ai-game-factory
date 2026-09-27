"""Bounds-checked validator for the supported static_prop GLB subset.

This deliberately rejects general glTF features the factory cannot prove safe.
It decodes geometry and transforms itself; accessor min/max are never trusted as
the source of measured bounds.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image

from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    AssetValidationResult,
    ValidationFinding,
)
from gamefactory.core.domain.asset_contracts import (
    ValidationFindingSeverity as Severity,
)

_COMPONENTS = {
    5120: (1, "b"),
    5121: (1, "B"),
    5122: (2, "h"),
    5123: (2, "H"),
    5125: (4, "I"),
    5126: (4, "f"),
}
_WIDTHS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4}
_MAX_GLTF_NODES = 100_000
_MAX_ACCESSOR_ELEMENTS = 1_000_000
_MAX_TOTAL_TRIANGLES = 100_000
_MAX_FILE_BYTES = 50 * 1024 * 1024


class _InvalidGLB(ValueError):
    pass


@dataclass(frozen=True)
class _MeshInfo:
    name: str
    triangle_count: int
    material_ids: frozenset[int]
    points: tuple[tuple[float, float, float], ...]


def _read_glb(path: Path, max_bytes: int) -> tuple[dict[str, Any], bytes]:
    size = path.stat().st_size
    if path.suffix.casefold() != ".glb":
        raise _InvalidGLB("artifact extension must be .glb")
    if size < 20 or size > max_bytes:
        raise _InvalidGLB(f"file size {size} is outside 20..{max_bytes} bytes")
    data = path.read_bytes()
    magic, version, total = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or total != len(data):
        raise _InvalidGLB("invalid GLB magic, version, or declared total length")
    offset = 12
    json_chunk: bytes | None = None
    bin_chunk: bytes | None = None
    while offset < total:
        if offset + 8 > total:
            raise _InvalidGLB("truncated GLB chunk header")
        length, kind = struct.unpack_from("<II", data, offset)
        offset += 8
        end = offset + length
        if length % 4 or end > total:
            raise _InvalidGLB("invalid or out-of-bounds GLB chunk length")
        chunk = data[offset:end]
        if kind == 0x4E4F534A:
            if json_chunk is not None or offset != 20:
                raise _InvalidGLB("JSON chunk must be first and unique")
            json_chunk = chunk
        elif kind == 0x004E4942:
            if bin_chunk is not None:
                raise _InvalidGLB("multiple BIN chunks are unsupported")
            bin_chunk = chunk
        else:
            raise _InvalidGLB("unknown GLB chunk type")
        offset = end
    if json_chunk is None or bin_chunk is None:
        raise _InvalidGLB("GLB must contain JSON and BIN chunks")
    try:
        document = json.loads(
            json_chunk.decode("utf-8").rstrip(" \t\r\n\x00"),
            object_pairs_hook=_unique_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"invalid numeric token {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _InvalidGLB(f"invalid JSON chunk: {exc}") from exc
    if not isinstance(document, dict) or document.get("asset", {}).get("version") != "2.0":
        raise _InvalidGLB("unsupported glTF document/version")
    return document, bin_chunk


def _accessor(
    document: dict[str, Any], binary: bytes, index: int, expected_type: str | None = None
) -> list[tuple[int | float, ...]]:
    accessors = document.get("accessors", [])
    views = document.get("bufferViews", [])
    if not isinstance(index, int) or not 0 <= index < len(accessors):
        raise _InvalidGLB("accessor index is invalid")
    accessor = accessors[index]
    if not isinstance(accessor, dict) or "sparse" in accessor:
        raise _InvalidGLB("invalid or sparse accessor is unsupported")
    typ, component = accessor.get("type"), accessor.get("componentType")
    if (
        typ not in _WIDTHS
        or (expected_type and typ != expected_type)
        or component not in _COMPONENTS
    ):
        raise _InvalidGLB("accessor type/component is unsupported")
    count = accessor.get("count")
    if not isinstance(count, int) or count <= 0 or count > _MAX_ACCESSOR_ELEMENTS:
        raise _InvalidGLB("accessor count is invalid or exceeds safety limit")
    view_index = accessor.get("bufferView")
    if not isinstance(view_index, int) or not 0 <= view_index < len(views):
        raise _InvalidGLB("accessor bufferView is missing or invalid")
    view = views[view_index]
    if not isinstance(view, dict) or view.get("buffer") != 0:
        raise _InvalidGLB("only the single embedded GLB buffer is supported")
    component_size, code = _COMPONENTS[component]
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
        raise _InvalidGLB("invalid accessor stride or offsets")
    end = base + (count - 1) * stride + packed_size
    declared_buffer_length = document.get("buffers", [{}])[0].get("byteLength", -1)
    if (
        view_end > len(binary)
        or view_end > declared_buffer_length
        or end > view_end
        or view_end < view_start
    ):
        raise _InvalidGLB("accessor reads outside its bufferView/BIN chunk")
    fmt = "<" + code * width
    result = [struct.unpack_from(fmt, binary, base + i * stride) for i in range(count)]
    if component == 5126 and any(not math.isfinite(v) for row in result for v in row):
        raise _InvalidGLB("accessor contains non-finite floating-point values")
    return result


def _mat_mul(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(4)) for c in range(4)] for r in range(4)]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _node_matrix(node: dict[str, Any]) -> list[list[float]]:
    if "matrix" in node:
        values = node["matrix"]
        if (
            not isinstance(values, list)
            or len(values) != 16
            or not all(math.isfinite(float(v)) for v in values)
        ):
            raise _InvalidGLB("node matrix is malformed")
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
        raise _InvalidGLB("node TRS is malformed")
    x, y, z, w = (float(v) for v in q)
    norm = math.sqrt(x * x + y * y + z * z + w * w)
    if norm < 1e-12:
        raise _InvalidGLB("node quaternion is zero")
    x, y, z, w = (v / norm for v in (x, y, z, w))
    r = [
        [1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
        [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
        [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)],
    ]
    m = [[r[i][j] * float(s[j]) for j in range(3)] + [float(t[i])] for i in range(3)]
    return [*m, [0.0, 0.0, 0.0, 1.0]]


def _material_texcoords(document: dict[str, Any], material_index: int) -> list[int]:
    materials = document.get("materials", [])
    if (
        not isinstance(material_index, int)
        or isinstance(material_index, bool)
        or not 0 <= material_index < len(materials)
    ):
        raise _InvalidGLB("primitive references nonexistent material")
    material = materials[material_index]
    if not isinstance(material, dict):
        raise _InvalidGLB("material entry is malformed")
    pbr = material.get("pbrMetallicRoughness", {})
    if not isinstance(pbr, dict):
        raise _InvalidGLB("pbrMetallicRoughness is malformed")
    texture_infos = (
        pbr.get("baseColorTexture"),
        pbr.get("metallicRoughnessTexture"),
        material.get("normalTexture"),
        material.get("occlusionTexture"),
        material.get("emissiveTexture"),
    )
    texcoords: list[int] = []
    textures = document.get("textures", [])
    for info in texture_infos:
        if info is None:
            continue
        if not isinstance(info, dict):
            raise _InvalidGLB("material texture slot is malformed")
        texture_index = info.get("index")
        if (
            not isinstance(texture_index, int)
            or isinstance(texture_index, bool)
            or not 0 <= texture_index < len(textures)
        ):
            raise _InvalidGLB("material references missing texture")
        texcoord = info.get("texCoord", 0)
        if not isinstance(texcoord, int) or isinstance(texcoord, bool) or texcoord < 0:
            raise _InvalidGLB("material texture texCoord set is invalid")
        texcoords.append(texcoord)
    return texcoords


def _inspect(
    document: dict[str, Any], binary: bytes
) -> tuple[
    list[_MeshInfo],
    list[tuple[float, float, float]],
    set[int],
    list[tuple[int, int]],
    int,
    dict[int, list[list[float]]],
]:
    if document.get("animations"):
        raise _InvalidGLB("animations are forbidden by the asset profile")
    if document.get("skins"):
        raise _InvalidGLB("skins/rigging are forbidden by the asset profile")
    if document.get("extensionsRequired") or document.get("extensionsUsed"):
        raise _InvalidGLB("glTF extensions are unsupported by the asset profile")

    def reject_extensions(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("extensions"):
                raise _InvalidGLB("glTF extensions are unsupported by the asset profile")
            for child in value.values():
                reject_extensions(child)
        elif isinstance(value, list):
            for child in value:
                reject_extensions(child)

    reject_extensions(document)
    if len(document.get("buffers", [])) != 1 or document["buffers"][0].get("uri") is not None:
        raise _InvalidGLB(
            "only one embedded GLB buffer is supported; external buffers are forbidden"
        )
    declared_length = document["buffers"][0].get("byteLength", -1)
    if not isinstance(declared_length, int) or declared_length < 0 or declared_length > len(binary):
        raise _InvalidGLB("declared buffer length exceeds embedded BIN data")
    if len(document.get("nodes", [])) > _MAX_GLTF_NODES:
        raise _InvalidGLB("node count exceeds safety limit")
    nodes, meshes = document.get("nodes", []), document.get("meshes", [])
    if not isinstance(nodes, list) or not isinstance(meshes, list):
        raise _InvalidGLB("nodes and meshes must be arrays")
    parents: dict[int, int] = {}
    for parent, node in enumerate(nodes):
        for child in node.get("children", []):
            if not isinstance(child, int) or not 0 <= child < len(nodes) or child in parents:
                raise _InvalidGLB("invalid or multiply-parented node")
            parents[child] = parent
    local = [_node_matrix(n) for n in nodes]
    world: dict[int, list[list[float]]] = {}

    def world_matrix(i: int, trail: set[int]) -> list[list[float]]:
        if i in world:
            return world[i]
        if i in trail:
            raise _InvalidGLB("node hierarchy cycle")
        if len(trail) >= 128:
            raise _InvalidGLB("node hierarchy exceeds maximum depth 128")
        val = (
            local[i]
            if i not in parents
            else _mat_mul(world_matrix(parents[i], trail | {i}), local[i])
        )
        world[i] = val
        return val

    scene_index = document.get("scene", 0)
    scenes = document.get("scenes", [])
    if not scenes or not isinstance(scene_index, int) or not 0 <= scene_index < len(scenes):
        raise _InvalidGLB("active scene is missing or invalid")
    reachable: set[int] = set()

    def visit(i: int, trail: set[int]) -> None:
        if i in trail:
            raise _InvalidGLB("node hierarchy cycle")
        if len(trail) >= 128:
            raise _InvalidGLB("node hierarchy exceeds maximum depth 128")
        if i in reachable:
            return
        if not 0 <= i < len(nodes):
            raise _InvalidGLB("scene references invalid node")
        reachable.add(i)
        for child in nodes[i].get("children", []):
            visit(child, trail | {i})

    for root in scenes[scene_index].get("nodes", []):
        visit(root, set())
    infos: list[_MeshInfo] = []
    all_points: list[tuple[float, float, float]] = []
    material_ids: set[int] = set()
    texture_refs: list[tuple[int, int]] = []
    total_triangles = 0
    used_meshes: set[int] = set()
    for ni in sorted(reachable):
        node = nodes[ni]
        if "mesh" not in node:
            continue
        mi = node["mesh"]
        if not isinstance(mi, int) or not 0 <= mi < len(meshes):
            raise _InvalidGLB("node mesh index invalid")
        used_meshes.add(mi)
        mesh = meshes[mi]
        name = str(node.get("name") or mesh.get("name") or "")
        pts: list[tuple[float, float, float]] = []
        tri_count = 0
        mats: set[int] = set()
        matrix = world_matrix(ni, set())
        for primitive in mesh.get("primitives", []):
            if primitive.get("mode", 4) != 4:
                raise _InvalidGLB("only triangle primitives are supported")
            attrs = primitive.get("attributes", {})
            if "JOINTS_0" in attrs or "WEIGHTS_0" in attrs or "POSITION" not in attrs:
                raise _InvalidGLB("skinned or positionless primitive is unsupported")
            positions = _accessor(document, binary, attrs["POSITION"], "VEC3")
            if document["accessors"][attrs["POSITION"]].get("componentType") != 5126:
                raise _InvalidGLB("POSITION must use float32")
            if "indices" in primitive:
                indices = _accessor(document, binary, primitive["indices"], "SCALAR")
                if document["accessors"][primitive["indices"]].get("componentType") == 5126:
                    raise _InvalidGLB("indices cannot be float")
                ids = [int(row[0]) for row in indices]
            else:
                ids = list(range(len(positions)))
            if len(ids) % 3 or any(i < 0 or i >= len(positions) for i in ids):
                raise _InvalidGLB("triangle indices are malformed or out of range")
            tri_count += len(ids) // 3
            for position_index in sorted(set(ids)):
                row = positions[position_index]
                x, y, z = row
                p: tuple[float, float, float] = (
                    float(sum(matrix[0][c] * (x, y, z, 1)[c] for c in range(4))),
                    float(sum(matrix[1][c] * (x, y, z, 1)[c] for c in range(4))),
                    float(sum(matrix[2][c] * (x, y, z, 1)[c] for c in range(4))),
                )
                pts.append(p)
            mat_id = primitive.get("material")
            if mat_id is not None:
                mats.add(mat_id)
                material_ids.add(mat_id)
                for texcoord in _material_texcoords(document, mat_id):
                    attribute = f"TEXCOORD_{texcoord}"
                    if attribute not in attrs:
                        raise _InvalidGLB(f"textured primitive is missing {attribute}")
                    uv = _accessor(document, binary, attrs[attribute], "VEC2")
                    if len(uv) != len(positions):
                        raise _InvalidGLB(f"{attribute} count differs from POSITION count")
            if "targets" in primitive:
                raise _InvalidGLB("morph targets are unsupported")
        infos.append(_MeshInfo(name, tri_count, frozenset(mats), tuple(pts)))
        all_points.extend(pts)
        total_triangles += tri_count
        if total_triangles > _MAX_TOTAL_TRIANGLES:
            raise _InvalidGLB(
                f"total geometry exceeds safety limit {_MAX_TOTAL_TRIANGLES} triangles"
            )
    if len(used_meshes) != len(meshes):
        raise _InvalidGLB("unreferenced mesh definitions are unsupported/unsafe")
    # External image URIs are rejected. Embedded images must be valid PNG/JPEG bytes.
    views = document.get("bufferViews", [])
    for image in document.get("images", []):
        if "uri" in image:
            raise _InvalidGLB("external image URI is forbidden")
        view_id = image.get("bufferView")
        if not isinstance(view_id, int) or not 0 <= view_id < len(views):
            raise _InvalidGLB("embedded image bufferView is invalid")
        view = views[view_id]
        start, end = (
            view.get("byteOffset", 0),
            view.get("byteOffset", 0) + view.get("byteLength", -1),
        )
        if start < 0 or end > len(binary) or end <= start:
            raise _InvalidGLB("image bytes are out of bounds")
        try:
            with Image.open(io.BytesIO(binary[start:end])) as img:
                w, h = img.size
                if w <= 0 or h <= 0 or max(w, h) > 16384 or w * h > 100_000_000:
                    raise _InvalidGLB("embedded image exceeds hard decoded-size safety limit")
                img.verify()
            with Image.open(io.BytesIO(binary[start:end])) as img:
                w, h = img.size
                if img.format not in {"PNG", "JPEG"}:
                    raise _InvalidGLB("only PNG/JPEG textures are supported")
                img.load()
                texture_refs.append((w, h))
        except Exception as exc:
            raise _InvalidGLB(f"embedded image is missing, corrupt, or unsupported: {exc}") from exc
    for texture in document.get("textures", []):
        source = texture.get("source")
        if not isinstance(source, int) or not 0 <= source < len(document.get("images", [])):
            raise _InvalidGLB("texture has missing or invalid image source")
    return infos, all_points, material_ids, texture_refs, total_triangles, world


def preflight_glb(path: Path, *, max_file_size_bytes: int = _MAX_FILE_BYTES) -> dict[str, Any]:
    """Reject malformed/unsafe/unsupported source GLBs before launching Blender."""
    if not path.is_file():
        raise ValueError("input GLB is not a regular file")
    doc, binary = _read_glb(path, max_file_size_bytes)
    infos, points, _, textures, triangles, _ = _inspect(doc, binary)
    if not points or triangles <= 0 or not infos:
        raise ValueError("input GLB has no nonempty static triangle mesh")
    return {
        "mesh_count": len(infos),
        "triangles": triangles,
        "materials": len(doc.get("materials", [])),
        "texture_max_dimension": max((max(w, h) for w, h in textures), default=0),
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def validate_glb(
    path: Path, spec: AssetSpecification, *, max_file_size_bytes: int = _MAX_FILE_BYTES
) -> AssetValidationResult:
    """Validate a processed GLB against the specification's bound profile."""
    profile = spec.bound_profile()
    contract = profile.processing_contract(spec)
    artifact = str(path)
    findings: list[ValidationFinding] = []

    def add(
        rule: str, passed: bool, expected: str, actual: str, message: str, warning: bool = False
    ) -> None:
        severity = Severity.PASS if passed else Severity.WARNING if warning else Severity.FAIL
        findings.append(ValidationFinding(rule, severity, expected, actual, artifact, message))

    if not path.is_file():
        add("glb.exists", False, "regular GLB file", "missing", "Artifact does not exist")
        return AssetValidationResult(
            Severity.FAIL, findings, "GLB validation failed: artifact missing"
        )
    try:
        doc, binary = _read_glb(path, max_file_size_bytes)
        infos, points, material_ids, textures, tris, world_matrices = _inspect(doc, binary)
        add(
            "glb.parse",
            True,
            "GLB 2.0 with bounded embedded buffers",
            "parsed",
            "GLB structure and accessors are valid",
        )
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        add("glb.hash", True, "SHA-256 recorded", digest, "Artifact hash computed")
        names = [info.name for info in infos]
        unique_names = len(names) == len(set(names))
        expected_names = profile.expected_mesh_names(spec)
        expected_lod0 = f"SM_{spec.asset_id}_LOD0"
        expected_lod1 = f"SM_{spec.asset_id}_LOD1"
        expected_collider = f"COL_{spec.asset_id}"
        add(
            "nodes.unique",
            unique_names,
            "unique mesh node names",
            str(names),
            "Duplicate names are ambiguous during engine import",
        )
        add(
            "nodes.profile",
            set(names) == expected_names,
            f"exactly {sorted(expected_names)}",
            str(names),
            "Unexpected or missing mesh nodes are rejected",
        )
        by_name = {info.name: info for info in infos}
        lod0 = by_name.get(expected_lod0)
        lod1 = by_name.get(expected_lod1)
        colliders = [by_name[expected_collider]] if expected_collider in by_name else []
        add(
            "mesh.nonempty",
            bool(points) and tris > 0,
            "nonempty triangle geometry",
            f"{tris} triangles",
            "Actual POSITION and index data decoded",
        )
        add(
            "lod0.present",
            lod0 is not None and lod0.triangle_count > 0,
            "LOD0 mesh geometry",
            "present" if lod0 else "missing",
            "LOD0 node must reference nonempty triangles",
        )
        lod1_required = bool(contract["lod1_required"])
        add(
            "lod1.present",
            (lod1 is not None and lod1.triangle_count > 0) or not lod1_required,
            "LOD1 mesh geometry" if lod1_required else "LOD1 optional",
            "present" if lod1 else "missing",
            "LOD1 is required by the profile contract"
            if lod1_required
            else "LOD1 is optional for this profile",
        )
        visual_points = list(lod0.points) if lod0 else []
        visual_mins = (
            [min(p[i] for p in visual_points) for i in range(3)] if visual_points else [0.0] * 3
        )
        visual_maxs = (
            [max(p[i] for p in visual_points) for i in range(3)] if visual_points else [0.0] * 3
        )
        collider_shape_ok = bool(colliders) and all(
            c.triangle_count == 12
            and len({tuple(round(v, 6) for v in p) for p in c.points}) == 8
            and abs(
                (max(p[0] for p in c.points) - min(p[0] for p in c.points))
                * (max(p[1] for p in c.points) - min(p[1] for p in c.points))
                * (max(p[2] for p in c.points) - min(p[2] for p in c.points))
            )
            > 1e-9
            and all(
                abs(min(p[i] for p in c.points) - visual_mins[i]) <= 0.01
                and abs(max(p[i] for p in c.points) - visual_maxs[i]) <= 0.01
                for i in range(3)
            )
            for c in colliders
        )
        add(
            "collider.geometry",
            collider_shape_ok,
            "axis-aligned box geometry matching visual bounds",
            f"{len(colliders)} collider mesh(es)",
            "Collider decoded vertices and triangles are checked against visual bounds",
        )
        add(
            "geometry.lod0_budget",
            lod0 is not None and lod0.triangle_count <= spec.geometry_budget.max_triangles_lod0,
            f"<= {spec.geometry_budget.max_triangles_lod0}",
            str(lod0.triangle_count if lod0 else 0),
            "LOD0 triangle budget",
        )
        add(
            "geometry.lod1_budget",
            lod1 is None or lod1.triangle_count <= spec.geometry_budget.max_triangles_lod1,
            f"<= {spec.geometry_budget.max_triangles_lod1}",
            str(lod1.triangle_count if lod1 else 0),
            "LOD1 triangle budget",
        )
        material_count = len(doc.get("materials", []))
        add(
            "materials.budget",
            material_count <= spec.material_budget.max_materials,
            f"<= {spec.material_budget.max_materials}",
            str(material_count),
            "Declared material count in GLB",
        )
        texture_max = max((max(w, h) for w, h in textures), default=0)
        add(
            "textures.dimension",
            texture_max <= spec.texture_budget.max_dimension,
            f"<= {spec.texture_budget.max_dimension}px",
            str(texture_max),
            "Embedded texture dimensions decoded from image bytes",
        )
        # Bounds include visual meshes only; collider is independently checked above.
        if not visual_points:
            raise _InvalidGLB("no non-collider visual geometry")
        mins = [min(p[i] for p in visual_points) for i in range(3)]
        maxs = [max(p[i] for p in visual_points) for i in range(3)]
        actual = [maxs[i] - mins[i] for i in range(3)]
        target = [spec.dimensions.width_m, spec.dimensions.height_m, spec.dimensions.depth_m]
        tolerance = float(contract["dimension_tolerance_m"])
        bounds_ok = all(abs(a - t) <= tolerance for a, t in zip(actual, target, strict=True))
        add(
            "scale.bounds",
            bounds_ok,
            f"X/Y/Z = {target}m ± {tolerance}m",
            str(actual),
            "Dimensions measured from transformed decoded vertices",
        )
        origin_ok = (
            abs((mins[0] + maxs[0]) / 2) <= tolerance and abs((mins[2] + maxs[2]) / 2) <= tolerance
        )
        if spec.origin_policy == "bottom_center":
            origin_ok = origin_ok and abs(mins[1]) <= tolerance
        elif spec.origin_policy == "center":
            origin_ok = origin_ok and abs((mins[1] + maxs[1]) / 2) <= tolerance
        add(
            "origin.policy",
            origin_ok,
            spec.origin_policy,
            f"min={mins}, max={maxs}",
            "Origin evaluated from transformed geometry",
        )
        snap_grid = contract.get("snap_grid_m")
        if snap_grid:
            snap_ok = all(
                abs((value / float(snap_grid)) - round(value / float(snap_grid))) * float(snap_grid)
                <= tolerance
                for value in actual
            )
            add(
                "dimensions.snap",
                snap_ok and origin_ok,
                f"module axes are multiples of {snap_grid} m and the origin is on the snap pivot",
                str(actual),
                "Modular dimensions and origin must land on the profile snap grid",
            )
        lod1_points = lod1.points if lod1 else ()
        if not lod1_required and not lod1_points:
            lod_bounds_match = True
        else:
            lod_bounds_match = bool(lod0 and lod1_points) and all(
                abs(min(point[index] for point in lod1_points) - mins[index]) <= tolerance
                and abs(max(point[index] for point in lod1_points) - maxs[index]) <= tolerance
                for index in range(3)
            )
        add(
            "lod1.bounds",
            lod_bounds_match,
            f"LOD1 bounds match LOD0 ± {tolerance}m",
            str(lod1 and lod1.points[:1]),
            "LOD1 geometry must preserve the visual envelope",
        )
        identity = True
        for node_index, node in enumerate(doc.get("nodes", [])):
            if "mesh" not in node:
                continue
            node_name = str(node.get("name") or "")
            if node_name.startswith("COL_"):
                continue
            expected_matrix = [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, 1.0, 0.0],
                [0.0, 0.0, 0.0, 1.0],
            ]
            identity = identity and all(
                abs(world_matrices[node_index][r][c] - expected_matrix[r][c]) <= 1e-5
                for r in range(4)
                for c in range(4)
            )
        add(
            "orientation.identity",
            identity,
            "mesh nodes carry no residual transforms; +Y up/-Z front coordinates",
            "identity" if identity else "non-identity transform",
            "Coordinate convention is checked through node transforms and world-space bounds",
        )
        if material_ids and max(material_ids) >= len(doc.get("materials", [])):
            raise _InvalidGLB("primitive references nonexistent material")
    except (OSError, _InvalidGLB, KeyError, TypeError, ValueError, IndexError, struct.error) as exc:
        add(
            "glb.parse",
            False,
            "supported safe profile GLB subset",
            str(exc),
            "GLB could not be safely validated",
        )
    status = (
        Severity.FAIL
        if any(f.severity == Severity.FAIL for f in findings)
        else Severity.WARNING
        if any(f.severity == Severity.WARNING for f in findings)
        else Severity.PASS
    )
    failures = sum(f.severity == Severity.FAIL for f in findings)
    return AssetValidationResult(
        status,
        findings,
        f"{'Passed' if not failures else 'Failed'} with {failures} failing rule(s)",
    )


def render_asset_report(
    asset_id: str, raw_hash: str, processed_hash: str, result: AssetValidationResult
) -> str:
    """Return a stable, human-readable processing and validation summary."""
    lines = [
        f"Asset: {asset_id}",
        "",
        "Artifacts",
        f"Raw SHA-256: {raw_hash}",
        f"Processed SHA-256: {processed_hash}",
        "",
        f"Validation: {result.status.value}",
    ]
    lines.extend(
        f"{f.rule_id:28} {f.severity.value:7} expected={f.expected}; actual={f.actual}; {f.message}"
        for f in result.findings
    )
    return "\n".join(lines) + "\n"
