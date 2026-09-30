"""Focused tests for the isolated V0.7 cold gate and local exporter boundary."""

from __future__ import annotations

import hashlib
import json
import runpy
import struct
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path
from typing import Any

import pytest

import gamefactory.workflows.assembly_evidence as assembly_evidence
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.domain.errors import ArtifactError
from gamefactory.workflows.assembly_evidence import (
    EvidenceFile,
    export_local_assembly_evidence,
    verify_local_assembly_evidence_bundle,
)

ROOT = Path(__file__).resolve().parents[2]
SOURCE_VERIFIER = ROOT / "scripts" / "verify_asset_bundle.py"
PACKAGED_VERIFIER = (
    ROOT / "src" / "gamefactory" / "resources" / "scripts" / "verify_asset_bundle.py"
)
EXPORTED_FIXTURE = ROOT / "tests" / "fixtures" / "v07_local_assembly_evidence"


def _receipt(kind: str, *, status: str = "APPROVED") -> dict[str, Any]:
    task_id = f"task-{kind}"
    inputs: dict[str, Any] = {"scope": {"handler_context": {}}}
    fingerprint = compute_operation_hash(task_id, kind, inputs)
    return {
        "schema_version": "production-receipt-0.7.0",
        "workflow_id": "workflow-1",
        "revision": 1,
        "source_version": 1,
        "approval_type": kind,
        "status": status,
        "task_id": task_id,
        "inputs": inputs,
        "operation_hash": fingerprint,
        "fingerprint": fingerprint,
        "actor": "operator",
        "comment": "",
    }


def _binding() -> dict[str, Any]:
    return {
        "schema_version": "asset-evidence-0.7.0",
        "workflow_id": "workflow-1",
        "revision": 1,
        "source_version": 1,
        "asset_id": "prop_evidence_test",
        "generation_mode": "local_operator_assembly",
        "paid": False,
        "product_ready": False,
        "review_views": [
            "front",
            "rear",
            "left",
            "right",
            "side",
            "three_quarter",
            "three_quarter_front",
            "three_quarter_rear",
            "top",
        ],
    }


def test_cold_v07_dispatch_isolated_outside_project(tmp_path: Path) -> None:
    assert SOURCE_VERIFIER.read_bytes() == PACKAGED_VERIFIER.read_bytes()
    bundle = tmp_path / "unsupported"
    bundle.mkdir()
    (bundle / "manifest.json").write_text(
        json.dumps({"schema_version": "asset-evidence-0.7.0", "paid": True}),
        encoding="utf-8",
    )
    for verifier in (SOURCE_VERIFIER, PACKAGED_VERIFIER):
        result = subprocess.run(
            [sys.executable, "-I", str(verifier), str(bundle)],
            cwd=Path(tempfile.gettempdir()),
            capture_output=True,
            check=False,
            text=True,
            timeout=30,
        )
        assert result.returncode == 1
        assert json.loads(result.stdout)["status"] == "FAIL"


def test_cold_gate_uses_packaged_verifier_without_a_checkout_script(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert SOURCE_VERIFIER.read_bytes() == PACKAGED_VERIFIER.read_bytes()
    fake_installed_module = (
        tmp_path / "site-packages" / "gamefactory" / "workflows" / "assembly_evidence.py"
    )
    monkeypatch.setattr(assembly_evidence, "__file__", str(fake_installed_module))
    report = assembly_evidence._cold_gate(EXPORTED_FIXTURE)
    assert report["status"] == "PASS"
    assert report["product_ready"] is True


def _cold_result(verifier: Path, bundle: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-I", str(verifier), str(bundle)],
        cwd=Path(tempfile.gettempdir()),
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )


def _rehash_role(bundle: Path, role: str, payload: bytes) -> None:
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["role"] == role)
    target = bundle / row["path"]
    target.write_bytes(payload)
    row["sha256"] = hashlib.sha256(payload).hexdigest()
    row["size"] = len(payload)
    manifest_path.write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _role_document(bundle: Path, role: str) -> tuple[dict[str, Any], dict[str, Any]]:
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["role"] == role)
    return manifest, json.loads((bundle / row["path"]).read_text(encoding="utf-8"))


