"""Real Blender/Godot V0.7 character workflow with a zero-cost fake provider."""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from gamefactory.adapters.dcc.blender import BlenderAdapter
from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
from gamefactory.adapters.fakes.glb_generator import create_box_glb
from gamefactory.adapters.images.concept_ingest import MAX_PROVENANCE_SIDECAR_BYTES
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ConceptVersionRepository,
    CostLedgerRepository,
    ExecutionRepository,
    ProductionReadinessRepository,
    ProjectRepository,
    ProviderInvocationRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.accounting.ledger import EntryType, LedgerEntry
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.asset_contracts import parse_asset_specification_v07
from gamefactory.core.domain.asset_profiles import (
    AssetProfileV07,
    ProfileRegistry,
    parse_profile_document_v07,
)
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    AuditEvent,
    CostClass,
    Execution,
    ExecutionStatus,
    Project,
    TaskStatus,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.workflows.asset_production import (
    AssetProductionHandlers,
    create_asset_production_workflow,
    register_asset_production_handlers,
)
from gamefactory.workflows.concept_versions import replace_concept
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.production_readiness import (
    DefaultReadinessProbes,
    ReadinessCheck,
    ReadinessContext,
)

_PROFILE_PATH = (
    Path(__file__).resolve().parents[1] / "fixtures" / "profiles" / "character_profile_070.yml"
)
_REQUIRED_VIEWS = ("front", "rear", "left", "right", "three_quarter")
_GODOT_PATH = Path(
    os.environ.get(
        "GAMEFACTORY_TEST_GODOT",
        r"C:\Users\lenovo\devel\godot\Godot_v4.7.2-stable_win64.exe\Godot_v4.7.2-stable_win64_console.exe",
    )
)


def _profile() -> AssetProfileV07:
    document = parse_profile_document_v07(_PROFILE_PATH.read_text(encoding="utf-8"))
    raw = document.model_dump(mode="json")
    raw["review_views"] = list(_REQUIRED_VIEWS)
    return AssetProfileV07(parse_profile_document_v07(raw))


def _registry(profile: AssetProfileV07) -> ProfileRegistry:
    return ProfileRegistry(available=(), unsupported=(), available_v07=(profile,))


def _spec(profile: AssetProfileV07) -> Any:
    return parse_asset_specification_v07(
        {
            "schema_version": "0.7.0",
            "asset_id": "character_full_journey",
            "category": "character",
            "profile": profile.profile_id,
            "profile_version": profile.version,
            "intent": "Static unrigged provider character workflow integration fixture",
            "source_kind": "provider_generated",
            "dimensions": {"width_m": 0.6, "depth_m": 0.5, "height_m": 1.8},
            "orientation": {"up": "+Y", "front": "-Z"},
            "origin_policy": "bottom_center",
            "geometry_budget": {
                "max_triangles_lod0": 20000,
                "max_triangles_lod1": 10000,
                "lod_ratio": 0.5,
            },
            "collider": {
                "policy": "capsule",
                "capsule": {"radius_m": 0.25, "height_m": 1.8},
            },
            "lod_policy": "lod0_lod1",
        },
        registry=_registry(profile),
    )


class _CharacterFixtureProvider(FakeAssetGenerationProvider):
    def __init__(self, *args, split_raw_multimesh: bool = False, **kwargs):
        super().__init__(*args, **kwargs)
        self.split_raw_multimesh = split_raw_multimesh

    def _write_output(self, request):
        params = request.parameters
        spec = params["specification"]
        dims = spec["dimensions"]
        output = Path(params["output_path"])
        glb = create_box_glb(
            width_m=float(dims["width_m"]),
            depth_m=float(dims["depth_m"]),
            height_m=float(dims["height_m"]),
            mesh_name=str(params["asset_id"]),
            include_lod1=False,
            include_collider=False,
            include_texture=True,
        )
        if self.split_raw_multimesh:
            glb = _split_box_glb_into_unnamed_mesh_nodes(glb)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(glb)
        return output


