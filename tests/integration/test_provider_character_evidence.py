from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import shutil
import struct
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

import gamefactory.workflows.provider_character_evidence as evidence_module
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    CostLedgerRepository,
    EvidenceRepository,
    ExecutionRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.accounting.ledger import EntryType
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.domain.errors import ArtifactError
from gamefactory.core.domain.models import (
    CostClass,
    GateStatus,
    QualityGate,
    WorkflowStatus,
    generate_id,
)
from gamefactory.workflows.provider_character_evidence import (
    _cold_gate,
    revalidate_current_provider_character_evidence_bundle,
)

_SIBLING_PATH = Path(__file__).resolve().parent / "test_v07_character_paid_workflow.py"
_sibling_spec = importlib.util.spec_from_file_location(
    "test_v07_character_paid_workflow", _SIBLING_PATH
)
if _sibling_spec is None or _sibling_spec.loader is None:
    raise ImportError(f"Cannot load sibling test module from {_SIBLING_PATH}")
_sibling_module = importlib.util.module_from_spec(_sibling_spec)
_sibling_spec.loader.exec_module(_sibling_module)
_approve = _sibling_module._approve
_setup = _sibling_module._setup


def _add_uv1_alias(glb: bytes) -> bytes:
    json_length, json_kind = struct.unpack_from("<II", glb, 12)
    assert json_kind == 0x4E4F534A
    json_start = 20
    document = json.loads(glb[json_start : json_start + json_length].decode("utf-8").rstrip())
    bin_header = json_start + json_length
    bin_length, bin_kind = struct.unpack_from("<II", glb, bin_header)
    assert bin_kind == 0x004E4942
    binary = glb[bin_header + 8 : bin_header + 8 + bin_length]
    uv0_index = next(
        accessor
        for accessor in range(len(document["accessors"]))
        if any(
            primitive.get("attributes", {}).get("TEXCOORD_0") == accessor
            for mesh in document["meshes"]
            for primitive in mesh["primitives"]
        )
    )
    uv1_index = len(document["accessors"])
    document["accessors"].append(dict(document["accessors"][uv0_index]))
    for mesh in document["meshes"]:
        for primitive in mesh["primitives"]:
            primitive["attributes"]["TEXCOORD_1"] = uv1_index
    for material in document.get("materials", []):
        slot = material.get("pbrMetallicRoughness", {}).get("baseColorTexture")
        if isinstance(slot, dict):
            slot["texCoord"] = 1
    json_chunk = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    json_chunk += b" " * ((-len(json_chunk)) % 4)
    total = 12 + 8 + len(json_chunk) + 8 + len(binary)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_chunk), 0x4E4F534A)
        + json_chunk
        + struct.pack("<II", len(binary), 0x004E4942)
        + binary
    )


