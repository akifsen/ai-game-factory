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
from pathlib import Path
from typing import Any

from PIL import Image

from gamefactory.adapters.assets.validation_rules import (
    Composition,
    InvalidGLB,
    MeshInfo,
    NodeInfo,
    NormalizationRecord,
    ParsedGLB,
    RuleInput,
    select_composition,
)
from gamefactory.core.domain.asset_contracts import (
    AssetSpecification,
    AssetSpecificationV07,
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


_InvalidGLB = InvalidGLB
_MeshInfo = MeshInfo


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
        local_pts: list[tuple[float, float, float]] = []
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
                local_pts.append((float(x), float(y), float(z)))
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
        infos.append(_MeshInfo(name, tri_count, frozenset(mats), tuple(pts), ni, tuple(local_pts)))
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


def parse_glb(path: Path, *, max_file_size_bytes: int = _MAX_FILE_BYTES) -> ParsedGLB:
    """Parse and inspect one GLB into the structure every rule group reads."""
    doc, binary = _read_glb(path, max_file_size_bytes)
    infos, points, material_ids, textures, tris, world_matrices = _inspect(doc, binary)
    raw_nodes = doc.get("nodes", [])
    parents: dict[int, int] = {}
    for parent, node in enumerate(raw_nodes):
        for child in node.get("children", []):
            parents[child] = parent
    local = [_node_matrix(node) for node in raw_nodes]
    world: dict[int, list[list[float]]] = {}

    def world_of(index: int, depth: int = 0) -> list[list[float]]:
        if index in world:
            return world[index]
        if depth > 128:
            raise _InvalidGLB("node hierarchy exceeds maximum depth 128")
        value = (
            local[index]
            if index not in parents
            else _mat_mul(world_of(parents[index], depth + 1), local[index])
        )
        world[index] = value
        return value

    nodes: list[NodeInfo] = []
    for index, node in enumerate(raw_nodes):
        extras = node.get("extras", {})
        if not isinstance(extras, dict):
            raise _InvalidGLB("node extras must be an object")
        mesh = node.get("mesh")
        nodes.append(
            NodeInfo(
                index=index,
                name=str(node.get("name") or ""),
                parent=parents.get(index),
                children=tuple(node.get("children", [])),
                mesh=mesh if isinstance(mesh, int) else None,
                local=local[index],
                world=world_of(index),
                extras=extras,
            )
        )
    scene = doc["scenes"][doc.get("scene", 0)]
    return ParsedGLB(
        document=doc,
        binary=binary,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        meshes=infos,
        points=points,
        material_ids=material_ids,
        textures=textures,
        triangles=tris,
        world=world_matrices,
        nodes=nodes,
        scene_roots=tuple(scene.get("nodes", [])),
    )


def composition_for(spec: AssetSpecification | AssetSpecificationV07) -> Composition:
    """Rule groups for a specification, derived from typed capabilities (ADR 0018)."""
    if isinstance(spec, AssetSpecificationV07):
        profile = spec.bound_profile()
        return select_composition(
            geometry_mode=profile.geometry_mode,
            collider_policy=spec.collider.policy,
            sockets_declared=bool(spec.sockets),
            requires_normalization=spec.source_kind == "local_operator_assembly",
        )
    return select_composition(
        geometry_mode="single_mesh",
        collider_policy=spec.collider_policy,
        sockets_declared=False,
        requires_normalization=False,
    )


_PARSE_ERRORS = (OSError, _InvalidGLB, KeyError, TypeError, ValueError, IndexError, struct.error)


def validate_glb(
    path: Path,
    spec: AssetSpecification | AssetSpecificationV07,
    *,
    max_file_size_bytes: int = _MAX_FILE_BYTES,
    normalization: NormalizationRecord | None = None,
) -> AssetValidationResult:
    """Validate a processed GLB against the specification's bound profile.

    The ordered rule composition comes from :func:`composition_for`. Single-mesh
    box profiles keep the frozen V0.4-V0.6 finding order. ``normalization`` is the
    ``source_front`` record (with the retained source parsed) for assemblies.
    """
    profile: Any
    if isinstance(spec, AssetSpecificationV07):
        profile = spec.bound_profile()
        contract = profile.processing_contract(spec)
    else:
        profile = spec.bound_profile()
        contract = profile.processing_contract(spec)
    artifact = str(path)
    findings: list[ValidationFinding] = []
    composition = composition_for(spec)

    if not path.is_file():
        findings.append(
            ValidationFinding(
                "glb.exists",
                Severity.FAIL,
                "regular GLB file",
                "missing",
                artifact,
                "Artifact does not exist",
            )
        )
        return AssetValidationResult(
            Severity.FAIL, findings, "GLB validation failed: artifact missing"
        )
    try:
        parsed = parse_glb(path, max_file_size_bytes=max_file_size_bytes)
        rule_input = RuleInput(
            artifact=artifact,
            parsed=parsed,
            spec=spec,
            contract=contract,
            assembly=getattr(profile, "assembly", None),
            normalization=normalization,
        )
        for rule in composition.rules:
            findings.extend(rule(rule_input))
    except _PARSE_ERRORS as exc:
        findings.append(
            ValidationFinding(
                "glb.parse",
                Severity.FAIL,
                "supported safe profile GLB subset",
                str(exc),
                artifact,
                "GLB could not be safely validated",
            )
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