def _write_role_document(
    bundle: Path, manifest: dict[str, Any], role: str, document: dict[str, Any]
) -> str:
    row = next(item for item in manifest["files"] if item["role"] == role)
    payload = (json.dumps(document, sort_keys=True, indent=2, ensure_ascii=False) + "\n").encode()
    (bundle / row["path"]).write_bytes(payload)
    row["sha256"] = hashlib.sha256(payload).hexdigest()
    row["size"] = len(payload)
    return row["sha256"]


def _rebind_runtime_observation(bundle: Path) -> None:
    """Rehash every internal runtime/final/production receipt link after a test mutation."""
    manifest, observation = _role_document(bundle, "runtime_observation")
    capture_hashes = {
        row["view"]: row["sha256"] for row in manifest["files"] if row["role"] == "runtime_capture"
    }
    manifest["capture_sha256"] = capture_hashes
    observation["captures"] = {view: {"sha256": digest} for view, digest in capture_hashes.items()}
    observation_hash = _write_role_document(bundle, manifest, "runtime_observation", observation)
    _manifest, runtime_index = _role_document(bundle, "runtime_index")
    runtime_index["observation"] = observation
    runtime_index["capture_sha256"] = capture_hashes
    runtime_index["runtime_observation_sha256"] = observation_hash
    runtime_index_hash = _write_role_document(bundle, manifest, "runtime_index", runtime_index)
    manifest["runtime_observation_sha256"] = observation_hash
    manifest["runtime_index_sha256"] = runtime_index_hash

    _manifest, final_receipt = _role_document(bundle, "final_approval")
    context = final_receipt["inputs"]["scope"]["handler_context"]
    context["runtime_index_sha256"] = runtime_index_hash
    context["capture_sha256"] = capture_hashes
    fingerprint = compute_operation_hash(
        final_receipt["task_id"], final_receipt["approval_type"], final_receipt["inputs"]
    )
    final_receipt["operation_hash"] = fingerprint
    final_receipt["fingerprint"] = fingerprint
    _write_role_document(bundle, manifest, "final_approval", final_receipt)
    manifest["final_review"]["fingerprint"] = fingerprint
    manifest["human_reviews"]["final_visual_review"] = fingerprint

    _manifest, production = _role_document(bundle, "production_receipt")
    receipt_map = {}
    for role, kind in (
        ("concept_approval", "concept_review"),
        ("source_approval", "source_review"),
        ("final_approval", "final_visual_review"),
    ):
        _unused_manifest, receipt = _role_document(bundle, role)
        receipt_map[kind] = receipt
    production["binding"] = {
        **{
            key: value
            for key, value in manifest.items()
            if key not in {"product_ready", "files", "final_review", "human_reviews"}
        },
        "product_ready": False,
    }
    production["human_reviews"] = {
        kind: {"operation_hash": row["operation_hash"], "status": row["status"]}
        for kind, row in receipt_map.items()
    }
    production["role_sha256"] = {
        f"{row['role']}:{row.get('view', '')}": row["sha256"]
        for row in manifest["files"]
        if row["role"] not in {"production_receipt", "review_html"}
    }
    _write_role_document(bundle, manifest, "production_receipt", production)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _rebind_processed_asset(bundle: Path, processed_bytes: bytes) -> None:
    """Rebind the complete downstream snapshot after a processed-GLB test mutation."""
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    processed_row = next(row for row in manifest["files"] if row["role"] == "processed_glb")
    (bundle / processed_row["path"]).write_bytes(processed_bytes)
    processed_hash = hashlib.sha256(processed_bytes).hexdigest()
    processed_row["sha256"] = processed_hash
    processed_row["size"] = len(processed_bytes)
    manifest["processed_glb_sha256"] = processed_hash

    _unused, report = _role_document(bundle, "processing_report")
    report["output_glb_sha256"] = processed_hash
    report_hash = _write_role_document(bundle, manifest, "processing_report", report)
    manifest["processing_report_sha256"] = report_hash
    manifest["process_report_sha256"] = report_hash

    _unused, validation = _role_document(bundle, "validation")
    validation["processed_glb_sha256"] = processed_hash
    validation_hash = _write_role_document(bundle, manifest, "validation", validation)
    manifest["validation_sha256"] = validation_hash

    _unused, request = _role_document(bundle, "runtime_request")
    request["processed_glb_sha256"] = processed_hash
    request["request_digest"] = hashlib.sha256(
        json.dumps(
            {key: value for key, value in request.items() if key != "request_digest"},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()
    request_hash = _write_role_document(bundle, manifest, "runtime_request", request)
    manifest["runtime_request_digest"] = request["request_digest"]

    _unused, observation = _role_document(bundle, "runtime_observation")
    observation["processed_glb_sha256"] = processed_hash
    observation["request_digest"] = request["request_digest"]
    observation_hash = _write_role_document(bundle, manifest, "runtime_observation", observation)
    manifest["runtime_observation_sha256"] = observation_hash

    _unused, runtime_index = _role_document(bundle, "runtime_index")
    runtime_index["processed_glb_sha256"] = processed_hash
    runtime_index["request_digest"] = request["request_digest"]
    runtime_index["runtime_request_sha256"] = request_hash
    runtime_index["runtime_observation_sha256"] = observation_hash
    runtime_index["observation"] = observation
    index_hash = _write_role_document(bundle, manifest, "runtime_index", runtime_index)
    manifest["runtime_index_sha256"] = index_hash

    _unused, final_receipt = _role_document(bundle, "final_approval")
    context = final_receipt["inputs"]["scope"]["handler_context"]
    context["processed_glb_sha256"] = processed_hash
    context["process_report_sha256"] = report_hash
    context["validation_report_sha256"] = validation_hash
    context["runtime_index_sha256"] = index_hash
    fingerprint = compute_operation_hash(
        final_receipt["task_id"], final_receipt["approval_type"], final_receipt["inputs"]
    )
    final_receipt["operation_hash"] = fingerprint
    final_receipt["fingerprint"] = fingerprint
    _write_role_document(bundle, manifest, "final_approval", final_receipt)
    manifest["final_review"]["fingerprint"] = fingerprint
    manifest["human_reviews"]["final_visual_review"] = fingerprint

    _unused, production = _role_document(bundle, "production_receipt")
    approval_rows = {}
    for role, kind in (
        ("concept_approval", "concept_review"),
        ("source_approval", "source_review"),
        ("final_approval", "final_visual_review"),
    ):
        _ignored, receipt = _role_document(bundle, role)
        approval_rows[kind] = receipt
    production["human_reviews"] = {
        kind: {"operation_hash": row["operation_hash"], "status": row["status"]}
        for kind, row in approval_rows.items()
    }
    production["binding"] = {
        **{
            key: value
            for key, value in manifest.items()
            if key not in {"product_ready", "files", "final_review", "human_reviews"}
        },
        "product_ready": False,
    }
    production["role_sha256"] = {
        f"{row['role']}:{row.get('view', '')}": row["sha256"]
        for row in manifest["files"]
        if row["role"] not in {"production_receipt", "review_html"}
    }
    _write_role_document(bundle, manifest, "production_receipt", production)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def test_cold_verifier_accepts_actual_exported_v07_fixture() -> None:
    assert SOURCE_VERIFIER.read_bytes() == PACKAGED_VERIFIER.read_bytes()
    result = _cold_result(PACKAGED_VERIFIER, EXPORTED_FIXTURE)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "PASS"
    assert report["product_ready"] is True
    assert report["human_identity_authenticated"] is False
    assert report["execution_origin_authenticated"] is False
    assert report["capture_origin_authenticated"] is False
    assert report["currentness"] == "as_of_export_snapshot"


def test_public_readonly_bundle_verifier_runs_both_isolated_copies() -> None:
    report = verify_local_assembly_evidence_bundle(EXPORTED_FIXTURE)
    assert report["status"] == "PASS"
    assert report["verified_files"] == 29
    assert report["execution_origin_authenticated"] is False


def test_cold_gate_installed_wheel_uses_packaged_verifier_without_source_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    installed_module = tmp_path / "wheel" / "site-packages" / "gamefactory" / "workflows"
    installed_module.mkdir(parents=True)
    monkeypatch.setattr(
        assembly_evidence, "__file__", str(installed_module / "assembly_evidence.py")
    )
    original_run = assembly_evidence.subprocess.run
    invocations: list[list[str]] = []

    def record_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        invocations.append(command)
        return original_run(command, **kwargs)

    monkeypatch.setattr(assembly_evidence.subprocess, "run", record_run)
    report = assembly_evidence.verify_local_assembly_evidence_bundle(EXPORTED_FIXTURE)
    assert report["status"] == "PASS"
    assert len(invocations) == 1
    assert Path(invocations[0][2]).resolve() == PACKAGED_VERIFIER.resolve()


def test_cold_verifier_rejects_rehashed_active_html_in_exported_fixture(
    tmp_path: Path,
) -> None:
    bundle = tmp_path / "tampered-html"
    import shutil

    shutil.copytree(EXPORTED_FIXTURE, bundle)
    html_payload = (bundle / "index.html").read_bytes()
    _rehash_role(
        bundle,
        "review_html",
        html_payload + b'<script src="https://example.invalid/active.js"></script>\n',
    )
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert "active or external HTML element" in json.loads(result.stdout)["error"]


@pytest.mark.parametrize(
    ("role", "field", "value", "message"),
    [
        ("specification", "profile_version", True, "does not bind local asset/profile"),
        ("bound_profile", "version", True, "profile document binding mismatch"),
        ("processing_report", "exit_code", False, "processing report"),
    ],
)
def test_cold_verifier_rejects_rehashed_boolean_integer_confusions(
    tmp_path: Path, role: str, field: str, value: bool, message: str
) -> None:
    import shutil

    bundle = tmp_path / f"tampered-{role}"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["role"] == role)
    target = bundle / row["path"]
    document = json.loads(target.read_text(encoding="utf-8"))
    document[field] = value
    payload = (json.dumps(document, sort_keys=True, indent=2) + "\n").encode("utf-8")
    _rehash_role(bundle, role, payload)
    if role == "processing_report":
        manifest_path = bundle / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        digest = hashlib.sha256(payload).hexdigest()
        manifest["processing_report_sha256"] = digest
        manifest["process_report_sha256"] = digest
        manifest_path.write_text(
            json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert message in json.loads(result.stdout)["error"]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("camera_direction", [0.0, -1.0, 0.0], "framing is outside profile policy"),
        ("camera_up", [0.0, 0.0, 1.0], "framing is outside profile policy"),
    ],
)
def test_cold_verifier_rejects_rehashed_runtime_camera_semantic_tampering(
    tmp_path: Path, field: str, value: list[float], message: str
) -> None:
    import shutil

    bundle = tmp_path / f"tampered-{field}"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest, observation = _role_document(bundle, "runtime_observation")
    observation["view_framing"]["three_quarter"][field] = value
    _write_role_document(bundle, manifest, "runtime_observation", observation)
    _rebind_runtime_observation(bundle)
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert message in json.loads(result.stdout)["error"]