def test_paid_character_workflow_exports_cold_verified_provider_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, db, engine, workflow, _tasks, provider, handlers, profile = _setup(
        tmp_path,
        crash_once=False,
        multimesh=True,
        live_readiness=True,
        provider_cost_class=CostClass.PAID,
        provider_estimate=5.0,
        provider_cost_unit="fake_credits",
    )
    original_write_output = provider._write_output

    def write_uv1_provider_output(request):
        output = original_write_output(request)
        output.write_bytes(_add_uv1_alias(output.read_bytes()))
        return output

    monkeypatch.setattr(provider, "_write_output", write_uv1_provider_output)
    concept_gate = engine.run_workflow(workflow.id)
    assert concept_gate.status == WorkflowStatus.BLOCKED, concept_gate.error_message
    assert concept_gate.pending_approval_id
    _approve(engine, db, concept_gate.pending_approval_id)

    paid_gate = engine.run_workflow(workflow.id)
    assert paid_gate.status == WorkflowStatus.BLOCKED
    assert paid_gate.pending_approval_id
    assert provider.invocation_count == 0
    _approve(engine, db, paid_gate.pending_approval_id)

    final_gate = engine.run_workflow(workflow.id)
    assert final_gate.status == WorkflowStatus.BLOCKED
    assert final_gate.pending_approval_id
    assert provider.invocation_count == 1
    _approve(engine, db, final_gate.pending_approval_id)

    completion = engine.run_workflow(workflow.id)
    assert completion.status == WorkflowStatus.COMPLETED, completion.error_message
    execution = ExecutionRepository(db).get_latest_attempt(f"{workflow.id}-PROVIDER-EVIDENCE")
    assert execution is not None
    manifests = [
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow.id)
        if item.artifact_type == "provider-character-evidence-manifest"
    ]
    assert len(manifests) == 1
    manifest_artifact = manifests[0]
    target = root / manifest_artifact.relative_path.rsplit("/", 1)[0]
    report = _cold_gate(target)
    assert report["status"] == "PASS"
    assert report["product_ready"] is True
    assert report["paid"] is True
    assert report["human_identity_authenticated"] is False
    assert report["capture_origin_authenticated"] is False
    assert target.is_dir()
    assert (target / "manifest.json").is_file()
    assert manifest_artifact.relative_path.endswith(
        f"provider-evidence/{execution.id}-a{execution.attempt_number}/manifest.json"
    )
    entries = CostLedgerRepository(db).list_by_workflow(workflow.id)
    provider_entries = [
        item for item in entries if item.task_id == f"{workflow.id}-PAID-GENERATION"
    ]
    assert [item.entry_type for item in provider_entries] == [
        EntryType.RESERVE,
        EntryType.SETTLE,
        EntryType.RELEASE,
    ]
    assert (
        sum(item.amount for item in provider_entries if item.entry_type == EntryType.SETTLE) == 5.0
    )
    manifest = json.loads((target / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["review_views"] == list(profile.review_views)
    raw_row = next(row for row in manifest["files"] if row["role"] == "provider_generated_glb")
    raw = (target / raw_row["path"]).read_bytes()
    raw_json_length = struct.unpack_from("<I", raw, 12)[0]
    raw_document = json.loads(raw[20 : 20 + raw_json_length].decode("utf-8").rstrip())
    raw_mesh_nodes = [node for node in raw_document["nodes"] if "mesh" in node]
    assert len(raw_mesh_nodes) == 2
    assert any("name" not in node for node in raw_mesh_nodes)
    assert all(
        "TEXCOORD_1" in raw_document["meshes"][node["mesh"]]["primitives"][0]["attributes"]
        for node in raw_mesh_nodes
    )
    assert raw_document["materials"][0]["pbrMetallicRoughness"]["baseColorTexture"]["texCoord"] == 1

    database_before = db.db_path.read_bytes()
    provider_calls_before = provider.invocation_count
    runner_calls: list[object] = []
    project_files_before = {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    }

    def forbidden_runner_call(*_args, **_kwargs):
        runner_calls.append(object())
        raise AssertionError("Read-only evidence revalidation invoked an external tool")

    monkeypatch.setattr(handlers.runner, "run", forbidden_runner_call)
    monkeypatch.setattr(provider, "generate", forbidden_runner_call)
    current_adapter_name = provider._name
    provider._name = "reconfigured-current-provider"
    try:
        repeated_report = revalidate_current_provider_character_evidence_bundle(handlers, workflow)
    finally:
        provider._name = current_adapter_name
    assert repeated_report["status"] == "PASS"
    assert repeated_report["evidence_manifest"] == manifest_artifact.relative_path
    assert repeated_report["evidence_manifest_sha256"] == manifest_artifact.content_hash
    assert repeated_report["evidence_manifest_artifact_id"] == manifest_artifact.id
    assert provider.invocation_count == provider_calls_before
    assert runner_calls == []
    assert db.db_path.read_bytes() == database_before
    assert {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file() and not path.is_symlink()
    } == project_files_before

    # Rehash a forged request and every dependent request/observation/approval
    # pin. The cold verifier must still reject the stale harness binding.
    tampered = root / "provider-character-wrong-harness"
    shutil.copytree(target, tampered)
    altered_manifest = json.loads((tampered / "manifest.json").read_text(encoding="utf-8"))

    def row_for(role: str) -> dict[str, object]:
        return next(row for row in altered_manifest["files"] if row["role"] == role)

    def rewrite_role(role: str, value: dict[str, object]) -> str:
        row = row_for(role)
        encoded = (
            json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        ).encode()
        (tampered / row["path"]).write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        row["sha256"], row["size"] = digest, len(encoded)
        return digest

    request_row = row_for("runtime_request")
    request = json.loads((tampered / request_row["path"]).read_text(encoding="utf-8"))
    request["harness_sha256"] = "0" * 64
    request.pop("request_digest")
    request_digest = hashlib.sha256(
        json.dumps(request, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    ).hexdigest()
    request["request_digest"] = request_digest
    request_sha = rewrite_role("runtime_request", request)
    altered_manifest["runtime_request_digest"] = request_digest

    observation_row = row_for("runtime_observation")
    observation = json.loads((tampered / observation_row["path"]).read_text(encoding="utf-8"))
    observation["request_digest"] = request_digest
    observation["harness_sha256"] = "0" * 64
    observation_sha = rewrite_role("runtime_observation", observation)
    altered_manifest["runtime_observation_sha256"] = observation_sha

    final_row = row_for("final_approval")
    final_receipt = json.loads((tampered / final_row["path"]).read_text(encoding="utf-8"))
    final_context = final_receipt["inputs"]["scope"]["handler_context"]
    final_context["runtime_request_digest"] = request_digest
    final_context["artifacts"]["asset-runtime-request"] = request_sha
    final_context["artifacts"]["asset-runtime-observation"] = observation_sha
    final_digest = compute_operation_hash(
        final_receipt["task_id"], "final_visual_review", final_receipt["inputs"]
    )
    final_receipt["operation_hash"] = final_digest
    final_receipt["fingerprint"] = final_digest
    rewrite_role("final_approval", final_receipt)
    altered_manifest["final_review"]["fingerprint"] = final_digest
    altered_manifest["human_reviews"]["final_visual_review"] = final_digest
    production_row = row_for("production_receipt")
    production_receipt = json.loads((tampered / production_row["path"]).read_text(encoding="utf-8"))
    production_receipt["binding"] = {
        key: value
        for key, value in altered_manifest.items()
        if key not in {"product_ready", "files", "final_review", "human_reviews"}
    }
    production_receipt["human_reviews"] = {
        "concept_review": {
            "operation_hash": altered_manifest["human_reviews"]["concept_review"],
            "status": "APPROVED",
        },
        "paid_generation": {
            "operation_hash": altered_manifest["human_reviews"]["paid_generation"],
            "status": "APPROVED",
        },
        "final_visual_review": {"operation_hash": final_digest, "status": "APPROVED"},
    }
    production_receipt["role_sha256"] = {
        f"{row['role']}:{row.get('view', '')}": row["sha256"]
        for row in altered_manifest["files"]
        if row["role"] not in {"review_html", "production_receipt"}
    }
    rewrite_role("production_receipt", production_receipt)
    (tampered / "manifest.json").write_text(
        json.dumps(altered_manifest, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="request and observation are not current and bound"):
        _cold_gate(tampered)

    # Rehash the processing report and every production receipt pin while
    # preserving an internally consistent bundle. The semantic report check
    # must catch a contradiction with decoded GLB geometry.
    bad_report = root / "provider-character-wrong-report"
    shutil.copytree(target, bad_report)
    bad_manifest = json.loads((bad_report / "manifest.json").read_text(encoding="utf-8"))
    report_row = next(row for row in bad_manifest["files"] if row["role"] == "processing_report")
    report_path = bad_report / report_row["path"]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["processed_metrics"]["lod0_triangles"] += 1
    report_bytes = (
        json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode()
    report_path.write_bytes(report_bytes)
    report_digest = hashlib.sha256(report_bytes).hexdigest()
    report_row["sha256"], report_row["size"] = report_digest, len(report_bytes)
    bad_manifest["processing_report_sha256"] = report_digest
    receipt_row = next(row for row in bad_manifest["files"] if row["role"] == "production_receipt")
    receipt_path = bad_report / receipt_row["path"]
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["binding"] = {
        key: value
        for key, value in bad_manifest.items()
        if key not in {"product_ready", "files", "final_review", "human_reviews"}
    }
    receipt["role_sha256"]["processing_report:"] = report_digest
    receipt_bytes = (
        json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    ).encode()
    receipt_path.write_bytes(receipt_bytes)
    receipt_row["sha256"], receipt_row["size"] = (
        hashlib.sha256(receipt_bytes).hexdigest(),
        len(receipt_bytes),
    )
    (bad_report / "manifest.json").write_text(
        json.dumps(bad_manifest, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="processed metrics differ from decoded GLB"):
        _cold_gate(bad_report)

    # Rehash the HTML row and manifest. The parser must reject active markup
    # even though the modified page remains covered by the manifest digest.
    bad_html = root / "provider-character-active-html"
    shutil.copytree(target, bad_html)
    html_manifest = json.loads((bad_html / "manifest.json").read_text(encoding="utf-8"))
    html_row = next(row for row in html_manifest["files"] if row["role"] == "review_html")
    html_payload = (bad_html / html_row["path"]).read_text(encoding="utf-8")
    html_payload = html_payload.replace(
        "</main>", '<script src="https://evil.invalid/x.js"></script></main>'
    )
    html_bytes = html_payload.encode("utf-8")
    (bad_html / html_row["path"]).write_bytes(html_bytes)
    html_row["sha256"], html_row["size"] = hashlib.sha256(html_bytes).hexdigest(), len(html_bytes)
    (bad_html / "manifest.json").write_text(
        json.dumps(html_manifest, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
        + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ArtifactError, match="active or external HTML element is forbidden"):
        _cold_gate(bad_html)

    def write_role(bundle: Path, manifest: dict[str, Any], role: str, value: object) -> str:
        row = next(item for item in manifest["files"] if item["role"] == role)
        encoded = (
            json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        ).encode()
        (bundle / row["path"]).write_bytes(encoded)
        digest = hashlib.sha256(encoded).hexdigest()
        row["sha256"], row["size"] = digest, len(encoded)
        if role == "provider_operation":
            manifest["provider_operation_sha256"] = digest
        elif role == "cost_record":
            manifest["cost_record_sha256"] = digest
        return digest

    def write_rebound_receipt(bundle: Path, manifest: dict[str, Any]) -> None:
        receipt_row = next(
            item for item in manifest["files"] if item["role"] == "production_receipt"
        )
        receipt_path = bundle / receipt_row["path"]
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        receipt["binding"] = {
            key: value
            for key, value in manifest.items()
            if key not in {"product_ready", "files", "final_review", "human_reviews"}
        }
        receipt["role_sha256"] = {
            f"{row['role']}:{row.get('view', '')}": row["sha256"]
            for row in manifest["files"]
            if row["role"] not in {"review_html", "production_receipt"}
        }
        encoded = (
            json.dumps(receipt, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
            + "\n"
        ).encode()
        receipt_path.write_bytes(encoded)
        receipt_row["sha256"], receipt_row["size"] = (
            hashlib.sha256(encoded).hexdigest(),
            len(encoded),
        )
        (bundle / "manifest.json").write_text(
            json.dumps(manifest, sort_keys=True, ensure_ascii=False, allow_nan=False, indent=2)
            + "\n",
            encoding="utf-8",
        )

    def forged_bundle(
        name: str,
        mutate: Callable[[dict[str, Any], dict[str, Any]], None],
        expected_error: str,
    ) -> None:
        bundle = root / name
        shutil.copytree(target, bundle)
        forged_manifest: dict[str, Any] = json.loads(
            (bundle / "manifest.json").read_text(encoding="utf-8")
        )
        operation_row = next(
            row for row in forged_manifest["files"] if row["role"] == "provider_operation"
        )
        operation = json.loads((bundle / operation_row["path"]).read_text(encoding="utf-8"))
        cost_row = next(row for row in forged_manifest["files"] if row["role"] == "cost_record")
        cost_record = json.loads((bundle / cost_row["path"]).read_text(encoding="utf-8"))
        mutate(operation, cost_record)
        write_role(bundle, forged_manifest, "provider_operation", operation)
        cost_record["slice_sha256"] = hashlib.sha256(
            json.dumps(
                cost_record["entries"], sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode()
        ).hexdigest()
        forged_manifest["cost_ledger_row_count"] = len(cost_record["entries"])
        forged_manifest["cost_ledger_slice_sha256"] = cost_record["slice_sha256"]
        write_role(bundle, forged_manifest, "cost_record", cost_record)
        write_rebound_receipt(bundle, forged_manifest)
        with pytest.raises(ArtifactError, match=expected_error):
            _cold_gate(bundle)

    def unknown_reserve_execution(_operation: dict[str, Any], cost_record: dict[str, Any]) -> None:
        reserve = next(row for row in cost_record["entries"] if row["entry_type"] == "RESERVE")
        reserve["intent_id"] = None
        reserve["execution_id"] = "EXEC-foreign-paid-attempt"
        reserve["request_fingerprint"] = None

    forged_bundle(
        "provider-character-unknown-reserve-execution",
        unknown_reserve_execution,
        "reservation metadata conflicts with its paid intent",
    )

    def mixed_reserve_fingerprint(_operation: dict[str, Any], cost_record: dict[str, Any]) -> None:
        reserve = next(row for row in cost_record["entries"] if row["entry_type"] == "RESERVE")
        reserve["intent_id"] = None
        reserve["request_fingerprint"] = "f" * 64

    forged_bundle(
        "provider-character-mixed-reserve-fingerprint",
        mixed_reserve_fingerprint,
        "reservation metadata conflicts with its paid intent",
    )

    def stale_settlement_execution(_operation: dict[str, Any], cost_record: dict[str, Any]) -> None:
        settlement = next(row for row in cost_record["entries"] if row["entry_type"] == "SETTLE")
        settlement["execution_id"] = "EXEC-foreign-paid-attempt"

    forged_bundle(
        "provider-character-stale-settlement-execution",
        stale_settlement_execution,
        "terminal ledger row is not pinned to the paid execution",
    )

    def foreign_task_revision(operation: dict[str, Any], _cost_record: dict[str, Any]) -> None:
        history = operation["paid_execution_history"]
        history[0]["task_id"] = "foreign-paid-task"
        history[0]["revision_number"] = 2

    forged_bundle(
        "provider-character-foreign-paid-task-revision",
        foreign_task_revision,
        "paid execution history is incomplete or conflicting",
    )

    def newer_failed_paid_attempt(operation: dict[str, Any], _cost_record: dict[str, Any]) -> None:
        failed_attempt = dict(operation["paid_execution_history"][-1])
        failed_attempt.update(
            id="EXEC-newer-failed",
            attempt_number=2,
            status="FAILED",
            provider=None,
            external_op_id=None,
        )
        operation["paid_execution_history"].append(failed_attempt)

    forged_bundle(
        "provider-character-newer-failed-paid-attempt",
        newer_failed_paid_attempt,
        "paid execution history is incomplete or conflicting",
    )

    def foreign_provider_operation(operation: dict[str, Any], _cost_record: dict[str, Any]) -> None:
        old_attempt = dict(operation["paid_execution_history"][-1])
        old_attempt.update(
            id="EXEC-prior-uncertain",
            attempt_number=1,
            status="UNCERTAIN",
            provider="foreign_provider",
            external_op_id=operation["external_task_id"],
        )
        current_attempt = dict(operation["paid_execution_history"][-1])
        current_attempt["attempt_number"] = 2
        operation["attempt_number"] = 2
        operation["paid_execution_history"] = [old_attempt, current_attempt]

    forged_bundle(
        "provider-character-foreign-provider-operation",
        foreign_provider_operation,
        "paid execution history is incomplete or conflicting",
    )

    def foreign_external_operation(operation: dict[str, Any], _cost_record: dict[str, Any]) -> None:
        old_attempt = dict(operation["paid_execution_history"][-1])
        old_attempt.update(
            id="EXEC-prior-uncertain",
            attempt_number=1,
            status="UNCERTAIN",
            provider=None,
            external_op_id="FOREIGN-REMOTE-OPERATION",
        )
        current_attempt = dict(operation["paid_execution_history"][-1])
        current_attempt["attempt_number"] = 2
        operation["attempt_number"] = 2
        operation["paid_execution_history"] = [old_attempt, current_attempt]

    forged_bundle(
        "provider-character-foreign-external-operation",
        foreign_external_operation,
        "paid execution history is incomplete or conflicting",
    )

    # Exercise persisted publication drift that occurs while the cold check is
    # in progress. The API must bind the post-cold result to the exact engine
    # evidence, gate, and artifact metadata captured before cold verification.
    original_cold_gate = evidence_module._cold_gate

    def expect_drift_rejected(mutate: Callable[[], None], restore: Callable[[], None]) -> None:
        baseline: dict[str, Any] = {}

        def cold_then_mutate(directory: Path) -> dict[str, Any]:
            result = original_cold_gate(directory)
            mutate()
            baseline["database"] = db.db_path.read_bytes()
            baseline["files"] = {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            return result

        monkeypatch.setattr(evidence_module, "_cold_gate", cold_then_mutate)
        try:
            with pytest.raises(ArtifactError):
                revalidate_current_provider_character_evidence_bundle(handlers, workflow)
            assert db.db_path.read_bytes() == baseline["database"]
            assert {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            } == baseline["files"]
        finally:
            restore()
            monkeypatch.setattr(evidence_module, "_cold_gate", original_cold_gate)

    original_size = manifest_artifact.file_size

    def change_manifest_size() -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET file_size = ? WHERE id = ?",
                (original_size + 1, manifest_artifact.id),
            )

    def restore_manifest_size() -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET file_size = ? WHERE id = ?",
                (original_size, manifest_artifact.id),
            )

    expect_drift_rejected(change_manifest_size, restore_manifest_size)

    def change_manifest_validation_state() -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET validation_state = 'VALID' WHERE id = ?",
                (manifest_artifact.id,),
            )

    def restore_manifest_validation_state() -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET validation_state = 'VERIFIED' WHERE id = ?",
                (manifest_artifact.id,),
            )

    expect_drift_rejected(change_manifest_validation_state, restore_manifest_validation_state)

    engine_evidence = next(
        item
        for item in EvidenceRepository(db).list_by_task(execution.task_id)
        if item.execution_id == execution.id
        and item.evidence_type == "handler:asset_v07_provider_evidence"
    )
    original_raw_data = engine_evidence.raw_data
    original_gate_id = original_raw_data["gate_id"]
    synthetic_gate_id = generate_id("GATE")
    QualityGateRepository(db).save(
        QualityGate(
            id=synthetic_gate_id,
            task_id=execution.task_id,
            gate_type="handler:asset_v07_provider_evidence",
            status=GateStatus.PASSED,
            reason="mid-cold drift fixture",
        )
    )

    def change_evidence_gate_pointer() -> None:
        altered_raw_data = {**original_raw_data, "gate_id": synthetic_gate_id}
        with db.transaction() as conn:
            conn.execute(
                "UPDATE evidences SET raw_data_json = ? WHERE id = ?",
                (json.dumps(altered_raw_data), engine_evidence.id),
            )

    def restore_evidence_gate_pointer() -> None:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE evidences SET raw_data_json = ? WHERE id = ?",
                (json.dumps(original_raw_data), engine_evidence.id),
            )
            conn.execute("DELETE FROM quality_gates WHERE id = ?", (synthetic_gate_id,))

    expect_drift_rejected(change_evidence_gate_pointer, restore_evidence_gate_pointer)
    assert original_gate_id == engine_evidence.raw_data["gate_id"]

    # Verify read-only revalidation rejects malformed root approval receipts
    # (JSON array, null, scalar string, list-of-objects) without tracebacks or
    # modifying the database or project files.
    corrupted_receipt_cases = (
        ("final_approval", b"[]\n"),
        ("final_approval", b"null\n"),
        ("final_approval", b'"scalar string approval receipt"\n'),
        ("final_approval", b'[{"role": "final_approval", "malformed": true}]\n'),
        ("concept_approval", b"[]\n"),
        ("concept_approval", b"null\n"),
        ("concept_approval", b'"scalar string concept receipt"\n'),
        ("concept_approval", b'[{"role": "concept_approval", "malformed": true}]\n'),
        ("paid_approval", b"[]\n"),
        ("paid_approval", b"null\n"),
        ("paid_approval", b'"scalar string paid receipt"\n'),
        ("paid_approval", b'[{"role": "paid_approval", "malformed": true}]\n'),
    )

    for role, corrupted_bytes in corrupted_receipt_cases:
        receipt_row = next(row for row in manifest["files"] if row["role"] == role)
        receipt_file = target / receipt_row["path"]
        original_bytes = receipt_file.read_bytes()
        try:
            # 1. Intentional tamper
            receipt_file.write_bytes(corrupted_bytes)

            # 2. Capture state after intentional tamper
            db_state_after_tamper = db.db_path.read_bytes()
            files_after_tamper = {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }

            # 3. Call revalidation API - must raise controlled ArtifactError with no traceback
            with pytest.raises(
                ArtifactError, match=f"^Provider-character {role} receipt is malformed$"
            ) as exc_info:
                revalidate_current_provider_character_evidence_bundle(handlers, workflow)
            assert exc_info.value.__cause__ is None
            assert exc_info.value.__context__ is None

            # 4. Verify readonly DB/files no writes (compare state after intentional tamper to after API)
            assert db.db_path.read_bytes() == db_state_after_tamper
            files_after_api = {
                path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in root.rglob("*")
                if path.is_file() and not path.is_symlink()
            }
            assert files_after_api == files_after_tamper
        finally:
            receipt_file.write_bytes(original_bytes)

    # Verify canonical tuple/list semantic comparison succeeds for valid dicts.
    final_row = next(row for row in manifest["files"] if row["role"] == "final_approval")
    disk_receipt = json.loads((target / final_row["path"]).read_text(encoding="utf-8"))
    persisted_workflow = WorkflowRepository(db).get(workflow.id)
    evidence_task = TaskRepository(db).get(execution.task_id)
    live_receipt = handlers.current_provider_character_evidence_inputs(
        persisted_workflow, evidence_task, execution
    )["final_review_receipt"]
    # Direct Python comparison fails because snapshot has tuples in scope artifacts
    # whereas the decoded JSON receipt from disk has lists.
    assert disk_receipt != live_receipt
    # Canonical serialization treats tuples and lists as equivalent JSON arrays.
    assert evidence_module._canonical(disk_receipt) == evidence_module._canonical(live_receipt)

    original_inputs = handlers.current_provider_character_evidence_inputs

    def inputs_with_tuple_views(wf: Any, t: Any, ex: Any) -> dict[str, Any]:
        snapshot_dict = copy.deepcopy(original_inputs(wf, t, ex))
        final_receipt_copy = snapshot_dict["final_review_receipt"]
        inputs_copy = final_receipt_copy["inputs"]
        if "review_views" in inputs_copy and isinstance(inputs_copy["review_views"], list):
            inputs_copy["review_views"] = tuple(inputs_copy["review_views"])
        elif "scope" in inputs_copy and isinstance(inputs_copy["scope"], dict):
            if "artifacts" in inputs_copy["scope"] and isinstance(
                inputs_copy["scope"]["artifacts"], list
            ):
                inputs_copy["scope"]["artifacts"] = tuple(inputs_copy["scope"]["artifacts"])
        return cast(dict[str, Any], snapshot_dict)

    monkeypatch.setattr(
        handlers, "current_provider_character_evidence_inputs", inputs_with_tuple_views
    )
    tuple_report = revalidate_current_provider_character_evidence_bundle(handlers, workflow)
    assert tuple_report["status"] == "PASS"
    monkeypatch.setattr(handlers, "current_provider_character_evidence_inputs", original_inputs)

    # Verify valid dict with an actual semantic mismatch raises controlled diagnostic
    final_file = target / final_row["path"]
    final_original_bytes = final_file.read_bytes()
    final_dict = json.loads(final_original_bytes.decode("utf-8"))
    final_dict["decision_notes"] = "unexpected discrepancy"
    try:
        final_file.write_text(json.dumps(final_dict, indent=2) + "\n", encoding="utf-8")
        db_after_tamper = db.db_path.read_bytes()
        files_after_tamper = {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        }
        with pytest.raises(
            ArtifactError,
            match=r"^Provider-character final_approval differs from the current human approval: fields .*decision_notes",
        ) as exc_info:
            revalidate_current_provider_character_evidence_bundle(handlers, workflow)
        assert exc_info.value.__cause__ is None
        assert exc_info.value.__context__ is None
        assert db.db_path.read_bytes() == db_after_tamper
        assert {
            path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in root.rglob("*")
            if path.is_file() and not path.is_symlink()
        } == files_after_tamper
    finally:
        final_file.write_bytes(final_original_bytes)

    # Verify bundle is still valid after restoring all original files
    final_report = revalidate_current_provider_character_evidence_bundle(handlers, workflow)
    assert final_report["status"] == "PASS"

    assert provider.invocation_count == provider_calls_before
    assert runner_calls == []
