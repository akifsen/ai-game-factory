"""Bounded standalone authored assembly source ingest adapter.

Implements ADR 0016 bounded ingest for local_operator_assembly sources:
- Retains one bounded 50MiB input byte snapshot and preflights retained snapshot
- Reuses existing bounded glb_validator._inspect for geometry/accessor/image/material/extension/skin/animation/depth safety
- Requires nonempty processable actual triangle geometry
- Validates PathGuard containment with exclusive ownership and no overwrite publication
- Pre-resolution lexical component checks rejecting symlinks, junctions, and reparse points
- Exclusive directory claim (mkdir without replace) followed by atomic publication-completion marker write (written last atomically), not whole-directory filesystem atomicity
- Deterministic cleanup removing only exact owned files with ownership evidence and rmdir if empty (never recursive delete final directory or foreign-added files)
- Rejects external resources, path escapes, symlinks, corrupt GLBs, and unauthorized wrappers
- Standalone verification requiring mandatory pinned SHA-256 provenance digest before trusting metadata
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import struct
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from gamefactory.adapters.assets.glb_validator import _inspect, _InvalidGLB, _node_matrix
from gamefactory.core.domain.assembly_source import (
    MAX_ASSEMBLY_SOURCE_BYTES,
    MAX_PROVENANCE_SIDECAR_BYTES,
    PUBLICATION_MARKER_FILENAME,
    AssemblyIngestResult,
    AssemblyPublicationMarker,
    AssemblySourceProvenance,
    DerivedFromEntry,
    assert_provenance_matches_spec,
    create_assembly_provenance,
)
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import utc_now_iso
from gamefactory.core.execution.path_guard import PathGuard

GLB_MAGIC = b"glTF"
CHUNK_TYPE_JSON = 0x4E4F534A
CHUNK_TYPE_BIN = 0x004E4942
_HEX_64_REGEX = re.compile(r"^[0-9a-fA-F]{64}$")
_ROT_Y_180 = [[-1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, -1.0]]


def _is_symlink_or_reparse(path: Path) -> bool:
    """Detect symbolic links, directory junctions, and reparse points."""
    try:
        if path.is_symlink() or os.path.islink(path):
            return True
        st = path.lstat()
        if hasattr(st, "st_reparse_tag") and st.st_reparse_tag != 0:
            return True
        file_attr = getattr(st, "st_file_attributes", 0)
        if file_attr & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400):
            return True
    except (OSError, ValueError):
        pass
    return False


def _assert_no_symlink_or_reparse_components(path: Path, label: str) -> None:
    """Reject any symlink, junction, or reparse point along lexical path components BEFORE resolution."""
    raw = path
    components = list(reversed(raw.parents)) + [raw]
    for comp in components:
        try:
            exists_no_follow = os.path.lexists(comp)
        except (OSError, ValueError):
            exists_no_follow = False
        if exists_no_follow or _is_symlink_or_reparse(comp):
            if _is_symlink_or_reparse(comp):
                raise ValidationError(
                    f"{label} contains a symlink, junction, or reparse component: {comp}",
                    details={"path": str(raw), "component": str(comp)},
                )


def _read_bounded(path: Path, max_bytes: int, label: str) -> bytes:
    """Read file content with strict size bound, symlink/reparse rejection, and existence check."""
    _assert_no_symlink_or_reparse_components(path, label)
    if not path.is_file():
        raise ValidationError(
            f"{label} does not exist or is not a regular file: {path}",
            details={"path": str(path)},
        )
    try:
        with path.open("rb") as stream:
            data = stream.read(max_bytes + 1)
    except OSError as exc:
        raise ValidationError(f"Cannot read {label} at {path}: {exc}") from exc

    if len(data) > max_bytes:
        raise ValidationError(
            f"{label} exceeds maximum allowed size of {max_bytes} bytes: {path}",
            details={"path": str(path), "size": len(data), "limit": max_bytes},
        )
    return data


@dataclass(frozen=True)
class _OwnedFile:
    path: Path
    device: int
    inode: int
    size: int
    sha256: str

    def at(self, path: Path) -> _OwnedFile:
        return _OwnedFile(path, self.device, self.inode, self.size, self.sha256)


def _write_owned_file(path: Path, content: bytes) -> _OwnedFile:
    """Create a file exclusively and retain its filesystem identity for safe cleanup."""
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        identity = os.fstat(stream.fileno())
    return _OwnedFile(
        path=path,
        device=identity.st_dev,
        inode=identity.st_ino,
        size=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
    )


def _is_same_owned_file(owned_file: _OwnedFile) -> bool:
    try:
        current = owned_file.path.lstat()
        if (
            not stat.S_ISREG(current.st_mode)
            or current.st_dev != owned_file.device
            or current.st_ino != owned_file.inode
            or current.st_size != owned_file.size
        ):
            return False
        return hashlib.sha256(owned_file.path.read_bytes()).hexdigest() == owned_file.sha256
    except OSError:
        return False


def _publish_noreplace(source: _OwnedFile, destination: Path, label: str) -> _OwnedFile:
    """Atomically add a hard link without replacing any pre-existing destination."""
    if not _is_same_owned_file(source):
        raise ValidationError(f"Staged {label} changed before publication")
    try:
        os.link(source.path, destination)
    except FileExistsError as exc:
        raise ValidationError(
            f"Publication destination already exists: {destination}",
            details={"path": str(destination), "file": label},
        ) from exc
    except OSError as exc:
        raise ValidationError(
            f"Atomic no-replace publication of {label} is unsupported or failed at {destination}: {exc}",
            details={"path": str(destination), "file": label},
        ) from exc
    return source.at(destination)


def _remove_owned_staging_files(staging_dir: Path, owned_files: tuple[_OwnedFile, ...]) -> None:
    """Remove only unchanged files whose captured filesystem identity is ours."""
    for owned_file in owned_files:
        try:
            if _is_same_owned_file(owned_file):
                owned_file.path.unlink()
        except OSError:
            # Cleanup is best-effort; orphaned content is fail-closed and preserved.
            pass
    try:
        staging_dir.rmdir()
    except OSError:
        pass


def _require_list_field(obj: dict[str, Any], field: str, label: str) -> list[Any]:
    value = obj.get(field, [])
    if not isinstance(value, list):
        raise ValidationError(f"GLB {label} must be a list")
    return value


def _validate_source_json_shapes(document: dict[str, Any]) -> None:
    """Reject malformed source container shapes before legacy parser traversal."""
    for collection_name in ("scenes", "nodes", "meshes", "accessors", "bufferViews", "buffers"):
        collection = _require_list_field(document, collection_name, collection_name)
        for index, item in enumerate(collection):
            if not isinstance(item, dict):
                raise ValidationError(f"GLB {collection_name}[{index}] must be an object")

    for scene_index, scene in enumerate(document.get("scenes", [])):
        _require_list_field(scene, "nodes", f"scenes[{scene_index}].nodes")
    for node_index, node in enumerate(document.get("nodes", [])):
        if "children" in node:
            _require_list_field(node, "children", f"nodes[{node_index}].children")
    for mesh_index, mesh in enumerate(document.get("meshes", [])):
        primitives = _require_list_field(mesh, "primitives", f"meshes[{mesh_index}].primitives")
        for primitive_index, primitive in enumerate(primitives):
            if not isinstance(primitive, dict):
                raise ValidationError(
                    f"GLB meshes[{mesh_index}].primitives[{primitive_index}] must be an object"
                )
            if "attributes" in primitive and not isinstance(primitive["attributes"], dict):
                raise ValidationError(
                    f"GLB meshes[{mesh_index}].primitives[{primitive_index}].attributes must be an object"
                )


def _numeric_vector(value: Any, length: int, label: str) -> list[float]:
    if not isinstance(value, list) or len(value) != length:
        raise ValidationError(f"GLB {label} must be a {length}-element numeric array")
    if any(
        isinstance(component, bool) or not isinstance(component, (int, float))
        for component in value
    ):
        raise ValidationError(f"GLB {label} must contain only numeric values")
    converted = [float(component) for component in value]
    if not all(math.isfinite(component) for component in converted):
        raise ValidationError(f"GLB {label} must contain only finite numbers")
    return converted


def _validate_source_node_transforms(document: dict[str, Any]) -> None:
    """Validate raw authored TRS values before conversion to a matrix."""
    for node_index, node in enumerate(document.get("nodes", [])):
        name = node.get("name", f"nodes[{node_index}]")
        trs_present = any(key in node for key in ("translation", "rotation", "scale"))
        if "matrix" in node and trs_present:
            raise ValidationError(f"GLB node '{name}' cannot combine matrix and TRS transforms")
        if "translation" in node:
            _numeric_vector(node["translation"], 3, f"node '{name}' translation")
        if "rotation" in node:
            rotation = _numeric_vector(node["rotation"], 4, f"node '{name}' rotation")
            norm = math.sqrt(sum(component * component for component in rotation))
            if norm < 1e-12 or abs(norm - 1.0) > 1e-5:
                raise ValidationError(
                    f"GLB node '{name}' rotation quaternion must be nonzero and normalized"
                )
        if "scale" in node:
            scale = _numeric_vector(node["scale"], 3, f"node '{name}' scale")
            if any(component <= 0.0 for component in scale):
                raise ValidationError(f"GLB node '{name}' scale components must all be positive")
        if "matrix" in node:
            matrix = _numeric_vector(node["matrix"], 16, f"node '{name}' matrix")
            if (
                any(abs(matrix[index]) > 1e-8 for index in (3, 7, 11))
                or abs(matrix[15] - 1.0) > 1e-8
            ):
                raise ValidationError(f"GLB node '{name}' matrix must be affine")


def _reject_boolean_integer_fields(document: dict[str, Any]) -> None:
    """Reject JSON booleans where glTF's closed integer fields are required.

    Python treats ``bool`` as an ``int``; the shared legacy GLB parser retains
    its historical behavior, so the stricter authored-source boundary checks
    these integer fields before calling it.
    """

    def check(value: Any, label: str) -> None:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValidationError(f"GLB {label} must be an integer")

    if "scene" in document:
        check(document["scene"], "scene index")
    for scene_index, scene in enumerate(document.get("scenes", [])):
        if isinstance(scene, dict):
            for index in scene.get("nodes", []):
                check(index, f"scenes[{scene_index}].nodes entry")
    for node_index, node in enumerate(document.get("nodes", [])):
        if not isinstance(node, dict):
            continue
        for field in ("mesh", "skin", "camera"):
            if field in node:
                check(node[field], f"nodes[{node_index}].{field}")
        for child in node.get("children", []):
            check(child, f"nodes[{node_index}].children entry")
    for mesh_index, mesh in enumerate(document.get("meshes", [])):
        if not isinstance(mesh, dict):
            continue
        for primitive_index, primitive in enumerate(mesh.get("primitives", [])):
            if not isinstance(primitive, dict):
                continue
            for field in ("indices", "material"):
                if field in primitive:
                    check(
                        primitive[field],
                        f"meshes[{mesh_index}].primitives[{primitive_index}].{field}",
                    )
            attributes = primitive.get("attributes", {})
            if isinstance(attributes, dict):
                for semantic, accessor_index in attributes.items():
                    check(accessor_index, f"meshes[{mesh_index}].primitives attributes {semantic}")
    for accessor_index, accessor in enumerate(document.get("accessors", [])):
        if not isinstance(accessor, dict):
            continue
        for field in ("bufferView", "byteOffset", "count", "componentType"):
            if field in accessor:
                check(accessor[field], f"accessors[{accessor_index}].{field}")
    for view_index, view in enumerate(document.get("bufferViews", [])):
        if not isinstance(view, dict):
            continue
        for field in ("buffer", "byteOffset", "byteLength", "byteStride"):
            if field in view:
                check(view[field], f"bufferViews[{view_index}].{field}")
    for buffer_index, buffer in enumerate(document.get("buffers", [])):
        if isinstance(buffer, dict) and "byteLength" in buffer:
            check(buffer["byteLength"], f"buffers[{buffer_index}].byteLength")


def _unique_pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    res: dict[str, Any] = {}
    for k, v in pairs:
        if k in res:
            raise ValidationError(f"Duplicate JSON key '{k}' in GLB document")
        res[k] = v
    return res


def _quaternion_matrix(q: Any) -> list[list[float]]:
    """Convert quaternion [x, y, z, w] or 'identity' to 3x3 rotation matrix."""
    if q == "identity" or q == [0, 0, 0, 1] or q == (0, 0, 0, 1):
        return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    if isinstance(q, (list, tuple)) and len(q) == 4:
        x, y, z, w = [float(v) for v in q]
        norm = math.sqrt(x * x + y * y + z * z + w * w)
        if norm < 1e-12:
            return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
        x, y, z, w = x / norm, y / norm, z / norm, w / norm
        return [
            [1.0 - 2.0 * (y * y + z * z), 2.0 * (x * y - z * w), 2.0 * (x * z + y * w)],
            [2.0 * (x * y + z * w), 1.0 - 2.0 * (x * x + z * z), 2.0 * (y * z - x * w)],
            [2.0 * (x * z - y * w), 2.0 * (y * z + x * w), 1.0 - 2.0 * (x * x + y * y)],
        ]
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _mat_mul_3x3(a: list[list[float]], b: list[list[float]]) -> list[list[float]]:
    return [[sum(a[r][k] * b[k][c] for k in range(3)) for c in range(3)] for r in range(3)]


def _get_assembly_tolerances(spec: AssetSpecificationV07) -> tuple[float, float, float, float]:
    """Return (pivot_tolerance_m, basis_tolerance_deg, socket_position_tolerance_m, socket_angle_tolerance_deg)."""
    profile = spec.bound_profile()
    assembly = profile.assembly
    if assembly is None:
        raise ValidationError(f"Bound profile {profile.profile_id!r} has no assembly tolerances")
    return (
        float(assembly.pivot_tolerance_m),
        float(assembly.basis_tolerance_deg),
        float(assembly.socket_position_tolerance_m),
        float(assembly.socket_angle_tolerance_deg),
    )


def _basis_angle_difference_deg(actual: list[list[float]], expected: list[list[float]]) -> float:
    """Compute the angular difference in degrees between two 3x3 rotation matrices."""
    r_rel = [
        [sum(expected[k][r] * actual[k][c] for k in range(3)) for c in range(3)] for r in range(3)
    ]
    trace = r_rel[0][0] + r_rel[1][1] + r_rel[2][2]
    cos_theta = max(-1.0, min(1.0, (trace - 1.0) / 2.0))
    return math.degrees(math.acos(cos_theta))


def _validate_node_scale_and_orthonormality(
    node_name: str,
    matrix_4x4: list[list[float]],
    *,
    require_identity_scale: bool = False,
) -> list[float]:
    """Validate positive uniform scale, column orthogonality, right-handedness, and absence of reflection or shear."""
    columns = [[matrix_4x4[r][c] for r in range(3)] for c in range(3)]
    lengths = [math.sqrt(sum(v * v for v in col)) for col in columns]
    min_len = min(lengths)
    max_len = max(lengths)

    if min_len < 1e-12:
        raise ValidationError(f"Node '{node_name}' scale must be nonzero: {lengths}")

    if max_len - min_len > 1e-5:
        raise ValidationError(f"Node '{node_name}' scale must be uniform: {lengths}")

    scale = lengths[0]
    if require_identity_scale and abs(scale - 1.0) > 1e-5:
        raise ValidationError(f"Node '{node_name}' scale must be identity (1.0), got {scale:.6f}")

    # Normalized column vectors u[c]
    u = [[columns[c][r] / lengths[c] for r in range(3)] for c in range(3)]

    # Orthogonality checks: dot product of normalized columns must be 0
    dot_01 = sum(u[0][i] * u[1][i] for i in range(3))
    dot_02 = sum(u[0][i] * u[2][i] for i in range(3))
    dot_12 = sum(u[1][i] * u[2][i] for i in range(3))
    if max(abs(dot_01), abs(dot_02), abs(dot_12)) > 1e-4:
        raise ValidationError(
            f"Node '{node_name}' transformation matrix has non-orthogonal axes (shear detected): "
            f"dots=({dot_01:.6f}, {dot_02:.6f}, {dot_12:.6f})"
        )

    # 3x3 submatrix determinant
    m = matrix_4x4
    det = (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )

    if det <= 0.0:
        raise ValidationError(
            f"Node '{node_name}' has non-positive determinant ({det:.6f}); negative scale, reflection, or left-handed basis is forbidden"
        )

    det_u = det / (scale * scale * scale)
    if abs(det_u - 1.0) > 1e-4:
        raise ValidationError(
            f"Node '{node_name}' normalized basis determinant is {det_u:.6f} (expected +1.0); improper rotation or reflection detected"
        )

    return lengths


def _resolve_and_check_package_dir(
    package_dir: Path | str,
    managed_root: Path | str | None,
) -> Path:
    """Resolve and validate package directory with pre-resolution and post-resolution checks."""
    if managed_root is not None:
        m_root = Path(managed_root)
        _assert_no_symlink_or_reparse_components(m_root, "Managed root")
        lexical_pkg = (
            m_root / Path(package_dir) if not Path(package_dir).is_absolute() else Path(package_dir)
        )
        _assert_no_symlink_or_reparse_components(lexical_pkg, "Package directory")
        guard = PathGuard(m_root)
        safe_pkg_dir = guard.resolve_safe_path(package_dir)
        _assert_no_symlink_or_reparse_components(safe_pkg_dir, "Resolved package directory")
    else:
        raw_str = str(package_dir).strip()
        lexical_pkg = Path(raw_str)
        _assert_no_symlink_or_reparse_components(lexical_pkg, "Package directory")
        safe_pkg_dir = lexical_pkg.resolve()
        _assert_no_symlink_or_reparse_components(safe_pkg_dir, "Resolved package directory")

    if not safe_pkg_dir.is_dir() or _is_symlink_or_reparse(safe_pkg_dir):
        raise ValidationError(
            f"Package directory does not exist or is a symlink: {safe_pkg_dir}",
            details={"package_dir": str(safe_pkg_dir)},
        )
    return safe_pkg_dir


def _check_publication_marker(
    safe_pkg_dir: Path,
    expected_spec_fp: str,
    glb_sha256: str,
    glb_byte_size: int,
    prov_sha256: str,
    prov_byte_size: int,
) -> AssemblyPublicationMarker:
    """Verify that the package directory contains a valid, matching publication-completion marker."""
    marker_path = safe_pkg_dir / PUBLICATION_MARKER_FILENAME
    _assert_no_symlink_or_reparse_components(marker_path, "Publication completion marker")
    if not marker_path.is_file() or _is_symlink_or_reparse(marker_path):
        raise ValidationError(
            f"Package at '{safe_pkg_dir}' is incomplete: publication-completion marker '{PUBLICATION_MARKER_FILENAME}' is missing"
        )

    marker_bytes = _read_bounded(marker_path, MAX_PROVENANCE_SIDECAR_BYTES, "Publication marker")
    try:
        marker_dict = json.loads(marker_bytes.decode("utf-8"))
        marker = AssemblyPublicationMarker.model_validate(marker_dict)
    except Exception as exc:
        raise ValidationError(
            f"Publication-completion marker at '{marker_path}' is malformed or invalid: {exc}"
        ) from exc

    if marker.spec_fingerprint != expected_spec_fp:
        raise ValidationError(
            f"Publication marker spec_fingerprint '{marker.spec_fingerprint}' does not match expected '{expected_spec_fp}'"
        )
    if marker.source_artifact_sha256.lower() != glb_sha256.lower():
        raise ValidationError(
            f"Publication marker source_artifact_sha256 '{marker.source_artifact_sha256}' does not match GLB digest '{glb_sha256}'"
        )
    if marker.source_artifact_byte_size != glb_byte_size:
        raise ValidationError(
            f"Publication marker source_artifact_byte_size {marker.source_artifact_byte_size} does not match GLB size {glb_byte_size}"
        )
    if marker.provenance_sha256.lower() != prov_sha256.lower():
        raise ValidationError(
            f"Publication marker provenance_sha256 '{marker.provenance_sha256}' does not match provenance digest '{prov_sha256}'"
        )
    if marker.provenance_byte_size != prov_byte_size:
        raise ValidationError(
            f"Publication marker provenance_byte_size {marker.provenance_byte_size} does not match provenance size {prov_byte_size}"
        )

    return marker


def preflight_assembly_glb(
    glb_bytes: bytes,
    spec: AssetSpecificationV07,
    *,
    source_front: Literal["-Z", "+Z"] | None = None,
) -> tuple[dict[str, Any], bytes]:
    """Strictly preflight a self-contained assembly GLB byte snapshot against AssetSpecificationV07.

    Verifies:
    1. Binary GLB structure (glTF 2.0, chunks, bounds)
    2. Deep geometry, accessor, image, material, extension, skin, animation, and depth safety via glb_validator._inspect
    3. Nonempty processable static triangle geometry
    4. Assembly GLB contract: exactly one active ROOT (identity), root PART direct child,
       declared LOD0 direct identity mesh children and socket leaves only (processed generated
       LOD1 and root box remain later processor scope).
    5. Rejection of extra/unreachable PART/SOCKET nodes, arbitrary wrappers, and scene aliases.
    6. Respect pending declared source-front normalization without falsely comparing raw source +Z top transform to canonical before rotation.
    """
    total = len(glb_bytes)
    if total < 20:
        raise ValidationError(f"GLB byte stream too small ({total} bytes, minimum 20 bytes)")
    if total > MAX_ASSEMBLY_SOURCE_BYTES:
        raise ValidationError(
            f"GLB byte stream exceeds limit of {MAX_ASSEMBLY_SOURCE_BYTES} bytes ({total} bytes)"
        )

    magic, version, declared_total = struct.unpack_from("<4sII", glb_bytes, 0)
    if magic != GLB_MAGIC:
        raise ValidationError(f"Invalid GLB magic header: {magic!r}, expected {GLB_MAGIC!r}")
    if version != 2:
        raise ValidationError(f"Unsupported glTF version {version}, expected version 2")
    if declared_total != total:
        raise ValidationError(
            f"GLB declared length {declared_total} does not match actual length {total}"
        )

    offset = 12
    json_chunk: bytes | None = None
    bin_chunk: bytes | None = None

    while offset < total:
        if offset + 8 > total:
            raise ValidationError("Truncated GLB chunk header")
        chunk_length, chunk_type = struct.unpack_from("<II", glb_bytes, offset)
        offset += 8
        end = offset + chunk_length
        if chunk_length % 4 != 0 or end > total:
            raise ValidationError("Invalid or out-of-bounds GLB chunk length")

        chunk_data = glb_bytes[offset:end]
        if chunk_type == CHUNK_TYPE_JSON:
            if json_chunk is not None or offset != 20:
                raise ValidationError("JSON chunk must be the first and unique chunk in GLB")
            json_chunk = chunk_data
        elif chunk_type == CHUNK_TYPE_BIN:
            if bin_chunk is not None:
                raise ValidationError("Multiple BIN chunks are forbidden in GLB")
            bin_chunk = chunk_data
        else:
            raise ValidationError(f"Unsupported GLB chunk type 0x{chunk_type:08X}")
        offset = end

    if json_chunk is None or bin_chunk is None:
        raise ValidationError("GLB must contain both JSON and BIN chunks")

    try:
        decoded_json = json_chunk.decode("utf-8").rstrip(" \t\r\n\x00")
        document = json.loads(
            decoded_json,
            object_pairs_hook=_unique_pairs_hook,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Invalid numeric token {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(f"Invalid GLB JSON chunk: {exc}") from exc

    if not isinstance(document, dict):
        raise ValidationError("GLB JSON chunk must be a JSON object")

    _validate_source_json_shapes(document)
    _reject_boolean_integer_fields(document)
    _validate_source_node_transforms(document)

    asset_info = document.get("asset")
    if not isinstance(asset_info, dict) or asset_info.get("version") != "2.0":
        raise ValidationError("GLB asset.version must be '2.0'")

    # Reuse existing glb_validator._inspect on the decoded document and BIN chunk
    try:
        infos, points, material_ids, textures, total_triangles, world = _inspect(
            document, bin_chunk
        )
    except _InvalidGLB as exc:
        raise ValidationError(f"GLB geometry/structure validation failed: {exc}") from exc
    except Exception as exc:
        raise ValidationError(f"GLB validation error: {exc}") from exc

    # Require nonempty processable actual geometry
    if not points or total_triangles <= 0 or not infos:
        raise ValidationError("Assembly GLB contains no nonempty processable geometry")

    # Node arrays and scenes
    raw_nodes = document.get("nodes", [])
    if not isinstance(raw_nodes, list):
        raise ValidationError("GLB nodes must be a list")

    scenes = document.get("scenes", [])
    scene_idx = document.get("scene", 0)
    if not scenes or not isinstance(scene_idx, int) or not (0 <= scene_idx < len(scenes)):
        raise ValidationError("Active scene is missing or invalid in GLB")

    active_scene = scenes[scene_idx]
    if not isinstance(active_scene, dict):
        raise ValidationError("Active scene entry must be a dictionary")

    scene_nodes = active_scene.get("nodes", [])
    if not isinstance(scene_nodes, list) or len(scene_nodes) != 1:
        raise ValidationError(
            f"Active scene must contain exactly 1 root node named 'ROOT', found {len(scene_nodes) if isinstance(scene_nodes, list) else type(scene_nodes).__name__}"
        )

    root_node_idx = scene_nodes[0]
    if not isinstance(root_node_idx, int) or not (0 <= root_node_idx < len(raw_nodes)):
        raise ValidationError(f"Active scene root node index {root_node_idx} is out of bounds")

    root_node = raw_nodes[root_node_idx]
    if not isinstance(root_node, dict):
        raise ValidationError("ROOT node must be a dictionary")
    if root_node.get("name") != "ROOT":
        raise ValidationError(
            f"Active scene root node must be named 'ROOT', found {root_node.get('name')!r}"
        )
    if "mesh" in root_node:
        raise ValidationError("ROOT node cannot contain a mesh")

    # ROOT node transform must be identity
    root_mat = _node_matrix(root_node)
    for r in range(4):
        for c in range(4):
            expected_val = 1.0 if r == c else 0.0
            if abs(root_mat[r][c] - expected_val) > 1e-5:
                raise ValidationError("ROOT node transform must be identity")

    # Collect unique node names and parent map
    node_by_name: dict[str, tuple[int, dict[str, Any]]] = {}
    parent_by_index: dict[int, int] = {}

    for idx, node in enumerate(raw_nodes):
        if not isinstance(node, dict):
            raise ValidationError(f"GLB node at index {idx} must be a dictionary")
        name = node.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValidationError(f"GLB node at index {idx} must have a non-empty name")
        if name in node_by_name:
            raise ValidationError(f"Duplicate node name '{name}' in GLB")
        node_by_name[name] = (idx, node)

        children = node.get("children")
        if children is not None:
            if not isinstance(children, list):
                raise ValidationError(f"Node '{name}' children must be a list")
            for child_idx in children:
                if not isinstance(child_idx, int) or child_idx < 0 or child_idx >= len(raw_nodes):
                    raise ValidationError(f"Invalid child index {child_idx} in node {idx}")
                if child_idx in parent_by_index:
                    raise ValidationError(
                        f"Node index {child_idx} has multiple parents ({parent_by_index[child_idx]} and {idx})"
                    )
                parent_by_index[child_idx] = idx

    # Check reachability from ROOT: reject unreachable nodes and scene aliases
    reachable: set[int] = set()

    def _mark_reachable(curr_idx: int) -> None:
        if curr_idx in reachable:
            return
        reachable.add(curr_idx)
        for c_idx in raw_nodes[curr_idx].get("children", []):
            _mark_reachable(c_idx)

    _mark_reachable(root_node_idx)
    if len(reachable) != len(raw_nodes):
        unreachable_names = [
            raw_nodes[i].get("name", i) for i in range(len(raw_nodes)) if i not in reachable
        ]
        raise ValidationError(
            f"GLB contains {len(unreachable_names)} unreachable/unattached nodes: {unreachable_names}; all nodes must be under ROOT"
        )

    # Retrieve profile-bound assembly tolerances (or fallbacks)
    pivot_tol_m, basis_tol_deg, sock_pos_tol_m, sock_angle_tol_deg = _get_assembly_tolerances(spec)

    # Spec parts and root PART check
    spec_parts = {p.part_id: p for p in (spec.parts or [])}
    root_parts = [p for p in (spec.parts or []) if p.parent == "root"]
    if len(root_parts) != 1:
        raise ValidationError(
            f"Specification must declare exactly one root part, found {len(root_parts)}"
        )
    root_part_spec = root_parts[0]
    root_part_name = f"PART_{root_part_spec.part_id}"

    # ROOT direct child must be the root PART node (no arbitrary wrapper)
    root_children = root_node.get("children", [])
    if not isinstance(root_children, list) or len(root_children) != 1:
        raise ValidationError(
            f"ROOT node must have exactly 1 direct child (the root PART '{root_part_name}'), found {len(root_children)}"
        )
    root_child_idx = root_children[0]
    if raw_nodes[root_child_idx].get("name") != root_part_name:
        raise ValidationError(
            f"ROOT node direct child must be '{root_part_name}', found {raw_nodes[root_child_idx].get('name')!r}"
        )

    # Verify all PART nodes
    actual_part_nodes = {
        name: entry for name, entry in node_by_name.items() if name.startswith("PART_")
    }
    for part_id, part in spec_parts.items():
        part_name = f"PART_{part_id}"
        if part_name not in actual_part_nodes:
            raise ValidationError(f"Required part node '{part_name}' is missing from GLB")
        part_idx, part_node = actual_part_nodes[part_name]

        # Parent topology check
        if part.parent == "root":
            if parent_by_index.get(part_idx) != root_node_idx:
                raise ValidationError(
                    f"Root part '{part_name}' parent must be ROOT (index {root_node_idx}), got {parent_by_index.get(part_idx)}"
                )
        else:
            expected_parent_name = f"PART_{part.parent}"
            if expected_parent_name not in actual_part_nodes:
                raise ValidationError(
                    f"Declared parent '{expected_parent_name}' for part '{part_name}' is missing from GLB"
                )
            expected_parent_idx, _ = actual_part_nodes[expected_parent_name]
            if parent_by_index.get(part_idx) != expected_parent_idx:
                raise ValidationError(
                    f"Part '{part_name}' parent in GLB (index {parent_by_index.get(part_idx)}) does not match "
                    f"declared parent '{expected_parent_name}' (index {expected_parent_idx})"
                )

        # Uniform positive scale, orthogonality, and shear check
        part_local = _node_matrix(part_node)
        lengths = _validate_node_scale_and_orthonormality(part_name, part_local)

        # Structural contract: Part node motion extras
        extras = part_node.get("extras")
        if not isinstance(extras, dict):
            raise ValidationError(
                f"Part node '{part_name}' must declare motion extras ('gf_motion'), found {type(extras).__name__}"
            )
        gf_motion = extras.get("gf_motion")
        if gf_motion != part.pivot.motion.kind:
            raise ValidationError(
                f"Part node '{part_name}' motion extras 'gf_motion' ('{gf_motion}') does not match declared motion kind '{part.pivot.motion.kind}'"
            )
        if part.pivot.motion.axis is not None:
            gf_axis = extras.get("gf_axis")
            axis = _numeric_vector(gf_axis, 3, f"part node '{part_name}' extras gf_axis")
            axis_norm = math.sqrt(sum(component * component for component in axis))
            if abs(axis_norm - 1.0) > 1e-5:
                raise ValidationError(f"Part node '{part_name}' extras gf_axis must be normalized")
            if not all(
                math.isclose(a, float(b), abs_tol=1e-5)
                for a, b in zip(axis, part.pivot.motion.axis, strict=True)
            ):
                raise ValidationError(
                    f"Part node '{part_name}' motion extras 'gf_axis' {axis} does not match declared axis {part.pivot.motion.axis}"
                )
        elif "gf_axis" in extras:
            raise ValidationError(
                f"Part node '{part_name}' fixed motion must not declare unexpected 'gf_axis' metadata"
            )

        # Part transform verification
        # For non-root parts, compare against declared canonical pivot
        if part.parent != "root":
            translation = [part_local[r][3] for r in range(3)]
            declared_pos = [float(v) for v in part.pivot.position_m]
            dist = math.sqrt(
                sum((t - d) ** 2 for t, d in zip(translation, declared_pos, strict=True))
            )
            if dist > pivot_tol_m + 1e-6:
                raise ValidationError(
                    f"Part '{part_name}' translation {translation} does not match declared pivot position {declared_pos}: "
                    f"difference {dist:.4f}m exceeds tolerance {pivot_tol_m}m"
                )
            # Basis rotation check
            expected_basis = _quaternion_matrix(part.pivot.basis)
            actual_basis = [[part_local[r][c] / lengths[c] for c in range(3)] for r in range(3)]
            angle_deg = _basis_angle_difference_deg(actual_basis, expected_basis)
            if angle_deg > basis_tol_deg + 1e-5:
                raise ValidationError(
                    f"Part '{part_name}' rotation does not match declared pivot basis: "
                    f"difference {angle_deg:.3f}° exceeds tolerance {basis_tol_deg}°"
                )
        else:
            # Top root PART: Pending declared source-front normalization
            # Normative canonical = Ry180 * raw => raw = Ry180 * canonical.
            # In parent frame (ROOT node frame):
            # raw_basis = Ry180 * canonical_basis
            # raw_translation = Ry180 * canonical_translation
            effective_front = source_front or "-Z"
            declared_pos = [float(v) for v in part.pivot.position_m]
            expected_canonical_basis = _quaternion_matrix(part.pivot.basis)
            if effective_front == "+Z":
                expected_top_basis = _mat_mul_3x3(_ROT_Y_180, expected_canonical_basis)
                expected_top_pos = [-declared_pos[0], declared_pos[1], -declared_pos[2]]
            else:
                expected_top_basis = expected_canonical_basis
                expected_top_pos = declared_pos

            # Check root translation
            translation = [part_local[r][3] for r in range(3)]
            dist = math.sqrt(
                sum((t - d) ** 2 for t, d in zip(translation, expected_top_pos, strict=True))
            )
            if dist > pivot_tol_m + 1e-6:
                raise ValidationError(
                    f"Root part '{part_name}' translation {translation} does not match declared pivot position {declared_pos} "
                    f"under source_front '{effective_front}' (expected {expected_top_pos}): difference {dist:.4f}m exceeds tolerance {pivot_tol_m}m"
                )

            # Check root basis rotation
            actual_top_basis = [[part_local[r][c] / lengths[c] for c in range(3)] for r in range(3)]
            angle_deg = _basis_angle_difference_deg(actual_top_basis, expected_top_basis)
            if angle_deg > basis_tol_deg + 1e-5:
                raise ValidationError(
                    f"Root part '{part_name}' rotation does not match declared source_front '{effective_front}' orientation: "
                    f"difference {angle_deg:.3f}° exceeds tolerance {basis_tol_deg}°"
                )

    # Reject unexpected PART nodes
    expected_part_names = {f"PART_{p}" for p in spec_parts}
    for actual_p_name in actual_part_nodes:
        if actual_p_name not in expected_part_names:
            raise ValidationError(f"Unexpected undeclared PART node '{actual_p_name}' in GLB")

    # Verify all SOCKET nodes
    spec_sockets = {s.socket_id: s for s in (spec.sockets or [])}
    actual_socket_nodes = {
        name: entry for name, entry in node_by_name.items() if name.startswith("SOCKET_")
    }
    for socket_id, socket in spec_sockets.items():
        sock_name = f"SOCKET_{socket_id}"
        if sock_name not in actual_socket_nodes:
            raise ValidationError(f"Required socket node '{sock_name}' is missing from GLB")
        sock_idx, sock_node = actual_socket_nodes[sock_name]

        # Sockets must be leaf nodes (no children)
        if sock_node.get("children"):
            raise ValidationError(f"Socket node '{sock_name}' must be a leaf (no children)")
        if "mesh" in sock_node:
            raise ValidationError(f"Socket node '{sock_name}' cannot contain a mesh")

        expected_parent_name = f"PART_{socket.parent_part}"
        if expected_parent_name not in actual_part_nodes:
            raise ValidationError(
                f"Parent part '{expected_parent_name}' for socket '{sock_name}' is missing from GLB"
            )
        expected_parent_idx, _ = actual_part_nodes[expected_parent_name]
        if parent_by_index.get(sock_idx) != expected_parent_idx:
            raise ValidationError(
                f"Socket '{sock_name}' parent in GLB (index {parent_by_index.get(sock_idx)}) does not match "
                f"declared parent '{expected_parent_name}' (index {expected_parent_idx})"
            )

        # Identity scale, orthogonality, and shear check on socket node
        sock_local = _node_matrix(sock_node)
        _validate_node_scale_and_orthonormality(sock_name, sock_local, require_identity_scale=True)

        # Socket declared local position check
        sock_translation = [sock_local[r][3] for r in range(3)]
        declared_sock_pos = [float(v) for v in socket.translation_m]
        sock_dist = math.sqrt(
            sum((t - d) ** 2 for t, d in zip(sock_translation, declared_sock_pos, strict=True))
        )
        if sock_dist > sock_pos_tol_m + 1e-6:
            raise ValidationError(
                f"Socket '{sock_name}' translation {sock_translation} does not match declared translation {declared_sock_pos}: "
                f"difference {sock_dist:.4f}m exceeds tolerance {sock_pos_tol_m}m"
            )

        # Socket declared basis/rotation check
        expected_sock_basis = _quaternion_matrix(socket.rotation)
        actual_sock_basis = [[sock_local[r][c] for c in range(3)] for r in range(3)]
        sock_angle_deg = _basis_angle_difference_deg(actual_sock_basis, expected_sock_basis)
        if sock_angle_deg > sock_angle_tol_deg + 1e-5:
            raise ValidationError(
                f"Socket '{sock_name}' rotation does not match declared rotation: "
                f"difference {sock_angle_deg:.3f}° exceeds tolerance {sock_angle_tol_deg}°"
            )

    # Reject unexpected SOCKET nodes
    expected_socket_names = {f"SOCKET_{s}" for s in spec_sockets}
    for actual_s_name in actual_socket_nodes:
        if actual_s_name not in expected_socket_names:
            raise ValidationError(f"Unexpected undeclared SOCKET node '{actual_s_name}' in GLB")

    # Direct identity LOD0 mesh child check for each PART node:
    # Source GLB can contain only declared LOD0 direct identity mesh children and socket leaves;
    # processed generated LOD1/root box remain later processor scope.
    for part_id in spec_parts:
        part_name = f"PART_{part_id}"
        part_idx, part_node = actual_part_nodes[part_name]
        lod0_mesh_name = f"SM_{spec.asset_id}_{part_id}_LOD0"
        part_children = part_node.get("children", [])
        mesh_children = [ci for ci in part_children if "mesh" in raw_nodes[ci]]
        if len(mesh_children) != 1:
            raise ValidationError(
                f"Part '{part_name}' must have exactly 1 direct LOD0 mesh child, found {len(mesh_children)}"
            )
        mesh_idx = mesh_children[0]
        mesh_node = raw_nodes[mesh_idx]
        if mesh_node.get("name") != lod0_mesh_name:
            raise ValidationError(
                f"Mesh child of '{part_name}' must be named '{lod0_mesh_name}', found {mesh_node.get('name')!r}"
            )
        if mesh_node.get("children"):
            raise ValidationError(f"Mesh node '{lod0_mesh_name}' cannot have children")

        mesh_local = _node_matrix(mesh_node)
        for r in range(4):
            for c in range(4):
                exp_v = 1.0 if r == c else 0.0
                if abs(mesh_local[r][c] - exp_v) > 1e-5:
                    raise ValidationError(
                        f"Mesh child node '{lod0_mesh_name}' transform must be identity"
                    )

        # All other children of this PART node must be declared child PART_ or SOCKET_ nodes
        for ci in part_children:
            if ci == mesh_idx:
                continue
            child_name = str(raw_nodes[ci].get("name", ""))
            if not (child_name.startswith("PART_") or child_name.startswith("SOCKET_")):
                raise ValidationError(
                    f"Unexpected child node '{child_name}' under '{part_name}'; source GLB can contain only declared LOD0 direct identity mesh children and socket leaves"
                )

    # Ensure every node in the entire GLB is either ROOT, declared PART_, declared SOCKET_, or declared LOD0 mesh
    allowed_mesh_names = {f"SM_{spec.asset_id}_{p}_LOD0" for p in spec_parts}
    allowed_node_names = {"ROOT"} | expected_part_names | expected_socket_names | allowed_mesh_names
    for name in node_by_name:
        if name not in allowed_node_names:
            raise ValidationError(
                f"Undeclared or unauthorized node '{name}' in GLB; arbitrary wrappers and processor-scope nodes are forbidden in source GLB"
            )

    return document, bin_chunk


def ingest_assembly_source(
    *,
    source_glb_path: Path | str,
    managed_root: Path | str,
    relative_package_dir: Path | str,
    spec: AssetSpecificationV07,
    authoring_tool_name: str,
    authoring_tool_version: str,
    source_front: Literal["-Z", "+Z"],
    actor: str,
    reason: str,
    derived_from: list[DerivedFromEntry] | None = None,
    expected_source_sha256: str | None = None,
    expected_source_byte_size: int | None = None,
) -> AssemblyIngestResult:
    """Ingest an authored assembly source GLB into a managed package directory under PathGuard.

    Guarantees:
    - Retains one bounded byte snapshot (<= 50 MiB)
    - Preflights the RETAINED snapshot bytes, preventing race mutations on original source
    - PathGuard containment checks against escapes, symlinks, and device names
    - Pre-resolution lexical component rejection of symlinks, junctions, and reparse points
    - Exclusive ownership: atomic mkdir claim prevents POSIX os.rename overwrite of concurrent/empty directory
    - Complete-package success with deterministic cleanup of only owned staging/claim
    - Returns deeply immutable AssemblyIngestResult with canonical provenance sidecar
    """
    if not isinstance(spec, AssetSpecificationV07):
        raise ValidationError(f"spec must be an AssetSpecificationV07, got {type(spec).__name__}")
    if spec.source_kind != "local_operator_assembly":
        raise ValidationError(
            f"spec source_kind must be 'local_operator_assembly', got '{spec.source_kind}'"
        )

    # 1. Lexical checks before resolution
    m_root = Path(managed_root)
    _assert_no_symlink_or_reparse_components(m_root, "Managed root")

    lexical_pkg_path = m_root / Path(relative_package_dir)
    _assert_no_symlink_or_reparse_components(lexical_pkg_path, "Package directory")

    # Resolve and validate target package directory with PathGuard
    guard = PathGuard(m_root)
    safe_package_dir = guard.resolve_safe_path(relative_package_dir)
    _assert_no_symlink_or_reparse_components(safe_package_dir, "Resolved package directory")

    if safe_package_dir.exists() or safe_package_dir.is_symlink():
        raise ValidationError(
            f"Package target directory already exists or is a symlink: {safe_package_dir}",
            details={"package_dir": str(safe_package_dir)},
        )

    # 2. Read single bounded snapshot from source with pre-resolution checks
    src_path = Path(source_glb_path)
    _assert_no_symlink_or_reparse_components(src_path, "Source GLB")
    source_bytes = _read_bounded(src_path, MAX_ASSEMBLY_SOURCE_BYTES, "Assembly source GLB")
    source_size = len(source_bytes)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()

    if expected_source_sha256 is not None:
        if source_sha256.lower() != expected_source_sha256.strip().lower():
            raise ValidationError(
                f"Source SHA-256 mismatch: expected {expected_source_sha256}, got {source_sha256}",
                details={"expected": expected_source_sha256, "actual": source_sha256},
            )

    if expected_source_byte_size is not None:
        if source_size != expected_source_byte_size:
            raise ValidationError(
                f"Source byte size mismatch: expected {expected_source_byte_size}, got {source_size}",
                details={"expected": expected_source_byte_size, "actual": source_size},
            )

    # 3. Preflight the RETAINED snapshot bytes
    preflight_assembly_glb(source_bytes, spec, source_front=source_front)

    # 4. Construct typed provenance
    provenance = create_assembly_provenance(
        source_artifact_sha256=source_sha256,
        source_artifact_byte_size=source_size,
        authoring_tool_name=authoring_tool_name,
        authoring_tool_version=authoring_tool_version,
        source_front=source_front,
        spec=spec,
        actor=actor,
        reason=reason,
        derived_from=derived_from,
    )
    prov_bytes = provenance.to_canonical_bytes()
    if len(prov_bytes) > MAX_PROVENANCE_SIDECAR_BYTES:
        raise ValidationError(
            "Canonical provenance sidecar exceeds the bounded package size",
            details={"actual": len(prov_bytes), "maximum": MAX_PROVENANCE_SIDECAR_BYTES},
        )
    prov_sha256 = provenance.provenance_hash()

    # 5. Staged publication with exclusive claim, completion marker, and own-only cleanup
    guard.ensure_safe_parent(safe_package_dir)
    parent_dir = safe_package_dir.parent
    _assert_no_symlink_or_reparse_components(parent_dir, "Package parent directory")

    staging_dir = parent_dir / f".tmp_assembly_{safe_package_dir.name}_{uuid.uuid4().hex}"
    staging_dir.mkdir(parents=False, exist_ok=False)
    _assert_no_symlink_or_reparse_components(staging_dir, "Staging directory")

    claimed_package_dir = False
    owned_in_staging: list[_OwnedFile] = []
    owned_in_final: list[_OwnedFile] = []
    try:
        staged_glb = staging_dir / "source.glb"
        staged_prov = staging_dir / "source_provenance.json"
        staged_marker = staging_dir / PUBLICATION_MARKER_FILENAME

        # Write retained snapshot bytes and provenance
        owned_staging_glb = _write_owned_file(staged_glb, source_bytes)
        owned_in_staging.append(owned_staging_glb)
        owned_staging_prov = _write_owned_file(staged_prov, prov_bytes)
        owned_in_staging.append(owned_staging_prov)

        # Construct and write publication-completion marker in staging
        marker = AssemblyPublicationMarker(
            schema_version="0.7.0",
            marker_type="assembly_publication_completion",
            source_artifact_sha256=source_sha256,
            source_artifact_byte_size=source_size,
            provenance_sha256=prov_sha256,
            provenance_byte_size=len(prov_bytes),
            spec_fingerprint=provenance.spec_fingerprint,
            created_at=utc_now_iso(),
        )
        marker_bytes = marker.to_canonical_bytes()
        owned_staging_marker = _write_owned_file(staged_marker, marker_bytes)
        owned_in_staging.append(owned_staging_marker)

        # Integrity checks on staged files
        written_glb = staged_glb.read_bytes()
        written_prov = staged_prov.read_bytes()
        written_marker = staged_marker.read_bytes()
        if (
            len(written_glb) != source_size
            or hashlib.sha256(written_glb).hexdigest() != source_sha256
        ):
            raise ValidationError("Integrity check failed on staged GLB bytes")
        if (
            len(written_prov) != len(prov_bytes)
            or hashlib.sha256(written_prov).hexdigest() != prov_sha256
        ):
            raise ValidationError("Integrity check failed on staged provenance bytes")
        if (
            len(written_marker) != len(marker.to_canonical_bytes())
            or hashlib.sha256(written_marker).hexdigest()
            != hashlib.sha256(marker.to_canonical_bytes()).hexdigest()
        ):
            raise ValidationError("Integrity check failed on staged publication marker bytes")

        # Exclusive atomic mkdir claim:
        # mkdir(exist_ok=False) is atomic and fails on POSIX and Windows if destination exists
        # (even if empty!), preventing POSIX os.rename from overwriting a concurrent/empty directory.
        try:
            safe_package_dir.mkdir(parents=False, exist_ok=False)
            claimed_package_dir = True
        except (FileExistsError, OSError) as exc:
            raise ValidationError(
                f"Package target directory already exists or was claimed concurrently: {safe_package_dir}",
                details={"package_dir": str(safe_package_dir)},
            ) from exc

        # Atomically hard-link each staged file into the package with no-replace
        # semantics. The staging and destination directories share their parent,
        # so hard links remain on one filesystem; unsupported filesystems fail closed.
        target_glb = safe_package_dir / "source.glb"
        target_prov = safe_package_dir / "source_provenance.json"
        owned_in_final.append(_publish_noreplace(owned_staging_glb, target_glb, "source GLB"))

        owned_in_final.append(
            _publish_noreplace(owned_staging_prov, target_prov, "provenance sidecar")
        )

        # Remove the package-data staging links first. Then atomically create the
        # completion marker with no-replace semantics as the final package mutation.
        target_marker = safe_package_dir / PUBLICATION_MARKER_FILENAME
        _remove_owned_staging_files(staging_dir, (owned_staging_glb, owned_staging_prov))
        owned_in_final.append(
            _publish_noreplace(owned_staging_marker, target_marker, "completion marker")
        )
        # Staging-only cleanup after publication is best-effort; it must never
        # turn an already published package into a reported failed publication.
        _remove_owned_staging_files(staging_dir, (owned_staging_marker,))

    except Exception:
        # Deterministic cleanup of only owned staging/claim:
        # 1. Clean up staging directory if it still exists
        _remove_owned_staging_files(staging_dir, tuple(owned_in_staging))
        # 2. If safe_package_dir was claimed during this execution:
        # Remove ONLY exact owned files with ownership evidence, then rmdir if empty.
        # NEVER recursively delete safe_package_dir or delete foreign-added files!
        if claimed_package_dir and safe_package_dir.exists():
            for owned in owned_in_final:
                try:
                    if _is_same_owned_file(owned):
                        owned.path.unlink()
                except OSError:
                    pass
            try:
                safe_package_dir.rmdir()
            except OSError:
                # If foreign files were injected or rmdir fails, directory and foreign files are preserved!
                pass
        raise

    retained_glb_path = safe_package_dir / "source.glb"
    retained_prov_path = safe_package_dir / "source_provenance.json"

    return AssemblyIngestResult(
        retained_glb_path=retained_glb_path,
        retained_provenance_path=retained_prov_path,
        retained_glb_sha256=source_sha256,
        retained_glb_byte_size=source_size,
        retained_provenance_sha256=prov_sha256,
        retained_provenance_bytes=prov_bytes,
        spec_fingerprint=provenance.spec_fingerprint,
        package_dir=safe_package_dir,
    )


def verify_retained_assembly(
    package_dir: Path | str,
    spec: AssetSpecificationV07,
    *,
    expected_provenance_sha256: str,
    managed_root: Path | str | None = None,
) -> AssemblyIngestResult:
    """Authenticated standalone verification of an ingested assembly package against AssetSpecificationV07.

    Requires mandatory pinned expected_provenance_sha256 digest to prevent accepting tampered metadata
    or coordinated GLB+sidecar hash rewrites. Compares the pinned digest BEFORE trusting sidecar metadata.
    Enforces publication-completion marker validation to reject unfinalized or incomplete packages.
    """
    if not isinstance(spec, AssetSpecificationV07):
        raise ValidationError(f"spec must be an AssetSpecificationV07, got {type(spec).__name__}")

    # Mandatory strict SHA-256 pinned provenance digest check
    if not isinstance(expected_provenance_sha256, str) or not _HEX_64_REGEX.fullmatch(
        expected_provenance_sha256.strip()
    ):
        raise ValidationError(
            f"expected_provenance_sha256 must be a valid 64-character hex SHA-256 digest: {expected_provenance_sha256!r}"
        )
    pinned_digest = expected_provenance_sha256.strip().lower()

    # Pre-resolution and post-resolution lexical checks
    safe_pkg_dir = _resolve_and_check_package_dir(package_dir, managed_root)

    glb_path = safe_pkg_dir / "source.glb"
    prov_path = safe_pkg_dir / "source_provenance.json"

    _assert_no_symlink_or_reparse_components(glb_path, "Retained source GLB")
    _assert_no_symlink_or_reparse_components(prov_path, "Retained provenance sidecar")

    # Read and hash provenance sidecar bytes
    prov_bytes = _read_bounded(prov_path, MAX_PROVENANCE_SIDECAR_BYTES, "Provenance sidecar")
    prov_sha256 = hashlib.sha256(prov_bytes).hexdigest().lower()

    # Compare digest BEFORE trusting metadata
    if prov_sha256 != pinned_digest:
        raise ValidationError(
            f"Authenticated provenance verification failed: sidecar digest '{prov_sha256}' does not match pinned '{pinned_digest}'",
            details={"expected": pinned_digest, "actual": prov_sha256},
        )

    try:
        prov_dict = json.loads(
            prov_bytes.decode("utf-8"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(f"Invalid numeric constant {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValidationError(f"Corrupt provenance JSON at {prov_path}: {exc}") from exc

    if not isinstance(prov_dict, dict):
        raise ValidationError(f"Provenance sidecar must contain a JSON object: {prov_path}")

    # Strict model validation rejecting extra, missing, or invalid fields
    try:
        provenance = AssemblySourceProvenance.model_validate(prov_dict)
    except Exception as exc:
        raise ValidationError(f"Provenance schema validation failed: {exc}") from exc

    # Assert provenance strictly matches spec
    assert_provenance_matches_spec(provenance, spec)

    # Read and verify retained source GLB
    glb_bytes = _read_bounded(glb_path, MAX_ASSEMBLY_SOURCE_BYTES, "Retained assembly GLB")
    glb_size = len(glb_bytes)
    glb_sha256 = hashlib.sha256(glb_bytes).hexdigest().lower()

    if glb_size != provenance.source_artifact_byte_size:
        raise ValidationError(
            f"Retained GLB byte size {glb_size} does not match provenance byte size {provenance.source_artifact_byte_size}"
        )
    if glb_sha256 != provenance.source_artifact_sha256.lower():
        raise ValidationError(
            f"Retained GLB SHA-256 {glb_sha256} does not match provenance SHA-256 {provenance.source_artifact_sha256}"
        )

    # Verify publication-completion marker
    _check_publication_marker(
        safe_pkg_dir=safe_pkg_dir,
        expected_spec_fp=provenance.spec_fingerprint,
        glb_sha256=glb_sha256,
        glb_byte_size=glb_size,
        prov_sha256=prov_sha256,
        prov_byte_size=len(prov_bytes),
    )

    # Re-run GLB preflight on the retained bytes
    preflight_assembly_glb(glb_bytes, spec, source_front=provenance.source_front)

    return AssemblyIngestResult(
        retained_glb_path=glb_path,
        retained_provenance_path=prov_path,
        retained_glb_sha256=glb_sha256,
        retained_glb_byte_size=glb_size,
        retained_provenance_sha256=prov_sha256,
        retained_provenance_bytes=prov_bytes,
        spec_fingerprint=provenance.spec_fingerprint,
        package_dir=safe_pkg_dir,
    )


def inspect_untrusted_assembly_package(
    package_dir: Path | str,
    spec: AssetSpecificationV07,
    *,
    managed_root: Path | str | None = None,
) -> AssemblyIngestResult:
    """Inspect an unauthenticated assembly source package without claiming verification.

    WARNING: This does NOT verify the package against a trusted pinned provenance digest.
    Never use this for authenticated acceptance or cold verification gates.
    Applies the same pre-resolution lexical path checks and publication-completion marker
    verification as authenticated verification.
    """
    if not isinstance(spec, AssetSpecificationV07):
        raise ValidationError(f"spec must be an AssetSpecificationV07, got {type(spec).__name__}")

    # Pre-resolution and post-resolution lexical checks
    safe_pkg_dir = _resolve_and_check_package_dir(package_dir, managed_root)

    prov_path = safe_pkg_dir / "source_provenance.json"
    _assert_no_symlink_or_reparse_components(prov_path, "Retained provenance sidecar")
    prov_bytes = _read_bounded(prov_path, MAX_PROVENANCE_SIDECAR_BYTES, "Provenance sidecar")
    prov_sha256 = hashlib.sha256(prov_bytes).hexdigest()

    try:
        prov_dict = json.loads(prov_bytes.decode("utf-8"))
        provenance = AssemblySourceProvenance.model_validate(prov_dict)
    except Exception as exc:
        raise ValidationError(f"Provenance inspection failed: {exc}") from exc

    glb_path = safe_pkg_dir / "source.glb"
    _assert_no_symlink_or_reparse_components(glb_path, "Retained assembly GLB")
    glb_bytes = _read_bounded(glb_path, MAX_ASSEMBLY_SOURCE_BYTES, "Retained assembly GLB")
    glb_sha256 = hashlib.sha256(glb_bytes).hexdigest()

    # Verify publication-completion marker
    _check_publication_marker(
        safe_pkg_dir=safe_pkg_dir,
        expected_spec_fp=provenance.spec_fingerprint,
        glb_sha256=glb_sha256,
        glb_byte_size=len(glb_bytes),
        prov_sha256=prov_sha256,
        prov_byte_size=len(prov_bytes),
    )

    preflight_assembly_glb(glb_bytes, spec, source_front=provenance.source_front)

    return AssemblyIngestResult(
        retained_glb_path=glb_path,
        retained_provenance_path=prov_path,
        retained_glb_sha256=glb_sha256,
        retained_glb_byte_size=len(glb_bytes),
        retained_provenance_sha256=prov_sha256,
        retained_provenance_bytes=prov_bytes,
        spec_fingerprint=provenance.spec_fingerprint,
        package_dir=safe_pkg_dir,
    )
