"""Build bound V0.8-3B candidate runtime requests."""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.internal_rig_canonical import reviewed_text_sha256, sha256_bytes
from gamefactory.adapters.assets.v08_candidate_geometry import (
    capsule_center_from_aabb,
    visual_aabb_in_reference_root_frame,
)
from gamefactory.adapters.assets.v08_candidate_rules import decode_candidate_glb
from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    candidate_runtime_bound_payload_canonical,
    candidate_runtime_request_digest,
    candidate_runtime_rest_aabb_canonical_sha256,
    strict_runtime_int,
)
from gamefactory.adapters.assets.v08_candidate_runtime_pins import (
    PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256,
    PINNED_PACKAGED_PROFILE_DOCUMENT_HASH,
    PINNED_RUNTIME_CONTRACT_SHA256,
    assert_packaged_harness_path,
)
from gamefactory.adapters.assets.v08_candidate_validate import validate_v08_candidate_glb
from gamefactory.core.domain.asset_contracts import AssetValidationResult
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetProfileV08Candidate,
    AssetSpecificationV08Candidate,
    candidate_spec_fingerprint,
    load_packaged_candidate_profile,
    profile_document_hash,
    revalidate_candidate_binding,
)
from gamefactory.core.domain.v08_candidate_runtime_contract import (
    RUNTIME_REQUEST_SCHEMA,
    load_packaged_candidate_runtime_contract,
    runtime_contract_sha256,
)
from gamefactory.core.domain.v08_candidate_runtime_models import CandidateRuntimeRequestBound
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner


class CandidateRuntimeRequestError(ValueError):
    """Candidate runtime request cannot be built."""


def _godot_version(godot_executable: Path, proc: ProcessRunner) -> str:
    result = proc.run(
        CommandRequest(
            args=[str(godot_executable), "--version"],
            cwd=Path.cwd(),
            timeout_seconds=30.0,
            minimal_env=True,
        )
    )
    if result.timed_out:
        raise CandidateRuntimeRequestError("Godot --version timed out")
    if result.exit_code != 0:
        raise CandidateRuntimeRequestError(f"Godot --version failed with exit {result.exit_code}")
    line = (result.stdout or result.stderr or "").strip().splitlines()
    if not line:
        raise CandidateRuntimeRequestError("Godot --version produced no output")
    return line[0].strip()


def _require_pass_validation(
    glb_path: Path,
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
) -> AssetValidationResult:
    result = validate_v08_candidate_glb(glb_path, spec, profile=profile)
    if result.status.value != "PASS":
        raise CandidateRuntimeRequestError(
            "GLB preflight validation did not PASS; runtime stage is blocked"
        )
    return result


