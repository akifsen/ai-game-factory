"""Export-time oracle validation for internal rig evidence (V0.8-2)."""

from __future__ import annotations

import json
import math
from pathlib import Path
from unittest.mock import patch

import pytest

from gamefactory.adapters.assets.internal_rig_canonical import runtime_request_digest, sha256_bytes
from gamefactory.adapters.assets.internal_rig_evidence import export_rig_evidence_bundle
from gamefactory.adapters.assets.internal_skin_decode import decode_internal_skinned_glb
from gamefactory.adapters.assets.internal_skin_region import REGION_BOUNDARY_TOLERANCE
from gamefactory.adapters.engines.skin_deformation_oracle import _expected_region_populations
from gamefactory.adapters.engines.skin_oracle_verify import OracleObservationError
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract

CONTRACT = load_internal_skin_contract()


def _axis_angle_basis(axis: tuple[float, float, float], degrees: float) -> list[list[float]]:
    ax, ay, az = axis
    length = math.sqrt(ax * ax + ay * ay + az * az)
    ax, ay, az = ax / length, ay / length, az / length
    rad = math.radians(degrees)
    c, s = math.cos(rad), math.sin(rad)
    t = 1.0 - c
    return [
        [t * ax * ax + c, t * ax * ay - s * az, t * ax * az + s * ay],
        [t * ax * ay + s * az, t * ay * ay + c, t * ay * az - s * ax],
        [t * ax * az - s * ay, t * ay * az + s * ax, t * az * az + c],
    ]


