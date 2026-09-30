"""Bounded Blender processing for static, provider-generated V0.7 characters."""

from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any, cast

from gamefactory.adapters.assets.glb_validator import (
    _MAX_EXPANDED_POSITION_ELEMENTS,
    _accessor,
    _check_expanded_position_budget,
    _inspect,
    _InvalidGLB,
    _read_glb_bytes,
)
from gamefactory.adapters.assets.v07_geometry_validation import (
    triangle_soups_equivalent,
    validate_v07_geometry,
)
from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.dcc.blender_environment import (
    BLENDER_PYTHON_FAILURE_EXIT_CODE,
    BLENDER_PYTHONPATH_ENV,
    BlenderDependencyPreflight,
    blender_env_overrides,
    format_blender_preflight_failure_message,
    parse_blender_python_paths,
    run_blender_dependency_preflight,
)
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.asset_profiles import AssetProfileV07
from gamefactory.core.domain.errors import DccFailedError, ValidationError
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

_MAX_GLB_BYTES = 50 * 1024 * 1024
_MAX_JSON_BYTES = 1024 * 1024
# Scalar components across distinct non-POSITION character attribute accessors.
# POSITION work is separately bounded by the expanded-instance vertex cap.
_MAX_CHARACTER_ATTRIBUTE_SCALAR_VALUES = 16_000_000
_INLINE_LOG_CHARS = 12_000


def _lexical(path: Path | str) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _reject_links(path: Path, label: str) -> None:
    current = path
    while True:
        try:
            metadata = current.lstat()
            reparse = bool(
                getattr(metadata, "st_reparse_tag", 0)
                or getattr(metadata, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
            )
        except OSError:
            reparse = False
        if current.is_symlink() or getattr(current, "is_junction", lambda: False)() or reparse:
            raise ValidationError(f"{label} path traverses a symbolic link or junction: {current}")
        if current.parent == current:
            return
        current = current.parent


def _read_regular_bounded(path: Path, label: str, maximum: int) -> bytes:
    try:
        _reject_links(path, label)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise DccFailedError(f"{label} must be a regular file: {path}")
        with path.open("rb") as stream:
            data = stream.read(maximum + 1)
        after = path.lstat()
    except (DccFailedError, ValidationError):
        raise
    except OSError as exc:
        raise DccFailedError(f"Cannot read {label}: {exc}") from exc
    if len(data) > maximum:
        raise DccFailedError(f"{label} exceeds the {maximum}-byte limit")
    if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
        raise DccFailedError(f"{label} changed while it was read")
    return data


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    data = _read_regular_bounded(path, label, _MAX_JSON_BYTES)
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=lambda item: (_ for _ in ()).throw(ValueError(item)),
            parse_float=lambda item: float(item),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise DccFailedError(f"{label} is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DccFailedError(f"{label} must be a JSON object")

    def ensure_finite(item: Any) -> None:
        if isinstance(item, float) and not math.isfinite(item):
            raise DccFailedError(f"{label} contains a non-finite JSON number")
        if isinstance(item, dict):
            for child in item.values():
                ensure_finite(child)
        elif isinstance(item, list):
            for child in item:
                ensure_finite(child)

    ensure_finite(value)
    return value


def _unique_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _is_json_number(value: Any) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        return False


def _is_json_integer(value: Any) -> bool:
    return type(value) is int


def _typed_json_equal(actual: Any, expected: Any) -> bool:
    if isinstance(expected, dict):
        return (
            isinstance(actual, dict)
            and actual.keys() == expected.keys()
            and all(_typed_json_equal(actual[key], expected[key]) for key in expected)
        )
    if isinstance(expected, list):
        return (
            isinstance(actual, list)
            and len(actual) == len(expected)
            and all(
                _typed_json_equal(left, right) for left, right in zip(actual, expected, strict=True)
            )
        )
    if type(expected) in {int, float}:
        return _is_json_number(actual) and type(actual) is type(expected) and actual == expected
    return type(actual) is type(expected) and actual == expected


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _bounded_json_bytes(value: dict[str, Any], label: str) -> bytes:
    encoder = json.JSONEncoder(sort_keys=True, indent=2, allow_nan=False)
    output = bytearray()
    for piece in encoder.iterencode(value):
        encoded = piece.encode("utf-8")
        if len(output) + len(encoded) + 1 > _MAX_JSON_BYTES:
            raise ValidationError(f"Serialized {label} exceeds {_MAX_JSON_BYTES} bytes")
        output.extend(encoded)
    output.extend(b"\n")
    return bytes(output)


@dataclass(frozen=True)
class _FileIdentity:
    device: int
    inode: int
    size: int
    sha256: str


def _identity(path: Path, label: str, maximum: int = _MAX_GLB_BYTES) -> _FileIdentity:
    before = path.lstat()
    data = _read_regular_bounded(path, label, maximum)
    metadata = path.lstat()
    if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino) or len(
        data
    ) != metadata.st_size:
        raise DccFailedError(f"{label} changed while capturing its ownership identity")
    return _FileIdentity(
        metadata.st_dev, metadata.st_ino, len(data), hashlib.sha256(data).hexdigest()
    )


def _unlink_if_unchanged(path: Path, identity: _FileIdentity | None) -> None:
    if identity is None:
        return
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or (before.st_dev, before.st_ino) != (
            identity.device,
            identity.inode,
        ):
            return
        data = _read_regular_bounded(path, "owned character staging file", max(identity.size, 1))
        after = path.lstat()
        if (
            (after.st_dev, after.st_ino) == (identity.device, identity.inode)
            and len(data) == identity.size
            and hashlib.sha256(data).hexdigest() == identity.sha256
        ):
            path.unlink()
    except (OSError, DccFailedError, ValidationError):
        return


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        from types import MappingProxyType

        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


@dataclass(frozen=True)
class CharacterProcessResult:
    status: str
    exit_code: int
    duration_seconds: float
    blender_version: str
    script_sha256: str
    raw_glb_sha256: str
    processed_glb_sha256: str
    spec_sha256: str
    profile_sha256: str
    uniform_scale: float
    translation_gltf_m: tuple[float, float, float]
    processed_glb_path: Path
    report_path: Path
    report_sha256: str
    report_data: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "report_data", _freeze(self.report_data))

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "exit_code": self.exit_code,
            "duration_seconds": self.duration_seconds,
            "blender_version": self.blender_version,
            "script_sha256": self.script_sha256,
            "raw_glb_sha256": self.raw_glb_sha256,
            "processed_glb_sha256": self.processed_glb_sha256,
            "spec_sha256": self.spec_sha256,
            "profile_sha256": self.profile_sha256,
            "uniform_scale": self.uniform_scale,
            "translation_gltf_m": list(self.translation_gltf_m),
            "processed_glb_path": str(self.processed_glb_path),
            "report_path": str(self.report_path),
            "report_sha256": self.report_sha256,
            "report_data": _thaw(self.report_data),
        }


