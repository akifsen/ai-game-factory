"""Verify Godot skin deformation oracle payloads (ADR 0019 / V0.8-2)."""

from __future__ import annotations

import math
from typing import Any

from gamefactory.adapters.assets.internal_rig_canonical import runtime_request_digest
from gamefactory.adapters.assets.internal_skin_region import (
    REGION_BOUNDARY_TOLERANCE,
    strict_region_boundary_tolerance,
    vertex_inside_region_box,
)
from gamefactory.core.domain.internal_skin_contract import InternalSkinContract


class OracleObservationError(ValueError):
    """Oracle payload failed schema or measurement checks."""


def _finite(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise OracleObservationError(f"{label} must be numeric")
    out = float(value)
    if not math.isfinite(out):
        raise OracleObservationError(f"{label} must be finite")
    return out


def _recompute_displacement(
    payload: dict[str, Any],
    contract: InternalSkinContract,
    *,
    boundary_tolerance: float = REGION_BOUNDARY_TOLERANCE,
) -> tuple[float, float, int, int]:
    samples = payload.get("vertex_samples")
    if not isinstance(samples, list) or not samples:
        raise OracleObservationError("vertex_samples must be a non-empty array")
    oracle = contract.oracle
    max_aff = 0.0
    max_unaff = 0.0
    aff_count = 0
    unaff_count = 0
    affected = {"min": list(oracle.affected.min_xyz), "max": list(oracle.affected.max_xyz)}
    unaffected = {"min": list(oracle.unaffected.min_xyz), "max": list(oracle.unaffected.max_xyz)}
    for entry in samples:
        if not isinstance(entry, dict):
            raise OracleObservationError("vertex sample must be an object")
        rest = entry.get("rest_position")
        posed = entry.get("posed_position")
        if (
            not isinstance(rest, list)
            or not isinstance(posed, list)
            or len(rest) != 3
            or len(posed) != 3
        ):
            raise OracleObservationError("vertex sample positions must be length-3 arrays")
        rp = (float(rest[0]), float(rest[1]), float(rest[2]))
        pp = (float(posed[0]), float(posed[1]), float(posed[2]))
        delta = math.sqrt(sum((pp[i] - rp[i]) ** 2 for i in range(3)))
        if vertex_inside_region_box(rp, affected, boundary_tolerance=boundary_tolerance):
            aff_count += 1
            max_aff = max(max_aff, delta)
        if vertex_inside_region_box(rp, unaffected, boundary_tolerance=boundary_tolerance):
            unaff_count += 1
            max_unaff = max(max_unaff, delta)
    return max_aff, max_unaff, aff_count, unaff_count


def verify_oracle_payload(
    payload: dict[str, Any],
    contract: InternalSkinContract,
    *,
    expect_pass: bool,
    request: dict[str, Any] | None = None,
    glb_sha256: str | None = None,
    contract_sha256: str | None = None,
    harness_sha256: str | None = None,
) -> None:
    """Raise if process output does not match contract thresholds and schema."""
    import_exit = payload.get("import_exit_code")
    if import_exit is not None and import_exit != 0:
        raise OracleObservationError(f"Godot import must succeed, got exit {import_exit}")
    process_exit = payload.get("process_exit_code")
    if process_exit is None:
        raise OracleObservationError("process_exit_code is required")

    if payload.get("method") != "bake_mesh_from_current_skeleton_pose":
        raise OracleObservationError("oracle must report bake_mesh_from_current_skeleton_pose")
    if payload.get("max_affected_displacement") is None:
        reason = str(payload.get("reason", "unknown"))
        if process_exit != 0:
            raise OracleObservationError(
                f"oracle process failed without geometry observation: {reason}"
            )
        raise OracleObservationError(f"oracle did not produce geometry observation: {reason}")
    status = payload.get("status")
    if status not in {"PASS", "FAIL"}:
        raise OracleObservationError("status must be PASS or FAIL")

    for key in (
        "request_digest",
        "glb_sha256",
        "contract_sha256",
        "harness_sha256",
        "godot_version",
        "pose_bone",
        "observed_bone_transform",
        "vertex_samples",
    ):
        if payload.get(key) is None:
            raise OracleObservationError(f"observation missing binding field: {key}")

    boundary_tolerance = REGION_BOUNDARY_TOLERANCE
    if request is not None:
        expected_digest = runtime_request_digest(request)
        if payload.get("request_digest") != expected_digest:
            raise OracleObservationError("request_digest does not match bound request")
        boundary_tolerance = strict_region_boundary_tolerance(
            request.get("region_boundary_tolerance")
        )
        for key in ("vertex_count", "affected_vertex_count", "unaffected_vertex_count"):
            req_val = request.get(key)
            obs_val = payload.get(key)
            if not isinstance(req_val, int) or type(req_val) is bool:
                raise OracleObservationError(f"request {key} must be a strict integer")
            if obs_val != req_val:
                raise OracleObservationError(f"observation {key} does not match bound request")
    if glb_sha256 is not None and payload.get("glb_sha256") != glb_sha256:
        raise OracleObservationError("observation glb_sha256 mismatch")
    if contract_sha256 is not None and payload.get("contract_sha256") != contract_sha256:
        raise OracleObservationError("observation contract_sha256 mismatch")
    if harness_sha256 is not None and payload.get("harness_sha256") != harness_sha256:
        raise OracleObservationError("observation harness_sha256 mismatch")
    if payload.get("pose_bone") != contract.oracle.pose_bone:
        raise OracleObservationError("observation pose_bone mismatch")

    transform = payload.get("observed_bone_transform")
    if not isinstance(transform, dict) or transform.get("kind") not in {"basis", "quaternion"}:
        raise OracleObservationError("observed_bone_transform must declare basis or quaternion")

    if status == "PASS" and process_exit != 0:
        raise OracleObservationError("PASS payload requires process exit code 0")
    if status == "FAIL" and expect_pass:
        raise OracleObservationError(
            f"expected PASS observation, got FAIL: {payload.get('reason')}"
        )
    if status == "PASS" and not expect_pass:
        raise OracleObservationError("false-deformation case must not report PASS")

    max_aff, max_unaff, aff_count, unaff_count = _recompute_displacement(
        payload, contract, boundary_tolerance=boundary_tolerance
    )
    reported_aff = _finite(payload.get("max_affected_displacement"), "max_affected_displacement")
    reported_unaff = _finite(
        payload.get("max_unaffected_displacement"), "max_unaffected_displacement"
    )
    if abs(reported_aff - max_aff) > 1e-5 or abs(reported_unaff - max_unaff) > 1e-5:
        raise OracleObservationError("displacement metrics do not match vertex_samples")
    vcount = payload.get("vertex_count")
    if not isinstance(vcount, int) or vcount <= 0:
        raise OracleObservationError("vertex_count must be a positive integer")
    if payload.get("affected_vertex_count") != aff_count:
        raise OracleObservationError("affected_vertex_count does not match recomputation")
    if payload.get("unaffected_vertex_count") != unaff_count:
        raise OracleObservationError("unaffected_vertex_count does not match recomputation")

    oracle = contract.oracle
    measured_pass = (
        max_aff >= oracle.min_affected_displacement
        and max_aff <= oracle.max_affected_displacement
        and max_unaff <= oracle.max_unaffected_displacement
    )
    if measured_pass != (status == "PASS"):
        raise OracleObservationError("payload status does not match measured displacements")

    if expect_pass:
        if status != "PASS":
            raise OracleObservationError(
                f"expected PASS observation, got {status}: {payload.get('reason')}"
            )
        if process_exit != 0:
            raise OracleObservationError("positive oracle must exit 0")
    else:
        if status != "FAIL":
            raise OracleObservationError(
                "false-deformation case must observe FAIL from displacement"
            )
        if process_exit != 1:
            raise OracleObservationError(
                "semantic negative oracle must exit 1 with valid measurements"
            )
        reason = str(payload.get("reason", ""))
        if (
            "affected region did not move enough" not in reason
            and "displacement thresholds not met" not in reason
        ):
            raise OracleObservationError(f"unexpected FAIL reason: {reason}")