def _split_box_glb_into_unnamed_mesh_nodes(glb: bytes) -> bytes:
    """Partition a box's twelve triangles across two unnamed static GLB nodes."""
    magic, version, _total = struct.unpack_from("<4sII", glb, 0)
    assert magic == b"glTF" and version == 2
    json_length, json_type = struct.unpack_from("<II", glb, 12)
    assert json_type == 0x4E4F534A
    json_start = 20
    document = json.loads(glb[json_start : json_start + json_length].decode().rstrip(" \t\r\n\0"))
    bin_header = json_start + json_length
    bin_length, bin_type = struct.unpack_from("<II", glb, bin_header)
    assert bin_type == 0x004E4942
    binary_start = bin_header + 8
    binary = bytearray(glb[binary_start : binary_start + bin_length])

    original_primitive = document["meshes"][0]["primitives"][0]
    index_accessor = document["accessors"][original_primitive["indices"]]
    index_view = document["bufferViews"][index_accessor["bufferView"]]
    index_start = index_view.get("byteOffset", 0) + index_accessor.get("byteOffset", 0)
    count = index_accessor["count"]
    assert index_accessor["componentType"] == 5123 and count == 36
    indices = struct.unpack_from(f"<{count}H", binary, index_start)
    new_primitives = []
    for face_indices in (indices[:18], indices[18:]):
        while len(binary) % 4:
            binary.append(0)
        offset = len(binary)
        index_bytes = struct.pack(f"<{len(face_indices)}H", *face_indices)
        binary.extend(index_bytes)
        view_index = len(document["bufferViews"])
        document["bufferViews"].append(
            {
                "buffer": 0,
                "byteOffset": offset,
                "byteLength": len(index_bytes),
                "target": 34963,
            }
        )
        accessor_index = len(document["accessors"])
        document["accessors"].append(
            {
                "bufferView": view_index,
                "byteOffset": 0,
                "componentType": 5123,
                "count": len(face_indices),
                "type": "SCALAR",
                "min": [min(face_indices)],
                "max": [max(face_indices)],
            }
        )
        primitive = dict(original_primitive)
        primitive["indices"] = accessor_index
        new_primitives.append(primitive)
    document["meshes"] = [{"primitives": [primitive]} for primitive in new_primitives]
    document["nodes"] = [{"mesh": 0}, {"mesh": 1}]
    document["scenes"][0]["nodes"] = [0, 1]
    document["buffers"][0]["byteLength"] = len(binary)

    json_bytes = json.dumps(document, separators=(",", ":")).encode()
    json_bytes += b" " * ((4 - len(json_bytes) % 4) % 4)
    binary_bytes = bytes(binary)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary_bytes)
    return (
        struct.pack("<4sII", b"glTF", 2, total)
        + struct.pack("<II", len(json_bytes), 0x4E4F534A)
        + json_bytes
        + struct.pack("<II", len(binary_bytes), 0x004E4942)
        + binary_bytes
    )


class _TrustedCharacterReadiness:
    """Cheap integration probe that verifies the typed injected profile itself."""

    def __init__(self, profile: AssetProfileV07) -> None:
        self.profile = profile

    def evaluate(self, context: ReadinessContext) -> list[ReadinessCheck]:
        spec = context.specification
        profile_ok = spec["schema_version"] == "0.7.0" and self.profile.qualified == (
            f"{spec['profile']}@{spec['profile_version']}"
        )
        return [
            ReadinessCheck("provider_available", "provider", "PASS", True, "isolated fake", {}),
            ReadinessCheck("blender_available", "tools", "PASS", True, "integration", {}),
            ReadinessCheck("godot_available", "tools", "PASS", True, "protocol runner", {}),
            ReadinessCheck("workspace_writable", "filesystem", "PASS", True, "writable", {}),
            ReadinessCheck(
                "profile_supported",
                "profile",
                "PASS" if profile_ok else "FAIL",
                True,
                "injected profile identity matches the exact character spec",
                {"profile": self.profile.qualified},
            ),
        ]

    def recheck_critical(self, context: ReadinessContext) -> list[ReadinessCheck]:
        return self.evaluate(context)