def test_cold_verifier_rejects_rehashed_negative_frame_offset(tmp_path: Path) -> None:
    import shutil

    bundle = tmp_path / "negative-frame-offset"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest, observation = _role_document(bundle, "runtime_observation")
    frame = observation["view_framing"]["three_quarter"]
    frame["center_offset_ratio"] = -7.0
    frame["horizontally_centered"] = True
    _write_role_document(bundle, manifest, "runtime_observation", observation)
    _rebind_runtime_observation(bundle)
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert "framing is outside profile policy" in json.loads(result.stdout)["error"]


@pytest.mark.parametrize(
    ("field", "value", "expected_failure"),
    [
        ("status", "FAIL", "status"),
        ("revision", True, "revision"),
        ("unrecognized", "extra", "closed_fields"),
    ],
)
def test_cold_verifier_rejects_rehashed_invalid_production_receipt(
    tmp_path: Path, field: str, value: Any, expected_failure: str
) -> None:
    import shutil

    bundle = tmp_path / "failed-production-receipt"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest, receipt = _role_document(bundle, "production_receipt")
    receipt[field] = value
    _write_role_document(bundle, manifest, "production_receipt", receipt)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert (
        f"production receipt is inconsistent: {expected_failure}"
        in json.loads(result.stdout)["error"]
    )