def _triangle_soup(
    document: dict[str, Any],
    binary: bytes,
    world: dict[int, list[list[float]]],
    node_names: set[str] | None = None,
    *,
    max_expanded_position_elements: int = _MAX_EXPANDED_POSITION_ELEMENTS,
) -> tuple[tuple[tuple[float, float, float], ...], ...]:
    """Decode every reachable triangle in world space from a bounded GLB context."""
    nodes = document.get("nodes", [])
    meshes = document.get("meshes", [])
    selected_nodes = {
        index
        for index, node in enumerate(nodes)
        if index in world
        and "mesh" in node
        and (node_names is None or node.get("name") in node_names)
    }
    _check_expanded_position_budget(
        document,
        nodes,
        meshes,
        selected_nodes,
        maximum=max_expanded_position_elements,
    )
    triangles: list[tuple[tuple[float, float, float], ...]] = []
    for node_index, node in enumerate(nodes):
        if "mesh" not in node or node_index not in world:
            continue
        if node_names is not None and node.get("name") not in node_names:
            continue
        mesh_index = node["mesh"]
        mesh = meshes[mesh_index]
        matrix = world[node_index]
        for primitive in mesh.get("primitives", []):
            attrs = primitive["attributes"]
            positions = _accessor(document, binary, attrs["POSITION"], "VEC3")
            if "indices" in primitive:
                indices = [
                    int(row[0])
                    for row in _accessor(document, binary, primitive["indices"], "SCALAR")
                ]
            else:
                indices = list(range(len(positions)))
            if len(indices) % 3:
                raise DccFailedError("GLB triangle index count is malformed")
            for offset in range(0, len(indices), 3):
                transformed: list[tuple[float, float, float]] = []
                for index in indices[offset : offset + 3]:
                    point = positions[index]
                    transformed.append(
                        (
                            float(
                                sum(
                                    matrix[0][column] * (*point, 1.0)[column] for column in range(4)
                                )
                            ),
                            float(
                                sum(
                                    matrix[1][column] * (*point, 1.0)[column] for column in range(4)
                                )
                            ),
                            float(
                                sum(
                                    matrix[2][column] * (*point, 1.0)[column] for column in range(4)
                                )
                            ),
                        )
                    )
                cyclic = [tuple(transformed[i:] + transformed[:i]) for i in range(3)]
                triangles.append(min(cyclic))
    return tuple(sorted(triangles))