def _setup(
    tmp_path: Path,
    *,
    crash_once: bool = True,
    multimesh: bool = False,
    live_readiness: bool = False,
    provider_cost_class: CostClass = CostClass.LOCAL,
    provider_estimate: float | None = 0.0,
    provider_cost_unit: str = "credits",
):
    root = tmp_path / "project"
    root.mkdir()
    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    project_id = "character-paid-project"
    ProjectRepository(db).save(
        Project(project_id, "Character paid integration", "godot", str(root))
    )
    profile = _profile()
    spec = _spec(profile)
    registry = _registry(profile)
    concept = root / "concepts" / "character.png"
    concept.parent.mkdir(parents=True)
    Image.new("RGB", (32, 32), (30, 70, 120)).save(concept)
    provenance = root / "concepts" / "character-provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "source": "integration-fixture",
                "sha256": hashlib.sha256(concept.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    blender_path = BlenderAdapter().find_candidate_executable()
    if not blender_path:
        pytest.skip("A real Blender executable is required for this integration test")
    if not _GODOT_PATH.is_file():
        pytest.skip(f"A real Godot executable is required: {_GODOT_PATH}")
    provider = _CharacterFixtureProvider(
        cost_class=provider_cost_class,
        simulate_crash=crash_once,
        intent_repo=ProviderOperationIntentRepository(db),
        split_raw_multimesh=multimesh,
    )
    workflow, tasks = create_asset_production_workflow(
        project_id,
        root,
        spec,
        concept,
        provenance,
        provider_name="fake",
        provider_estimate=provider_estimate,
        provider_cost_unit=provider_cost_unit,
        workflow_id="character-paid-e2e",
        revision_repository=AssetRevisionRepository(db),
        profile_registry=registry,
    )
    engine = WorkflowEngine(root, db, asset_provider=provider)
    handlers = AssetProductionHandlers(
        root,
        engine.art_repo,
        AssetRevisionRepository(db),
        engine.app_repo,
        engine.intent_repo,
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        provider,
        blender_path=blender_path,
        godot_path=str(_GODOT_PATH),
        runner=ProcessRunner(sanitize_output=True),
        accounting=engine.accounting,
        ledger=engine.ledger_repo,
        audit=engine.audit_repo,
        tasks=engine.task_repo,
        readiness_probes=(
            DefaultReadinessProbes() if live_readiness else _TrustedCharacterReadiness(profile)
        ),
        profile_registry=registry,
    )
    register_asset_production_handlers(engine.handler_registry, handlers)
    return root, db, engine, workflow, tasks, provider, handlers, profile


def _approve(engine: WorkflowEngine, db: Database, approval_id: str) -> None:
    repository = ApprovalRepository(db)
    approval = repository.get(approval_id)
    assert approval is not None
    workflow = engine.wf_repo.get(approval.workflow_id)
    task = TaskRepository(db).get(approval.task_id)
    assert workflow is not None and task is not None
    approved = ApprovalService.approve(
        approval,
        "integration-operator",
        current_inputs=engine.approval_inputs(workflow, task),
    )
    assert repository.decide_if_pending(
        approved,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type="Approval",
            entity_id=approved.id,
            action="APPROVED",
            actor="integration-operator",
        ),
    )


def _assert_portable_provenance_sidecar(root: Path, artifact) -> None:
    document = json.loads((root / artifact.relative_path).read_text(encoding="utf-8"))
    sidecar_path = document.get("sidecar_path")
    assert isinstance(sidecar_path, str) and sidecar_path
    assert not Path(sidecar_path).is_absolute()
    assert "\\" not in sidecar_path
    sidecar = root / sidecar_path
    assert sidecar.is_file()
    assert hashlib.sha256(sidecar.read_bytes()).hexdigest() == document.get("sidecar_hash")


def test_default_live_readiness_keeps_character_profile_and_portable_tool_identity(
    tmp_path: Path,
) -> None:
    root, db, engine, workflow, _tasks, provider, _handlers, profile = _setup(
        tmp_path, crash_once=False, live_readiness=True
    )
    concept_gate = engine.run_workflow(workflow.id)
    assert concept_gate.status == WorkflowStatus.BLOCKED
    assert concept_gate.pending_approval_id
    _approve(engine, db, concept_gate.pending_approval_id)
    paid_gate = engine.run_workflow(workflow.id)
    assert paid_gate.status == WorkflowStatus.BLOCKED, paid_gate.error_message
    assert paid_gate.pending_approval_id
    assert provider.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow.id) == []
    readiness = ProductionReadinessRepository(db).get_active_for_workflow(workflow.id)
    assert readiness is not None and readiness.result == "PASS"
    report = json.loads(readiness.report_json)
    checks = {item["name"]: item for item in report["checks"]}
    assert checks["profile_supported"]["status"] == "PASS"
    assert checks["profile_supported"]["observed"]["profile"] == profile.qualified
    for tool in ("blender", "godot"):
        identity = report["tool_identities"][tool]
        assert isinstance(identity, dict) and identity["basename"]
        assert ":" not in identity["basename"] and "/" not in identity["basename"]
        assert isinstance(checks[f"{tool}_launch_version"]["observed"]["version"], str)
    serialized = json.dumps(report, sort_keys=True)
    assert not re.search(r"(?i)(?:[A-Z]:[\\/]|\\\\|(?<!\w)/)", serialized)


