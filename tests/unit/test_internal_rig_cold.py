"""Cold verifier tests for internal rig evidence bundles (V0.8-2)."""

from __future__ import annotations

import hashlib
import json
import math
import os
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.internal_rig_canonical import (
    PINNED_CONTRACT_CANONICAL_SHA256,
    VALIDATOR_CONTRACT_VERSION,
    contract_canonical_digest,
    reviewed_text_sha256,
    runtime_request_digest,
    sha256_bytes,
)
from gamefactory.adapters.assets.internal_rig_evidence import export_rig_evidence_bundle
from gamefactory.adapters.assets.internal_skin import validate_internal_skinned_glb
from gamefactory.adapters.assets.internal_skin_decode import decode_internal_skinned_glb
from gamefactory.adapters.assets.internal_skin_region import (
    REGION_BOUNDARY_TOLERANCE,
    vertex_inside_region_box,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import Variant, build_humanoid_skinned_glb
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract

REPO = Path(__file__).resolve().parents[2]
VERIFIER_CHECKOUT = REPO / "scripts" / "verify_rig_bundle.py"
VERIFIER = Path(
    str(resources.files("gamefactory.resources.scripts").joinpath("verify_rig_bundle.py"))
)
CONTRACT = load_internal_skin_contract()


def _digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


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


def _region_counts(decoded: object) -> tuple[int, int, list[dict], float, float]:
    oracle = CONTRACT.oracle
    affected = {
        "min": list(oracle.affected.min_xyz),
        "max": list(oracle.affected.max_xyz),
    }
    unaffected = {
        "min": list(oracle.unaffected.min_xyz),
        "max": list(oracle.unaffected.max_xyz),
    }

    samples: list[dict] = []
    max_aff = 0.0
    max_unaff = 0.0
    aff = 0
    unaff = 0
    for i, pos in enumerate(decoded.primitive.positions):
        rp = (float(pos[0]), float(pos[1]), float(pos[2]))
        in_aff = vertex_inside_region_box(rp, affected)
        in_unaff = vertex_inside_region_box(rp, unaffected)
        if in_aff:
            aff += 1
            posed = (rp[0] + 0.08, rp[1], rp[2])
            delta = 0.08
            max_aff = max(max_aff, delta)
        elif in_unaff:
            unaff += 1
            posed = rp
        else:
            posed = rp
        samples.append(
            {
                "vertex_index": i,
                "rest_position": [rp[0], rp[1], rp[2]],
                "posed_position": [posed[0], posed[1], posed[2]],
            }
        )
    return aff, unaff, samples, max_aff, max_unaff


def _fake_observation(
    glb_sha256: str,
    contract_sha256: str,
    harness_sha256: str,
    request: dict,
    decoded: object,
    *,
    status: str = "PASS",
) -> dict:
    aff, unaff, samples, max_aff, max_unaff = _region_counts(decoded)
    oracle = CONTRACT.oracle
    return {
        "schema_version": "rig-runtime-observation-0.8.0",
        "status": status,
        "reason": "displacement thresholds met"
        if status == "PASS"
        else "displacement thresholds not met",
        "method": "bake_mesh_from_current_skeleton_pose",
        "request_digest": runtime_request_digest(request),
        "glb_sha256": glb_sha256,
        "contract_sha256": contract_sha256,
        "harness_sha256": harness_sha256,
        "godot_version": "offline-fake",
        "pose_bone": oracle.pose_bone,
        "observed_bone_transform": {
            "kind": "basis",
            "basis": _axis_angle_basis(
                tuple(float(v) for v in oracle.rotation_axis),
                float(oracle.rotation_degrees),
            ),
        },
        "vertex_samples": samples,
        "max_affected_displacement": max_aff,
        "max_unaffected_displacement": max_unaff,
        "affected_vertex_count": aff,
        "unaffected_vertex_count": unaff,
        "vertex_count": len(decoded.primitive.positions),
    }


def _cold_failure_payload(out: str) -> dict:
    lines = out.strip().splitlines()
    assert lines[0] == "FAILED"
    return json.loads(lines[1])


def _cold_failure_reason(out: str) -> str:
    return str(_cold_failure_payload(out)["error"])


def _positive_validation_report(
    tmp_path: Path,
    glb_sha256: str,
    contract_bytes: bytes,
    contract_sha256: str,
) -> dict:
    pos_path = tmp_path / "_positive_ref.glb"
    pos_path.write_bytes(build_humanoid_skinned_glb("positive"))
    validation = validate_internal_skinned_glb(pos_path, CONTRACT)
    report = validation.to_dict()
    report["schema_version"] = "rig-validation-report-0.8.0"
    report["glb_sha256"] = glb_sha256
    report["contract_id"] = CONTRACT.contract_id
    report["validator_contract_version"] = VALIDATOR_CONTRACT_VERSION
    report["contract_canonical_sha256"] = contract_canonical_digest(contract_bytes)
    report["contract_sha256"] = contract_sha256
    report["status"] = "PASS"
    report["passed"] = True
    return report


def _build_offline_bundle(
    tmp_path: Path,
    variant: Variant = "positive",
    *,
    forge_semantic_pass_claims: bool = False,
) -> Path:
    glb = build_humanoid_skinned_glb(variant)
    glb_sha256 = _digest(glb)
    contract_path = Path(
        str(
            resources.files("gamefactory.resources.internal_skin").joinpath(
                "humanoid_12bone_contract.json"
            )
        )
    )
    contract_bytes = contract_path.read_bytes()
    contract_sha256 = _digest(contract_bytes)
    harness_path = Path(
        str(resources.files("gamefactory.resources.godot").joinpath("skin_deformation_harness.gd"))
    )
    harness_bytes = harness_path.read_bytes()
    harness_sha256, _ = reviewed_text_sha256(harness_path)
    blender_path = Path(
        str(
            resources.files("gamefactory.resources.blender").joinpath(
                "export_humanoid_12bone_fixture.py"
            )
        )
    )
    blender_sha256, _ = reviewed_text_sha256(blender_path)
    source_decl = Path(
        str(
            resources.files("gamefactory.resources.internal_rig").joinpath(
                "source_declaration.json"
            )
        )
    )
    glb_path = tmp_path / "x.glb"
    glb_path.write_bytes(glb)
    validation = validate_internal_skinned_glb(glb_path, CONTRACT)
    try:
        decoded = decode_internal_skinned_glb(glb_path)
    except Exception:
        decoded = None
    if forge_semantic_pass_claims and variant != "positive":
        report = _positive_validation_report(tmp_path, glb_sha256, contract_bytes, contract_sha256)
    else:
        report = validation.to_dict()
        report["schema_version"] = "rig-validation-report-0.8.0"
        report["glb_sha256"] = glb_sha256
        report["contract_id"] = CONTRACT.contract_id
        report["validator_contract_version"] = VALIDATOR_CONTRACT_VERSION
        report["contract_canonical_sha256"] = contract_canonical_digest(contract_bytes)
        report["contract_sha256"] = contract_sha256

    aff_count = 0
    unaff_count = 0
    vertex_total = 0
    if decoded is not None:
        vertex_total = len(decoded.primitive.positions)
        aff_count, unaff_count, _, _, _ = _region_counts(decoded)

    request = {
        "schema_version": "rig-runtime-request-0.8.0",
        "glb_sha256": glb_sha256,
        "contract_sha256": contract_sha256,
        "harness_sha256": harness_sha256,
        "godot_version": "offline-fake",
        "pose_bone": CONTRACT.oracle.pose_bone,
        "rotation_axis": list(CONTRACT.oracle.rotation_axis),
        "rotation_degrees": CONTRACT.oracle.rotation_degrees,
        "affected": {
            "min": list(CONTRACT.oracle.affected.min_xyz),
            "max": list(CONTRACT.oracle.affected.max_xyz),
        },
        "unaffected": {
            "min": list(CONTRACT.oracle.unaffected.min_xyz),
            "max": list(CONTRACT.oracle.unaffected.max_xyz),
        },
        "min_affected_displacement": CONTRACT.oracle.min_affected_displacement,
        "max_unaffected_displacement": CONTRACT.oracle.max_unaffected_displacement,
        "max_affected_displacement": CONTRACT.oracle.max_affected_displacement,
        "vertex_count": vertex_total,
        "affected_vertex_count": aff_count,
        "unaffected_vertex_count": unaff_count,
        "region_boundary_tolerance": REGION_BOUNDARY_TOLERANCE,
        "request_digest": "",
    }
    request["request_digest"] = runtime_request_digest(request)
    if forge_semantic_pass_claims and variant != "positive":
        obs_status = "PASS"
    else:
        obs_status = "PASS" if validation.status.value == "PASS" else "FAIL"
    obs_decoded = decoded
    if obs_decoded is None and forge_semantic_pass_claims and variant != "positive":
        pos_path = tmp_path / "_positive_ref_obs.glb"
        pos_path.write_bytes(build_humanoid_skinned_glb("positive"))
        obs_decoded = decode_internal_skinned_glb(pos_path)
    if obs_decoded is None:
        observation = {
            "schema_version": "rig-runtime-observation-0.8.0",
            "status": "FAIL",
            "reason": "parse failure fixture",
            "method": "bake_mesh_from_current_skeleton_pose",
            "request_digest": runtime_request_digest(request),
            "glb_sha256": glb_sha256,
            "contract_sha256": contract_sha256,
            "harness_sha256": harness_sha256,
            "godot_version": "offline-fake",
            "pose_bone": CONTRACT.oracle.pose_bone,
            "observed_bone_transform": {
                "kind": "basis",
                "basis": [[1, 0, 0], [0, 1, 0], [0, 0, 1]],
            },
            "vertex_samples": [
                {
                    "vertex_index": 0,
                    "rest_position": [0, 0, 0],
                    "posed_position": [0, 0, 0],
                }
            ],
            "max_affected_displacement": 0.0,
            "max_unaffected_displacement": 0.0,
            "affected_vertex_count": 0,
            "unaffected_vertex_count": 0,
            "vertex_count": 1,
        }
    else:
        observation = _fake_observation(
            glb_sha256,
            contract_sha256,
            harness_sha256,
            request,
            obs_decoded,
            status=obs_status,
        )

    bundle = tmp_path / "bundle"
    bundle.mkdir()
    paths = {
        "evidence/skinned.glb": glb,
        "evidence/rig_verification_contract.json": contract_bytes,
        "reviewed/export_humanoid_12bone_fixture.py": blender_path.read_bytes(),
        "reviewed/skin_deformation_harness.gd": harness_bytes,
        "evidence/source_declaration.json": source_decl.read_bytes(),
    }
    files: list[dict] = []
    for rel, raw in paths.items():
        dest = bundle / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(raw)
        files.append(
            {"path": rel, "role": _role_for(rel), "size": len(raw), "sha256": _digest(raw)}
        )
    for rel, payload in (
        ("evidence/rig_validation_report.json", report),
        ("evidence/rig_runtime_request.json", request),
        ("evidence/rig_runtime_observation.json", observation),
    ):
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        (bundle / rel).write_bytes(raw)
        files.append(
            {"path": rel, "role": _role_for(rel), "size": len(raw), "sha256": _digest(raw)}
        )
    manifest = {
        "schema_version": "rig-evidence-0.8.0",
        "bundle_id": "test",
        "contract_id": CONTRACT.contract_id,
        "glb_sha256": glb_sha256,
        "contract_sha256": contract_sha256,
        "harness_reviewed_sha256": harness_sha256,
        "blender_script_reviewed_sha256": blender_sha256,
        "validation_status": "PASS"
        if forge_semantic_pass_claims and variant != "positive"
        else report["status"],
        "runtime_status": "PASS"
        if forge_semantic_pass_claims and variant != "positive"
        else observation.get("status"),
        "runtime_request_digest": request["request_digest"],
        "files": sorted(files, key=lambda item: item["path"]),
    }
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    return bundle


def _role_for(rel: str) -> str:
    return {
        "evidence/skinned.glb": "skinned_glb",
        "evidence/rig_verification_contract.json": "rig_verification_contract",
        "evidence/rig_validation_report.json": "rig_validation_report",
        "evidence/rig_runtime_request.json": "rig_runtime_request",
        "evidence/rig_runtime_observation.json": "rig_runtime_observation",
        "reviewed/export_humanoid_12bone_fixture.py": "reviewed_blender_export_script",
        "reviewed/skin_deformation_harness.gd": "reviewed_godot_harness",
        "evidence/source_declaration.json": "source_declaration",
    }[rel]


def _run_cold(bundle: Path) -> tuple[int, str, str]:
    proc = subprocess.run(
        [sys.executable, "-I", str(VERIFIER), str(bundle)],
        capture_output=True,
        text=True,
        cwd=bundle.parent,
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_positive_offline_fake_observation_unauthenticated(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err
    lines = out.strip().splitlines()
    assert lines[0] == "CONSISTENT_BUT_UNAUTHENTICATED"
    payload = json.loads(lines[1])
    assert payload["outcome"] == "CONSISTENT_BUT_UNAUTHENTICATED"
    assert payload["integrity_outcome"] == "VERIFIED"
    assert payload["execution_provenance"] == "CONSISTENT_BUT_UNAUTHENTICATED"
    assert payload.get("pins_authenticated") is not True


def test_cold_verifier_matches_packaged_copy() -> None:
    assert VERIFIER.read_bytes() == VERIFIER_CHECKOUT.read_bytes()


def test_cold_runtime_request_digest_matches_package_for_nested_fields() -> None:
    oracle = CONTRACT.oracle
    request = {
        "schema_version": "rig-runtime-request-0.8.0",
        "glb_sha256": "a" * 64,
        "contract_sha256": "b" * 64,
        "harness_sha256": "c" * 64,
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
        "vertex_count": 16,
        "affected_vertex_count": 8,
        "unaffected_vertex_count": 8,
        "region_boundary_tolerance": REGION_BOUNDARY_TOLERANCE,
        "extra": {
            "region_boundary_tolerance": 2.0,
            "label": "pinned-1e-06",
            "ratio": 0.125,
        },
    }
    payload = json.dumps(request)
    code = f"""
import importlib.util
import json
import sys
from pathlib import Path

path = Path({repr(str(VERIFIER))})
spec = importlib.util.spec_from_file_location("verify_rig_bundle_cold", path)
mod = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules["verify_rig_bundle_cold"] = mod
spec.loader.exec_module(mod)
request = json.loads({repr(payload)})
print(mod._runtime_request_digest(request))
"""
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    cold_digest = completed.stdout.strip()
    assert cold_digest == runtime_request_digest(request)


def _assert_semantic_cold_failure(out: str, rule_id: str) -> None:
    reason = _cold_failure_reason(out)
    assert "manifest runtime_status must be PASS" not in reason
    assert "hash mismatch" not in reason.lower()
    semantic_markers = (
        "findings",
        "recomputed",
        "static validation",
        "InvalidGLB",
        "decode",
        "parse",
        "joint influence",
        "animations",
        "forbidden",
        "supported",
    )
    assert any(marker.lower() in reason.lower() for marker in semantic_markers)
    if rule_id != "internal_skin.parse":
        assert rule_id in reason or "findings" in reason or "static validation" in reason


@pytest.mark.parametrize(
    ("variant", "rule_id"),
    [
        ("weight_sum", "internal_skin.weights.normalized"),
        ("missing_bone", "internal_skin.topology"),
        ("inverse_bind_mismatch", "internal_skin.inverse_bind"),
        ("rest_mismatch", "internal_skin.rest_pose"),
        ("five_influences", "internal_skin.parse"),
        ("unweighted_vertex", "internal_skin.weights.coverage"),
        ("animation_present", "internal_skin.parse"),
        ("wrong_parent", "internal_skin.topology"),
    ],
)
def test_semantic_tamper_fails_cold(tmp_path: Path, variant: Variant, rule_id: str) -> None:
    bundle = _build_offline_bundle(tmp_path, variant, forge_semantic_pass_claims=True)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"
    _assert_semantic_cold_failure(out, rule_id)


def test_forged_validation_pass_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path, "weight_sum", forge_semantic_pass_claims=True)
    report_path = bundle / "evidence/rig_validation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["status"] = "PASS"
    report["passed"] = True
    raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    _refresh_manifest_file(bundle, "evidence/rig_validation_report.json", raw)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    _assert_semantic_cold_failure(out, "internal_skin.weights.normalized")


def test_manifest_hash_corruption_fails(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["sha256"] = "0" * 64
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    code, out, _ = _run_cold(bundle)
    assert code == 1


def test_path_traversal_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["path"] = "../escape.glb"
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    code, _, _ = _run_cold(bundle)
    assert code == 1


def test_self_consistent_wrong_contract_fails(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    contract_path = bundle / "evidence/rig_verification_contract.json"
    data = json.loads(contract_path.read_text(encoding="utf-8"))
    data["contract_id"] = "attacker_contract"
    raw = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()
    contract_path.write_bytes(raw)
    digest = sha256_bytes(raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == "evidence/rig_verification_contract.json":
            entry["sha256"] = digest
            entry["size"] = len(raw)
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    assert contract_canonical_digest(raw) != PINNED_CONTRACT_CANONICAL_SHA256
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_export_rig_evidence_offline(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = export_rig_evidence_bundle(glb, tmp_path / "exported")
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "rig-evidence-0.8.0"
    roles = {entry["role"] for entry in manifest["files"]}
    assert "skinned_glb" in roles
    assert "rig_validation_report" in roles
    assert "rig_runtime_request" not in roles


def test_bundled_verifier_script_never_executed(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    marker = tmp_path / "evil_marker.txt"
    evil = (
        "import pathlib\n"
        f"pathlib.Path({repr(str(marker))}).write_text('ran', encoding='utf-8')\n"
        "raise SystemExit(42)\n"
    )
    (bundle / "verify_rig_bundle.py").write_text(evil, encoding="utf-8")
    code, out, _ = _run_cold(bundle)
    assert not marker.exists()
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_invented_rest_position_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["vertex_samples"][0]["rest_position"] = [9.0, 9.0, 9.0]
    _rewrite_observation(bundle, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_wrong_observed_pose_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["observed_bone_transform"] = {"kind": "basis", "basis": [[1, 0, 0], [0, 1, 0], [0, 0, 1]]}
    _rewrite_observation(bundle, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1


def _rewrite_observation(bundle: Path, obs: dict) -> None:
    raw = (json.dumps(obs, sort_keys=True, indent=2) + "\n").encode()
    rel = "evidence/rig_runtime_observation.json"
    (bundle / rel).write_bytes(raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == rel:
            entry["sha256"] = _digest(raw)
            entry["size"] = len(raw)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _refresh_manifest_file(bundle: Path, rel: str, raw: bytes) -> None:
    (bundle / rel).write_bytes(raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["path"] == rel:
            entry["sha256"] = _digest(raw)
            entry["size"] = len(raw)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )


def _rehash_contract_binding_chain(
    bundle: Path,
    contract_raw: bytes,
    *,
    report: dict,
    request: dict,
    observation: dict,
) -> None:
    contract_sha256 = _digest(contract_raw)
    contract_canonical = contract_canonical_digest(contract_raw)
    report["contract_sha256"] = contract_sha256
    report["contract_canonical_sha256"] = contract_canonical
    request["contract_sha256"] = contract_sha256
    observation["contract_sha256"] = contract_sha256
    digest = runtime_request_digest(request)
    request["request_digest"] = digest
    observation["request_digest"] = digest
    _refresh_manifest_file(bundle, "evidence/rig_verification_contract.json", contract_raw)
    report_raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    _refresh_manifest_file(bundle, "evidence/rig_validation_report.json", report_raw)
    for rel, payload in (
        ("evidence/rig_runtime_request.json", request),
        ("evidence/rig_runtime_observation.json", observation),
    ):
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        _refresh_manifest_file(bundle, rel, raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["contract_sha256"] = contract_sha256
    manifest["runtime_request_digest"] = digest
    manifest["validation_status"] = report.get("status")
    _rewrite_manifest(bundle, manifest)


def test_export_rejects_preexisting_output(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = tmp_path / "exported"
    out.mkdir()
    (out / "keep.txt").write_text("stay", encoding="utf-8")
    with pytest.raises(ValueError, match="must not exist"):
        export_rig_evidence_bundle(glb, out)
    assert (out / "keep.txt").read_text(encoding="utf-8") == "stay"


def test_export_preserves_unrelated_legacy_stage_dir(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    out = tmp_path / "exported"
    legacy = tmp_path / ".exported.export-stage"
    legacy.mkdir()
    (legacy / "sentinel.txt").write_text("keep", encoding="utf-8")
    export_rig_evidence_bundle(glb, out)
    assert (legacy / "sentinel.txt").read_text(encoding="utf-8") == "keep"
    assert out.is_dir()


def test_export_rejects_weakened_custom_contract(tmp_path: Path) -> None:
    from dataclasses import replace

    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    weakened = replace(
        CONTRACT.oracle,
        max_affected_displacement=8.0,
    )
    custom = replace(CONTRACT, oracle=weakened)
    with pytest.raises(ValueError, match="must match frozen"):
        export_rig_evidence_bundle(glb, tmp_path / "out", contract=custom)


def test_runtime_status_fail_rejected_even_when_consistent(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["status"] = "FAIL"
    obs["reason"] = "displacement thresholds not met"
    obs["max_affected_displacement"] = 0.0
    _rewrite_observation(bundle, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_forged_pass_max_affected_displacement_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    sample = obs["vertex_samples"][0]
    rest = sample["rest_position"]
    sample["posed_position"] = [rest[0] + 1.0, rest[1], rest[2]]
    obs["max_affected_displacement"] = 1.0
    obs["status"] = "PASS"
    _rewrite_observation(bundle, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_forged_pass_status_with_measured_fail_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["status"] = "PASS"
    for sample in obs["vertex_samples"]:
        if sample["rest_position"] != sample["posed_position"]:
            sample["posed_position"] = list(sample["rest_position"])
    obs["max_affected_displacement"] = 0.0
    _rewrite_observation(bundle, obs)
    code, _, _ = _run_cold(bundle)
    assert code == 1


def test_weak_contract_pose_and_limits_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    report = json.loads(
        (bundle / "evidence/rig_validation_report.json").read_text(encoding="utf-8")
    )
    request = json.loads((bundle / "evidence/rig_runtime_request.json").read_text(encoding="utf-8"))
    observation = json.loads(
        (bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8")
    )
    data = json.loads(
        (bundle / "evidence/rig_verification_contract.json").read_text(encoding="utf-8")
    )
    data["rest_pose"] = "A"
    data["deformation_oracle"]["rotation_degrees"] = 8.0
    raw = (json.dumps(data, sort_keys=True, indent=2) + "\n").encode()
    report["status"] = "PASS"
    report["passed"] = True
    _rehash_contract_binding_chain(
        bundle,
        raw,
        report=report,
        request=request,
        observation=observation,
    )
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "pinned reviewed contract" in reason
    assert "hash mismatch" not in reason.lower()


def test_request_observation_pose_mismatch_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    req = json.loads((bundle / "evidence/rig_runtime_request.json").read_text(encoding="utf-8"))
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    req["rotation_degrees"] = float(req["rotation_degrees"]) + 5.0
    obs["observed_bone_transform"] = {
        "kind": "basis",
        "basis": _axis_angle_basis(
            tuple(float(v) for v in req["rotation_axis"]),
            float(req["rotation_degrees"]),
        ),
    }
    _rehash_runtime_bindings(bundle, req, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "rotation_degrees" in reason or "runtime request" in reason
    assert "hash mismatch" not in reason.lower()


def _rehash_reviewed_tool_chain(
    bundle: Path,
    rel: str,
    raw: bytes,
    *,
    manifest_key: str,
    request: dict,
    observation: dict,
) -> None:
    _refresh_manifest_file(bundle, rel, raw)
    reviewed_lf, _ = reviewed_text_sha256(bundle / rel)
    request["harness_sha256"] = reviewed_lf
    observation["harness_sha256"] = reviewed_lf
    _rehash_runtime_bindings(bundle, request, observation)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest[manifest_key] = reviewed_lf
    _rewrite_manifest(bundle, manifest)


def test_harness_semantic_text_tamper_rejects_pinned_reviewed_pin(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    rel = "reviewed/skin_deformation_harness.gd"
    raw = (bundle / rel).read_bytes() + b"\n# weakened semantic probe\n"
    request = json.loads((bundle / "evidence/rig_runtime_request.json").read_text(encoding="utf-8"))
    observation = json.loads(
        (bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8")
    )
    _rehash_reviewed_tool_chain(
        bundle,
        rel,
        raw,
        manifest_key="harness_reviewed_sha256",
        request=request,
        observation=observation,
    )
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "pinned" in reason or "harness" in reason
    assert "hash mismatch" not in reason.lower()


def test_blender_semantic_text_tamper_rejects_pinned_reviewed_pin(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    rel = "reviewed/export_humanoid_12bone_fixture.py"
    raw = (bundle / rel).read_bytes() + b"\n# weakened export semantic probe\n"
    _refresh_manifest_file(bundle, rel, raw)
    blender_lf, _ = reviewed_text_sha256(bundle / rel)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["blender_script_reviewed_sha256"] = blender_lf
    _rewrite_manifest(bundle, manifest)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "pinned" in reason or "blender" in reason
    assert "hash mismatch" not in reason.lower()


def test_harness_crlf_bundle_pass_lf_reviewed_pin(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    rel = "reviewed/skin_deformation_harness.gd"
    raw = (bundle / rel).read_bytes().replace(b"\n", b"\r\n")
    _refresh_manifest_file(bundle, rel, raw)
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err


def test_manifest_oversize_declared_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["role"] == "skinned_glb":
            entry["size"] = 1
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    code, _, _ = _run_cold(bundle)
    assert code == 1


def test_manifest_unknown_role_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["role"] = "evil_role"
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    code, _, _ = _run_cold(bundle)
    assert code == 1


def test_duplicate_vertex_index_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    dup = dict(obs["vertex_samples"][0])
    dup["vertex_index"] = obs["vertex_samples"][1]["vertex_index"]
    obs["vertex_samples"].append(dup)
    obs["vertex_count"] = len(obs["vertex_samples"])
    _rewrite_observation(bundle, obs)
    code, _, _ = _run_cold(bundle)
    assert code == 1


def test_malformed_observation_string_metric_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["max_unaffected_displacement"] = "0.0"
    _rewrite_observation(bundle, obs)
    code, _, _ = _run_cold(bundle)
    assert code == 1


def test_malformed_manifest_role_list_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["role"] = ["skinned_glb"]
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    code, _, _ = _run_cold(bundle)
    assert code == 1


def test_observation_json_overflow_unknown_field_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    rel = "evidence/rig_runtime_observation.json"
    raw = (bundle / rel).read_bytes().decode("utf-8")
    raw = raw.rstrip()[:-1] + ', "ignored_overflow": 1e400}\n'
    _refresh_manifest_file(bundle, rel, raw.encode("utf-8"))
    code, out, err = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"
    assert "Traceback" not in out + err


def test_observation_null_metric_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["max_affected_displacement"] = None
    _rewrite_observation(bundle, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_observation_empty_basis_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["observed_bone_transform"] = {"kind": "basis", "basis": []}
    _rewrite_observation(bundle, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_observation_missing_metric_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    del obs["max_affected_displacement"]
    _rewrite_observation(bundle, obs)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"


def test_reordered_vertex_samples_accepted(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    obs = json.loads((bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8"))
    obs["vertex_samples"] = list(reversed(obs["vertex_samples"]))
    _rewrite_observation(bundle, obs)
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err


def test_blender_crlf_bundle_pass_lf_reviewed_pin(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    rel = "reviewed/export_humanoid_12bone_fixture.py"
    raw = (bundle / rel).read_bytes().replace(b"\n", b"\r\n")
    _refresh_manifest_file(bundle, rel, raw)
    code, out, err = _run_cold(bundle)
    assert code == 0, out + err


def _rewrite_manifest(bundle: Path, manifest: dict) -> None:
    raw = (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode()
    (bundle / "manifest.json").write_bytes(raw)


def _rehash_runtime_bindings(bundle: Path, request: dict, observation: dict) -> None:
    digest = runtime_request_digest(request)
    request["request_digest"] = digest
    observation["request_digest"] = digest
    for rel, payload in (
        ("evidence/rig_runtime_request.json", request),
        ("evidence/rig_runtime_observation.json", observation),
    ):
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        _refresh_manifest_file(bundle, rel, raw)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["runtime_request_digest"] = digest
    _rewrite_manifest(bundle, manifest)


@pytest.mark.parametrize("bundle_id", [7, "", None])
def test_manifest_bundle_id_strict_nonempty_rejected(tmp_path: Path, bundle_id: object) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["bundle_id"] = bundle_id
    _rewrite_manifest(bundle, manifest)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "bundle_id" in reason
    assert "hash mismatch" not in reason.lower()


@pytest.mark.parametrize("godot_version", [7, ""])
def test_runtime_godot_version_strict_string_rehash_rejected(
    tmp_path: Path, godot_version: object
) -> None:
    bundle = _build_offline_bundle(tmp_path)
    request = json.loads((bundle / "evidence/rig_runtime_request.json").read_text(encoding="utf-8"))
    observation = json.loads(
        (bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8")
    )
    request["godot_version"] = godot_version
    observation["godot_version"] = godot_version
    _rehash_runtime_bindings(bundle, request, observation)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "godot_version" in reason
    assert "hash mismatch" not in reason.lower()


def test_manifest_wrong_harness_reviewed_pin_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["harness_reviewed_sha256"] = "a" * 64
    _rewrite_manifest(bundle, manifest)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "harness_reviewed_sha256" in reason
    assert "hash mismatch" not in reason.lower()


def test_manifest_missing_runtime_status_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    del manifest["runtime_status"]
    _rewrite_manifest(bundle, manifest)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "runtime_status" in _cold_failure_reason(out)


@pytest.mark.parametrize("passed", ["true", False])
def test_validation_report_passed_strict_bool_rejected(tmp_path: Path, passed: object) -> None:
    bundle = _build_offline_bundle(tmp_path)
    report_path = bundle / "evidence/rig_validation_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["passed"] = passed
    raw = (json.dumps(report, sort_keys=True, indent=2) + "\n").encode()
    _refresh_manifest_file(bundle, "evidence/rig_validation_report.json", raw)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "passed" in _cold_failure_reason(out)


def test_manifest_file_entry_extra_key_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    manifest["files"][0]["extra"] = 1
    _rewrite_manifest(bundle, manifest)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert "unknown keys" in _cold_failure_reason(out)


def test_runtime_request_wrong_region_tolerance_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    request = json.loads((bundle / "evidence/rig_runtime_request.json").read_text(encoding="utf-8"))
    observation = json.loads(
        (bundle / "evidence/rig_runtime_observation.json").read_text(encoding="utf-8")
    )
    request["region_boundary_tolerance"] = 0.1
    for rel, payload in (
        ("evidence/rig_runtime_request.json", request),
        ("evidence/rig_runtime_observation.json", observation),
    ):
        raw = (json.dumps(payload, sort_keys=True, indent=2) + "\n").encode()
        _refresh_manifest_file(bundle, rel, raw)
    code, out, _ = _run_cold(bundle)
    assert code == 1
    reason = _cold_failure_reason(out)
    assert "region_boundary_tolerance" in reason


def test_export_rejects_invalid_bundle_id(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    with pytest.raises(ValueError, match="bundle_id"):
        export_rig_evidence_bundle(glb, tmp_path / "out", bundle_id="")


def test_manifest_path_symlink_rejected(tmp_path: Path) -> None:
    bundle = _build_offline_bundle(tmp_path)
    target = bundle / "evidence/skinned.glb"
    link = bundle / "evidence/skinned_link.glb"
    try:
        os.symlink(target, link)
    except OSError:
        pytest.skip("cannot create symlink on this platform")
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        if entry["role"] == "skinned_glb":
            entry["path"] = "evidence/skinned_link.glb"
    (bundle / "manifest.json").write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    code, out, _ = _run_cold(bundle)
    assert code == 1
    assert out.splitlines()[0] == "FAILED"
