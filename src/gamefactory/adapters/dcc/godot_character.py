"""Standalone Godot runtime proof for static V0.7 character assets.

This adapter imports the exact processed GLB in a fresh Godot project, checks
the imported LOD meshes, constructs and queries the specified runtime capsule,
and captures every profile-declared review view. It does not invoke providers or
write workflow/database state.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from importlib.resources import files
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.glb_validator import (
    _inspect,
    _InvalidGLB,
    _node_matrix,
    _read_glb_bytes,
)
from gamefactory.adapters.assets.v07_geometry_validation import validate_v07_geometry
from gamefactory.adapters.dcc.character_processor import (
    _MAX_GLB_BYTES,
    _raw_facts,
    _read_regular_bounded,
)
from gamefactory.adapters.dcc.godot_assembly import (
    _camera_up,
    _display_driver,
    _isolated_environment,
    _read_bounded_regular_file,
    _reject_reparse_components,
    _unique_json_object,
    _validate_review_views,
)
from gamefactory.adapters.engines.godot import GodotAdapter
from gamefactory.adapters.engines.godot_image import decode_png
from gamefactory.core.domain.asset_contracts import AssetSpecificationV07
from gamefactory.core.domain.asset_profiles import AssetProfileV07
from gamefactory.core.domain.camera_framing import (
    view_axis_label,
    view_direction,
)
from gamefactory.core.domain.errors import ToolExecutionError, ValidationError
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

REQUEST_SCHEMA_VERSION = "character-runtime-request-0.7.0"
OBSERVATION_SCHEMA_VERSION = "character-runtime-observation-0.7.0"
_MAX_JSON_BYTES = 1024 * 1024
_MAX_CAPTURE_BYTES = 4 * 1024 * 1024
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_REQUIRED_CHARACTER_VIEWS = frozenset({"front", "rear", "left", "right", "three_quarter"})


@dataclass(frozen=True)
class CharacterRuntimeResult:
    """Real-engine runtime verification result and exact proof artifacts."""

    status: str
    observation: dict[str, Any]
    artifacts: dict[str, bytes]
    findings: list[str]
    request_digest: str
    raw_glb_sha256: str
    processed_glb_sha256: str
    captures: dict[str, bytes]


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def _harness_resource() -> bytes:
    harness_path = files("gamefactory").joinpath("resources/godot/character_runtime_harness_v07.gd")
    if not harness_path.is_file():
        raise ToolExecutionError("Packaged Godot character runtime harness is missing")
    return harness_path.read_bytes()


def _bounded_json(value: dict[str, Any], label: str, limit: int = _MAX_JSON_BYTES) -> bytes:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(raw) > limit:
        raise ValidationError(f"{label} exceeds the {limit}-byte limit")
    return raw


def _strict_json(raw: bytes, label: str) -> dict[str, Any]:
    def reject_constant(value: str) -> None:
        raise ValueError(f"invalid JSON constant {value}")

    def parse_finite_float(value: str) -> float:
        parsed = float(value)
        if not math.isfinite(parsed):
            raise ValueError("non-finite JSON number")
        return parsed

    def validate_numbers(value: Any) -> None:
        if type(value) is int:
            try:
                converted = float(value)
            except OverflowError as exc:
                raise ValueError("JSON integer is outside finite numeric range") from exc
            if not math.isfinite(converted):
                raise ValueError("JSON integer is outside finite numeric range")
        elif type(value) is float and not math.isfinite(value):
            raise ValueError("non-finite JSON number")
        elif isinstance(value, dict):
            for child in value.values():
                validate_numbers(child)
        elif isinstance(value, list):
            for child in value:
                validate_numbers(child)

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_unique_json_object,
            parse_constant=reject_constant,
            parse_float=parse_finite_float,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} is not strict UTF-8 JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValidationError(f"{label} must be a JSON object")
    try:
        validate_numbers(value)
    except (ValueError, RecursionError) as exc:
        raise ValidationError(f"{label} contains an invalid numeric value: {exc}") from exc
    return value


def _actual_glb_facts(
    raw: bytes,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    processed_path: Path,
) -> dict[str, Any]:
    try:
        document, binary = _read_glb_bytes(raw, _MAX_GLB_BYTES)
        if document.get("skins") or document.get("animations"):
            raise _InvalidGLB("character GLB must not contain skins or animations")
        mesh_infos, _all_points, _mats, _textures, _triangles, world = _inspect(document, binary)
    except (_InvalidGLB, KeyError, TypeError, ValueError, IndexError, RecursionError) as exc:
        raise ValidationError(f"Processed character GLB failed bounded inspection: {exc}") from exc

    lod_names = (f"SM_{spec.asset_id}_LOD0", f"SM_{spec.asset_id}_LOD1")
    by_name = {mesh.name: mesh for mesh in mesh_infos}
    required_lod1 = profile.document.processing.lod1_required or spec.lod_policy == "lod0_lod1"
    expected_names = {lod_names[0], lod_names[1]} if required_lod1 else {lod_names[0]}
    if set(by_name) != expected_names or any(
        by_name[name].triangle_count <= 0 for name in expected_names
    ):
        raise ValidationError(
            f"Processed character GLB mesh inventory differs from declared LODs: {sorted(by_name)}"
        )
    if any(name.startswith("COL_") for name in by_name):
        raise ValidationError("Capsule collider is runtime-only; processed GLB contains a COL mesh")

    nodes = document.get("nodes")
    if not isinstance(nodes, list):
        raise ValidationError("Processed character GLB node list is invalid")
    if document.get("cameras") or any(
        isinstance(node, dict) and ("camera" in node or "skin" in node) for node in nodes
    ):
        raise ValidationError("Processed character GLB cannot contain camera or skin semantics")
    mesh_nodes = [node for node in nodes if isinstance(node, dict) and "mesh" in node]
    names = [node.get("name") for node in mesh_nodes]
    if set(names) != expected_names or len(names) != len(expected_names):
        raise ValidationError(
            "Processed character GLB contains extra, duplicate, or unnamed mesh nodes"
        )
    transforms: dict[str, dict[str, Any]] = {}
    node_indices = {id(node): index for index, node in enumerate(nodes)}
    for node in mesh_nodes:
        name = node["name"]
        matrix = _node_matrix(node)
        if any(
            abs(matrix[row][column] - (1.0 if row == column else 0.0)) > 1e-5
            for row in range(4)
            for column in range(4)
        ):
            raise ValidationError(
                f"Processed character {name} must have an identity local transform"
            )
        world_matrix = world[node_indices[id(node)]]
        if any(
            abs(world_matrix[row][column] - (1.0 if row == column else 0.0)) > 1e-5
            for row in range(4)
            for column in range(4)
        ):
            raise ValidationError(f"Processed character {name} has a transformed GLB ancestor")
        transforms[name] = {
            "position": [matrix[i][3] for i in range(3)],
            "basis": [[matrix[r][c] for c in range(3)] for r in range(3)],
            "scale": [1.0, 1.0, 1.0],
        }
    if any(
        str(node.get("name", "")).startswith(("PART_", "SOCKET_"))
        for node in nodes
        if isinstance(node, dict)
    ):
        raise ValidationError("Single-mesh character GLB cannot contain PART or SOCKET semantics")

    lod0_points = by_name[lod_names[0]].points
    if not lod0_points:
        raise ValidationError("Processed character LOD0 has no decoded vertices")
    minima = [min(point[axis] for point in lod0_points) for axis in range(3)]
    maxima = [max(point[axis] for point in lod0_points) for axis in range(3)]
    dimensions = [maxima[i] - minima[i] for i in range(3)]
    tolerance = profile.document.processing.dimension_tolerance_m
    expected_dimensions = [
        spec.dimensions.width_m,
        spec.dimensions.height_m,
        spec.dimensions.depth_m,
    ]
    if any(
        abs(actual - expected) > tolerance
        for actual, expected in zip(dimensions, expected_dimensions, strict=True)
    ):
        raise ValidationError(
            "Processed character LOD0 bounds differ from the typed V0.7 dimensions"
        )
    if abs((minima[0] + maxima[0]) / 2) > tolerance or abs((minima[2] + maxima[2]) / 2) > tolerance:
        raise ValidationError("Processed character LOD0 is not centered on X/Z")
    if spec.origin_policy == "bottom_center":
        origin_ok = abs(minima[1]) <= tolerance
    else:
        origin_ok = abs((minima[1] + maxima[1]) / 2) <= tolerance
    if not origin_ok:
        raise ValidationError(
            "Processed character LOD0 bounds do not match the declared origin policy"
        )
    if required_lod1:
        lod1_points = by_name[lod_names[1]].points
        lod1_min = [min(point[axis] for point in lod1_points) for axis in range(3)]
        lod1_max = [max(point[axis] for point in lod1_points) for axis in range(3)]
        if any(
            abs(lod1_min[i] - minima[i]) > tolerance or abs(lod1_max[i] - maxima[i]) > tolerance
            for i in range(3)
        ):
            raise ValidationError("Processed character LOD1 bounds differ from LOD0")
    try:
        geometry = validate_v07_geometry(
            processed_path, spec, profile, max_file_size_bytes=_MAX_GLB_BYTES
        )
    except Exception as exc:
        if isinstance(exc, ValidationError):
            raise
        raise ValidationError(
            f"Processed character failed V0.7 geometry validation: {exc}"
        ) from exc
    if not geometry.passed:
        failures = [finding.message for finding in geometry.findings if not finding.passed]
        raise ValidationError(f"Processed character failed V0.7 geometry validation: {failures}")
    return {
        "mesh_names": sorted(expected_names),
        "lod1_required": required_lod1,
        "transforms": transforms,
        "bounds_min": minima,
        "bounds_max": maxima,
        "dimensions": dimensions,
    }


def _validate_character_inputs(
    spec: object, profile: object
) -> tuple[AssetSpecificationV07, AssetProfileV07, list[str]]:
    if type(spec) is not AssetSpecificationV07 or type(profile) is not AssetProfileV07:
        raise ValidationError(
            "verify_godot_character requires exact typed V0.7 specification/profile"
        )
    typed_spec = spec
    typed_profile = profile
    try:
        bound_profile = typed_spec.bound_profile()
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValidationError(f"Character specification has no valid bound profile: {exc}") from exc
    if bound_profile != typed_profile:
        raise ValidationError("Supplied profile differs from the specification-bound V0.7 profile")
    if (
        typed_spec.source_kind != "provider_generated"
        or typed_spec.category != "character"
        or typed_spec.parts is not None
        or typed_spec.sockets is not None
        or typed_profile.geometry_mode != "single_mesh"
        or typed_profile.accepted_source_kinds != ("provider_generated",)
        or typed_profile.document.godot.body_kind != "static_body"
        or typed_profile.document.godot.require_ray_hit is not True
        or typed_profile.document.godot.require_area is not False
        or typed_spec.collider.policy != "capsule"
        or typed_spec.collider.capsule is None
        or typed_spec.orientation.up != "+Y"
        or typed_spec.orientation.front != "-Z"
    ):
        raise ValidationError(
            "Godot character verification requires provider_generated, unrigged single_mesh V0.7 character with capsule collider"
        )
    try:
        typed_profile.check_specification(typed_spec)
    except ValueError as exc:
        raise ValidationError(
            f"Character specification is not accepted by its profile: {exc}"
        ) from exc
    if not (
        typed_profile.document.processing.lod1_required or typed_spec.lod_policy == "lod0_lod1"
    ):
        raise ValidationError("Godot character runtime verification requires a declared LOD1")
    views = _validate_review_views(typed_profile.review_views)
    if not _REQUIRED_CHARACTER_VIEWS.issubset(views):
        missing = sorted(_REQUIRED_CHARACTER_VIEWS.difference(views))
        raise ValidationError(f"Character review_views omit required coverage: {missing}")
    return typed_spec, typed_profile, views


def _strict_vector_close(
    value: Any, expected: tuple[float, float, float], tolerance: float = 1e-4
) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    if any(type(component) not in {int, float} for component in value):
        return False
    try:
        actual = tuple(float(component) for component in value)
    except (OverflowError, TypeError, ValueError):
        return False
    return all(math.isfinite(component) for component in actual) and all(
        abs(actual[index] - expected[index]) <= tolerance for index in range(3)
    )


def _validate_observation(
    observation: dict[str, Any],
    *,
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    views: list[str],
    request: dict[str, Any],
    facts: dict[str, Any],
) -> None:
    def require(condition: bool, message: str) -> None:
        if not condition:
            raise ValidationError(
                f"Godot character observation failed independent validation: {message}"
            )

    for key in (
        "workflow_id",
        "revision",
        "asset_id",
        "execution_id",
        "attempt_number",
        "raw_glb_sha256",
        "processed_glb_sha256",
        "harness_sha256",
        "profile_sha256",
        "specification_sha256",
        "request_digest",
    ):
        value = observation.get(key)
        expected = request[key]
        if key in {"revision", "attempt_number"}:
            require(type(value) is int, f"{key} must be a JSON integer")
        else:
            require(isinstance(value, str), f"{key} must be a string")
        require(value == expected, f"{key} binding mismatch (stale attempt rejected)")
    require(
        observation.get("schema_version") == OBSERVATION_SCHEMA_VERSION, "schema version mismatch"
    )
    status = observation.get("status")
    errors = observation.get("errors")
    require(status in {"PASS", "FAIL"}, "status must be PASS or FAIL")
    if status == "FAIL":
        require(
            isinstance(errors, list)
            and bool(errors)
            and all(isinstance(item, str) and item for item in errors),
            "FAIL observation has no controlled findings",
        )
        return
    require(errors == [], "PASS observation contains errors")

    lods = observation.get("lods")
    if not isinstance(lods, dict) or set(lods) != set(facts["mesh_names"]):
        raise ValidationError(
            "Godot character observation failed independent validation: LOD inventory mismatch"
        )
    for name, transform in facts["transforms"].items():
        item = lods.get(name)
        if not isinstance(item, dict):
            raise ValidationError(
                f"Godot character observation failed independent validation: {name} observation is missing"
            )
        surface_count = item.get("surface_count")
        require(
            item.get("mesh_present") is True and type(surface_count) is int and surface_count > 0,
            f"{name} is empty",
        )
        require(item.get("visible") is (name.endswith("LOD0")), f"{name} visibility is incorrect")
        require(
            _strict_vector_close(item.get("local_position"), tuple(transform["position"])),
            f"{name} local position differs from processed GLB",
        )
        require(
            _strict_vector_close(item.get("local_scale"), tuple(transform["scale"])),
            f"{name} local scale differs from processed GLB",
        )
        basis = item.get("local_basis")
        expected_basis = transform["basis"]
        require(
            isinstance(basis, list)
            and len(basis) == 3
            and all(
                _strict_vector_close(row, tuple(expected_basis[i])) for i, row in enumerate(basis)
            ),
            f"{name} local basis differs from processed GLB",
        )
    collider = observation.get("collider")
    capsule = spec.collider.capsule
    if not isinstance(collider, dict) or capsule is None:
        raise ValidationError(
            "Godot character observation failed independent validation: capsule observation missing"
        )
    require(collider.get("body_kind") == "StaticBody3D", "runtime body is not StaticBody3D")
    require(collider.get("shape_class") == "CapsuleShape3D", "runtime shape is not CapsuleShape3D")
    require(
        _number_close(collider.get("radius_m"), capsule.radius_m), "runtime capsule radius mismatch"
    )
    require(
        _number_close(collider.get("height_m"), capsule.height_m), "runtime capsule height mismatch"
    )
    expected_center = capsule.height_m / 2 if spec.origin_policy == "bottom_center" else 0.0
    require(
        _number_close(collider.get("center_y_m"), expected_center),
        "runtime capsule origin offset mismatch",
    )
    require(collider.get("physics_ray_hit") is True, "runtime capsule ray query missed")
    policy = profile.framing
    framing = observation.get("view_framing")
    captures = observation.get("captures")
    if not isinstance(framing, dict) or set(framing) != set(views):
        raise ValidationError(
            "Godot character observation failed independent validation: framing views differ from profile"
        )
    if not isinstance(captures, dict) or set(captures) != set(views):
        raise ValidationError(
            "Godot character observation failed independent validation: capture views differ from profile"
        )
    for view in views:
        frame = framing[view]
        require(isinstance(frame, dict) and frame.get("ok") is True, f"{view} framing failed")
        require(frame.get("view_axis") == view_axis_label(view), f"{view} camera axis mismatch")
        direction = view_direction(view)
        expected_direction = (-direction[0], -direction[1], -direction[2])
        require(frame.get("view_axis") == view_axis_label(view), f"{view} camera axis mismatch")
        require(
            _strict_vector_close(frame.get("camera_direction"), expected_direction),
            f"{view} camera direction mismatch",
        )
        require(
            _strict_vector_close(frame.get("camera_up"), _camera_up(view, expected_direction)),
            f"{view} camera up mismatch",
        )
        ratio = frame.get("height_ratio")
        require(
            _number_close(ratio, ratio)
            and policy.min_screen_fraction <= float(ratio) <= policy.max_screen_fraction,
            f"{view} framing ratio is outside profile limits",
        )
        require(
            frame.get("inside_viewport") is True and frame.get("margin_ok") is True,
            f"{view} is outside frame",
        )
        capture = captures[view]
        require(
            isinstance(capture, dict)
            and isinstance(capture.get("sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", capture["sha256"]) is not None,
            f"{view} capture hash is missing",
        )


def _number_close(value: Any, expected: float, tolerance: float = 1e-4) -> bool:
    if type(value) not in {int, float}:
        return False
    try:
        actual = float(value)
    except (OverflowError, TypeError, ValueError):
        return False
    return math.isfinite(actual) and abs(actual - expected) <= tolerance


_UNSAFE_GODOT_EXTENSIONS = frozenset(
    {
        ".gd",
        ".godot",
        ".uid",
        ".tscn",
        ".scn",
        ".res",
        ".tres",
        ".import",
        ".exe",
        ".dll",
        ".so",
        ".dylib",
        ".sh",
        ".bat",
        ".cmd",
        ".ps1",
        ".py",
        ".vbs",
        ".bin",
        ".com",
        ".scr",
    }
)


def _is_safe_res_path(path: Any, *, allowed_extension: str | None = None) -> bool:
    if not isinstance(path, str) or not path.startswith("res://"):
        return False
    if "\0" in path or "\\" in path or "\r" in path or "\n" in path:
        return False
    sub = path[6:]
    if not sub or sub.startswith("/") or sub.endswith("/") or ":" in sub or "//" in sub:
        return False
    parts = sub.split("/")
    for part in parts:
        if part in {"..", ".", ""}:
            return False
    lower = path.casefold()
    for ext in _UNSAFE_GODOT_EXTENSIONS:
        if lower.endswith(ext) or (ext + "/") in lower or (ext + ".") in lower:
            return False
    if allowed_extension is not None:
        if not lower.endswith(allowed_extension.casefold()):
            return False
    else:
        if any("." in part for part in parts):
            return False
    return True


def _validate_request_portability(value: Any, breadcrumb: str = "$") -> None:
    """Ensure the emitted or validated request has no absolute external paths, URLs, secrets, or path traversal."""
    if isinstance(value, dict):
        for key, child in value.items():
            lower_k = key.lower()
            if any(
                marker in lower_k
                for marker in (
                    "secret",
                    "token",
                    "password",
                    "credential",
                    "api_key",
                    "authorization",
                )
            ):
                raise ValidationError(
                    f"Request must not contain credential fields at {breadcrumb}.{key}"
                )
            _validate_request_portability(child, f"{breadcrumb}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _validate_request_portability(child, f"{breadcrumb}[{index}]")
    elif isinstance(value, str):
        if (
            re.search(r"(?i)https?://|wss?://", value)
            or re.match(r"(?i)^[a-z]:[\\/]", value)
            or value.startswith(("\\\\", "//", "/", "user://"))
            or ".." in value.split("/")
            or "\\" in value
            or "\0" in value
        ):
            raise ValidationError(
                f"Request must not contain absolute external paths, URLs, or path traversal at {breadcrumb}: {value!r}"
            )


def _validate_retained_request(request: dict[str, Any]) -> None:
    if not isinstance(request, dict):
        raise ValidationError("Request must be a JSON dictionary")
    for key in (
        "workflow_id",
        "revision",
        "asset_id",
        "execution_id",
        "attempt_number",
        "raw_glb_sha256",
        "processed_glb_sha256",
        "harness_sha256",
        "profile_sha256",
        "specification_sha256",
        "request_digest",
        "glb",
        "output_dir",
        "observation_path",
        "review_views",
        "spec",
        "profile",
    ):
        if key not in request:
            raise ValidationError(f"Request is missing required key: {key}")

    output_dir = request.get("output_dir")
    if not _is_safe_res_path(output_dir):
        raise ValidationError(
            f"Request output_dir must be a safe res:// directory reference: {output_dir}"
        )

    obs_path = request.get("observation_path")
    if not _is_safe_res_path(obs_path, allowed_extension=".json"):
        raise ValidationError(
            f"Request observation_path must be a safe res:// *.json reference: {obs_path}"
        )

    glb_path = request.get("glb")
    if not _is_safe_res_path(glb_path, allowed_extension=".glb"):
        raise ValidationError(f"Request glb must be a safe res:// *.glb reference: {glb_path}")

    _validate_request_portability(request)


def _promote_file_exclusively(destination: Path, content: bytes, label: str) -> None:
    """Promote an attempt artifact exclusively to the output directory with strict link and size guards."""
    _reject_reparse_components(destination.parent, f"{label} destination parent")
    _reject_reparse_components(destination, f"{label} destination")
    if destination.exists() or destination.is_symlink():
        raise ValidationError(
            f"{label} destination already exists; refusing overwrite: {destination}"
        )
    try:
        with destination.open("xb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
            fd_stat = os.fstat(stream.fileno())
    except FileExistsError as exc:
        raise ValidationError(
            f"{label} destination already exists; refusing overwrite: {destination}"
        ) from exc
    except OSError as exc:
        raise ValidationError(f"{label} could not be written safely: {exc}") from exc

    if not stat.S_ISREG(fd_stat.st_mode):
        raise ValidationError(
            f"{label} promoted file descriptor is not a regular file: {destination}"
        )
    if fd_stat.st_size != len(content):
        raise ValidationError(f"{label} promoted file descriptor size mismatch: {destination}")

    _reject_reparse_components(destination.parent, f"{label} promoted parent")
    _reject_reparse_components(destination, f"{label} promoted file")
    stat_info = destination.lstat()
    if not stat.S_ISREG(stat_info.st_mode) or destination.is_symlink():
        raise ValidationError(f"{label} promoted file is not a regular file: {destination}")
    if stat_info.st_size != len(content):
        raise ValidationError(f"{label} promoted file size mismatch: {destination}")
    if fd_stat.st_dev != 0 and fd_stat.st_ino != 0:
        if (stat_info.st_dev, stat_info.st_ino) != (fd_stat.st_dev, fd_stat.st_ino):
            raise ValidationError(f"{label} promoted file was replaced after write: {destination}")

    read_back = _read_bounded_regular_file(
        destination, limit=len(content), label=f"{label} promoted file"
    )
    if hashlib.sha256(read_back).hexdigest() != hashlib.sha256(content).hexdigest():
        raise ValidationError(f"{label} promoted file content hash mismatch: {destination}")


def verify_godot_character(
    spec: AssetSpecificationV07,
    profile: AssetProfileV07,
    raw_glb: Path | str,
    raw_glb_sha256: str,
    processed_glb: Path | str,
    processed_glb_sha256: str,
    execution_id: str,
    attempt_number: int = 1,
    workflow_id: str = "character-verification",
    revision: int = 1,
    output_dir: Path | str | None = None,
    godot_executable: Path | str | None = None,
    runner: ProcessRunner | Any | None = None,
    timeout_seconds: float = 90.0,
    raise_on_failure: bool = True,
) -> CharacterRuntimeResult:
    """Import, inspect, collide, frame, and capture one processed static character."""
    spec, profile, views = _validate_character_inputs(spec, profile)
    for label, value in (("execution_id", execution_id), ("workflow_id", workflow_id)):
        if not isinstance(value, str) or not _SAFE_COMPONENT.fullmatch(value):
            raise ValidationError(f"{label} must be a safe single path component")
    if (
        type(attempt_number) is not int
        or attempt_number < 1
        or type(revision) is not int
        or revision < 1
    ):
        raise ValidationError("attempt_number and revision must be positive JSON integers")
    if (
        type(timeout_seconds) not in {int, float}
        or not math.isfinite(float(timeout_seconds))
        or timeout_seconds <= 0
    ):
        raise ValidationError("timeout_seconds must be finite and positive")
    for label, digest in (
        ("raw_glb_sha256", raw_glb_sha256),
        ("processed_glb_sha256", processed_glb_sha256),
    ):
        if not isinstance(digest, str) or re.fullmatch(r"[0-9a-fA-F]{64}", digest) is None:
            raise ValidationError(f"{label} must be an explicit hexadecimal SHA-256 pin")

    raw_path, processed_path = Path(raw_glb), Path(processed_glb)
    raw_bytes = _read_regular_bounded(raw_path, "raw character GLB", _MAX_GLB_BYTES)
    actual_raw_hash = hashlib.sha256(raw_bytes).hexdigest()
    if actual_raw_hash != raw_glb_sha256.casefold():
        raise ValidationError("Raw character GLB differs from its explicit SHA-256 pin")
    _raw_facts(raw_bytes, spec, profile)
    processed_bytes = _read_regular_bounded(
        processed_path, "processed character GLB", _MAX_GLB_BYTES
    )
    actual_processed_hash = hashlib.sha256(processed_bytes).hexdigest()
    if actual_processed_hash != processed_glb_sha256.casefold():
        raise ValidationError("Processed character GLB differs from its explicit SHA-256 pin")
    facts = _actual_glb_facts(processed_bytes, spec, profile, processed_path)

    if output_dir is None:
        out_dir = Path(
            tempfile.mkdtemp(
                prefix=f"godot-character-{execution_id}-a{attempt_number}-{actual_processed_hash[:12]}-"
            )
        )
    else:
        out_dir = Path(output_dir)
        _reject_reparse_components(out_dir, "output_dir")
        if out_dir.exists():
            raise ValidationError(f"output_dir must be fresh and attempt-specific: {out_dir}")
        try:
            out_dir.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise ValidationError(f"output_dir parent must exist and be writable: {exc}") from exc

    harness_bytes = _harness_resource()
    harness_sha = hashlib.sha256(harness_bytes).hexdigest()
    spec_dict = spec.model_dump(mode="json")
    profile_dict = profile.document.model_dump(mode="json")
    spec_sha = _canonical_hash(spec_dict)
    profile_sha = _canonical_hash(profile_dict)
    promoted_obs_path = out_dir / "runtime-observation.json"
    promoted_request_path = out_dir / "runtime-request.json"

    # Select the engine only after all deterministic typed/hash/GLB validation.
    engine = GodotAdapter(runner=runner)
    godot_exe: str | None = None
    if godot_executable is not None:
        executable = Path(godot_executable).resolve()
        if not executable.is_file():
            raise ToolExecutionError(f"Specified Godot executable not found: {godot_executable}")
        godot_exe = str(executable)
    else:
        env_exe = os.environ.get("GAMEFACTORY_TEST_GODOT") or os.environ.get(
            "GAMEFACTORY_GODOT_PATH"
        )
        if env_exe and Path(env_exe).is_file():
            godot_exe = str(Path(env_exe).resolve())
        else:
            godot_exe = engine.find_candidate_executable()
    if godot_exe is None:
        raise ToolExecutionError("Godot executable not found; configure GAMEFACTORY_TEST_GODOT")

    stage = out_dir / ".stage"
    _reject_reparse_components(stage, "stage")
    stage.mkdir(exist_ok=False)
    captures_stage_dir = stage / "captures"
    _reject_reparse_components(captures_stage_dir, "captures stage directory")
    captures_stage_dir.mkdir(exist_ok=False)
    staged_obs_path = stage / "runtime-observation.json"
    (stage / "project.godot").write_text(
        'config_version=5\n\n[importer_defaults]\n\nscene={\n"nodes/use_name_suffixes": false,\n"nodes/use_node_type_suffixes": false\n}\n',
        encoding="utf-8",
    )
    (stage / ".factory-character-harness.gd").write_bytes(harness_bytes)
    staged_glb = stage / "character.glb"
    with staged_glb.open("xb") as stream:
        stream.write(processed_bytes)
    if (
        _read_regular_bounded(staged_glb, "staged processed character GLB", _MAX_GLB_BYTES)
        != processed_bytes
    ):
        raise ValidationError("Staged processed character changed before Godot dispatch")
    request_payload: dict[str, Any] = {
        "schema_version": REQUEST_SCHEMA_VERSION,
        "workflow_id": workflow_id,
        "revision": revision,
        "asset_id": spec.asset_id,
        "execution_id": execution_id,
        "attempt_number": attempt_number,
        "raw_glb_sha256": actual_raw_hash,
        "processed_glb_sha256": actual_processed_hash,
        "harness_sha256": harness_sha,
        "profile_sha256": profile_sha,
        "specification_sha256": spec_sha,
        "glb": "res://character.glb",
        "output_dir": "res://captures",
        "observation_path": "res://runtime-observation.json",
        "review_views": views,
        "spec": {
            "dimensions": spec_dict["dimensions"],
            "origin_policy": spec.origin_policy,
            "collider": spec_dict["collider"],
            "lod_policy": spec.lod_policy,
        },
        "profile": {
            "profile_id": profile.profile_id,
            "version": profile.version,
            "geometry_mode": profile.geometry_mode,
            "lod1_required": facts["lod1_required"],
            "framing": profile_dict["framing"],
        },
    }
    request_payload["request_digest"] = _canonical_hash(request_payload)
    _validate_retained_request(request_payload)
    request_bytes = _bounded_json(request_payload, "Godot character runtime request", 128 * 1024)
    request_path = stage / "runtime-request.json"
    with request_path.open("xb") as stream:
        stream.write(request_bytes)

    if (
        _read_regular_bounded(
            stage / ".factory-character-harness.gd",
            "staged Godot character harness",
            _MAX_JSON_BYTES,
        )
        != harness_bytes
    ):
        raise ValidationError("Packaged Godot character harness changed before dispatch")
    if (
        _read_regular_bounded(request_path, "staged Godot character request", 128 * 1024)
        != request_bytes
    ):
        raise ValidationError("Godot character runtime request changed before dispatch")

    env = _isolated_environment(stage / "scratch")
    proc_runner = runner or ProcessRunner(sanitize_output=True)
    import_result = proc_runner.run(
        CommandRequest(
            args=[godot_exe, "--headless", "--path", str(stage), "--import"],
            cwd=stage,
            env_overrides=env,
            timeout_seconds=min(float(timeout_seconds), 60.0),
            minimal_env=True,
        )
    )
    if import_result.exit_code != 0:
        raise ToolExecutionError(
            "Godot character project import failed",
            exit_code=import_result.exit_code,
            stderr=import_result.stderr,
            details={"stdout": import_result.stdout, "stderr": import_result.stderr},
        )
    runtime_result = proc_runner.run(
        CommandRequest(
            args=[
                godot_exe,
                "--path",
                str(stage),
                "--display-driver",
                _display_driver(),
                "--rendering-driver",
                "opengl3",
                "--rendering-method",
                "gl_compatibility",
                "--audio-driver",
                "Dummy",
                "--windowed",
                "--resolution",
                "1280x720",
                "--script",
                "res://.factory-character-harness.gd",
                "--",
                "--request",
                "res://runtime-request.json",
            ],
            cwd=stage,
            env_overrides=env,
            timeout_seconds=float(timeout_seconds),
            minimal_env=True,
        )
    )
    if (
        _read_regular_bounded(
            stage / ".factory-character-harness.gd",
            "staged Godot character harness",
            _MAX_JSON_BYTES,
        )
        != harness_bytes
    ):
        raise ValidationError("Packaged Godot character harness changed during runtime")
    if (
        _read_regular_bounded(staged_glb, "staged processed character GLB", _MAX_GLB_BYTES)
        != processed_bytes
    ):
        raise ValidationError("Staged processed character changed during runtime")
    if (
        _read_regular_bounded(request_path, "staged Godot character request", 128 * 1024)
        != request_bytes
    ):
        raise ValidationError("Godot character runtime request changed during runtime")
    if not staged_obs_path.exists():
        raise ToolExecutionError(
            "Godot character runtime exited without an observation",
            exit_code=runtime_result.exit_code,
            stderr=runtime_result.stderr,
            details={"stdout": runtime_result.stdout, "stderr": runtime_result.stderr},
        )
    observation_bytes = _read_bounded_regular_file(
        staged_obs_path, limit=_MAX_JSON_BYTES, label="Godot character observation"
    )
    observation = _strict_json(observation_bytes, "Godot character observation")
    if observation.get("schema_version") != OBSERVATION_SCHEMA_VERSION:
        raise ValidationError("Godot character observation schema version mismatch")
    _validate_observation(
        observation, spec=spec, profile=profile, views=views, request=request_payload, facts=facts
    )

    base_artifacts = {
        "runtime-request.json": request_bytes,
        "runtime-observation.json": observation_bytes,
        "character_runtime_harness_v07.gd": harness_bytes,
    }
    _promote_file_exclusively(promoted_request_path, request_bytes, "runtime request")
    _promote_file_exclusively(promoted_obs_path, observation_bytes, "runtime observation")

    failures = [str(item) for item in observation.get("errors", [])]
    if runtime_result.exit_code != 0 or observation.get("status") != "PASS" or failures:
        if raise_on_failure:
            raise ToolExecutionError(
                f"Godot character runtime verification failed: {failures}",
                exit_code=runtime_result.exit_code,
                stderr=runtime_result.stderr,
                details={"observation": observation, "stdout": runtime_result.stdout},
            )
        return CharacterRuntimeResult(
            "FAIL",
            observation,
            base_artifacts,
            failures,
            request_payload["request_digest"],
            actual_raw_hash,
            actual_processed_hash,
            {},
        )

    captures: dict[str, bytes] = {}
    for view in views:
        staged_capture = captures_stage_dir / f"{view}.png"
        if not staged_capture.exists():
            raise ToolExecutionError(f"Godot character capture is missing: {view}.png")
        png = _read_bounded_regular_file(
            staged_capture, limit=_MAX_CAPTURE_BYTES, label=f"{view} capture"
        )
        decoded = decode_png(png, 1280, 720)
        if hashlib.sha256(png).hexdigest() != observation["captures"][view]["sha256"]:
            raise ValidationError(f"Godot character capture digest mismatch for {view}")
        colors = decoded.image.convert("RGB").getcolors(maxcolors=2)
        if decoded.image.getbbox() is None or (colors is not None and len(colors) <= 1):
            raise ValidationError(f"Godot character capture is blank for {view}")
        captures[view] = png
        base_artifacts[f"{view}.png"] = png
        promoted_capture = out_dir / f"{view}.png"
        _promote_file_exclusively(promoted_capture, png, f"{view} capture")
    return CharacterRuntimeResult(
        "PASS",
        observation,
        base_artifacts,
        [],
        request_payload["request_digest"],
        actual_raw_hash,
        actual_processed_hash,
        captures,
    )