def test_cold_verifier_rejects_rehashed_crc_valid_png_with_wrong_chunk_order(
    tmp_path: Path,
) -> None:
    import shutil

    bundle = tmp_path / "wrong-png-chunk-order"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    row = next(
        item
        for item in manifest["files"]
        if item["role"] == "runtime_capture" and item["view"] == "front"
    )
    target = bundle / row["path"]
    original = target.read_bytes()

    def chunk_rows(raw: bytes) -> list[tuple[bytes, bytes]]:
        offset = 8
        chunks = []
        while offset < len(raw):
            length = struct.unpack_from(">I", raw, offset)[0]
            kind = raw[offset + 4 : offset + 8]
            data = raw[offset + 8 : offset + 8 + length]
            chunks.append((kind, data))
            offset += length + 12
        return chunks

    def chunk(kind: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + kind
            + data
            + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
        )

    parts = dict(chunk_rows(original))
    malformed = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IDAT", parts[b"IDAT"])
        + chunk(b"IHDR", parts[b"IHDR"])
        + chunk(b"IEND", b"")
    )
    target.write_bytes(malformed)
    row["size"] = len(malformed)
    row["sha256"] = hashlib.sha256(malformed).hexdigest()
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    _rebind_runtime_observation(bundle)
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert "IHDR must be first" in json.loads(result.stdout)["error"]


