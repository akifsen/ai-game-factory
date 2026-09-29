"""Integration tests for Slice E1: concept versions, request-changes, and CHANGES_REQUESTED semantics."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import tests.integration.test_accounting_v06 as harness
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    AuditLogRepository,
    ConceptVersionRecord,
    ConceptVersionRepository,
    PaidRequestSnapshotRepository,
    ProductionReadinessRepository,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.cli.main import main
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_contracts import parse_asset_specification
from gamefactory.core.domain.errors import ApprovalRequired, ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    AuditEvent,
    Project,
    TaskStatus,
    WorkflowStatus,
    generate_id,
)
from gamefactory.workflows.asset_evidence import export_asset_evidence_bundle
from gamefactory.workflows.concept_versions import replace_concept
from gamefactory.workflows.handlers import RegisteredTaskHandler, TaskHandlerResult
from tests.integration.test_accounting_v06 import (
    SPEC_PATH,
    _decision,
    _setup_accounting_env,
)


def _request_changes(
    db: Database,
    approval_id: str,
    *,
    actor: str = "test_reviewer",
    comment: str = "Please revise concept",
) -> None:
    repo = ApprovalRepository(db)
    approval = repo.get(approval_id)
    assert approval is not None
    decided = ApprovalService.request_changes(approval, actor, comment=comment)
    event = AuditEvent(
        id=generate_id("AUDIT"),
        entity_type="Approval",
        entity_id=decided.id,
        action=decided.status.value,
        actor=actor,
        previous_state="PENDING",
        new_state=decided.status.value,
        details={
            "workflow_id": decided.workflow_id,
            "task_id": decided.task_id,
            "comment": decided.comment,
        },
    )
    assert repo.decide_if_pending(decided, event)


def test_v06_workflow_creates_concept_version_1_active(tmp_path: Path) -> None:
    engine, db, workflow_id, _, _ = _setup_accounting_env(tmp_path)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED
    assert result.pending_approval_id is not None

    cv_repo = ConceptVersionRepository(db)
    active = cv_repo.active_for_workflow(workflow_id)
    assert active is not None
    assert active.version == 1
    assert active.status == "ACTIVE"
    assert active.actor == "system"
    assert active.reason == "initial concept"
    assert active.provenance_type == "UNKNOWN"
    assert active.source_type == "imported"

    art_repo = ArtifactRepository(db)
    concept_art = art_repo.get(active.artifact_id)
    assert concept_art is not None
    assert concept_art.artifact_type == "asset-concept"
    assert concept_art.content_hash == active.content_hash

    assert active.provenance_artifact_id is not None
    prov_art = art_repo.get(active.provenance_artifact_id)
    assert prov_art is not None
    assert prov_art.artifact_type == "asset-concept-provenance"
    assert prov_art.content_hash == active.provenance_hash


def test_changes_requested_blocks_with_concept_revision_required(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED
    assert result.pending_approval_id is not None
    concept_approval_id = result.pending_approval_id

    _request_changes(db, concept_approval_id, comment="Lighting is too dark")

    result2 = engine.run_workflow(workflow_id)
    assert result2.status == WorkflowStatus.BLOCKED
    assert result2.error_code == "CONCEPT_REVISION_REQUIRED"
    assert result2.pending_approval_id is None
    assert "gamefactory asset concept replace" in (result2.error_message or "")

    # Task and workflow must be BLOCKED, not FAILED
    task_repo = TaskRepository(db)
    concept_task = next(
        t for t in task_repo.list_by_workflow(workflow_id) if t.task_type == "asset_concept_review"
    )
    assert concept_task.status == TaskStatus.BLOCKED
    wf = WorkflowRepository(db).get(workflow_id)
    assert wf is not None and wf.status == WorkflowStatus.BLOCKED

    # No new approval created, no provider invocation, zero intents
    assert len(ApprovalRepository(db).list_by_workflow(workflow_id)) == 1
    assert fake.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []

    # Repeated resume stays BLOCKED with the same code
    result3 = engine.run_workflow(workflow_id)
    assert result3.status == WorkflowStatus.BLOCKED
    assert result3.error_code == "CONCEPT_REVISION_REQUIRED"
    assert result3.pending_approval_id is None


def test_rejected_on_concept_fails_terminally(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED
    assert result.pending_approval_id is not None

    _decision(engine, db, result.pending_approval_id, approve=False)

    result2 = engine.run_workflow(workflow_id)
    assert result2.status == WorkflowStatus.FAILED
    assert result2.error_code == "VISUAL_REVIEW_REJECTED"

    task_repo = TaskRepository(db)
    concept_task = next(
        t for t in task_repo.list_by_workflow(workflow_id) if t.task_type == "asset_concept_review"
    )
    assert concept_task.status == TaskStatus.FAILED
    wf = WorkflowRepository(db).get(workflow_id)
    assert wf is not None and wf.status == WorkflowStatus.FAILED


def test_legacy_graph_changes_requested_fails_legacy_semantics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    orig_create = harness.create_asset_production_workflow

    def legacy_graph(*args, **kwargs):
        workflow, tasks = orig_create(*args, **kwargs)
        removed = {f"{workflow.id}-PAID-REQUEST", f"{workflow.id}-READINESS"}
        legacy = [task for task in tasks if task.id not in removed]
        for task in legacy:
            task.parameters = {k: v for k, v in task.parameters.items() if k != "graph_version"}
            if task.id.endswith("-PAID-GENERATION"):
                task.depends_on = [f"{workflow.id}-CONCEPT-REVIEW"]
        return workflow, legacy

    monkeypatch.setattr(harness, "create_asset_production_workflow", legacy_graph)
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    result = engine.run_workflow(workflow_id)
    assert result.status == WorkflowStatus.BLOCKED
    approval_id = result.pending_approval_id
    assert approval_id is not None

    _request_changes(db, approval_id, comment="Legacy graph revision request")

    result2 = engine.run_workflow(workflow_id)
    assert result2.status == WorkflowStatus.FAILED
    assert result2.error_code == "VISUAL_REVIEW_REJECTED"

    concept_task = next(
        t
        for t in TaskRepository(db).list_by_workflow(workflow_id)
        if t.task_type == "asset_concept_review"
    )
    assert concept_task.status == TaskStatus.FAILED
    wf = WorkflowRepository(db).get(workflow_id)
    assert wf is not None and wf.status == WorkflowStatus.FAILED


def test_migration_triggers_and_single_active_constraint(tmp_path: Path) -> None:
    db_path = tmp_path / "test.db"
    db = Database(db_path)
    MigrationRunner(db).apply_all()
    repo = ConceptVersionRepository(db)

    r1 = ConceptVersionRecord(
        id="cv-001",
        workflow_id="wf-1",
        asset_id="asset-1",
        revision_number=1,
        version=1,
        artifact_id="art-1",
        content_hash="hash-1",
        provenance_artifact_id="prov-1",
        provenance_hash="phash-1",
        actor="system",
        reason="initial concept",
        status="ACTIVE",
    )
    repo.add(r1)

    # 1. Triggers forbid content UPDATE
    conn = db.connect()
    with pytest.raises(
        sqlite3.IntegrityError, match="concept_versions: only status and superseded_at may change"
    ):
        conn.execute("UPDATE concept_versions SET artifact_id = 'art-2' WHERE id = 'cv-001';")

    with pytest.raises(
        sqlite3.IntegrityError, match="concept_versions: only status and superseded_at may change"
    ):
        conn.execute("UPDATE concept_versions SET content_hash = 'hash-2' WHERE id = 'cv-001';")

    with pytest.raises(
        sqlite3.IntegrityError, match="concept_versions: only status and superseded_at may change"
    ):
        conn.execute("UPDATE concept_versions SET provenance_hash = 'phash-2' WHERE id = 'cv-001';")

    with pytest.raises(
        sqlite3.IntegrityError, match="concept_versions: only status and superseded_at may change"
    ):
        conn.execute("UPDATE concept_versions SET version = 2 WHERE id = 'cv-001';")

    # 2. Trigger forbids DELETE
    with pytest.raises(sqlite3.IntegrityError, match="concept_versions: deletes are forbidden"):
        conn.execute("DELETE FROM concept_versions WHERE id = 'cv-001';")
    conn.close()

    # 3. Partial unique index forbids second ACTIVE row for same (asset_id, revision_number)
    r2_conflict = ConceptVersionRecord(
        id="cv-002",
        workflow_id="wf-1",
        asset_id="asset-1",
        revision_number=1,
        version=2,
        artifact_id="art-2",
        content_hash="hash-2",
        actor="operator",
        reason="second concept",
        status="ACTIVE",
    )
    with pytest.raises(sqlite3.IntegrityError):
        repo.add(r2_conflict)

    # 4. supersede_and_add marks old SUPERSEDED and adds new ACTIVE
    repo.supersede_and_add("cv-001", r2_conflict)
    active = repo.active_for_workflow("wf-1")
    assert active is not None and active.id == "cv-002"
    all_recs = repo.list_for_revision("asset-1", 1)
    assert len(all_recs) == 2
    assert all_recs[0].status == "SUPERSEDED"
    assert all_recs[0].superseded_at is not None
    assert all_recs[1].status == "ACTIVE"


def test_cli_request_changes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "project"
    root.mkdir(parents=True, exist_ok=True)
    db = Database(root / ".gamefactory/state/factory.db")
    MigrationRunner(db).apply_all()

    # Initialize project config
    assert main(["--project", str(root), "init"]) == 0

    # Setup a workflow with a pending approval
    project = Project(id="test-proj", name="Test Project", engine_type="godot", root_path=str(root))
    from gamefactory.adapters.persistence.repositories import ProjectRepository

    ProjectRepository(db).save(project)

    image = root / "concept.png"
    Image.new("RGB", (2, 2), (20, 70, 140)).save(image)
    provenance = root / "concept.json"
    provenance.write_text(
        json.dumps({"sha256": hashlib.sha256(image.read_bytes()).hexdigest()}), encoding="utf-8"
    )

    spec = parse_asset_specification(SPEC_PATH)
    from gamefactory.workflows.asset_production import create_asset_production_workflow

    workflow, tasks = create_asset_production_workflow(
        project.id,
        root,
        spec,
        image,
        provenance,
        provider_name="fake",
        provider_estimate=5.0,
        revision_repository=AssetRevisionRepository(db),
    )
    from gamefactory.workflows.engine import WorkflowEngine

    engine = WorkflowEngine(root, db)
    from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
    from gamefactory.workflows.asset_production import (
        AssetProductionHandlers,
        register_asset_production_handlers,
    )

    handlers = AssetProductionHandlers(
        root,
        engine.art_repo,
        AssetRevisionRepository(db),
        engine.app_repo,
        ProviderOperationIntentRepository(db),
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        FakeAssetGenerationProvider(),
    )
    register_asset_production_handlers(engine.handler_registry, handlers)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    res = engine.run_workflow(workflow.id)
    assert res.pending_approval_id is not None
    approval_id = res.pending_approval_id

    # Call CLI request-changes
    exit_code = main(
        [
            "--project",
            str(root),
            "request-changes",
            approval_id,
            "--comment",
            "Color scheme mismatch",
            "--actor",
            "lead_artist",
        ]
    )
    assert exit_code == 0

    app = ApprovalRepository(db).get(approval_id)
    assert app is not None
    assert app.status == ApprovalStatus.CHANGES_REQUESTED
    assert app.actor == "lead_artist"
    assert app.comment == "Color scheme mismatch"

    audits = AuditLogRepository(db).list_by_entity("Approval", approval_id)
    assert len(audits) >= 1
    assert any(a.action == "CHANGES_REQUESTED" and a.actor == "lead_artist" for a in audits)


def _create_concept_v2(
    tmp_path: Path, color: tuple[int, int, int] = (200, 100, 50)
) -> tuple[Path, Path]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    img_path = tmp_path / "concept_v2.png"
    Image.new("RGB", (4, 4), color).save(img_path)
    sha = hashlib.sha256(img_path.read_bytes()).hexdigest()
    prov_path = tmp_path / "concept_v2.json"
    prov_path.write_text(json.dumps({"sha256": sha}), encoding="utf-8")
    return img_path, prov_path


def test_concept_replace_changes_requested_to_v2_and_completes_to_evidence(
    tmp_path: Path,
) -> None:
    engine, db, workflow_id, fake, handlers = _setup_accounting_env(tmp_path)

    def godot_stub(wf, task, execution):
        processed = next(
            a
            for a in handlers.artifacts.list_by_workflow(wf.id)
            if a.artifact_type == "asset-processed-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(processed)
        path = handlers._path(task, f"runtime-{execution.id}.json")
        obs_data = {
            "status": "PASS",
            "workflow_id": wf.id,
            "revision": int(task.parameters["revision_number"]),
            "asset_id": str(task.parameters["asset_id"]),
            "execution_id": execution.id,
            "attempt_number": execution.attempt_number,
            "processed_glb_sha256": processed.content_hash,
            "mesh_visible": True,
            "collision_shape_present": True,
            "physics_body_present": True,
            "physics_ray_hit": True,
            "area_present": False,
            "errors": [],
            "mesh_bounds": {
                "size": [
                    task.parameters["specification"]["dimensions"]["width_m"],
                    task.parameters["specification"]["dimensions"]["height_m"],
                    task.parameters["specification"]["dimensions"]["depth_m"],
                ]
            },
        }
        path.write_text(json.dumps(obs_data), encoding="utf-8")
        ids = [handlers._register(wf, task, execution, "asset-runtime-observation", path)]
        for angle in ("front", "three_quarter", "side"):
            capture = handlers._path(task, f"{execution.id}-{angle}.png")
            Image.new("RGB", (2, 2), (20, 70, 140)).save(capture)
            ids.append(handlers._register(wf, task, execution, "asset-runtime-capture", capture))
        return TaskHandlerResult(1, "deterministic runtime stub", ids)

    def process_stub(wf, task, execution):
        raw = next(
            a
            for a in handlers.artifacts.list_by_workflow(wf.id)
            if a.artifact_type == "asset-raw-glb"
        )
        handlers.artifact_manager.verify_artifact_integrity(raw)
        path = handlers._path(task, f"processed-attempt-{execution.attempt_number}.glb")
        path.write_bytes((handlers.root / raw.relative_path).read_bytes())
        report = handlers._path(task, f"processing-attempt-{execution.attempt_number}.json")
        report.write_text(
            json.dumps(
                {
                    "status": "SUCCESS",
                    "exit_code": 0,
                    "input_raw_glb_sha256": raw.content_hash,
                    "output_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    "processing_script_sha256": hashlib.sha256(b"test-stub").hexdigest(),
                }
            ),
            encoding="utf-8",
        )
        return TaskHandlerResult(
            1,
            "deterministic process stub",
            [
                handlers._register(wf, task, execution, "asset-processed-glb", path),
                handlers._register(wf, task, execution, "asset-processing-report", report),
            ],
        )

    handlers.process = process_stub
    process_metadata = engine.handler_registry._handlers["asset_process"].metadata
    engine.handler_registry._handlers["asset_process"] = RegisteredTaskHandler(
        process_stub, process_metadata
    )
    handlers.godot = godot_stub
    godot_metadata = engine.handler_registry._handlers["asset_godot"].metadata
    engine.handler_registry._handlers["asset_godot"] = RegisteredTaskHandler(
        godot_stub, godot_metadata
    )

    res = engine.run_workflow(workflow_id)
    assert res.status == WorkflowStatus.BLOCKED
    v1_approval_id = res.pending_approval_id
    assert v1_approval_id is not None

    _request_changes(db, v1_approval_id, comment="Lighting revision needed")
    res_blocked = engine.run_workflow(workflow_id)
    assert res_blocked.status == WorkflowStatus.BLOCKED
    assert res_blocked.error_code == "CONCEPT_REVISION_REQUIRED"

    art_repo = ArtifactRepository(db)
    v1_cv = ConceptVersionRepository(db).active_for_workflow(workflow_id)
    assert v1_cv is not None
    v1_art = art_repo.get(v1_cv.artifact_id)
    assert v1_art is not None

    v2_png, v2_prov = _create_concept_v2(tmp_path / "scratch")
    v2_sha = hashlib.sha256(v2_png.read_bytes()).hexdigest()

    result = replace_concept(
        engine.project_root,
        db,
        engine,
        workflow_id,
        v2_png,
        v2_prov,
        actor="lead_artist",
        reason="revised lighting",
    )
    assert result["old_version"] == 1
    assert result["new_version"] == 2
    assert result["old_concept_sha256"] == v1_art.content_hash
    assert result["new_concept_sha256"] == v2_sha
    assert result["new_approval_id"] is not None
    assert not result["dry_run"]

    # Verify v1 row is SUPERSEDED, files/artifacts intact
    cv_repo = ConceptVersionRepository(db)
    all_cvs = cv_repo.list_for_revision(result["asset_id"], result["revision"])
    assert len(all_cvs) == 2
    assert (
        all_cvs[0].version == 1
        and all_cvs[0].status == "SUPERSEDED"
        and all_cvs[0].superseded_at is not None
    )
    assert all_cvs[1].version == 2 and all_cvs[1].status == "ACTIVE"
    ArtifactManager(engine.project_root).verify_artifact_integrity(v1_art)

    # v1 approval still CHANGES_REQUESTED
    v1_app = ApprovalRepository(db).get(v1_approval_id)
    assert v1_app is not None and v1_app.status == ApprovalStatus.CHANGES_REQUESTED

    # v2 has new hash, asset_revisions has new hash
    rev = AssetRevisionRepository(db).get(result["asset_id"], result["revision"])
    assert rev is not None and rev.concept_hash == v2_sha

    # New pending concept approval exists, provider intents 0, invocations 0
    new_concept_app = ApprovalRepository(db).get(result["new_approval_id"])
    assert new_concept_app is not None and new_concept_app.status == ApprovalStatus.PENDING
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []
    assert fake.invocation_count == 0

    # Approve v2 -> resume -> PAID-REQUEST snapshot binding concept_version == 2
    _decision(engine, db, result["new_approval_id"], approve=True)
    res_paid_gate = engine.run_workflow(workflow_id)
    assert res_paid_gate.status == WorkflowStatus.BLOCKED
    assert res_paid_gate.pending_approval_id is not None

    snap_rec = PaidRequestSnapshotRepository(db).get_active_for_workflow(workflow_id)
    assert snap_rec is not None
    assert snap_rec.concept_version == 2
    snap_data = json.loads(snap_rec.canonical_json)
    assert snap_data["binding"]["concept_version"] == 2
    assert snap_data["binding"]["concept_sha256"] == v2_sha

    # Readiness passes
    readiness_rec = ProductionReadinessRepository(db).get_active_for_workflow(workflow_id)
    assert readiness_rec is not None and readiness_rec.result == "PASS"

    # Paid approval -> approve -> exactly one provider submission -> downstream stubs complete to final review gate
    paid_app_id = res_paid_gate.pending_approval_id
    paid_app = ApprovalRepository(db).get(paid_app_id)
    assert paid_app is not None and paid_app.approval_type == "paid_generation"

    _decision(engine, db, paid_app_id, approve=True)
    res_final = engine.run_workflow(workflow_id)
    assert res_final.status == WorkflowStatus.BLOCKED
    assert fake.invocation_count == 1
    assert len(ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)) == 1

    assert res_final.pending_approval_id is not None
    final_app = ApprovalRepository(db).get(res_final.pending_approval_id)
    assert final_app is not None and final_app.approval_type == "final_visual_review"

    # Evidence bundle export selects v2 concept and cold verifier passes
    out_dir = engine.project_root / "bundle_export"
    export_asset_evidence_bundle(engine.project_root, db, workflow_id, out_dir)

    manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
    concept_entries = [f for f in manifest["files"] if f["role"] == "concept"]
    assert len(concept_entries) == 1
    assert concept_entries[0]["sha256"] == v2_sha

    # Cold verifier run with sys.executable -I
    cmd = [sys.executable, "-I", str(out_dir / "verify_asset_bundle.py"), str(out_dir)]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    assert proc.returncode == 0, (
        f"Verifier failed ({proc.returncode}):\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
    )

    # Semantic tampering with a consistent manifest must still be rejected by the
    # V0.6 binding rules (not merely by the digest check).
    def rehashed_copy(name: str, relative: str, mutate: Any) -> Path:
        copy = engine.project_root / name
        shutil.copytree(out_dir, copy)
        target = copy / relative
        data = json.loads(target.read_text(encoding="utf-8"))
        mutate(data)
        target.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        raw = target.read_bytes()
        manifest_path = copy / "manifest.json"
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        for entry in manifest_data["files"]:
            if entry["path"] == relative:
                entry["sha256"] = hashlib.sha256(raw).hexdigest()
                entry["size"] = len(raw)
        manifest_path.write_text(json.dumps(manifest_data, indent=2) + "\n", encoding="utf-8")
        return copy

    def verify(bundle: Path) -> str:
        result = subprocess.run(
            [sys.executable, "-I", str(bundle / "verify_asset_bundle.py"), str(bundle)],
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        return result.stdout

    def point_snapshot_at_v1(data: dict[str, Any]) -> None:
        data["binding"]["concept_sha256"] = v1_art.content_hash

    def point_approval_at_v1(data: dict[str, Any]) -> None:
        data["inputs"]["scope"]["handler_context"]["concept_sha256"] = v1_art.content_hash

    snapshot_tampered = rehashed_copy(
        "bundle_snapshot_tampered", "evidence/paid_request_snapshot.json", point_snapshot_at_v1
    )
    assert "paid approval does not bind" in verify(snapshot_tampered)
    approval_tampered = rehashed_copy(
        "bundle_concept_approval_tampered", "evidence/concept_approval.json", point_approval_at_v1
    )
    assert "approval" in verify(approval_tampered)


def test_replace_after_concept_approved_paid_pending(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    res1 = engine.run_workflow(workflow_id)
    assert res1.status == WorkflowStatus.BLOCKED
    concept_app_id = res1.pending_approval_id
    assert concept_app_id is not None
    _decision(engine, db, concept_app_id, approve=True)

    res2 = engine.run_workflow(workflow_id)
    assert res2.status == WorkflowStatus.BLOCKED
    old_paid_app_id = res2.pending_approval_id
    assert old_paid_app_id is not None

    snap_repo = PaidRequestSnapshotRepository(db)
    readiness_repo = ProductionReadinessRepository(db)
    old_snap = snap_repo.get_active_for_workflow(workflow_id)
    old_readiness = readiness_repo.get_active_for_workflow(workflow_id)
    assert old_snap is not None and old_readiness is not None

    v2_png, v2_prov = _create_concept_v2(tmp_path / "scratch2")
    v2_sha = hashlib.sha256(v2_png.read_bytes()).hexdigest()

    result = replace_concept(
        engine.project_root,
        db,
        engine,
        workflow_id,
        v2_png,
        v2_prov,
        actor="artist",
        reason="update before paid",
    )
    assert result["old_version"] == 1
    assert result["new_version"] == 2

    # Old snapshot/readiness become SUPERSEDED
    snap_after = snap_repo.get_active_for_workflow(workflow_id)
    readiness_after = readiness_repo.get_active_for_workflow(workflow_id)
    assert snap_after is None
    assert readiness_after is None
    old_snap_rec = next(r for r in snap_repo.list_by_workflow(workflow_id) if r.id == old_snap.id)
    assert old_snap_rec.status == "SUPERSEDED"

    # Old pending paid approval can no longer be approved due to hash mismatch
    old_paid_app = ApprovalRepository(db).get(old_paid_app_id)
    assert old_paid_app is not None
    paid_task = TaskRepository(db).get(old_paid_app.task_id)
    assert paid_task is not None
    wf = WorkflowRepository(db).get(workflow_id)
    assert wf is not None

    with pytest.raises(ApprovalRequired):
        ApprovalService.approve(
            old_paid_app,
            "tester",
            current_inputs=engine.approval_inputs(wf, paid_task),
        )

    # Approve v2 concept approval
    new_concept_app_id = result["new_approval_id"]
    assert new_concept_app_id is not None
    _decision(engine, db, new_concept_app_id, approve=True)

    # Resume workflow -> executes snapshot and readiness for v2 -> stops at new paid approval
    res3 = engine.run_workflow(workflow_id)
    assert res3.status == WorkflowStatus.BLOCKED
    assert res3.pending_approval_id is not None and res3.pending_approval_id != old_paid_app_id

    new_snap = snap_repo.get_active_for_workflow(workflow_id)
    assert new_snap is not None
    assert new_snap.concept_version == 2
    assert new_snap.status == "ACTIVE"
    content = json.loads(new_snap.canonical_json)
    assert content["binding"]["concept_sha256"] == v2_sha


def test_old_pending_concept_approval_cannot_be_approved_after_replacement(
    tmp_path: Path,
) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    res = engine.run_workflow(workflow_id)
    assert res.status == WorkflowStatus.BLOCKED
    old_concept_app_id = res.pending_approval_id
    assert old_concept_app_id is not None

    v2_png, v2_prov = _create_concept_v2(tmp_path / "scratch3")
    replace_concept(
        engine.project_root,
        db,
        engine,
        workflow_id,
        v2_png,
        v2_prov,
        actor="artist",
        reason="replace while pending",
    )

    old_app = ApprovalRepository(db).get(old_concept_app_id)
    assert old_app is not None
    concept_task = TaskRepository(db).get(old_app.task_id)
    assert concept_task is not None
    wf = WorkflowRepository(db).get(workflow_id)
    assert wf is not None

    with pytest.raises(ApprovalRequired):
        ApprovalService.approve(
            old_app,
            "tester",
            current_inputs=engine.approval_inputs(wf, concept_task),
        )


def test_replace_refused_when_provider_intent_exists(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    # Reach paid, approve, dispatch once
    res1 = engine.run_workflow(workflow_id)
    assert res1.pending_approval_id is not None
    _decision(engine, db, res1.pending_approval_id, approve=True)
    res2 = engine.run_workflow(workflow_id)
    assert res2.pending_approval_id is not None
    _decision(engine, db, res2.pending_approval_id, approve=True)
    engine.run_workflow(workflow_id)  # Dispatches paid

    assert len(ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)) > 0

    # Dump state before
    # Dump state before
    cv_before = [
        c.to_dict()
        for c in ConceptVersionRepository(db).list_for_revision("prop_energy_crate_01", 1)
    ]
    apps_before = [a.status.value for a in ApprovalRepository(db).list_by_workflow(workflow_id)]
    tasks_before = [
        (t.id, t.status.value) for t in TaskRepository(db).list_by_workflow(workflow_id)
    ]

    v2_png, v2_prov = _create_concept_v2(tmp_path / "scratch4")
    with pytest.raises(ValidationError, match="new asset revision"):
        replace_concept(
            engine.project_root,
            db,
            engine,
            workflow_id,
            v2_png,
            v2_prov,
            actor="artist",
            reason="cannot replace",
        )

    # State identical after
    cv_after = [
        c.to_dict()
        for c in ConceptVersionRepository(db).list_for_revision("prop_energy_crate_01", 1)
    ]
    apps_after = [a.status.value for a in ApprovalRepository(db).list_by_workflow(workflow_id)]
    tasks_after = [(t.id, t.status.value) for t in TaskRepository(db).list_by_workflow(workflow_id)]

    assert cv_before == cv_after
    assert apps_before == apps_after
    assert tasks_before == tasks_after


def test_replace_refused_when_paid_approval_approved_not_dispatched(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    res1 = engine.run_workflow(workflow_id)
    assert res1.pending_approval_id is not None
    _decision(engine, db, res1.pending_approval_id, approve=True)
    res2 = engine.run_workflow(workflow_id)
    paid_app_id = res2.pending_approval_id
    assert paid_app_id is not None
    _decision(engine, db, paid_app_id, approve=True)  # Approved, not yet dispatched

    v2_png, v2_prov = _create_concept_v2(tmp_path / "scratch5")
    with pytest.raises(ValidationError, match="new asset revision"):
        replace_concept(
            engine.project_root,
            db,
            engine,
            workflow_id,
            v2_png,
            v2_prov,
            actor="artist",
            reason="should fail",
        )


def test_replace_refused_for_legacy_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    orig_create = harness.create_asset_production_workflow

    def legacy_graph(*args, **kwargs):
        workflow, tasks = orig_create(*args, **kwargs)
        removed = {f"{workflow.id}-PAID-REQUEST", f"{workflow.id}-READINESS"}
        legacy = [task for task in tasks if task.id not in removed]
        for task in legacy:
            task.parameters = {k: v for k, v in task.parameters.items() if k != "graph_version"}
            if task.id.endswith("-PAID-GENERATION"):
                task.depends_on = [f"{workflow.id}-CONCEPT-REVIEW"]
        return workflow, legacy

    monkeypatch.setattr(harness, "create_asset_production_workflow", legacy_graph)
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    res = engine.run_workflow(workflow_id)
    assert res.status == WorkflowStatus.BLOCKED

    v2_png, v2_prov = _create_concept_v2(tmp_path / "scratch6")
    with pytest.raises(ValidationError, match="new asset revision"):
        replace_concept(
            engine.project_root,
            db,
            engine,
            workflow_id,
            v2_png,
            v2_prov,
            actor="artist",
            reason="legacy graph test",
        )


def test_dry_run_changes_nothing(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, _ = _setup_accounting_env(tmp_path)
    res = engine.run_workflow(workflow_id)
    assert res.pending_approval_id is not None
    _request_changes(db, res.pending_approval_id, comment="revise")
    engine.run_workflow(workflow_id)

    cv_before = [
        c.to_dict()
        for c in ConceptVersionRepository(db).list_for_revision("prop_energy_crate_01", 1)
    ]
    apps_before = [a.id for a in ApprovalRepository(db).list_by_workflow(workflow_id)]
    tasks_before = [
        (t.id, t.status.value) for t in TaskRepository(db).list_by_workflow(workflow_id)
    ]
    arts_before = [a.id for a in ArtifactRepository(db).list_by_workflow(workflow_id)]

    v2_png, v2_prov = _create_concept_v2(tmp_path / "scratch7")
    plan = replace_concept(
        engine.project_root,
        db,
        engine,
        workflow_id,
        v2_png,
        v2_prov,
        actor="artist",
        reason="dry run check",
        dry_run=True,
    )
    assert plan["dry_run"] is True
    assert plan["new_approval_id"] is None
    assert plan["new_version"] == 2

    cv_after = [
        c.to_dict()
        for c in ConceptVersionRepository(db).list_for_revision("prop_energy_crate_01", 1)
    ]
    apps_after = [a.id for a in ApprovalRepository(db).list_by_workflow(workflow_id)]
    tasks_after = [(t.id, t.status.value) for t in TaskRepository(db).list_by_workflow(workflow_id)]
    arts_after = [a.id for a in ArtifactRepository(db).list_by_workflow(workflow_id)]

    assert cv_before == cv_after
    assert apps_before == apps_after
    assert tasks_before == tasks_after
    assert arts_before == arts_after


def test_cli_smoke_asset_concept_replace(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = tmp_path / "cli_project"
    root.mkdir(parents=True, exist_ok=True)
    db = Database(root / ".gamefactory/state/factory.db")
    MigrationRunner(db).apply_all()

    assert main(["--project", str(root), "init"]) == 0

    project = Project(id="test-proj", name="Test Project", engine_type="godot", root_path=str(root))
    from gamefactory.adapters.persistence.repositories import ProjectRepository

    ProjectRepository(db).save(project)

    image = root / "concept.png"
    Image.new("RGB", (2, 2), (20, 70, 140)).save(image)
    provenance = root / "concept.json"
    provenance.write_text(
        json.dumps({"sha256": hashlib.sha256(image.read_bytes()).hexdigest()}), encoding="utf-8"
    )

    spec = parse_asset_specification(SPEC_PATH)
    from gamefactory.adapters.fakes.fake_provider import FakeAssetGenerationProvider
    from gamefactory.workflows.asset_production import (
        AssetProductionHandlers,
        create_asset_production_workflow,
        register_asset_production_handlers,
    )
    from gamefactory.workflows.engine import WorkflowEngine

    workflow, tasks = create_asset_production_workflow(
        project.id,
        root,
        spec,
        image,
        provenance,
        provider_name="fake",
        provider_estimate=5.0,
        revision_repository=AssetRevisionRepository(db),
    )
    engine = WorkflowEngine(root, db)
    handlers = AssetProductionHandlers(
        root,
        engine.art_repo,
        AssetRevisionRepository(db),
        engine.app_repo,
        ProviderOperationIntentRepository(db),
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
        FakeAssetGenerationProvider(),
    )
    register_asset_production_handlers(engine.handler_registry, handlers)
    engine.register_workflow(workflow, tasks, allow_existing_empty_placeholder=True)
    res = engine.run_workflow(workflow.id)
    assert res.pending_approval_id is not None

    # Request changes
    assert (
        main(
            [
                "--project",
                str(root),
                "request-changes",
                res.pending_approval_id,
                "--comment",
                "Needs revision",
                "--actor",
                "lead",
            ]
        )
        == 0
    )

    v2_png, v2_prov = _create_concept_v2(root / "new_concepts")

    # 1. Dry run with --json
    capsys.readouterr()
    exit_code_dry = main(
        [
            "--project",
            str(root),
            "asset",
            "concept",
            "replace",
            "--workflow",
            workflow.id,
            "--concept",
            str(v2_png),
            "--provenance",
            str(v2_prov),
            "--actor",
            "lead",
            "--reason",
            "cli smoke dry run",
            "--dry-run",
            "--json",
        ]
    )
    assert exit_code_dry == 0
    captured_dry = capsys.readouterr()
    parsed_dry = json.loads(captured_dry.out)
    assert parsed_dry["dry_run"] is True
    assert parsed_dry["old_version"] == 1
    assert parsed_dry["new_version"] == 2

    # 2. Real replacement without --dry-run (human text output)
    exit_code_apply = main(
        [
            "--project",
            str(root),
            "asset",
            "concept",
            "replace",
            "--workflow",
            workflow.id,
            "--concept",
            str(v2_png),
            "--provenance",
            str(v2_prov),
            "--actor",
            "lead",
            "--reason",
            "cli smoke real replacement",
        ]
    )
    assert exit_code_apply == 0
    captured_apply = capsys.readouterr()
    assert "Concept replaced for asset" in captured_apply.out
    assert "Version: v1 -> v2" in captured_apply.out
    assert "gamefactory approve" in captured_apply.out

    # 3. Test asset inspect shows concept_versions
    exit_code_inspect = main(
        [
            "--project",
            str(root),
            "asset",
            "inspect",
            spec.asset_id,
            "--json",
        ]
    )
    assert exit_code_inspect == 0
    captured_inspect = capsys.readouterr()
    parsed_inspect = json.loads(captured_inspect.out)
    assert "concept_versions" in parsed_inspect
    assert len(parsed_inspect["concept_versions"]) == 2
    assert parsed_inspect["concept_versions"][0]["version"] == 1
    assert parsed_inspect["concept_versions"][0]["status"] == "SUPERSEDED"
    assert parsed_inspect["concept_versions"][1]["version"] == 2
    assert parsed_inspect["concept_versions"][1]["status"] == "ACTIVE"


def test_interrupted_replacement_cannot_dispatch_a_snapshot_for_a_superseded_concept(
    tmp_path: Path,
) -> None:
    """A new ACTIVE concept whose snapshot was never superseded must not be paid for."""
    engine, db, workflow_id, fake, handlers = _setup_accounting_env(tmp_path)
    concept_gate = engine.run_workflow(workflow_id)
    _decision(engine, db, concept_gate.pending_approval_id or "", approve=True)
    paid_gate = engine.run_workflow(workflow_id)
    _decision(engine, db, paid_gate.pending_approval_id or "", approve=True)

    # Simulate a replacement interrupted after the version switch: the approved
    # snapshot still names v1 while v2 became ACTIVE.
    versions = ConceptVersionRepository(db)
    active = versions.active_for_workflow(workflow_id)
    assert active is not None
    v2_png, _ = _create_concept_v2(tmp_path / "interrupted")
    target = handlers._path(
        TaskRepository(db).get(f"{workflow_id}-CONCEPT-REVIEW"), "concept-v2.png"
    )
    target.write_bytes(v2_png.read_bytes())
    artifact = ArtifactManager(engine.project_root).register_file_artifact(
        workflow_id,
        f"{workflow_id}-CONCEPT-REVIEW",
        "asset-concept",
        "asset_concept_review",
        target.relative_to(engine.project_root).as_posix(),
    )
    ArtifactRepository(db).save(artifact)
    versions.supersede_and_add(
        active.id,
        ConceptVersionRecord(
            id="VER-INTERRUPTED",
            workflow_id=workflow_id,
            asset_id=active.asset_id,
            revision_number=active.revision_number,
            version=2,
            artifact_id=artifact.id,
            content_hash=artifact.content_hash,
            actor="test",
            reason="interrupted replacement",
        ),
    )

    engine.run_workflow(workflow_id)

    assert fake.invocation_count == 0
    assert ProviderOperationIntentRepository(db).list_by_workflow(workflow_id) == []