def _validate_character_vertex_attributes(
    document: dict[str, Any],
    *,
    max_scalar_values: int = _MAX_CHARACTER_ATTRIBUTE_SCALAR_VALUES,
) -> tuple[int, ...]:
    """Validate stream descriptors and bound unique attribute scalar values."""
    accessors = document.get("accessors", [])
    unique_attributes: dict[int, int] = {}
    for mesh in document.get("meshes", []):
        for primitive in mesh.get("primitives", []):
            attrs = primitive.get("attributes", {})
            if not isinstance(attrs, dict):
                raise _InvalidGLB("character primitive attributes must be an object")
            position_index = attrs.get("POSITION")
            if type(position_index) is not int or not 0 <= position_index < len(accessors):
                raise _InvalidGLB("character POSITION accessor index is invalid")
            position = accessors[position_index]
            if not isinstance(position, dict) or type(position.get("count")) is not int:
                raise _InvalidGLB("character POSITION accessor is malformed")
            position_count = position["count"]
            for semantic, accessor_index in attrs.items():
                if semantic == "POSITION":
                    allowed_types = {"VEC3"}
                    allowed_components = {5126}
                elif semantic == "NORMAL":
                    allowed_types = {"VEC3"}
                    allowed_components = {5126}
                elif semantic == "TANGENT":
                    allowed_types = {"VEC4"}
                    allowed_components = {5126}
                elif semantic.startswith("TEXCOORD_"):
                    suffix = semantic.removeprefix("TEXCOORD_")
                    if not suffix.isdecimal() or str(int(suffix)) != suffix:
                        raise _InvalidGLB("character TEXCOORD semantic index is invalid")
                    allowed_types = {"VEC2"}
                    allowed_components = {5126, 5121, 5123}
                elif semantic.startswith("COLOR_"):
                    suffix = semantic.removeprefix("COLOR_")
                    if not suffix.isdecimal() or str(int(suffix)) != suffix:
                        raise _InvalidGLB("character COLOR semantic index is invalid")
                    allowed_types = {"VEC3", "VEC4"}
                    allowed_components = {5126, 5121, 5123}
                else:
                    raise _InvalidGLB(f"unsupported character vertex attribute {semantic!r}")
                if type(accessor_index) is not int or not 0 <= accessor_index < len(accessors):
                    raise _InvalidGLB("character vertex attribute accessor index is invalid")
                accessor = accessors[accessor_index]
                if (
                    not isinstance(accessor, dict)
                    or accessor.get("type") not in allowed_types
                    or accessor.get("componentType") not in allowed_components
                    or type(accessor.get("count")) is not int
                    or accessor.get("count") != position_count
                    or "sparse" in accessor
                ):
                    raise _InvalidGLB(f"character {semantic} accessor shape is unsupported")
                component = accessor["componentType"]
                normalized = accessor.get("normalized", False)
                if (
                    type(normalized) is not bool
                    or (component == 5126 and normalized)
                    or (component in {5121, 5123} and not normalized)
                ):
                    raise _InvalidGLB(
                        f"character {semantic} normalized flag is invalid for its component type"
                    )
                if semantic != "POSITION":
                    width = {"VEC2": 2, "VEC3": 3, "VEC4": 4}[accessor["type"]]
                    unique_attributes[accessor_index] = accessor["count"] * width
    total_values = sum(unique_attributes.values())
    if total_values > max_scalar_values:
        raise _InvalidGLB(
            "character attribute scalar values across distinct accessors "
            f"exceed safety limit {max_scalar_values}"
        )
    return tuple(unique_attributes)


def _decode_character_vertex_attributes(
    document: dict[str, Any], binary: bytes, accessor_indices: tuple[int, ...]
) -> None:
    """Decode each validated attribute descriptor once to check bounds and finiteness."""
    for accessor_index in accessor_indices:
        accessor = document["accessors"][accessor_index]
        _accessor(document, binary, accessor_index, accessor["type"])


