"""Unit tests for skin deformation oracle verification (ADR 0019)."""

from __future__ import annotations

import pytest

from gamefactory.adapters.engines.skin_oracle_verify import (
    OracleObservationError,
    verify_oracle_payload,
)
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract

CONTRACT = load_internal_skin_contract()


def _valid_payload(**overrides: object) -> dict:
    oracle = CONTRACT.oracle
    samples = [
        {
            "vertex_index": 0,
            "rest_position": list(oracle.affected.min_xyz),
            "posed_position": [
                oracle.affected.min_xyz[0] + 0.08,
                oracle.affected.min_xyz[1],
                oracle.affected.min_xyz[2],
            ],
        },
        {
            "vertex_index": 1,
            "rest_position": list(oracle.unaffected.min_xyz),
            "posed_position": list(oracle.unaffected.min_xyz),
        },
    ]
    base = {
        "method": "bake_mesh_from_current_skeleton_pose",
        "status": "PASS",
        "reason": "displacement thresholds met",
        "request_digest": "a" * 64,
        "glb_sha256": "b" * 64,
        "contract_sha256": "c" * 64,
        "harness_sha256": "d" * 64,
        "godot_version": "4.7.2.stable.official.test",
        "pose_bone": oracle.pose_bone,
        "observed_bone_transform": {"kind": "basis", "basis": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]},
        "vertex_samples": samples,
        "max_affected_displacement": 0.08,
        "max_unaffected_displacement": 0.0,
        "vertex_count": 16,
        "affected_vertex_count": 1,
        "unaffected_vertex_count": 1,
        "process_exit_code": 0,
        "import_exit_code": 0,
    }
    base.update(overrides)
    return base


def test_rejects_forged_pass_with_nonzero_exit() -> None:
    with pytest.raises(OracleObservationError, match="process exit"):
        verify_oracle_payload(_valid_payload(process_exit_code=1), CONTRACT, expect_pass=True)


def test_rejects_import_failure() -> None:
    with pytest.raises(OracleObservationError, match="import"):
        verify_oracle_payload(_valid_payload(import_exit_code=1), CONTRACT, expect_pass=True)


def test_rejects_missing_measurements() -> None:
    payload = _valid_payload()
    del payload["max_affected_displacement"]
    with pytest.raises(OracleObservationError, match="geometry observation"):
        verify_oracle_payload(payload, CONTRACT, expect_pass=True)


def test_accepts_semantic_negative() -> None:
    oracle = CONTRACT.oracle
    samples = [
        {
            "vertex_index": 0,
            "rest_position": list(oracle.affected.min_xyz),
            "posed_position": list(oracle.affected.min_xyz),
        },
        {
            "vertex_index": 1,
            "rest_position": list(oracle.unaffected.min_xyz),
            "posed_position": list(oracle.unaffected.min_xyz),
        },
    ]
    verify_oracle_payload(
        _valid_payload(
            status="FAIL",
            reason="affected region did not move enough for posed bone",
            max_affected_displacement=0.0,
            max_unaffected_displacement=0.0,
            affected_vertex_count=1,
            unaffected_vertex_count=1,
            vertex_samples=samples,
            process_exit_code=1,
        ),
        CONTRACT,
        expect_pass=False,
    )


def test_rejects_excessive_affected_displacement() -> None:
    oracle = CONTRACT.oracle
    samples = [
        {
            "vertex_index": 0,
            "rest_position": list(oracle.affected.min_xyz),
            "posed_position": [
                oracle.affected.min_xyz[0] + 1.0,
                oracle.affected.min_xyz[1],
                oracle.affected.min_xyz[2],
            ],
        },
        {
            "vertex_index": 1,
            "rest_position": list(oracle.unaffected.min_xyz),
            "posed_position": list(oracle.unaffected.min_xyz),
        },
    ]
    with pytest.raises(OracleObservationError, match="status"):
        verify_oracle_payload(
            _valid_payload(
                vertex_samples=samples,
                max_affected_displacement=1.0,
                vertex_count=16,
                affected_vertex_count=1,
                unaffected_vertex_count=1,
            ),
            CONTRACT,
            expect_pass=True,
        )


def test_rejects_malformed_status() -> None:
    with pytest.raises(OracleObservationError, match="status"):
        verify_oracle_payload(_valid_payload(status="MAYBE"), CONTRACT, expect_pass=True)