@pytest.mark.parametrize(
    ("path", "value", "message"),
    [
        (("parts_verified", 0, "local_position"), [7.0, 7.0, 7.0], "runtime PART_hull differs"),
        (
            ("sockets_verified", 0, "world_position"),
            [7.0, 7.0, 7.0],
            "runtime socket muzzle differs",
        ),
        (
            ("articulation_results", 0, "pivot_world_ok"),
            False,
            "articulation observation is incomplete",
        ),
    ],
)
def test_cold_verifier_rejects_rehashed_runtime_assembly_semantic_tampering(
    tmp_path: Path, path: tuple[str, int, str], value: Any, message: str
) -> None:
    import shutil

    bundle = tmp_path / f"tampered-{path[0]}"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest, observation = _role_document(bundle, "runtime_observation")
    observation[path[0]][path[1]][path[2]] = value
    _write_role_document(bundle, manifest, "runtime_observation", observation)
    _rebind_runtime_observation(bundle)
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert message in json.loads(result.stdout)["error"]


def test_cold_verifier_rejects_newer_failed_attempt_instead_of_falling_back(
    tmp_path: Path,
) -> None:
    import shutil

    bundle = tmp_path / "newer-failed-attempt"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest_path = bundle / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["current_attempt_history"]["godot"].append(
        {"id": "EXEC-newer", "attempt_number": 2, "status": "FAILED"}
    )
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n")
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert "latest successful attempt" in json.loads(result.stdout)["error"]


def test_cold_verifier_rejects_rehashed_request_profile_boolean_version(
    tmp_path: Path,
) -> None:
    import shutil

    bundle = tmp_path / "request-profile-bool-version"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest, request = _role_document(bundle, "runtime_request")
    request["profile"]["version"] = True
    _write_role_document(bundle, manifest, "runtime_request", request)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert "Godot request does not bind exact bundled inputs" in json.loads(result.stdout)["error"]