def _raw_facts(
    raw_bytes: bytes,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
) -> tuple[dict[str, Any], dict[str, Any], dict[int, list[list[float]]]]:
    try:
        document, binary = _read_glb_bytes(raw_bytes, _MAX_GLB_BYTES)
        if document.get("skins") or document.get("animations"):
            raise _InvalidGLB("character source must not contain skins or animations")
        for mesh in document.get("meshes", []):
            for primitive in mesh.get("primitives", []):
                attrs = primitive.get("attributes", {})
                if any(str(name).startswith(("JOINTS_", "WEIGHTS_")) for name in attrs):
                    raise _InvalidGLB("character source must not contain joints or weights")
        attribute_accessors = _validate_character_vertex_attributes(document)
        mesh_infos, points, material_ids, texture_refs, triangles, world = _inspect(
            document, binary
        )
        _decode_character_vertex_attributes(document, binary, attribute_accessors)
    except (_InvalidGLB, KeyError, TypeError, ValueError, IndexError, RecursionError) as exc:
        raise ValidationError(
            f"Character source GLB failed bounded static preflight: {exc}"
        ) from exc
    if not mesh_infos or not points or triangles <= 0:
        raise ValidationError("Character source must contain static triangle meshes")
    if triangles > spec.geometry_budget.max_triangles_lod0:
        raise ValidationError("Character raw triangles exceed the LOD0 budget")
    material_count = len(document.get("materials", []))
    texture_max = max((max(width, height) for width, height in texture_refs), default=0)
    if material_count > spec.material_budget.max_materials:
        raise ValidationError("Character raw materials exceed the material budget")
    if texture_max > spec.texture_budget.max_dimension:
        raise ValidationError("Character raw texture exceeds the texture dimension budget")
    minima = tuple(min(point[axis] for point in points) for axis in range(3))
    maxima = tuple(max(point[axis] for point in points) for axis in range(3))
    dimensions = tuple(maxima[axis] - minima[axis] for axis in range(3))
    if any(not math.isfinite(value) or value <= 1e-8 for value in dimensions):
        raise ValidationError("Character source has empty or non-finite bounds")
    scale = spec.dimensions.height_m / dimensions[1]
    target_dimensions = (
        spec.dimensions.width_m,
        spec.dimensions.height_m,
        spec.dimensions.depth_m,
    )
    scaled_dimensions = tuple(value * scale for value in dimensions)
    tolerance = profile.document.processing.dimension_tolerance_m
    if any(
        abs(scaled - expected) > tolerance
        for scaled, expected in zip(scaled_dimensions, target_dimensions, strict=True)
    ):
        raise ValidationError(
            "Character dimensions cannot fit with uniform height scaling: "
            f"uniform result {scaled_dimensions} versus specification {target_dimensions} "
            f"(tolerance {tolerance}m)"
        )
    return (
        document,
        {
            "bounds_min": minima,
            "bounds_max": maxima,
            "dimensions": dimensions,
            "triangles": triangles,
            "materials": material_count,
            "texture_max_dimension": texture_max,
            "uniform_scale": scale,
        },
        world,
    )


def _bounds(meshes: Sequence[Any]) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    points = [point for mesh in meshes for point in mesh.points]
    if not points:
        raise DccFailedError("Decoded character output has no visual points")
    return (
        tuple(min(point[axis] for point in points) for axis in range(3)),
        tuple(max(point[axis] for point in points) for axis in range(3)),
    )


