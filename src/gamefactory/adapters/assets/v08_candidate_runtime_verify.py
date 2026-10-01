"""Independently verify V0.8-3B candidate Godot runtime observations."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Literal, TypedDict

from gamefactory.adapters.assets.internal_rig_canonical import reviewed_text_sha256
from gamefactory.adapters.assets.v08_candidate_geometry import (
    capsule_center_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.v08_candidate_rules import decode_candidate_glb
from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    candidate_runtime_bound_payload_canonical,
    candidate_runtime_request_digest,
    candidate_runtime_rest_aabb_canonical_sha256,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import assert_no_link_in_path
from gamefactory.adapters.assets.v08_candidate_runtime_pins import (
    PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256,
    PINNED_PACKAGED_PROFILE_DOCUMENT_HASH,
    PINNED_RUNTIME_CONTRACT_SHA256,
    assert_packaged_harness_path,
)
from gamefactory.adapters.assets.v08_candidate_validate import validate_v08_candidate_glb
from gamefactory.adapters.engines.godot_image import ImageValidationError, decode_png
from gamefactory.core.domain.camera_framing import (
    BoundsAABB,
    framing_geometry,
    framing_metrics_from_projected_rect,
    view_axis_label,
)
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetProfileV08Candidate,
    AssetSpecificationV08Candidate,
    candidate_spec_fingerprint,
    load_packaged_candidate_profile,
    profile_document_hash,
    revalidate_candidate_binding,
)
from gamefactory.core.domain.v08_candidate_runtime_contract import (
    RUNTIME_OBSERVATION_SCHEMA,
    RUNTIME_REQUEST_SCHEMA,
    load_packaged_candidate_runtime_contract,
    runtime_contract_sha256,
)
from gamefactory.core.domain.v08_candidate_runtime_models import (
    CandidateRuntimeObservation,
    CandidateRuntimeRequestBound,
)


class CandidateRuntimeObservationError(ValueError):
    """Godot candidate runtime observation failed independent verification."""


class CandidateRuntimeVerificationSummary(TypedDict):
    integrity_outcome: Literal["VERIFIED"]
    execution_provenance: Literal["CONSISTENT_BUT_UNAUTHENTICATED"]
    bindings: Literal["VERIFIED"]


_AABB_TOLERANCE = 2e-3
_CENTER_TOLERANCE = 1e-4
_FRAMING_DISTANCE_TOLERANCE = 5e-3
_FILL_TOLERANCE = 1e-3
_FRAMING_NUMERIC_TOLERANCE = 1e-3
_CENTER_OFFSET_TOLERANCE = 1e-3
# Godot reports pixel rects; independent framing_geometry is continuous. Fake and Blender
# positives stay within one pixel per axis at 1280x720; allow two pixels so quantization
# does not mask a broad geometry mismatch (e.g. fill 0.56 vs ~0.65 at correct distance).
_PROJECTED_RECT_PIXEL_SLACK = 2.0
_MAX_CAPTURE_PNG_BYTES = 8 * 1024 * 1024


def _projected_rect_fraction_tolerance(viewport: tuple[int, int]) -> tuple[float, float]:
    width, height = viewport
    if width <= 0 or height <= 0:
        raise ValueError("viewport size must be positive")
    return (_PROJECTED_RECT_PIXEL_SLACK / float(width), _PROJECTED_RECT_PIXEL_SLACK / float(height))


def assert_png_nonblank(raw: bytes, width: int, height: int) -> None:
    """Conservative non-blank check after full decode."""
    decoded = decode_png(raw, width, height)
    image = decoded.image.convert("L")
    histogram = image.histogram()
    if not histogram or sum(histogram) == 0:
        raise ImageValidationError("image has no pixels")
    nonzero = [count for count in histogram if count > 0]
    if len(nonzero) < 2:
        raise ImageValidationError("capture appears blank or uniform")
    if max(histogram) >= sum(histogram) - 1:
        raise ImageValidationError("capture appears blank or uniform")


def _parse_bound_request(bound_request: dict[str, Any]) -> CandidateRuntimeRequestBound:
    payload = {
        key: value
        for key, value in bound_request.items()
        if key not in {"bound_payload_canonical", "glb", "capture_dir", "observation_path"}
    }
    try:
        return CandidateRuntimeRequestBound.model_validate(payload)
    except Exception as exc:
        raise CandidateRuntimeObservationError(f"bound request schema rejected: {exc}") from exc


def _parse_observation(observation: dict[str, Any]) -> CandidateRuntimeObservation:
    try:
        return CandidateRuntimeObservation.model_validate(observation)
    except Exception as exc:
        raise CandidateRuntimeObservationError(f"observation schema rejected: {exc}") from exc


def _trusted_identity_checks(
    bound: CandidateRuntimeRequestBound,
    *,
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
    harness_path: Path,
) -> None:
    try:
        assert_packaged_harness_path(harness_path)
    except ValueError as exc:
        raise CandidateRuntimeObservationError(str(exc)) from exc
    packaged_profile = load_packaged_candidate_profile()
    packaged_hash = profile_document_hash(packaged_profile.document)
    if packaged_hash != PINNED_PACKAGED_PROFILE_DOCUMENT_HASH:
        raise CandidateRuntimeObservationError(
            "packaged profile digest does not match reviewed pin"
        )
    if profile_document_hash(profile.document) != packaged_hash:
        raise CandidateRuntimeObservationError(
            "candidate runtime verification requires the reviewed packaged profile"
        )
    spec, profile = revalidate_candidate_binding(spec, profile)
    contract = load_packaged_candidate_runtime_contract()
    expected_spec_fp = candidate_spec_fingerprint(spec)
    if runtime_contract_sha256(contract) != PINNED_RUNTIME_CONTRACT_SHA256:
        raise CandidateRuntimeObservationError(
            "packaged runtime contract digest does not match reviewed pin"
        )
    harness_reviewed, harness_raw = reviewed_text_sha256(harness_path)
    if harness_reviewed != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise CandidateRuntimeObservationError(
            "candidate harness digest does not match reviewed pin"
        )
    if bound.profile_document_hash != PINNED_PACKAGED_PROFILE_DOCUMENT_HASH:
        raise CandidateRuntimeObservationError("profile_document_hash is not trusted")
    if bound.spec_fingerprint != expected_spec_fp:
        raise CandidateRuntimeObservationError("spec_fingerprint is not trusted")
    if bound.runtime_contract_sha256 != PINNED_RUNTIME_CONTRACT_SHA256:
        raise CandidateRuntimeObservationError("runtime_contract_sha256 is not trusted")
    if bound.harness_sha256 != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise CandidateRuntimeObservationError("harness reviewed hash is not trusted")
    if bound.asset_id != spec.asset_id:
        raise CandidateRuntimeObservationError("asset_id does not match specification")
    if bound.visual_mesh_name != spec.visual_mesh_name:
        raise CandidateRuntimeObservationError("visual_mesh_name does not match specification")
    capsule = spec.collider.capsule
    if capsule is None:
        raise CandidateRuntimeObservationError("specification collider.capsule is required")
    if abs(bound.capsule.radius_m - float(capsule.radius_m)) > 1e-5:
        raise CandidateRuntimeObservationError("bound capsule radius does not match specification")
    if abs(bound.capsule.height_m - float(capsule.height_m)) > 1e-5:
        raise CandidateRuntimeObservationError("bound capsule height does not match specification")
    if bound.processed_glb_sha256 != spec.processed_glb_sha256:
        raise CandidateRuntimeObservationError("processed_glb_sha256 does not match specification")


def _recompute_geometry_from_glb(
    glb_path: Path,
    *,
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
) -> tuple[dict[str, list[float]], list[float], str]:
    validation = validate_v08_candidate_glb(glb_path, spec, profile=profile)
    if validation.status.value != "PASS":
        raise CandidateRuntimeObservationError(
            "supplied GLB failed independent static validation during verification"
        )
    decoded = decode_candidate_glb(glb_path)
    if decoded.sha256 != spec.processed_glb_sha256:
        raise CandidateRuntimeObservationError(
            "decoded GLB digest does not match specification processed_glb_sha256"
        )
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    center = capsule_center_from_aabb(mins, maxs)
    rest_aabb = {
        "min": [float(mins[0]), float(mins[1]), float(mins[2])],
        "max": [float(maxs[0]), float(maxs[1]), float(maxs[2])],
    }
    digest = candidate_runtime_rest_aabb_canonical_sha256(rest_aabb)
    return rest_aabb, [float(center[0]), float(center[1]), float(center[2])], digest


def _compare_rest_claims(
    bound: CandidateRuntimeRequestBound,
    recomputed_aabb: dict[str, list[float]],
    recomputed_center: list[float],
    recomputed_aabb_digest: str,
) -> None:
    bound_rest = {
        "min": list(bound.rest_aabb.min),
        "max": list(bound.rest_aabb.max),
    }
    bound_digest = candidate_runtime_rest_aabb_canonical_sha256(bound_rest)
    if bound.rest_aabb_canonical_sha256 != bound_digest:
        raise CandidateRuntimeObservationError(
            "rest_aabb_canonical_sha256 does not match bound rest_aabb claims"
        )
    if bound.rest_aabb_canonical_sha256 != recomputed_aabb_digest:
        raise CandidateRuntimeObservationError("rest_aabb_canonical_sha256 is not GLB-derived")
    for axis in ("min", "max"):
        bound_vals = getattr(bound.rest_aabb, axis)
        observed = recomputed_aabb[axis]
        if any(abs(bound_vals[i] - observed[i]) > _AABB_TOLERANCE for i in range(3)):
            raise CandidateRuntimeObservationError(
                "bound rest_aabb does not match GLB-derived rest geometry"
            )
    if any(bound.capsule_center_m[i] != recomputed_center[i] for i in range(3)):
        raise CandidateRuntimeObservationError(
            "bound capsule_center_m does not match GLB-derived midpoint"
        )
    for index in range(3):
        mins, maxs = bound.rest_aabb.min, bound.rest_aabb.max
        expected_mid = (mins[index] + maxs[index]) / 2.0
        if bound.capsule_center_m[index] != expected_mid:
            raise CandidateRuntimeObservationError(
                "bound capsule_center_m is not the rest AABB midpoint"
            )


def _validate_view_framing(
    bound: CandidateRuntimeRequestBound,
    observation: CandidateRuntimeObservation,
    contract: Any,
) -> None:
    mins = bound.rest_aabb.min
    maxs = bound.rest_aabb.max
    bounds = BoundsAABB(
        min_x=mins[0],
        min_y=mins[1],
        min_z=mins[2],
        max_x=maxs[0],
        max_y=maxs[1],
        max_z=maxs[2],
    )
    viewport = (contract.viewport.width, contract.viewport.height)
    framing = contract.framing
    for view in bound.nine_view_set:
        measured = observation.view_framing.get(view)
        if measured is None:
            raise CandidateRuntimeObservationError(f"missing framing for view {view}")
        try:
            expected_axis = view_axis_label(view)
        except ValueError as exc:
            raise CandidateRuntimeObservationError(str(exc)) from exc
        if measured.view_axis != expected_axis:
            raise CandidateRuntimeObservationError(f"view_axis mismatch for {view}")
        geom = framing_geometry(
            bounds,
            view,
            fov_degrees=framing.fov_degrees,
            target_screen_fraction=framing.target_screen_fraction,
            viewport=viewport,
        )
        if abs(measured.camera_distance - geom.distance) > _FRAMING_DISTANCE_TOLERANCE:
            raise CandidateRuntimeObservationError(
                f"camera_distance for {view} does not match independent framing geometry"
            )
        rect = measured.projected_rect_pixels
        vp_w = float(viewport[0])
        vp_h = float(viewport[1])
        horiz_tol, vert_tol = _projected_rect_fraction_tolerance(viewport)
        observed_vert = rect.height / vp_h
        observed_horiz = rect.width / vp_w
        if abs(observed_vert - geom.vertical_fraction) > vert_tol:
            raise CandidateRuntimeObservationError(
                f"projected_rect height fraction for {view} does not match independent framing geometry"
            )
        if abs(observed_horiz - geom.horizontal_fraction) > horiz_tol:
            raise CandidateRuntimeObservationError(
                f"projected_rect width fraction for {view} does not match independent framing geometry"
            )
        metrics = framing_metrics_from_projected_rect(
            x=rect.x,
            y=rect.y,
            width=rect.width,
            height=rect.height,
            viewport_width=float(viewport[0]),
            viewport_height=float(viewport[1]),
            margin_fraction=framing.margin_fraction,
        )
        if abs(measured.height_ratio - metrics.height_ratio) > _FRAMING_NUMERIC_TOLERANCE:
            raise CandidateRuntimeObservationError(f"height_ratio mismatch for {view}")
        if abs(measured.fill_ratio - metrics.fill_ratio) > _FRAMING_NUMERIC_TOLERANCE:
            raise CandidateRuntimeObservationError(f"fill_ratio mismatch for {view}")
        if abs(measured.center_offset - metrics.center_offset) > _CENTER_OFFSET_TOLERANCE:
            raise CandidateRuntimeObservationError(f"center_offset mismatch for {view}")
        if measured.horizontally_centered != metrics.horizontally_centered:
            raise CandidateRuntimeObservationError(
                f"horizontally_centered flag contradicts center_offset for {view}"
            )
        fill = metrics.fill_ratio
        if not metrics.inside_margin:
            raise CandidateRuntimeObservationError(f"projected bounds outside margin for {view}")
        if fill + _FILL_TOLERANCE < framing.min_screen_fraction:
            raise CandidateRuntimeObservationError(f"fill_ratio below policy for {view}")
        if fill - _FILL_TOLERANCE > framing.max_screen_fraction:
            raise CandidateRuntimeObservationError(f"fill_ratio above policy for {view}")
        policy_ok = (
            metrics.inside_margin
            and framing.min_screen_fraction <= fill <= framing.max_screen_fraction
            and metrics.horizontally_centered
        )
        if measured.ok != policy_ok:
            raise CandidateRuntimeObservationError(f"view {view} ok flag disagrees with metrics")
        if measured.ok is not True:
            raise CandidateRuntimeObservationError(f"view {view} framing not ok")


def verify_candidate_runtime_observation(
    observation: dict[str, Any],
    bound_request: dict[str, Any],
    *,
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
    capture_dir: Path,
    glb_path: Path,
    glb_bytes: bytes,
    harness_path: Path,
    import_exit_code: int = 0,
    process_exit_code: int = 0,
) -> CandidateRuntimeVerificationSummary:
    bound = _parse_bound_request(bound_request)
    parsed_observation = _parse_observation(observation)
    contract = load_packaged_candidate_runtime_contract()
    _trusted_identity_checks(bound, spec=spec, profile=profile, harness_path=harness_path)

    if bound_request.get("schema_version") != RUNTIME_REQUEST_SCHEMA:
        raise CandidateRuntimeObservationError("bound request schema_version mismatch")
    canonical = bound_request.get("bound_payload_canonical")
    if not isinstance(canonical, str) or not canonical:
        raise CandidateRuntimeObservationError("bound_payload_canonical is required")
    if canonical != candidate_runtime_bound_payload_canonical(bound_request):
        raise CandidateRuntimeObservationError("bound_payload_canonical is not canonical")
    expected_digest = candidate_runtime_request_digest(bound_request)
    if bound.request_digest != expected_digest:
        raise CandidateRuntimeObservationError("bound request digest is not self-consistent")
    if parsed_observation.schema_version != RUNTIME_OBSERVATION_SCHEMA:
        raise CandidateRuntimeObservationError("observation schema_version mismatch")
    if import_exit_code != 0:
        raise CandidateRuntimeObservationError(f"Godot import failed with exit {import_exit_code}")
    if process_exit_code != 0 and parsed_observation.status == "PASS":
        raise CandidateRuntimeObservationError("PASS observation with non-zero process exit")
    if parsed_observation.production_eligible is not False:
        raise CandidateRuntimeObservationError("production_eligible must be false")
    if parsed_observation.candidate_state != "CLOSED":
        raise CandidateRuntimeObservationError("candidate_state must be CLOSED")
    if parsed_observation.public_status != "UNSUPPORTED":
        raise CandidateRuntimeObservationError("public_status must be UNSUPPORTED")
    if parsed_observation.status == "PASS" and parsed_observation.errors:
        raise CandidateRuntimeObservationError("PASS observation must have empty errors")
    for key in (
        "workflow_id",
        "revision",
        "execution_id",
        "strict_attempt_number",
        "asset_id",
        "processed_glb_sha256",
        "godot_version",
    ):
        if getattr(parsed_observation, key) != getattr(bound, key):
            raise CandidateRuntimeObservationError(
                f"observation {key} does not match bound request"
            )
    if parsed_observation.status != "PASS":
        raise CandidateRuntimeObservationError(
            f"runtime observation status is {parsed_observation.status!r}"
        )
    harness_reviewed, harness_raw = reviewed_text_sha256(harness_path)
    if harness_reviewed != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise CandidateRuntimeObservationError(
            "candidate harness digest does not match reviewed pin"
        )
    if parsed_observation.harness_sha256 != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise CandidateRuntimeObservationError("harness reviewed hash mismatch")
    if parsed_observation.harness_sha256_raw != harness_raw:
        raise CandidateRuntimeObservationError("harness raw bytes hash mismatch")
    if parsed_observation.request_digest != expected_digest:
        raise CandidateRuntimeObservationError(
            "observation request_digest does not match recomputed digest"
        )
    if parsed_observation.observed_glb_sha256 != bound.processed_glb_sha256:
        raise CandidateRuntimeObservationError("observed GLB hash does not match request")
    glb_digest = hashlib.sha256(glb_bytes).hexdigest()
    if glb_digest != bound.processed_glb_sha256:
        raise CandidateRuntimeObservationError("supplied GLB bytes do not match request hash")
    if hashlib.sha256(glb_path.read_bytes()).hexdigest() != glb_digest:
        raise CandidateRuntimeObservationError("GLB path bytes disagree with supplied snapshot")

    recomputed_aabb, recomputed_center, recomputed_aabb_digest = _recompute_geometry_from_glb(
        glb_path, spec=spec, profile=profile
    )
    _compare_rest_claims(bound, recomputed_aabb, recomputed_center, recomputed_aabb_digest)
    if parsed_observation.rest_aabb_canonical_sha256 != recomputed_aabb_digest:
        raise CandidateRuntimeObservationError(
            "observation rest_aabb_canonical_sha256 is not GLB-derived"
        )

    if parsed_observation.visual_mesh_name != bound.visual_mesh_name:
        raise CandidateRuntimeObservationError("visual mesh name mismatch")
    if parsed_observation.collision_shape_class != contract.collision_shape_class:
        raise CandidateRuntimeObservationError("collision shape class mismatch")
    if parsed_observation.runtime_body_kind != contract.runtime_body_kind:
        raise CandidateRuntimeObservationError("runtime body kind mismatch")
    if parsed_observation.physics_ray_hit is not True:
        raise CandidateRuntimeObservationError("physics ray must hit the runtime capsule body")

    capsule = parsed_observation.capsule
    if abs(capsule.observed_radius_m - bound.capsule.radius_m) > 1e-5:
        raise CandidateRuntimeObservationError("observed capsule radius differs from request")
    if abs(capsule.observed_height_m - bound.capsule.height_m) > 1e-5:
        raise CandidateRuntimeObservationError("observed capsule height differs from request")
    if any(
        abs(capsule.center_m[i] - bound.capsule_center_m[i]) > _CENTER_TOLERANCE for i in range(3)
    ):
        raise CandidateRuntimeObservationError("observed capsule center differs from request")
    if capsule.shape_class != contract.collision_shape_class:
        raise CandidateRuntimeObservationError("capsule shape_class mismatch")

    rest_bounds = parsed_observation.rest_mesh_bounds
    for axis_name, bound_vals in (("min", bound.rest_aabb.min), ("max", bound.rest_aabb.max)):
        observed = getattr(rest_bounds, axis_name)
        if any(abs(observed[i] - bound_vals[i]) > _AABB_TOLERANCE for i in range(3)):
            raise CandidateRuntimeObservationError("rest mesh bounds differ from bound request")

    _validate_view_framing(bound, parsed_observation, contract)

    try:
        assert_no_link_in_path(capture_dir, label="capture_dir")
    except ValueError as exc:
        raise CandidateRuntimeObservationError(str(exc)) from exc

    width = int(contract.viewport.width)
    height = int(contract.viewport.height)
    if len(parsed_observation.captures) != len(bound.nine_view_set):
        raise CandidateRuntimeObservationError("captures list incomplete")
    seen_views: set[str] = set()
    for entry in parsed_observation.captures:
        view = entry.view
        if view in seen_views or view not in bound.nine_view_set:
            raise CandidateRuntimeObservationError(f"invalid or duplicate capture view {view}")
        seen_views.add(view)
        if entry.request_digest != expected_digest:
            raise CandidateRuntimeObservationError("capture request_digest mismatch")
        if entry.execution_id != bound.execution_id:
            raise CandidateRuntimeObservationError("capture execution_id mismatch")
        if entry.revision != bound.revision:
            raise CandidateRuntimeObservationError("capture revision mismatch")
        if entry.strict_attempt_number != bound.strict_attempt_number:
            raise CandidateRuntimeObservationError("capture strict_attempt_number mismatch")
        if entry.png_width != width or entry.png_height != height:
            raise CandidateRuntimeObservationError(f"capture dimensions mismatch for {view}")
        png_path = capture_dir / f"{view}.png"
        if not png_path.is_file():
            raise CandidateRuntimeObservationError(f"missing PNG for view {view}")
        try:
            assert_no_link_in_path(png_path, label="capture PNG path")
        except ValueError as exc:
            raise CandidateRuntimeObservationError(str(exc)) from exc
        png_size = png_path.stat().st_size
        if png_size <= 0 or png_size > _MAX_CAPTURE_PNG_BYTES:
            raise CandidateRuntimeObservationError(f"capture PNG size out of bounds for {view}")
        raw = png_path.read_bytes()
        decoded = decode_png(raw, width, height)
        if entry.png_sha256 != decoded.sha256:
            raise CandidateRuntimeObservationError(f"capture png_sha256 mismatch for {view}")
        assert_png_nonblank(raw, width, height)
    if seen_views != set(bound.nine_view_set):
        raise CandidateRuntimeObservationError("captures missing one or more required views")

    return {
        "integrity_outcome": "VERIFIED",
        "execution_provenance": "CONSISTENT_BUT_UNAUTHENTICATED",
        "bindings": "VERIFIED",
    }