def build_bound_candidate_runtime_request(
    glb_path: Path,
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
    *,
    workflow_id: str,
    revision: int,
    execution_id: str,
    strict_attempt_number: int,
    godot_executable: Path,
    runner: ProcessRunner,
    harness_path: Path,
) -> dict[str, Any]:
    """Compute visual rest AABB, capsule center, and digest-bound request fields."""
    try:
        revision = strict_runtime_int(revision, "revision")
        strict_attempt_number = strict_runtime_int(strict_attempt_number, "strict_attempt_number")
    except Exception as exc:
        raise CandidateRuntimeRequestError(str(exc)) from exc
    try:
        assert_packaged_harness_path(harness_path)
    except ValueError as exc:
        raise CandidateRuntimeRequestError(str(exc)) from exc
    spec, profile = revalidate_candidate_binding(spec, profile)
    packaged_profile = load_packaged_candidate_profile()
    packaged_hash = profile_document_hash(packaged_profile.document)
    if packaged_hash != PINNED_PACKAGED_PROFILE_DOCUMENT_HASH:
        raise CandidateRuntimeRequestError("packaged profile digest does not match reviewed pin")
    if profile_document_hash(profile.document) != packaged_hash:
        raise CandidateRuntimeRequestError(
            "candidate runtime request requires the reviewed packaged profile"
        )
    glb_bytes = glb_path.read_bytes()
    glb_sha256 = sha256_bytes(glb_bytes)
    if spec.processed_glb_sha256 != glb_sha256:
        raise CandidateRuntimeRequestError("processed_glb_sha256 does not match GLB bytes")
    _require_pass_validation(glb_path, spec, profile)
    contract = load_packaged_candidate_runtime_contract()
    contract_hash = runtime_contract_sha256(contract)
    if contract_hash != PINNED_RUNTIME_CONTRACT_SHA256:
        raise CandidateRuntimeRequestError("packaged runtime contract digest does not match pin")
    harness_reviewed, _harness_raw = reviewed_text_sha256(harness_path)
    if harness_reviewed != PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256:
        raise CandidateRuntimeRequestError("candidate harness digest does not match reviewed pin")
    decoded = decode_candidate_glb(glb_path)
    if decoded.sha256 != glb_sha256:
        raise CandidateRuntimeRequestError("decoded GLB digest does not match file bytes")
    mins, maxs = visual_aabb_in_reference_root_frame(decoded)
    center = capsule_center_from_aabb(mins, maxs)
    for value in (*mins, *maxs, *center):
        if not math.isfinite(float(value)):
            raise CandidateRuntimeRequestError("rest AABB or capsule center is not finite")
    capsule = spec.collider.capsule
    if capsule is None:
        raise CandidateRuntimeRequestError("specification collider.capsule is required")
    radius_m = float(capsule.radius_m)
    height_m = float(capsule.height_m)
    if radius_m < float(contract.min_capsule_radius_m):
        raise CandidateRuntimeRequestError("capsule radius below frozen contract minimum")
    godot_version = _godot_version(godot_executable, runner)
    rest_aabb = {
        "min": [float(mins[0]), float(mins[1]), float(mins[2])],
        "max": [float(maxs[0]), float(maxs[1]), float(maxs[2])],
    }
    rest_aabb_canonical_sha256 = candidate_runtime_rest_aabb_canonical_sha256(rest_aabb)
    bound: dict[str, Any] = {
        "schema_version": RUNTIME_REQUEST_SCHEMA,
        "asset_id": spec.asset_id,
        "workflow_id": workflow_id,
        "revision": revision,
        "execution_id": execution_id,
        "strict_attempt_number": strict_attempt_number,
        "processed_glb_sha256": glb_sha256,
        "profile_document_hash": PINNED_PACKAGED_PROFILE_DOCUMENT_HASH,
        "spec_fingerprint": candidate_spec_fingerprint(spec),
        "runtime_contract_sha256": PINNED_RUNTIME_CONTRACT_SHA256,
        "visual_mesh_name": spec.visual_mesh_name,
        "rest_aabb": rest_aabb,
        "rest_aabb_canonical_sha256": rest_aabb_canonical_sha256,
        "capsule_center_m": [float(center[0]), float(center[1]), float(center[2])],
        "capsule": {"radius_m": radius_m, "height_m": height_m},
        "harness_sha256": PINNED_CANDIDATE_HARNESS_REVIEWED_SHA256,
        "nine_view_set": list(contract.nine_view_set),
        "framing": contract.framing.model_dump(mode="json"),
        "viewport": contract.viewport.model_dump(mode="json"),
        "renderer_profile": contract.renderer_profile,
        "godot_version": godot_version,
        "candidate_state": contract.candidate_state,
        "public_status": contract.public_status,
        "production_eligible": contract.production_eligible,
    }
    bound["bound_payload_canonical"] = candidate_runtime_bound_payload_canonical(bound)
    bound["request_digest"] = candidate_runtime_request_digest(bound)
    try:
        CandidateRuntimeRequestBound.model_validate(
            {key: value for key, value in bound.items() if key != "bound_payload_canonical"}
        )
    except Exception as exc:
        raise CandidateRuntimeRequestError(f"bound request schema rejected: {exc}") from exc
    decoded_again = decode_candidate_glb(glb_path)
    if decoded_again.sha256 != glb_sha256:
        raise CandidateRuntimeRequestError("decoded GLB digest changed on repeated read")
    return bound