class CharacterProcessor:
    """Process one typed V0.7 static character through standalone Blender."""

    def __init__(
        self,
        blender_executable: Path | str | None = None,
        runner: ProcessRunner | None = None,
        python_paths: Sequence[Path | str] | None = None,
        dependency_preflight: bool = True,
    ) -> None:
        self.runner = runner or ProcessRunner(sanitize_output=True)
        if blender_executable:
            self.blender_exe = str(Path(blender_executable).resolve())
        else:
            detect = BlenderAdapter(self.runner).detect_tool()
            if not detect.available or not detect.executable_path:
                raise DccFailedError("Blender executable is not available")
            self.blender_exe = detect.executable_path
        if python_paths is None:
            self.python_paths = parse_blender_python_paths(os.environ.get(BLENDER_PYTHONPATH_ENV))
        else:
            self.python_paths = parse_blender_python_paths(python_paths)
        self.dependency_preflight = dependency_preflight
        self.last_dependency_preflight: BlenderDependencyPreflight | None = None

    @staticmethod
    def get_script_path() -> Path:
        return Path(
            str(files("gamefactory").joinpath("resources/blender/process_character.py"))
        ).resolve()

    def process_character(
        self,
        raw_glb_path: Path | str,
        spec: AssetSpecificationV07,
        profile: AssetProfileV07,
        *,
        expected_raw_glb_sha256: str,
        processed_glb_path: Path | str,
        report_path: Path | str | None = None,
        lod1_ratio: float | None = None,
        timeout_seconds: float = 90.0,
    ) -> CharacterProcessResult:
        if not isinstance(spec, AssetSpecificationV07) or not isinstance(profile, AssetProfileV07):
            raise ValidationError("Character processing requires typed V0.7 spec and profile")
        if spec.bound_profile() != profile:
            raise ValidationError("Character spec is not bound to the supplied typed profile")
        try:
            profile.check_specification(spec)
        except ValueError as exc:
            raise ValidationError(f"Character spec/profile mismatch: {exc}") from exc
        if (
            spec.source_kind != "provider_generated"
            or spec.category != "character"
            or spec.parts is not None
            or spec.sockets is not None
            or profile.geometry_mode != "single_mesh"
            or profile.accepted_source_kinds != ("provider_generated",)
            or spec.collider.policy != "capsule"
            or spec.collider.capsule is None
            or spec.orientation.up != "+Y"
            or spec.orientation.front != "-Z"
        ):
            raise ValidationError(
                "Character processor requires provider_generated, single_mesh, capsule, "
                "unrigged character V0.7 inputs with no assembly semantics"
            )
        ratio = spec.geometry_budget.lod_ratio if lod1_ratio is None else lod1_ratio
        if not 0.05 <= ratio <= 0.95 or not math.isfinite(ratio):
            raise ValidationError("LOD1 ratio must be finite and between 0.05 and 0.95")
        lod1_required = profile.document.processing.lod1_required or spec.lod_policy == "lod0_lod1"
        if not lod1_required:
            raise ValidationError("Character processor requires a declared LOD1 output")
        if not isinstance(expected_raw_glb_sha256, str) or len(expected_raw_glb_sha256) != 64:
            raise ValidationError("expected_raw_glb_sha256 must be a pinned SHA-256 digest")
        try:
            bytes.fromhex(expected_raw_glb_sha256)
        except ValueError as exc:
            raise ValidationError("expected_raw_glb_sha256 must be hexadecimal") from exc
        if len(bytes.fromhex(expected_raw_glb_sha256)) != 32:
            raise ValidationError("expected_raw_glb_sha256 must encode exactly 32 bytes")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValidationError("timeout_seconds must be finite and positive")

        raw_path = _lexical(raw_glb_path)
        _reject_links(raw_path, "raw character GLB")
        if raw_path.suffix.casefold() != ".glb":
            raise ValidationError("raw character input must use the .glb extension")
        raw_bytes = _read_regular_bounded(raw_path, "raw character GLB", _MAX_GLB_BYTES)
        raw_hash = hashlib.sha256(raw_bytes).hexdigest()
        if raw_hash != expected_raw_glb_sha256.casefold():
            raise DccFailedError("Raw character GLB differs from its required pinned SHA-256")
        raw_document, raw_facts, raw_world = _raw_facts(raw_bytes, spec, profile)

        output = _lexical(processed_glb_path)
        report = _lexical(report_path or output.with_name(f"{spec.asset_id}_character_report.json"))
        if output.suffix.casefold() != ".glb" or report.suffix.casefold() != ".json":
            raise ValidationError("Processed character output/report extensions must be .glb/.json")
        keys = {os.path.normcase(str(path)) for path in (raw_path, output, report)}
        if len(keys) != 3:
            raise ValidationError("Raw source, processed output, and report paths must be distinct")
        for path, label in ((output, "processed output"), (report, "processing report")):
            _reject_links(path, label)
            if path.exists():
                raise ValidationError(f"refusing to overwrite existing {label}: {path}")
        capsule = spec.collider.capsule
        assert capsule is not None
        tolerance = profile.document.processing.dimension_tolerance_m
        if (
            capsule.height_m > spec.dimensions.height_m + tolerance
            or 2 * capsule.radius_m
            > max(spec.dimensions.width_m, spec.dimensions.depth_m) + tolerance
        ):
            raise ValidationError("Character capsule collider does not fit the declared dimensions")

        output.parent.mkdir(parents=True, exist_ok=True)
        report.parent.mkdir(parents=True, exist_ok=True)
        attempt = uuid.uuid4().hex
        staged_output = output.with_name(f".{output.stem}.{attempt}.stage.glb")
        staged_report = report.with_name(f".{report.stem}.{attempt}.stage.json")
        contract_path = output.with_name(f".{output.stem}.{attempt}.contract.json")
        spec_sha = _canonical_hash(spec.model_dump(mode="json"))
        profile_sha = _canonical_hash(profile.document.model_dump(mode="json"))
        script_path = self.get_script_path()
        script_bytes = script_path.read_bytes()
        script_sha = hashlib.sha256(script_bytes).hexdigest()
        contract = {
            "contract_version": "character-processing-0.7.0",
            "attempt_id": attempt,
            "asset_id": spec.asset_id,
            "spec_sha256": spec_sha,
            "profile_id": profile.profile_id,
            "profile_version": profile.version,
            "profile_sha256": profile_sha,
            "source_sha256": raw_hash,
            "script_sha256": script_sha,
            "dimensions": spec.dimensions.model_dump(mode="json"),
            "origin_policy": spec.origin_policy,
            "lod_policy": spec.lod_policy,
            "lod1_required": lod1_required,
            "lod1_ratio": ratio,
            "dimension_tolerance_m": tolerance,
            "geometry_budget": spec.geometry_budget.model_dump(mode="json"),
            "material_budget": spec.material_budget.model_dump(mode="json"),
            "texture_budget": spec.texture_budget.model_dump(mode="json"),
            "capsule": capsule.model_dump(mode="json"),
        }
        contract_bytes = _bounded_json_bytes(contract, "character contract")
        output_identity = report_identity = contract_identity = None
        published_output_identity = published_report_identity = None
        publication_complete = False
        try:
            with contract_path.open("xb") as stream:
                stream.write(contract_bytes)
            contract_identity = _identity(contract_path, "character contract", _MAX_JSON_BYTES)
            if self.dependency_preflight:
                preflight = run_blender_dependency_preflight(
                    blender_executable=self.blender_exe,
                    runner=self.runner,
                    python_paths=self.python_paths,
                )
                self.last_dependency_preflight = preflight
                if preflight.status != "PASS":
                    raise DccFailedError(format_blender_preflight_failure_message(preflight))
            command = [
                self.blender_exe,
                "--background",
                "--factory-startup",
                "--python-exit-code",
                str(BLENDER_PYTHON_FAILURE_EXIT_CODE),
                "--python",
                str(script_path),
                "--",
                "--input-glb",
                str(raw_path),
                "--output-glb",
                str(staged_output),
                "--report-path",
                str(staged_report),
                "--contract",
                str(contract_path),
            ]
            request = CommandRequest(
                args=command,
                cwd=output.parent,
                env_overrides=blender_env_overrides(self.python_paths),
                timeout_seconds=timeout_seconds,
            )
            result = self.runner.run(request)
            if result.exit_code != 0:
                excerpt = (result.stderr or result.stdout)[-_INLINE_LOG_CHARS:]
                raise DccFailedError(
                    f"Blender character processing failed (exit {result.exit_code}): {excerpt}",
                    exit_code=result.exit_code,
                    stderr=result.stderr,
                )
            output_bytes = _read_regular_bounded(
                staged_output, "staged character GLB", _MAX_GLB_BYTES
            )
            output_identity = _identity(staged_output, "staged character GLB")
            _read_regular_bounded(staged_report, "staged character report", _MAX_JSON_BYTES)
            report_identity = _identity(staged_report, "staged character report", _MAX_JSON_BYTES)
            report_data = _read_json_object(staged_report, "Blender character report")
            output_sha = hashlib.sha256(output_bytes).hexdigest()
            required_report = {
                "status": "SUCCESS",
                "exit_code": 0,
                "attempt_id": attempt,
                "asset_id": spec.asset_id,
                "source_sha256": raw_hash,
                "output_sha256": output_sha,
                "spec_sha256": spec_sha,
                "profile_id": profile.profile_id,
                "profile_version": profile.version,
                "profile_sha256": profile_sha,
                "script_sha256": script_sha,
            }
            if (
                not _is_json_integer(report_data.get("exit_code"))
                or not _is_json_integer(report_data.get("profile_version"))
                or any(
                    not _typed_json_equal(report_data.get(key), value)
                    for key, value in required_report.items()
                )
            ):
                raise DccFailedError("Blender character report has stale or forged bindings")
            blender_version = report_data.get("blender_version")
            if not isinstance(blender_version, str) or not blender_version.startswith("Blender "):
                raise DccFailedError("Blender character report has no valid tool version")

            raw_metrics_report = report_data.get("raw_metrics")
            if not isinstance(raw_metrics_report, dict):
                raise DccFailedError("Blender character report is missing raw metrics")
            for key, expected in (
                (
                    "bounds_min",
                    (
                        raw_facts["bounds_min"][0],
                        raw_facts["bounds_min"][2],
                        raw_facts["bounds_min"][1],
                    ),
                ),
                (
                    "bounds_max",
                    (
                        raw_facts["bounds_max"][0],
                        raw_facts["bounds_max"][2],
                        raw_facts["bounds_max"][1],
                    ),
                ),
                (
                    "dimensions",
                    (
                        raw_facts["dimensions"][0],
                        raw_facts["dimensions"][2],
                        raw_facts["dimensions"][1],
                    ),
                ),
            ):
                values = raw_metrics_report.get(key)
                if (
                    not isinstance(values, list)
                    or len(values) != 3
                    or any(
                        not _is_json_number(value) or abs(float(value) - expected[i]) > 1e-5
                        for i, value in enumerate(values)
                    )
                ):
                    raise DccFailedError(
                        f"Blender character report raw {key} differs from source GLB"
                    )
            if (
                not _is_json_integer(raw_metrics_report.get("lod0_triangles"))
                or raw_metrics_report.get("lod0_triangles") != raw_facts["triangles"]
            ):
                raise DccFailedError(
                    "Blender character report raw triangle count differs from source GLB"
                )

            geometry = validate_v07_geometry(
                staged_output,
                spec,
                profile,
                max_file_size_bytes=_MAX_GLB_BYTES,
            )
            if not geometry.passed:
                failures = [finding.message for finding in geometry.findings if not finding.passed]
                raise DccFailedError(
                    f"Outside-Blender V0.7 character validation failed: {failures}"
                )

            try:
                processed_doc, processed_bin = _read_glb_bytes(output_bytes, _MAX_GLB_BYTES)
                (
                    processed_meshes,
                    _,
                    material_ids,
                    textures,
                    processed_triangle_count,
                    processed_world,
                ) = _inspect(processed_doc, processed_bin)
            except (_InvalidGLB, KeyError, TypeError, ValueError, IndexError) as exc:
                raise DccFailedError(
                    f"Processed character GLB could not be decoded: {exc}"
                ) from exc
            lod0_name = f"SM_{spec.asset_id}_LOD0"
            lod1_name = f"SM_{spec.asset_id}_LOD1"
            names = [mesh.name for mesh in processed_meshes]
            if sorted(names) != sorted([lod0_name, lod1_name]):
                raise DccFailedError(f"Processed character mesh set is not exact: {names}")
            lod0 = next(mesh for mesh in processed_meshes if mesh.name == lod0_name)
            output_min, output_max = _bounds([lod0])
            output_dimensions = tuple(output_max[i] - output_min[i] for i in range(3))
            source_min = raw_facts["bounds_min"]
            source_max = raw_facts["bounds_max"]
            scale = raw_facts["uniform_scale"]
            target_center = (
                (source_min[0] + source_max[0]) / 2,
                source_min[1]
                if spec.origin_policy == "bottom_center"
                else (source_min[1] + source_max[1]) / 2,
                (source_min[2] + source_max[2]) / 2,
            )
            expected_translation = tuple(-value * scale for value in target_center)
            expected_output_min = tuple(
                source_min[i] * scale + expected_translation[i] for i in range(3)
            )
            expected_output_max = tuple(
                source_max[i] * scale + expected_translation[i] for i in range(3)
            )
            if any(
                abs(output_min[i] - expected_output_min[i]) > tolerance
                or abs(output_max[i] - expected_output_max[i]) > tolerance
                for i in range(3)
            ):
                raise DccFailedError(
                    "Processed bounds do not match uniform source fit and origin translation"
                )

            source_soup = _triangle_soup(
                raw_document, _read_glb_bytes(raw_bytes, _MAX_GLB_BYTES)[1], raw_world
            )
            processed_soup = _triangle_soup(
                processed_doc, processed_bin, processed_world, {lod0_name}
            )

            def transform_point(point: tuple[float, float, float]) -> tuple[float, float, float]:
                return (
                    float((point[0] - target_center[0]) * scale),
                    float((point[1] - target_center[1]) * scale),
                    float((point[2] - target_center[2]) * scale),
                )

            transformed_source_soup: tuple[tuple[tuple[float, float, float], ...], ...] = tuple(
                (
                    transform_point(triangle[0]),
                    transform_point(triangle[1]),
                    transform_point(triangle[2]),
                )
                for triangle in source_soup
            )
            if not triangle_soups_equivalent(
                transformed_source_soup, processed_soup, tolerance=1e-5
            ):
                raise DccFailedError(
                    "Processed LOD0 is not the pinned source under the reported uniform transform"
                )

            transform = report_data.get("transform")
            if not isinstance(transform, dict):
                raise DccFailedError("Blender character report is missing transformation metrics")
            reported_scale = transform.get("uniform_scale")
            reported_translation = transform.get("translation_gltf_m")
            if (
                not _is_json_number(reported_scale)
                or abs(float(cast(float, reported_scale)) - scale) > 1e-6
                or not isinstance(reported_translation, list)
                or len(reported_translation) != 3
                or any(
                    not _is_json_number(value)
                    or abs(float(value) - expected_translation[index]) > 1e-5
                    for index, value in enumerate(reported_translation)
                )
                or transform.get("scale_mode") != "uniform_height_fit"
                or transform.get("origin_policy") != spec.origin_policy
            ):
                raise DccFailedError(
                    "Blender character transformation report disagrees with decoded geometry"
                )
            reported_bounds = report_data.get("processed_metrics")
            if not isinstance(reported_bounds, dict):
                raise DccFailedError(
                    "Blender character report is missing processed geometry metrics"
                )
            for key, actual in (
                ("bounds_min", output_min),
                ("bounds_max", output_max),
                ("dimensions", output_dimensions),
            ):
                values = reported_bounds.get(key)
                if (
                    not isinstance(values, list)
                    or len(values) != 3
                    or any(
                        not _is_json_number(value) or abs(float(value) - actual[i]) > 1e-5
                        for i, value in enumerate(values)
                    )
                ):
                    raise DccFailedError(
                        f"Blender character report {key} differs from decoded output"
                    )
            actual_texture_max = max((max(width, height) for width, height in textures), default=0)
            if (
                not _is_json_integer(reported_bounds.get("texture_max_dimension"))
                or reported_bounds.get("texture_max_dimension") != actual_texture_max
            ):
                raise DccFailedError(
                    "Blender character report texture metrics disagree with output"
                )
            if (
                not _is_json_integer(reported_bounds.get("lod0_triangles"))
                or not _is_json_integer(reported_bounds.get("lod1_triangles"))
                or not _is_json_integer(reported_bounds.get("materials"))
                or reported_bounds.get("lod0_triangles")
                != next(mesh.triangle_count for mesh in processed_meshes if mesh.name == lod0_name)
                or reported_bounds.get("lod1_triangles")
                != next(mesh.triangle_count for mesh in processed_meshes if mesh.name == lod1_name)
                or reported_bounds.get("materials") != len(material_ids)
                or processed_triangle_count != sum(mesh.triangle_count for mesh in processed_meshes)
            ):
                raise DccFailedError(
                    "Blender character report mesh budgets differ from decoded output"
                )
            lod1_triangles = next(
                mesh.triangle_count for mesh in processed_meshes if mesh.name == lod1_name
            )
            lod0_triangles = lod0.triangle_count
            lod1_method = reported_bounds.get("lod1_method")
            actual_ratio = reported_bounds.get("lod1_actual_triangle_ratio")
            if (
                not _is_json_number(reported_bounds.get("lod1_requested_ratio"))
                or abs(float(cast(float, reported_bounds.get("lod1_requested_ratio"))) - ratio)
                > 1e-9
                or not _is_json_number(actual_ratio)
                or abs(float(cast(float, actual_ratio)) - lod1_triangles / lod0_triangles) > 1e-9
                or lod1_method not in {"decimated", "unchanged_fallback"}
            ):
                raise DccFailedError(
                    "Blender character report LOD1 method/ratio differs from decoded output"
                )
            if lod1_method == "unchanged_fallback":
                lod1_soup = _triangle_soup(
                    processed_doc, processed_bin, processed_world, {lod1_name}
                )
                if lod1_triangles != lod0_triangles or not triangle_soups_equivalent(
                    processed_soup, lod1_soup, tolerance=1e-5
                ):
                    raise DccFailedError("Unchanged LOD1 fallback differs from validated LOD0")
            if not _typed_json_equal(
                report_data.get("runtime_collider"),
                {"policy": "capsule", **capsule.model_dump(mode="json")},
            ):
                raise DccFailedError(
                    "Blender character report runtime capsule differs from the typed contract"
                )
            duration_seconds = report_data.get("duration_seconds")
            if not _is_json_number(duration_seconds) or float(cast(float, duration_seconds)) < 0:
                raise DccFailedError("Blender character report duration is invalid")

            if (
                hashlib.sha256(
                    _read_regular_bounded(raw_path, "raw character GLB", _MAX_GLB_BYTES)
                ).hexdigest()
                != raw_hash
            ):
                raise DccFailedError("Pinned raw character GLB changed during Blender processing")
            # Atomic hard-link publication gives no-replace semantics. If the second
            # publication fails, remove only our unchanged first target.
            if _identity(staged_output, "staged character GLB") != output_identity:
                raise DccFailedError("Staged character GLB changed before publication")
            if (
                _identity(staged_report, "staged character report", _MAX_JSON_BYTES)
                != report_identity
            ):
                raise DccFailedError("Staged character report changed before publication")
            try:
                os.link(staged_output, output)
            except FileExistsError as exc:
                raise DccFailedError(
                    "Processed character target appeared before publication"
                ) from exc
            # The successful hard link gives us ownership immediately. Use the
            # already captured staging identity for rollback even if verifying
            # the published path itself raises or observes a race.
            published_output_identity = output_identity
            final_output_identity = _identity(output, "published character GLB")
            if final_output_identity != published_output_identity:
                raise DccFailedError("Published character GLB differs from validated staging bytes")
            try:
                os.link(staged_report, report)
            except FileExistsError as exc:
                raise DccFailedError("Character report target appeared before publication") from exc
            published_report_identity = report_identity
            final_report_identity = _identity(report, "published character report", _MAX_JSON_BYTES)
            if final_report_identity != published_report_identity:
                raise DccFailedError(
                    "Published character report ownership could not be established"
                )
            result_value = CharacterProcessResult(
                status="SUCCESS",
                exit_code=0,
                duration_seconds=result.duration_seconds,
                blender_version=blender_version,
                script_sha256=script_sha,
                raw_glb_sha256=raw_hash,
                processed_glb_sha256=output_sha,
                spec_sha256=spec_sha,
                profile_sha256=profile_sha,
                uniform_scale=scale,
                translation_gltf_m=expected_translation,
                processed_glb_path=output,
                report_path=report,
                report_sha256=final_report_identity.sha256,
                report_data=report_data,
            )
            publication_complete = True
            return result_value
        finally:
            _unlink_if_unchanged(contract_path, contract_identity)
            _unlink_if_unchanged(staged_output, output_identity)
            _unlink_if_unchanged(staged_report, report_identity)
            if not publication_complete:
                _unlink_if_unchanged(report, published_report_identity)
                _unlink_if_unchanged(output, published_output_identity)