def test_zero_cost_provider_character_journey_query_recovery_and_human_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, engine, workflow, tasks, provider, handlers, profile = _setup(
        tmp_path, provider_cost_unit="fake_credits"
    )
    workflow_id = workflow.id
    assert len(tasks) == 10

    concept_gate = engine.run_workflow(workflow_id)
    assert concept_gate.status == WorkflowStatus.BLOCKED
    assert concept_gate.pending_approval_id
    _approve(engine, db, concept_gate.pending_approval_id)

    paid_gate = engine.run_workflow(workflow_id)
    assert paid_gate.status == WorkflowStatus.BLOCKED, paid_gate.error_message
    assert paid_gate.pending_approval_id
    assert provider.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []
    from gamefactory.adapters.persistence.repositories import (
        PaidRequestSnapshotRepository,
    )

    snapshot = PaidRequestSnapshotRepository(db).get_active_for_workflow(workflow_id)
    readiness = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert snapshot is not None and readiness is not None and readiness.result == "PASS"
    assert set(json.loads(snapshot.canonical_json)) == {
        "schema",
        "provider",
        "operation",
        "adapter",
        "binding",
        "request",
        "cost",
    }
    report = json.loads(readiness.report_json)
    assert any(
        check["name"] == "profile_supported" and check["status"] == "PASS"
        for check in report["checks"]
    )
    assert report["checks"]
    _approve(engine, db, paid_gate.pending_approval_id)

    crashed = engine.run_workflow(workflow_id)
    assert crashed.status == WorkflowStatus.BLOCKED
    assert provider.invocation_count == 1
    intent_repo = ProviderOperationIntentRepository(db)
    intent = intent_repo.get_by_task(f"{workflow_id}-PAID-GENERATION")
    assert intent is not None and intent.external_task_id
    provider.simulate_crash = False
    at_final_gate = engine.run_workflow(workflow_id)
    assert at_final_gate.status == WorkflowStatus.BLOCKED, at_final_gate.error_message
    final_approval = ApprovalRepository(db).get(at_final_gate.pending_approval_id or "")
    assert final_approval is not None and final_approval.approval_type == "final_visual_review"
    assert provider.invocation_count == 1
    assert len(intent_repo.list_by_workflow(workflow_id)) == 1
    assert (
        ProviderInvocationRepository(db).count(workflow_id) == 0
    )  # Fake-provider call count is tracked by provider.invocation_count
    assert len(ExecutionRepository(db).list_by_task(f"{workflow_id}-PAID-GENERATION")) == 2
    assert (
        sum(entry.amount for entry in CostLedgerRepository(db).list_by_workflow(workflow_id)) == 0
    )

    revision = AssetRevisionRepository(db).get("character_full_journey", 1)
    assert revision is not None and revision.raw_glb_hash and revision.processed_glb_hash
    raw_rows = [
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "asset-raw-glb"
    ]
    assert len(raw_rows) == 1
    assert raw_rows[0].content_hash == revision.raw_glb_hash
    initial_provenance = next(
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "asset-concept-provenance"
        and item.task_id == f"{workflow_id}-PREPARE"
    )
    _assert_portable_provenance_sidecar(root, initial_provenance)
    assert handlers._character_processed_artifact(
        workflow, "integration assertion"
    ).content_hash == (revision.processed_glb_hash)
    runtime_artifacts = ArtifactRepository(db).list_by_task(f"{workflow_id}-GODOT")
    assert sum(item.artifact_type == "asset-runtime-capture" for item in runtime_artifacts) == len(
        profile.review_views
    )
    observations = [
        item for item in runtime_artifacts if item.artifact_type == "asset-runtime-observation"
    ]
    assert len(observations) == 1
    runtime_observation = json.loads(
        (root / observations[0].relative_path).read_text(encoding="utf-8")
    )
    assert runtime_observation["status"] == "PASS"

    capture = next(
        item for item in runtime_artifacts if item.artifact_type == "asset-runtime-capture"
    )
    capture_path = root / capture.relative_path
    capture_bytes = capture_path.read_bytes()
    capture_path.write_bytes(capture_bytes + b"tampered")
    with pytest.raises(ArtifactError, match="integrity"):
        handlers.final_review_context(
            workflow, next(task for task in tasks if task.task_type == "asset_final_review")
        )
    capture_path.write_bytes(capture_bytes)

    final_task = TaskRepository(db).get(f"{workflow_id}-FINAL-REVIEW")
    assert final_task is not None
    current_context = handlers.final_review_context(workflow, final_task)
    assert current_context["runtime_capture_hashes"]
    assert current_context["runtime_request_digest"]
    _approve(engine, db, final_approval.id)
    selected: dict[str, Any] = {}
    current_call: dict[str, Any] = {}
    ledger_probes_completed = False
    original_selector = handlers.current_provider_character_evidence_inputs

    def capture_current_inputs(wf, evidence_task, evidence_execution):
        nonlocal ledger_probes_completed
        current_call.update(workflow=wf, task=evidence_task, execution=evidence_execution)
        result = original_selector(wf, evidence_task, evidence_execution)
        selected.update(result)
        if not ledger_probes_completed:
            assert_ledger_rejection_probes(wf, evidence_task, evidence_execution)
            ledger_probes_completed = True
        return result

    def assert_ledger_rejection_probes(wf, evidence_task, evidence_execution):
        # Run inside the real handler, while its persisted task and execution
        # are both RUNNING. Each probe restores its scoped repository override.
        paid_task = next(task for task in tasks if task.task_type == "asset_paid_generation")
        ledger_repo = handlers.ledger or handlers.accounting.ledger_repo
        original_list_by_task = ledger_repo.list_by_task
        current_rows = original_list_by_task(paid_task.id)
        current_settlement = next(row for row in current_rows if row.entry_type == EntryType.SETTLE)
        with monkeypatch.context() as scoped:
            scoped.setattr(
                ledger_repo,
                "list_by_task",
                lambda task_id, conn=None: (
                    original_list_by_task(task_id, conn)
                    + [replace(current_settlement, id=f"{current_settlement.id}-duplicate")]
                    if task_id == paid_task.id
                    else original_list_by_task(task_id, conn)
                ),
            )
            with pytest.raises(ArtifactError, match="one terminal settled ledger row"):
                original_selector(wf, evidence_task, evidence_execution)

        with monkeypatch.context() as scoped:

            def with_held_row(task_id: str, conn=None):
                rows = original_list_by_task(task_id, conn)
                if task_id == paid_task.id:
                    rows.append(
                        LedgerEntry(
                            id="adversarial-held-reservation",
                            project_id=workflow.project_id,
                            workflow_id=workflow_id,
                            task_id=paid_task.id,
                            entry_type=EntryType.RESERVE,
                            amount=1.0,
                            cost_unit=intent.cost_unit,
                        )
                    )
                return rows

            scoped.setattr(ledger_repo, "list_by_task", with_held_row)
            with pytest.raises(ArtifactError, match="conflicting, unsettled, or inconsistent"):
                original_selector(wf, evidence_task, evidence_execution)

        original_get_intent = handlers.intents.get_by_task
        with monkeypatch.context() as scoped:
            scoped.setattr(
                handlers.intents,
                "get_by_task",
                lambda task_id: (
                    replace(original_get_intent(task_id), actual_cost=None)
                    if task_id == paid_task.id
                    else original_get_intent(task_id)
                ),
            )
            with pytest.raises(ArtifactError, match="one terminal settled ledger row"):
                original_selector(wf, evidence_task, evidence_execution)

        original_execution_list = handlers.executions.list_by_task
        with monkeypatch.context() as scoped:

            def wrong_execution_provider(task_id: str):
                rows = original_execution_list(task_id)
                if task_id == paid_task.id:
                    rows = [replace(row, provider="forged-provider") for row in rows]
                return rows

            scoped.setattr(handlers.executions, "list_by_task", wrong_execution_provider)
            with pytest.raises(ArtifactError, match="Provider operation is not terminal"):
                original_selector(wf, evidence_task, evidence_execution)

        workflow_repository = WorkflowRepository(db)
        persisted_workflow = workflow_repository.get(wf.id)
        assert (
            persisted_workflow is not None and persisted_workflow.status == WorkflowStatus.RUNNING
        )
        try:
            workflow_repository.save(replace(persisted_workflow, status=WorkflowStatus.FAILED))
            with pytest.raises(ArtifactError, match="current persisted workflow status"):
                original_selector(wf, evidence_task, evidence_execution)
        finally:
            workflow_repository.save(persisted_workflow)

        with monkeypatch.context() as scoped:
            call_count = 0

            def ledger_changes_during_export(task_id: str, conn=None):
                nonlocal call_count
                rows = original_list_by_task(task_id, conn)
                if task_id == paid_task.id:
                    call_count += 1
                    if call_count >= 3:
                        rows.append(
                            LedgerEntry(
                                id="adversarial-export-race",
                                project_id=workflow.project_id,
                                workflow_id=workflow_id,
                                task_id=paid_task.id,
                                entry_type=EntryType.RESERVE,
                                amount=1.0,
                                cost_unit=intent.cost_unit,
                            )
                        )
                return rows

            scoped.setattr(ledger_repo, "list_by_task", ledger_changes_during_export)
            with pytest.raises(ArtifactError, match="ledger changed during evidence selection"):
                original_selector(wf, evidence_task, evidence_execution)

    monkeypatch.setattr(
        handlers, "current_provider_character_evidence_inputs", capture_current_inputs
    )
    result = engine.run_workflow(workflow_id)
    assert selected, result.error_message or result.to_dict()
    evidence_inputs = selected
    assert len(evidence_inputs["files"]) >= 14
    assert evidence_inputs["binding"]["raw_glb_sha256"] == revision.raw_glb_hash
    assert evidence_inputs["binding"]["processed_glb_sha256"] == revision.processed_glb_hash
    assert set(evidence_inputs["binding"]["capture_sha256"]) == set(profile.review_views)
    assert evidence_inputs["concept_review_receipt"]["status"] == "APPROVED"
    assert evidence_inputs["paid_review_receipt"]["status"] == "APPROVED"
    assert evidence_inputs["final_review_receipt"]["status"] == "APPROVED"
    assert evidence_inputs["final_review_receipt"]["actor"] == "integration-operator"
    assert evidence_inputs["final_review_receipt"]["reason"]
    assert evidence_inputs["final_review_receipt"]["decided_at"]
    assert evidence_inputs["attempt_history"]["process"][-1]["status"] == "COMPLETED"
    assert evidence_inputs["attempt_history"]["validate"][-1]["status"] == "COMPLETED"
    assert evidence_inputs["attempt_history"]["godot"][-1]["status"] == "COMPLETED"

    assert result.status == WorkflowStatus.COMPLETED, result.error_message
    receipts = [
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "asset-final-approval-record"
    ]
    assert len(receipts) == 1
    receipt = json.loads((root / receipts[0].relative_path).read_text(encoding="utf-8"))
    assert receipt["approval_id"] == final_approval.id
    assert receipt["fingerprint"] == final_approval.operation_hash
    evidence_task = next(
        item
        for item in TaskRepository(db).list_by_workflow(workflow_id)
        if item.task_type == "asset_v07_provider_evidence"
    )
    assert evidence_task.status == TaskStatus.COMPLETED
    manifests = [
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow_id)
        if item.artifact_type == "provider-character-evidence-manifest"
    ]
    assert len(manifests) == 1
    manifest = json.loads((root / manifests[0].relative_path).read_text(encoding="utf-8"))
    operation_row = next(row for row in manifest["files"] if row["role"] == "provider_operation")
    operation_path = (root / manifests[0].relative_path).parent / operation_row["path"]
    operation = json.loads(operation_path.read_text(encoding="utf-8"))
    paid_history = operation["paid_execution_history"]
    assert [row["attempt_number"] for row in paid_history] == [1, 2]
    assert [row["status"] for row in paid_history] == ["UNCERTAIN", "COMPLETED"]
    assert paid_history[-1]["id"] == operation["execution_id"]