def _mock_oracle(
    glb_path: Path,
    output_dir: Path,
    *,
    contract_sha256: str,
    harness_sha256: str,
    obs_overrides: dict | None = None,
    process_exit_code: int = 0,
) -> dict:
    glb_sha256 = sha256_bytes(glb_path.read_bytes())
    vertex_count, aff, unaff = _expected_region_populations(glb_path, CONTRACT)
    oracle = CONTRACT.oracle
    request = {
        "schema_version": "rig-runtime-request-0.8.0",
        "glb_sha256": glb_sha256,
        "contract_sha256": contract_sha256,
        "harness_sha256": harness_sha256,
        "godot_version": "4.7.2.stable.official.test",
        "pose_bone": oracle.pose_bone,
        "rotation_axis": list(oracle.rotation_axis),
        "rotation_degrees": oracle.rotation_degrees,
        "affected": {
            "min": list(oracle.affected.min_xyz),
            "max": list(oracle.affected.max_xyz),
        },
        "unaffected": {
            "min": list(oracle.unaffected.min_xyz),
            "max": list(oracle.unaffected.max_xyz),
        },
        "min_affected_displacement": oracle.min_affected_displacement,
        "max_unaffected_displacement": oracle.max_unaffected_displacement,
        "max_affected_displacement": oracle.max_affected_displacement,
        "vertex_count": vertex_count,
        "affected_vertex_count": aff,
        "unaffected_vertex_count": unaff,
        "region_boundary_tolerance": REGION_BOUNDARY_TOLERANCE,
    }
    request["request_digest"] = runtime_request_digest(request)
    stage = output_dir / "godot_skin_stage_test"
    stage.mkdir(parents=True, exist_ok=True)
    (stage / "request.json").write_text(
        json.dumps(
            {
                **request,
                "glb": "res://Main.tscn",
                "output_path": str(stage / "result.json"),
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    decoded = decode_internal_skinned_glb(glb_path)
    samples = []
    max_aff = 0.0
    max_unaff = 0.0
    for i, pos in enumerate(decoded.primitive.positions):
        rp = [float(pos[0]), float(pos[1]), float(pos[2])]
        in_aff = (
            oracle.affected.min_xyz[0] <= rp[0] <= oracle.affected.max_xyz[0]
            and oracle.affected.min_xyz[1] <= rp[1] <= oracle.affected.max_xyz[1]
            and oracle.affected.min_xyz[2] <= rp[2] <= oracle.affected.max_xyz[2]
        )
        if in_aff:
            posed = [rp[0] + 0.08, rp[1], rp[2]]
            max_aff = max(max_aff, 0.08)
        else:
            posed = rp
        samples.append({"vertex_index": i, "rest_position": rp, "posed_position": posed})
    basis = _axis_angle_basis(
        tuple(float(v) for v in oracle.rotation_axis),
        float(oracle.rotation_degrees),
    )
    payload: dict = {
        "schema_version": "rig-runtime-observation-0.8.0",
        "status": "PASS",
        "reason": "displacement thresholds met",
        "method": "bake_mesh_from_current_skeleton_pose",
        "request_digest": request["request_digest"],
        "glb_sha256": glb_sha256,
        "contract_sha256": contract_sha256,
        "harness_sha256": harness_sha256,
        "godot_version": request["godot_version"],
        "pose_bone": oracle.pose_bone,
        "observed_bone_transform": {"kind": "basis", "basis": basis},
        "vertex_samples": samples,
        "max_affected_displacement": max_aff,
        "max_unaffected_displacement": max_unaff,
        "vertex_count": vertex_count,
        "affected_vertex_count": aff,
        "unaffected_vertex_count": unaff,
        "process_exit_code": process_exit_code,
        "import_exit_code": 0,
        "stage_dir": str(stage),
    }
    if obs_overrides:
        payload.update(obs_overrides)
    return payload


def test_export_rejects_forged_oracle_pass_nonzero_exit(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = tmp_path / "bundle"

    def _fake_oracle(
        _godot: Path,
        glb_path: Path,
        _contract: object,
        output_dir: Path,
        *,
        contract_sha256: str | None = None,
        harness_sha256: str | None = None,
        **_kwargs: object,
    ) -> dict:
        return _mock_oracle(
            glb_path,
            output_dir,
            contract_sha256=contract_sha256 or "",
            harness_sha256=harness_sha256 or "",
            process_exit_code=1,
        )

    with patch(
        "gamefactory.adapters.assets.internal_rig_evidence.run_skin_deformation_oracle",
        side_effect=_fake_oracle,
    ):
        with pytest.raises(OracleObservationError):
            export_rig_evidence_bundle(glb, out, godot_executable=Path("godot"))
    assert not out.exists()


def test_export_rejects_bad_request_digest(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = tmp_path / "bundle"

    def _fake_oracle(
        _godot: Path,
        glb_path: Path,
        _contract: object,
        output_dir: Path,
        *,
        contract_sha256: str | None = None,
        harness_sha256: str | None = None,
        **_kwargs: object,
    ) -> dict:
        return _mock_oracle(
            glb_path,
            output_dir,
            contract_sha256=contract_sha256 or "",
            harness_sha256=harness_sha256 or "",
            obs_overrides={"request_digest": "0" * 64},
        )

    with patch(
        "gamefactory.adapters.assets.internal_rig_evidence.run_skin_deformation_oracle",
        side_effect=_fake_oracle,
    ):
        with pytest.raises(OracleObservationError):
            export_rig_evidence_bundle(glb, out, godot_executable=Path("godot"))
    assert not out.exists()


def test_export_rejects_forged_metrics(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = tmp_path / "bundle"

    def _fake_oracle(
        _godot: Path,
        glb_path: Path,
        _contract: object,
        output_dir: Path,
        *,
        contract_sha256: str | None = None,
        harness_sha256: str | None = None,
        **_kwargs: object,
    ) -> dict:
        return _mock_oracle(
            glb_path,
            output_dir,
            contract_sha256=contract_sha256 or "",
            harness_sha256=harness_sha256 or "",
            obs_overrides={"max_affected_displacement": 9.0},
        )

    with patch(
        "gamefactory.adapters.assets.internal_rig_evidence.run_skin_deformation_oracle",
        side_effect=_fake_oracle,
    ):
        with pytest.raises(OracleObservationError):
            export_rig_evidence_bundle(glb, out, godot_executable=Path("godot"))
    assert not out.exists()


def test_export_cold_rejects_bad_pose_before_publish(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = tmp_path / "bundle"

    def _fake_oracle(
        _godot: Path,
        glb_path: Path,
        _contract: object,
        output_dir: Path,
        *,
        contract_sha256: str | None = None,
        harness_sha256: str | None = None,
        **_kwargs: object,
    ) -> dict:
        payload = _mock_oracle(
            glb_path,
            output_dir,
            contract_sha256=contract_sha256 or "",
            harness_sha256=harness_sha256 or "",
        )
        payload["observed_bone_transform"] = {
            "kind": "basis",
            "basis": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
        }
        return payload

    with patch(
        "gamefactory.adapters.assets.internal_rig_evidence.run_skin_deformation_oracle",
        side_effect=_fake_oracle,
    ):
        with pytest.raises(ValueError, match="cold verification"):
            export_rig_evidence_bundle(glb, out, godot_executable=Path("godot"))
    assert not out.exists()