def test_cold_verifier_rejects_rehashed_request_digest_tampering(tmp_path: Path) -> None:
    import shutil

    bundle = tmp_path / "request-digest-mismatch"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest, request = _role_document(bundle, "runtime_request")
    request["review_views"] = request["review_views"][:-1]
    _write_role_document(bundle, manifest, "runtime_request", request)
    (bundle / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert "runtime request canonical digest mismatch" in json.loads(result.stdout)["error"]


def test_cold_verifier_rejects_rehashed_cross_revision_runtime_observation(
    tmp_path: Path,
) -> None:
    import shutil

    bundle = tmp_path / "cross-revision-observation"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    manifest, observation = _role_document(bundle, "runtime_observation")
    observation["revision"] = 2
    _write_role_document(bundle, manifest, "runtime_observation", observation)
    _rebind_runtime_observation(bundle)
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert "Godot observation is stale" in json.loads(result.stdout)["error"]


def test_cold_triangle_match_checks_oriented_triangles_not_aabb() -> None:
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    compare = verifier["_v07_compare_triangles"]
    first = ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (1.0, 1.0, 0.0))
    second = ((0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0))
    flipped_diagonal = (
        ((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
        ((1.0, 0.0, 0.0), (1.0, 1.0, 0.0), (0.0, 1.0, 0.0)),
    )
    assert compare([first, second], [second, first])
    assert not compare([first, second], list(flipped_diagonal))
    assert not compare([first], [tuple(reversed(first))])


def _glb_with_document(document: dict[str, Any], binary: bytes) -> bytes:
    json_chunk = json.dumps(document, separators=(",", ":"), allow_nan=False).encode("utf-8")
    json_chunk += b" " * (-len(json_chunk) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return (
        b"glTF"
        + struct.pack("<II", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def _source_lod0_after_front_normalization(verifier: dict[str, Any]) -> dict[str, Any]:
    row = next(
        item
        for item in json.loads((EXPORTED_FIXTURE / "manifest.json").read_text())["files"]
        if item["role"] == "raw_glb"
    )
    document, binary, nodes, _parents, world = verifier["_v07_glb"](
        (EXPORTED_FIXTURE / row["path"]).read_bytes()
    )
    triangles = verifier["_v07_tris"](document, binary, nodes, world, "_LOD0")
    # The source provenance declares +Z, so normalization is one 180-degree Y rotation.
    return {
        name: [tuple((-point[0], point[1], -point[2]) for point in triangle) for triangle in mesh]
        for name, mesh in triangles.items()
    }


def _tiny_png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
        )

    header = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    pixels = b"".join(b"\x00" + bytes((255, 255, 255, 255)) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _textured_processed_glb(verifier: dict[str, Any], image: bytes) -> tuple[dict, bytes]:
    manifest = json.loads((EXPORTED_FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["role"] == "processed_glb")
    document, binary, *_ = verifier["_v07_glb"]((EXPORTED_FIXTURE / row["path"]).read_bytes())
    primitive = document["meshes"][0]["primitives"][0]
    count = document["accessors"][primitive["attributes"]["POSITION"]]["count"]
    uv = struct.pack("<" + "f" * (count * 2), *([0.0] * (count * 2)))
    uv_offset = len(binary)
    document["bufferViews"].append(
        {"buffer": 0, "byteOffset": uv_offset, "byteLength": len(uv), "target": 34962}
    )
    primitive["attributes"]["TEXCOORD_0"] = len(document["bufferViews"]) - 1
    document["accessors"].append(
        {
            "bufferView": len(document["bufferViews"]) - 1,
            "componentType": 5126,
            "count": count,
            "type": "VEC2",
        }
    )
    image_offset = uv_offset + len(uv)
    image_view = len(document["bufferViews"])
    document["bufferViews"].append(
        {"buffer": 0, "byteOffset": image_offset, "byteLength": len(image)}
    )
    document["images"] = [{"bufferView": image_view, "mimeType": "image/png"}]
    document["textures"] = [{"source": 0}]
    document["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"] = {"index": 0}
    binary += uv + image
    document["buffers"][0]["byteLength"] = len(binary)
    return document, binary


def test_cold_glb_parser_accepts_embedded_textures_and_recomputes_core_budgets() -> None:
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    document, binary = _textured_processed_glb(verifier, _tiny_png(1, 1))
    parsed, _, nodes, _, world = verifier["_v07_glb"](_glb_with_document(document, binary))
    fixture_manifest = json.loads((EXPORTED_FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    spec_row = next(item for item in fixture_manifest["files"] if item["role"] == "specification")
    profile_row = next(
        item for item in fixture_manifest["files"] if item["role"] == "bound_profile"
    )
    specification = json.loads((EXPORTED_FIXTURE / spec_row["path"]).read_text(encoding="utf-8"))
    profile = json.loads((EXPORTED_FIXTURE / profile_row["path"]).read_text(encoding="utf-8"))
    verifier["_v07_enforce_core_budgets"](parsed, binary, nodes, world, specification, profile)


@pytest.mark.parametrize("budget_kind", ["lod0", "lod1", "material", "texture"])
def test_cold_core_budget_rejects_rehashed_glb_semantics(budget_kind: str) -> None:
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    document, binary = _textured_processed_glb(verifier, _tiny_png(2, 2))
    parsed, _, nodes, _, world = verifier["_v07_glb"](_glb_with_document(document, binary))
    fixture_manifest = json.loads((EXPORTED_FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    spec_row = next(item for item in fixture_manifest["files"] if item["role"] == "specification")
    profile_row = next(
        item for item in fixture_manifest["files"] if item["role"] == "bound_profile"
    )
    specification = json.loads((EXPORTED_FIXTURE / spec_row["path"]).read_text(encoding="utf-8"))
    profile = json.loads((EXPORTED_FIXTURE / profile_row["path"]).read_text(encoding="utf-8"))
    if budget_kind == "lod0":
        lod0_count = sum(
            len(triangles)
            for triangles in verifier["_v07_tris"](parsed, binary, nodes, world, "_LOD0").values()
        )
        limit = max(1, lod0_count - 1)
        specification["geometry_budget"]["max_triangles_lod0"] = limit
        specification["geometry_budget"]["max_triangles_lod1"] = min(5000, limit - 1)
    elif budget_kind == "lod1":
        lod1_count = sum(
            len(triangles)
            for triangles in verifier["_v07_tris"](parsed, binary, nodes, world, "_LOD1").values()
        )
        specification["geometry_budget"]["max_triangles_lod1"] = max(1, lod1_count - 1)
    elif budget_kind == "material":
        document["materials"].extend([{}] * 4)
        parsed["materials"] = document["materials"]
    else:
        specification["texture_budget"]["max_dimension"] = 1
    with pytest.raises(ValueError, match="exceeds specification"):
        verifier["_v07_enforce_core_budgets"](parsed, binary, nodes, world, specification, profile)


@pytest.mark.parametrize(
    ("budget_kind", "expected_error"),
    [
        ("material", "processed material count exceeds specification budget"),
        ("texture", "embedded texture exceeds specification dimension budget"),
    ],
)
def test_cold_bundle_recomputes_rehashed_material_and_texture_budgets(
    tmp_path: Path, budget_kind: str, expected_error: str
) -> None:
    import shutil

    bundle = tmp_path / f"{budget_kind}-budget-overflow"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    if budget_kind == "material":
        manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
        row = next(item for item in manifest["files"] if item["role"] == "processed_glb")
        document, binary, *_ = verifier["_v07_glb"]((bundle / row["path"]).read_bytes())
        document["materials"].extend([{}] * 4)
        mutated = _glb_with_document(document, binary)
    else:
        document, binary = _textured_processed_glb(verifier, _tiny_png(4096, 1))
        mutated = _glb_with_document(document, binary)
    _rebind_processed_asset(bundle, mutated)
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 1
    assert expected_error in json.loads(result.stdout)["error"]


def test_cold_bundle_accepts_rehashed_embedded_texture_and_image_buffer_view(
    tmp_path: Path,
) -> None:
    import shutil

    bundle = tmp_path / "valid-textured-assembly"
    shutil.copytree(EXPORTED_FIXTURE, bundle)
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    document, binary = _textured_processed_glb(verifier, _tiny_png(1, 1))
    _rebind_processed_asset(bundle, _glb_with_document(document, binary))
    result = _cold_result(PACKAGED_VERIFIER, bundle)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["product_ready"] is True


def test_actual_source_and_processed_triangles_match_once_after_plus_z_normalization() -> None:
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    expected = _source_lod0_after_front_normalization(verifier)
    row = next(
        item
        for item in json.loads((EXPORTED_FIXTURE / "manifest.json").read_text())["files"]
        if item["role"] == "processed_glb"
    )
    document, binary, nodes, _parents, world = verifier["_v07_glb"](
        (EXPORTED_FIXTURE / row["path"]).read_bytes()
    )
    actual = verifier["_v07_tris"](document, binary, nodes, world, "_LOD0")
    assert expected.keys() == actual.keys()
    assert all(
        verifier["_v07_compare_triangles"](expected[name], actual[name]) for name in expected
    )

    root_index = nodes["ROOT"]
    document["nodes"][root_index]["rotation"] = [0.0, 1.0, 0.0, 0.0]
    twice_normalized, normalized_binary, norm_nodes, _parents, norm_world = verifier["_v07_glb"](
        _glb_with_document(document, binary)
    )
    double_rotated = verifier["_v07_tris"](
        twice_normalized, normalized_binary, norm_nodes, norm_world, "_LOD0"
    )
    assert any(
        not verifier["_v07_compare_triangles"](expected[name], double_rotated[name])
        for name in expected
    )


def test_actual_processed_collider_requires_all_six_triangle_covered_faces() -> None:
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    manifest = json.loads((EXPORTED_FIXTURE / "manifest.json").read_text(encoding="utf-8"))
    row = next(item for item in manifest["files"] if item["role"] == "processed_glb")
    document, binary, nodes, _parents, world = verifier["_v07_glb"](
        (EXPORTED_FIXTURE / row["path"]).read_bytes()
    )
    collider_name = "COL_test_blender_tank"
    collider = verifier["_v07_tris"](document, binary, nodes, world, collider_name)[collider_name]
    bounds = verifier["_v07_bounds"](collider)
    assert verifier["_v07_check_box"](collider, bounds[0], bounds[1], 0.02)
    assert not verifier["_v07_check_box"](collider[:-1], bounds[0], bounds[1], 0.02)


def test_cold_camera_math_matches_godot_observation_and_rejects_mutated_axes() -> None:
    verifier = runpy.run_path(str(SOURCE_VERIFIER))
    expected_view = verifier["_v07_expected_view"]
    close = verifier["_v07_close_vector"]
    actual_godot_three_quarter = (
        [-0.642492592334747, -0.417620092630386, 0.642492592334747],
        [-0.295302003622055, 0.908621728420258, 0.295302003622055],
        "+X-Z",
    )
    direction, up, axis = expected_view("three_quarter")
    assert close(list(actual_godot_three_quarter[0]), list(direction), 1e-4)
    assert close(list(actual_godot_three_quarter[1]), list(up), 1e-4)
    assert axis == actual_godot_three_quarter[2]
    wrong_direction, wrong_up, wrong_axis = expected_view("three_quarter_rear")
    assert not close(list(actual_godot_three_quarter[0]), list(wrong_direction), 1e-4)
    assert not close(list(actual_godot_three_quarter[1]), list(wrong_up), 1e-4)
    assert wrong_axis != actual_godot_three_quarter[2]


def test_export_rejects_pending_receipt_before_creating_destination(tmp_path: Path) -> None:
    destination = tmp_path / "evidence"
    with pytest.raises(ArtifactError, match="pending, rejected, or stale"):
        export_local_assembly_evidence(
            destination,
            files=[EvidenceFile(role="unknown", data=b"x")],
            binding=_binding(),
            concept_review_receipt=_receipt("concept_review"),
            source_review_receipt=_receipt("source_review", status="PENDING"),
            final_review_receipt=_receipt("final_visual_review"),
        )
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_export_rejects_unknown_role_even_with_approved_inputs(tmp_path: Path) -> None:
    destination = tmp_path / "evidence"
    with pytest.raises(ArtifactError, match="Unknown evidence role"):
        export_local_assembly_evidence(
            destination,
            files=[EvidenceFile(role="unregistered_source", data=b"x")],
            binding=_binding(),
            concept_review_receipt=_receipt("concept_review"),
            source_review_receipt=_receipt("source_review"),
            final_review_receipt=_receipt("final_visual_review"),
        )
    assert not destination.exists()
    assert list(tmp_path.iterdir()) == []


def test_export_rejects_paid_evidence_roles(tmp_path: Path) -> None:
    destination = tmp_path / "evidence"
    with pytest.raises(ArtifactError, match="paid/provider"):
        export_local_assembly_evidence(
            destination,
            files=[EvidenceFile(role="provider_operation", data=b"x")],
            binding=_binding(),
            concept_review_receipt=_receipt("concept_review"),
            source_review_receipt=_receipt("source_review"),
            final_review_receipt=_receipt("final_visual_review"),
        )
    assert not destination.exists()


def test_evidence_file_rehashes_authoritative_row_bytes(tmp_path: Path) -> None:
    raw_path = tmp_path / "source.bin"
    raw_path.write_bytes(b"changed")
    from gamefactory.workflows.assembly_evidence import _read_bounded

    with pytest.raises(ArtifactError, match="pinned artifact row"):
        _read_bounded(
            EvidenceFile(
                role="raw_glb",
                path=raw_path,
                expected_sha256=hashlib.sha256(b"original").hexdigest(),
                expected_size=len(b"original"),
            )
        )