def test_character_concept_replacement_invalidates_paid_snapshot_and_approval(
    tmp_path: Path,
) -> None:
    root, db, engine, workflow, _tasks, provider, _handlers, _profile = _setup(
        tmp_path, crash_once=False
    )
    concept_gate = engine.run_workflow(workflow.id)
    assert concept_gate.status == WorkflowStatus.BLOCKED
    assert concept_gate.pending_approval_id
    _approve(engine, db, concept_gate.pending_approval_id)
    paid_gate = engine.run_workflow(workflow.id)
    assert paid_gate.status == WorkflowStatus.BLOCKED
    assert paid_gate.pending_approval_id
    old_paid_approval = ApprovalRepository(db).get(paid_gate.pending_approval_id)
    assert old_paid_approval is not None

    replacement = root / "replacement.png"
    Image.new("RGB", (32, 32), (180, 40, 20)).save(replacement)
    provenance = root / "replacement-provenance.json"
    provenance.write_text(
        json.dumps(
            {
                "source": "replacement-fixture",
                "sha256": hashlib.sha256(replacement.read_bytes()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )
    replaced = replace_concept(
        root,
        db,
        engine,
        workflow.id,
        replacement,
        provenance,
        actor="integration-operator",
        reason="Verify pre-paid character concept replacement invalidates pinned evidence",
    )
    assert replaced["new_version"] == 2
    assert replaced["new_concept_sha256"] != replaced["old_concept_sha256"]
    active_version = ConceptVersionRepository(db).active_for_workflow(workflow.id)
    assert active_version is not None and active_version.version == 2
    new_provenance = ArtifactRepository(db).get(active_version.provenance_artifact_id)
    assert new_provenance is not None
    _assert_portable_provenance_sidecar(root, new_provenance)
    assert provider.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow.id) == []
    assert CostLedgerRepository(db).list_by_workflow(workflow.id) == []
    assert all(
        item.status != "ACTIVE"
        for item in ProductionReadinessRepository(db).list_by_workflow(workflow.id)
    )

    rerun = engine.run_workflow(workflow.id)
    assert rerun.status == WorkflowStatus.BLOCKED
    assert rerun.pending_approval_id != old_paid_approval.id
    new_approval = ApprovalRepository(db).get(rerun.pending_approval_id or "")
    assert new_approval is not None and new_approval.approval_type == "concept_review"
    assert provider.invocation_count == 0


def test_v07_concept_replacement_rejects_oversized_sidecar_before_creating_outputs(
    tmp_path: Path,
) -> None:
    root, db, engine, workflow, tasks, _provider, _handlers, _profile = _setup(
        tmp_path, crash_once=False
    )
    concept_gate = engine.run_workflow(workflow.id)
    assert concept_gate.status == WorkflowStatus.BLOCKED and concept_gate.pending_approval_id
    _approve(engine, db, concept_gate.pending_approval_id)
    concept_gate = engine.run_workflow(workflow.id)
    assert concept_gate.status == WorkflowStatus.BLOCKED and concept_gate.pending_approval_id

    replacement = root / "oversize-replacement.png"
    Image.new("RGB", (32, 32), (15, 90, 35)).save(replacement)
    provenance = root / "oversize-provenance.json"
    provenance.write_bytes(b"{" + b"x" * MAX_PROVENANCE_SIDECAR_BYTES)
    asset_dir = (
        root
        / next(task for task in tasks if task.task_type == "asset_prepare").parameters["asset_dir"]
    )
    before = set(asset_dir.rglob("*"))
    with pytest.raises(ValidationError, match="exceeds size limit"):
        replace_concept(
            root,
            db,
            engine,
            workflow.id,
            replacement,
            provenance,
            actor="integration-operator",
            reason="Reject an oversized provenance sidecar before managed outputs are created",
        )
    assert set(asset_dir.rglob("*")) == before


@pytest.mark.parametrize(
    "tamper", ["missing", "foreign_revision", "foreign_workflow", "wrong_role"]
)
def test_current_character_concept_binding_rejects_foreign_or_missing_active_rows(
    tmp_path: Path, tamper: str
) -> None:
    root, db, engine, workflow, tasks, provider, handlers, _profile_value = _setup(
        tmp_path, crash_once=False
    )
    gate = engine.run_workflow(workflow.id)
    assert gate.status == WorkflowStatus.BLOCKED and gate.pending_approval_id
    _approve(engine, db, gate.pending_approval_id)
    # Prepare finishes and the concept approval is created; no paid effects occur.
    gate = engine.run_workflow(workflow.id)
    assert gate.status == WorkflowStatus.BLOCKED and gate.pending_approval_id
    readiness_before = ProductionReadinessRepository(db).list_by_workflow(workflow.id)
    versions = ConceptVersionRepository(db)
    active = versions.active_for_workflow(workflow.id)
    assert active is not None
    if tamper == "missing":
        with db.transaction() as conn:
            conn.execute("UPDATE concept_versions SET status='SUPERSEDED' WHERE id=?", (active.id,))
    elif tamper == "foreign_revision":
        foreign = replace(active, id=generate_id("CV"), asset_id="foreign-asset")
        versions.add(foreign)
    elif tamper == "foreign_workflow":
        with db.transaction() as conn:
            conn.execute("DROP TRIGGER trg_concept_versions_update_restricted")
            conn.execute(
                "UPDATE concept_versions SET workflow_id='foreign-workflow' WHERE id=?",
                (active.id,),
            )
    else:
        with db.transaction() as conn:
            conn.execute("DROP TRIGGER trg_concept_versions_update_restricted")
            conn.execute(
                "UPDATE concept_versions SET artifact_id=provenance_artifact_id WHERE id=?",
                (active.id,),
            )

    prepare_task = next(task for task in tasks if task.task_type == "asset_prepare")
    with pytest.raises(ArtifactError, match="active concept|artifacts"):
        handlers._guard_paid_task("snapshot", workflow, prepare_task)
    assert provider.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow.id) == []
    assert CostLedgerRepository(db).list_by_workflow(workflow.id) == []
    assert ProductionReadinessRepository(db).list_by_workflow(workflow.id) == readiness_before


def test_unnamed_multimesh_character_is_joined_and_passes_real_runtime(
    tmp_path: Path,
) -> None:
    root, db, engine, workflow, tasks, provider, handlers, profile = _setup(
        tmp_path, crash_once=False, multimesh=True
    )
    concept_gate = engine.run_workflow(workflow.id)
    assert concept_gate.status == WorkflowStatus.BLOCKED
    assert concept_gate.pending_approval_id
    _approve(engine, db, concept_gate.pending_approval_id)
    paid_gate = engine.run_workflow(workflow.id)
    assert paid_gate.status == WorkflowStatus.BLOCKED
    assert paid_gate.pending_approval_id
    _approve(engine, db, paid_gate.pending_approval_id)

    final_gate = engine.run_workflow(workflow.id)
    assert final_gate.status == WorkflowStatus.BLOCKED, final_gate.error_message
    assert provider.invocation_count == 1
    raw = next(
        item
        for item in ArtifactRepository(db).list_by_workflow(workflow.id)
        if item.artifact_type == "asset-raw-glb"
    )
    raw_path = root / raw.relative_path
    raw_bytes = raw_path.read_bytes()
    json_length = struct.unpack_from("<I", raw_bytes, 12)[0]
    raw_document = json.loads(raw_bytes[20 : 20 + json_length].decode().rstrip(" \t\r\n\0"))
    assert len(raw_document["nodes"]) == len(raw_document["meshes"]) == 2
    assert all("name" not in node for node in raw_document["nodes"])
    assert all("name" not in mesh for mesh in raw_document["meshes"])

    processed = handlers._character_processed_artifact(workflow, "multimesh integration")
    assert (
        processed.content_hash
        == AssetRevisionRepository(db).get("character_full_journey", 1).processed_glb_hash
    )
    runtime_rows = ArtifactRepository(db).list_by_task(f"{workflow.id}-GODOT")
    observations = [
        item for item in runtime_rows if item.artifact_type == "asset-runtime-observation"
    ]
    captures = [item for item in runtime_rows if item.artifact_type == "asset-runtime-capture"]
    assert len(observations) == 1 and len(captures) == len(profile.review_views)
    assert json.loads((root / observations[0].relative_path).read_text())["status"] == "PASS"


@pytest.mark.parametrize("new_status", [ExecutionStatus.FAILED, ExecutionStatus.RUNNING])
@pytest.mark.parametrize(
    ("task_type", "stage"),
    [
        ("asset_process", "Blender processing"),
        ("asset_validate", "independent validation"),
        ("asset_godot", "final visual review"),
    ],
)
def test_latest_noncompleted_character_attempt_blocks_old_completed_artifact(
    tmp_path: Path, new_status: ExecutionStatus, task_type: str, stage: str
) -> None:
    root, db, _engine, workflow, tasks, _provider, handlers, _profile = _setup(
        tmp_path, crash_once=False
    )
    task = next(task for task in tasks if task.task_type == task_type)
    TaskRepository(db).update_status(task.id, TaskStatus.COMPLETED)
    executions = ExecutionRepository(db)
    executions.save(
        Execution(f"{task_type}-attempt-1", task.id, 1, status=ExecutionStatus.COMPLETED)
    )
    executions.save(Execution(f"{task_type}-attempt-2", task.id, 2, status=new_status))
    with pytest.raises(ArtifactError, match="latest .* execution to be COMPLETED"):
        handlers._latest_character_execution(workflow, task_type, stage)


@pytest.mark.parametrize("task_status", [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.FAILED])
def test_noncompleted_character_task_cannot_reuse_completed_execution(
    tmp_path: Path, task_status: TaskStatus
) -> None:
    _root, db, _engine, workflow, tasks, _provider, handlers, _profile = _setup(
        tmp_path, crash_once=False
    )
    task = next(item for item in tasks if item.task_type == "asset_process")
    TaskRepository(db).update_status(task.id, TaskStatus.COMPLETED)
    ExecutionRepository(db).save(
        Execution("asset-process-completed", task.id, 1, status=ExecutionStatus.COMPLETED)
    )
    TaskRepository(db).update_status(task.id, task_status)
    with pytest.raises(ArtifactError, match="task to be COMPLETED"):
        handlers._latest_character_execution(workflow, "asset_process", "stale task status")
