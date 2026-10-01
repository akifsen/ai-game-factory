"""C2-B unit tests: production exporter, publication CAS, and evidence readiness."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pytest

from gamefactory.adapters.assets.v08_candidate_evidence import (
    CandidateEvidenceExporter,
    atomic_publish_staged_container,
    bind_production_candidate_evidence_exporter,
    fingerprint_publish_container,
    parse_bounded_publication_json,
)
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    Execution,
    ExecutionStatus,
    TaskStatus,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.workflows.v08_candidate_evidence_readiness import (
    assert_candidate_evidence_complete,
    assert_candidate_workflow_engine_finalized,
    candidate_evidence_readiness,
)
from gamefactory.workflows.v08_candidate_workflow import candidate_workflow_readiness
from tests.unit.test_v08_candidate_evidence_cold import (
    VERIFIER,
    _assert_c1_derived_bundle_copy_fidelity,
    _run_cold,
)
from tests.unit.test_v08_candidate_workflow import (
    _approve,
    _assert_no_c2_export_artifacts,
    _handlers,
    _run_to_test_only_gate,
)
from tests.unit.v08_candidate_c2b_publication_fixtures import (
    assert_single_c2_export_triplet,
    copy_prepared_stage_to_managed_namespace,
    inject_destination_directory_junction,
    inject_destination_file,
    inject_destination_immediately_before_atomic_rename,
    inject_empty_destination_directory,
    inject_nonempty_destination_directory,
    make_commit_denier,
    make_partial_insert_denier,
    race_publication_from_prepared_stages,
    run_workflow_to_evidence_publication_gate,
)


def _run_full_candidate_evidence(handlers, engine, workspace, workflow_id: str) -> None:
    blocked = engine.run_workflow(workflow_id)
    assert blocked.pending_approval_id
    _approve(engine, workspace.db, blocked.pending_approval_id)
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    if final.status != WorkflowStatus.COMPLETED:
        evidence = next(
            t
            for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
            if t.task_type == "v08_candidate_evidence"
        )
        from gamefactory.adapters.persistence.repositories import ExecutionRepository

        latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
        pytest.fail(latest.error_message if latest else final.error_message)
    assert final.status == WorkflowStatus.COMPLETED


def test_mock_c1_through_evidence_completed_readiness_and_outside_cold(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    prepare_snapshot = candidate_workflow_readiness(handlers, workflow_id)
    original_snapshot = json.loads(json.dumps(prepare_snapshot.payload))
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)

    readiness = assert_candidate_evidence_complete(handlers, workflow_id)
    assert readiness.candidate_evidence_complete is True
    assert readiness.production_eligible is False
    assert readiness.promotion_eligible is False

    manifest = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-evidence-manifest"
    )
    cold_bundle = workspace.root / Path(manifest.relative_path).parent
    copied = tmp_path / "outside-checkout"
    shutil.copytree(cold_bundle, copied)
    code, out, err = _run_cold(copied, verifier=VERIFIER)
    assert code == 0, out + err
    assert out.strip().splitlines()[0] == "CONSISTENT_BUT_UNAUTHENTICATED"
    _assert_c1_derived_bundle_copy_fidelity(
        workspace=workspace,
        workflow_id=workflow_id,
        bundle=cold_bundle,
        original_snapshot=original_snapshot,
    )


def test_pre_evidence_readiness_ok_while_evidence_readiness_fails(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace)
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    engine.run_workflow(workflow_id)
    candidate_workflow_readiness(handlers, workflow_id)
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_single_completion_marker_registered(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    markers = [
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-export-marker"
    ]
    assert len(markers) == 1


def _fail_evidence_run(handlers, engine, workspace, workflow_id: str) -> Execution:
    blocked = engine.run_workflow(workflow_id)
    assert blocked.pending_approval_id
    _approve(engine, workspace.db, blocked.pending_approval_id)
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    assert evidence.status == TaskStatus.FAILED
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    return latest


def test_exporter_rejects_foreign_database(tmp_path: Path) -> None:
    from gamefactory.adapters.persistence.database import Database
    from gamefactory.adapters.persistence.migrations import MigrationRunner
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    foreign = Database(tmp_path / "foreign.db")
    MigrationRunner(foreign).apply_all()
    with pytest.raises(ValidationError, match="factory-managed"):
        CandidateEvidenceExporter(
            project_root=workspace.root,
            db=foreign,
            artifacts=ArtifactRepository(workspace.db),
            artifact_manager=ArtifactManager(workspace.root),
            approvals=ApprovalRepository(workspace.db),
            executions=ExecutionRepository(workspace.db),
            tasks=TaskRepository(workspace.db),
        )


def test_post_cold_payload_mutation_fails_without_published_marker(tmp_path: Path) -> None:
    from gamefactory.adapters.assets import v08_candidate_evidence as evidence_mod
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    original_cold = evidence_mod.trusted_cold_verify_candidate_bundle

    def _cold_then_mutate(bundle_dir):
        result = original_cold(bundle_dir)
        manifest = bundle_dir / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        return result

    with patch(
        "gamefactory.workflows.v08_candidate_workflow.trusted_cold_verify_candidate_bundle",
        _cold_then_mutate,
    ):
        _fail_evidence_run(handlers, engine, workspace, workflow_id)


def test_post_dready_container_mutation_fails_publication(tmp_path: Path) -> None:
    from gamefactory.adapters.assets import v08_candidate_evidence as evidence_mod
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    original_prepare = evidence_mod.prepare_evidence_publication_container

    def _prepare_then_mutate(*args, **kwargs):
        layout, d_ready = original_prepare(*args, **kwargs)
        target = layout.container_root / "bundle" / "snapshot" / "snapshot.json"
        target.write_bytes(target.read_bytes() + b"\n")
        return layout, d_ready

    with patch(
        "gamefactory.workflows.v08_candidate_workflow.prepare_evidence_publication_container",
        _prepare_then_mutate,
    ):
        _fail_evidence_run(handlers, engine, workspace, workflow_id)


def test_existing_publication_destination_blocks_without_marker(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    base = handlers.c2_export_callback

    def _reserve_destination(request):
        container = Path(base(request))
        running = ExecutionRepository(workspace.db).get_latest_attempt(evidence_task.id)
        assert running is not None
        dest = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id / running.id
        dest.mkdir(parents=True, exist_ok=True)
        return container

    handlers.c2_export_callback = _reserve_destination
    _fail_evidence_run(handlers, engine, workspace, workflow_id)


def test_readiness_rejects_semantically_invalid_stored_result(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    repo = ArtifactRepository(workspace.db)
    result = next(
        a
        for a in repo.list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    )
    marker = next(
        a
        for a in repo.list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-export-marker"
    )
    result_path = workspace.root / result.relative_path
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    trusted = dict(doc["trusted_cold_result"])
    trusted["outcome"] = "FAILED"
    trusted["execution_provenance"] = "AUTHENTICATED_PRODUCTION"
    doc["trusted_cold_result"] = trusted
    result_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    result_hash = hashlib.sha256(result_path.read_bytes()).hexdigest()
    repo.save(replace(result, content_hash=result_hash, file_size=result_path.stat().st_size))
    marker_path = workspace.root / marker.relative_path
    marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_doc["result_sha256"] = result_hash
    marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    repo.save(
        replace(
            marker,
            content_hash=hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            file_size=marker_path.stat().st_size,
        )
    )
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_readiness_rejects_new_failed_attempt_after_completion(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    prepare = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    ExecutionRepository(workspace.db).save(
        Execution(
            id=generate_id("EXEC"),
            task_id=prepare.id,
            attempt_number=99,
            status=ExecutionStatus.FAILED,
            started_at=utc_now_iso(),
            completed_at=utc_now_iso(),
            external_op_id=None,
            pid=None,
            host=None,
            error_message="adversarial failed attempt",
            exit_code=1,
            stdout="",
            stderr="",
            cost=0,
            estimated_cost=0,
            cost_unit="usd",
            provider=None,
            retryable=False,
        )
    )
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_readiness_rejects_processed_revision_null_to_nonnull_drift(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    revisions = AssetRevisionRepository(workspace.db)
    rows = revisions.list_by_workflow(workflow_id)
    assert rows
    row = rows[0]
    drifted = replace(row, processed_glb_hash="f" * 64)
    revisions.save(drifted)
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_db_commit_failure_leaves_orphan_without_readiness(tmp_path: Path) -> None:
    from gamefactory.workflows import v08_candidate_evidence_publication as publication_mod
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    commit_flags: dict[str, object] = {}
    denier = make_commit_denier(workflow_id, commit_flags)

    with patch.object(publication_mod, "_commit_writer_connection", denier):
        latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)

    assert commit_flags.get("reached_inserts") is True
    assert commit_flags.get("denied_commit") is True
    assert isinstance(commit_flags.get("commit_exception"), sqlite3.DatabaseError)
    assert latest.error_message is not None
    assert "not authorized" in latest.error_message.lower() or "COMMIT" in latest.error_message
    assert "database is locked" not in (latest.error_message or "").lower()
    published = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id
    assert published.exists()
    assert any(published.iterdir())
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_atomic_publish_rejects_existing_destination_file(tmp_path: Path) -> None:
    publish_parent = tmp_path / "pub"
    publish_parent.mkdir()
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "bundle").mkdir()
    dest = publish_parent / "EXEC-1"
    dest.write_text("reserved", encoding="utf-8")
    with pytest.raises(Exception, match="already exists"):
        atomic_publish_staged_container(staging, publish_parent, "EXEC-1")


def test_publication_inventory_rejects_symlink_child_directory(tmp_path: Path) -> None:
    import subprocess
    import sys

    from gamefactory.adapters.assets.v08_candidate_evidence import COLD_BUNDLE_DIRNAME

    if sys.platform == "win32":
        target = tmp_path / "target"
        target.mkdir()
        junction = tmp_path / "junction"
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(junction), str(target)],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("junction creation unavailable")
        root = junction / "container"
        bundle = root / COLD_BUNDLE_DIRNAME
        bundle.mkdir(parents=True)
        (bundle / "manifest.json").write_text("{}", encoding="utf-8")
        with pytest.raises(ValidationError, match="bounded|lexical"):
            fingerprint_publish_container(root)
        return

    root = tmp_path / "container"
    bundle = root / COLD_BUNDLE_DIRNAME
    bundle.mkdir(parents=True)
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    real = bundle / "real"
    real.mkdir()
    (real / "file.txt").write_text("x", encoding="utf-8")
    link_parent = bundle / "linked"
    if not hasattr(os, "symlink"):
        pytest.skip("symlink unsupported on this platform")
    try:
        os.symlink(real, link_parent, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires elevated privileges on this host")
    with pytest.raises(ValidationError, match="bounded"):
        fingerprint_publish_container(root)


def test_evidence_task_completed_flip_blocks_publication(tmp_path: Path) -> None:
    from gamefactory.workflows import v08_candidate_evidence_publication as publication_mod
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    original_assert = publication_mod._assert_active_evidence_execution

    def _flip_task_under_lock(conn, handlers_arg, workflow, **kwargs):
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?;",
            (TaskStatus.FAILED.value, evidence_task.id),
        )
        return original_assert(conn, handlers_arg, workflow, **kwargs)

    with patch.object(publication_mod, "_assert_active_evidence_execution", _flip_task_under_lock):
        _fail_evidence_run(handlers, engine, workspace, workflow_id)


def test_engine_finalization_required_for_evidence_complete(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    TaskRepository(workspace.db).update_status(evidence_task.id, TaskStatus.RUNNING)
    with pytest.raises(CandidateCurrentnessError):
        assert_candidate_workflow_engine_finalized(handlers, workflow_id)


def test_deep_mutable_callback_snapshot_mutation_fails_publication(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    base = handlers.c2_export_callback

    def _mutate_snapshot(request):
        request.snapshot.payload["source_glb_hash"] = "f" * 64
        return base(request)

    handlers.c2_export_callback = _mutate_snapshot
    _fail_evidence_run(handlers, engine, workspace, workflow_id)


def test_newer_evidence_running_attempt_blocks_publication(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    base = handlers.c2_export_callback

    def _inject_running_attempt(request):
        container = Path(base(request))
        ExecutionRepository(workspace.db).save(
            Execution(
                id=generate_id("EXEC"),
                task_id=evidence_task.id,
                attempt_number=99,
                status=ExecutionStatus.RUNNING,
                started_at=utc_now_iso(),
                completed_at=None,
                external_op_id=None,
                pid=None,
                host=None,
                error_message=None,
                exit_code=None,
                stdout="",
                stderr="",
                cost=0,
                estimated_cost=0,
                cost_unit="usd",
                provider=None,
                retryable=False,
            )
        )
        return container

    handlers.c2_export_callback = _inject_running_attempt
    _fail_evidence_run(handlers, engine, workspace, workflow_id)


def test_foreign_manifest_workflow_id_fails_evidence(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    base = handlers.c2_export_callback

    def _foreign_manifest(request):
        container = Path(base(request))
        manifest = container / "bundle" / "manifest.json"
        doc = json.loads(manifest.read_text(encoding="utf-8"))
        doc["workflow_id"] = "WF-FOREIGN"
        manifest.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        return container

    handlers.c2_export_callback = _foreign_manifest
    _fail_evidence_run(handlers, engine, workspace, workflow_id)


def test_atomic_publish_moves_staging_directory(tmp_path: Path) -> None:
    publish_parent = tmp_path / "pub"
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "bundle").mkdir()
    (staging / "bundle" / "manifest.json").write_text("{}", encoding="utf-8")
    final = atomic_publish_staged_container(staging, publish_parent, "EXEC-1")
    assert final.is_dir()
    assert not staging.exists()
    assert (final / "bundle" / "manifest.json").is_file()


def test_atomic_publish_preserves_foreign_destination_file(tmp_path: Path) -> None:
    publish_parent = tmp_path / "pub"
    publish_parent.mkdir()
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "bundle").mkdir()
    dest = publish_parent / "EXEC-1"
    dest.write_text("foreign-payload", encoding="utf-8")
    with pytest.raises(Exception, match="already exists"):
        atomic_publish_staged_container(staging, publish_parent, "EXEC-1")
    assert dest.read_text(encoding="utf-8") == "foreign-payload"
    assert staging.is_dir()


def test_bounded_publication_json_rejects_duplicate_keys() -> None:
    raw = b'{"schema_version": "x", "schema_version": "y"}'
    with pytest.raises(ValidationError, match="duplicate JSON key"):
        parse_bounded_publication_json(raw)


def test_readiness_rejects_failed_task_with_completed_execution(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    TaskRepository(workspace.db).update_status(evidence_task.id, TaskStatus.FAILED)
    with pytest.raises(CandidateCurrentnessError, match="not completed"):
        candidate_evidence_readiness(handlers, workflow_id)


def test_readiness_rejects_controls_outside_published_container(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    repo = ArtifactRepository(workspace.db)
    result = next(
        a
        for a in repo.list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    )
    result_path = workspace.root / result.relative_path
    stray_dir = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id / "stray"
    stray_dir.mkdir(parents=True, exist_ok=True)
    stray_path = stray_dir / result_path.name
    shutil.copy2(result_path, stray_path)
    stray_rel = stray_path.relative_to(workspace.root).as_posix()
    with workspace.db.transaction() as conn:
        conn.execute(
            "UPDATE artifacts SET relative_path = ? WHERE id = ?;",
            (stray_rel, result.id),
        )
    with pytest.raises(CandidateCurrentnessError, match="same container"):
        candidate_evidence_readiness(handlers, workflow_id)


def test_candidate_evidence_lexical_rejects_symlink_ancestor(tmp_path: Path) -> None:
    import subprocess
    import sys

    from gamefactory.adapters.assets.v08_candidate_evidence import (
        COLD_BUNDLE_DIRNAME,
        candidate_evidence_lexical_unsafe,
    )

    if sys.platform == "win32":
        target = tmp_path / "target"
        target.mkdir()
        real_root = target / "real"
        real_root.mkdir()
        bundle = real_root / COLD_BUNDLE_DIRNAME
        bundle.mkdir()
        (bundle / "manifest.json").write_text("{}", encoding="utf-8")
        link_root = tmp_path / "junction"
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link_root), str(real_root)],
                check=True,
                capture_output=True,
            )
        except (OSError, subprocess.CalledProcessError):
            pytest.skip("junction creation unavailable")
        assert candidate_evidence_lexical_unsafe(link_root / COLD_BUNDLE_DIRNAME)
        with pytest.raises(ValidationError, match="reparse|link|lexical"):
            fingerprint_publish_container(link_root)
        return

    if not hasattr(os, "symlink"):
        pytest.skip("symlink unsupported on this platform")
    real_root = tmp_path / "real"
    real_root.mkdir()
    bundle = real_root / COLD_BUNDLE_DIRNAME
    bundle.mkdir()
    (bundle / "manifest.json").write_text("{}", encoding="utf-8")
    link_root = tmp_path / "linked"
    try:
        os.symlink(real_root, link_root, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires elevated privileges on this host")
    assert candidate_evidence_lexical_unsafe(link_root / COLD_BUNDLE_DIRNAME)
    with pytest.raises(ValidationError, match="reparse|link"):
        fingerprint_publish_container(link_root)


def test_readiness_invokes_trusted_cold_exactly_once(tmp_path: Path) -> None:
    from gamefactory.workflows import v08_candidate_evidence_readiness as readiness_mod
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    original = readiness_mod.trusted_cold_verify_candidate_bundle
    calls = 0

    def _counting_cold(bundle_dir):
        nonlocal calls
        calls += 1
        return original(bundle_dir)

    with patch.object(readiness_mod, "trusted_cold_verify_candidate_bundle", _counting_cold):
        candidate_evidence_readiness(handlers, workflow_id)
    assert calls == 1


def test_readiness_rejects_task_flip_during_only_cold(tmp_path: Path) -> None:
    from gamefactory.workflows import v08_candidate_evidence_readiness as readiness_mod
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    original = readiness_mod.trusted_cold_verify_candidate_bundle

    def _cold_then_flip(bundle_dir):
        result = original(bundle_dir)
        TaskRepository(workspace.db).update_status(evidence_task.id, TaskStatus.FAILED)
        return result

    with patch.object(readiness_mod, "trusted_cold_verify_candidate_bundle", _cold_then_flip):
        with pytest.raises(CandidateCurrentnessError, match="not completed under lock"):
            candidate_evidence_readiness(handlers, workflow_id)


def test_readiness_rejects_artifact_metadata_drift_after_cold(tmp_path: Path) -> None:
    from gamefactory.workflows import v08_candidate_evidence_readiness as readiness_mod
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    marker = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-export-marker"
    )
    original = readiness_mod.trusted_cold_verify_candidate_bundle

    def _cold_then_tamper(bundle_dir):
        result = original(bundle_dir)
        with workspace.db.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET producer = ? WHERE id = ?;",
                ("tampered-producer", marker.id),
            )
        return result

    with patch.object(readiness_mod, "trusted_cold_verify_candidate_bundle", _cold_then_tamper):
        with pytest.raises(CandidateCurrentnessError, match="registration metadata drifted"):
            candidate_evidence_readiness(handlers, workflow_id)


def test_readiness_rejects_coherent_rehash_wrong_request_digest(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    repo = ArtifactRepository(workspace.db)
    result = next(
        a
        for a in repo.list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    )
    marker = next(
        a
        for a in repo.list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-export-marker"
    )
    result_path = workspace.root / result.relative_path
    doc = json.loads(result_path.read_text(encoding="utf-8"))
    trusted = dict(doc["trusted_cold_result"])
    trusted["request_digest"] = "a" * 64
    trusted["verified_files"] = int(trusted.get("verified_files", 0))
    doc["trusted_cold_result"] = trusted
    result_path.write_text(json.dumps(doc, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    result_hash = hashlib.sha256(result_path.read_bytes()).hexdigest()
    repo.save(replace(result, content_hash=result_hash, file_size=result_path.stat().st_size))
    marker_path = workspace.root / marker.relative_path
    marker_doc = json.loads(marker_path.read_text(encoding="utf-8"))
    marker_doc["result_sha256"] = result_hash
    marker_path.write_text(
        json.dumps(marker_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
    )
    repo.save(
        replace(
            marker,
            content_hash=hashlib.sha256(marker_path.read_bytes()).hexdigest(),
            file_size=marker_path.stat().st_size,
        )
    )
    with pytest.raises(CandidateCurrentnessError, match="does not match fresh cold"):
        candidate_evidence_readiness(handlers, workflow_id)


def test_readiness_rejects_extra_root_file_in_publication_container(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    manifest = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-evidence-manifest"
    )
    container = (workspace.root / manifest.relative_path).parent.parent
    (container / "extra-unregistered.txt").write_text("x", encoding="utf-8")
    with pytest.raises(CandidateCurrentnessError, match="unexpected top-level"):
        candidate_evidence_readiness(handlers, workflow_id)


def test_managed_workflow_rejects_stage_extra_root_file_before_publication(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    base = handlers.c2_export_callback

    def _stage_extra_file(request):
        container = Path(base(request))
        (container / "extra-root.txt").write_text("unregistered", encoding="utf-8")
        return container

    handlers.c2_export_callback = _stage_extra_file
    _fail_evidence_run(handlers, engine, workspace, workflow_id)
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    published = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id
    assert not published.exists() or not any(published.iterdir())


def test_managed_workflow_rejects_stage_extra_root_directory_before_publication(
    tmp_path: Path,
) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    base = handlers.c2_export_callback

    def _stage_extra_dir(request):
        container = Path(base(request))
        (container / "extra-root-dir").mkdir()
        return container

    handlers.c2_export_callback = _stage_extra_dir
    _fail_evidence_run(handlers, engine, workspace, workflow_id)
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_concurrent_publication_cas_yields_single_triplet_and_final(tmp_path: Path) -> None:
    from gamefactory.workflows import v08_candidate_evidence_publication as publication_mod
    from gamefactory.workflows.v08_candidate_snapshot import CandidateBoundSnapshot

    workspace, workflow_id, handlers, workflow, evidence_task, execution, captured = (
        run_workflow_to_evidence_publication_gate(tmp_path)
    )
    kwargs = captured["kwargs"]
    snapshot_before: CandidateBoundSnapshot = kwargs["snapshot_before"]
    d_ready: str = kwargs["d_ready"]
    stage_source = Path(kwargs["staging_container"])
    stage_a = copy_prepared_stage_to_managed_namespace(workspace.root, stage_source, "race_a")
    stage_b = copy_prepared_stage_to_managed_namespace(workspace.root, stage_source, "race_b")

    running = replace(
        execution,
        status=ExecutionStatus.RUNNING,
        completed_at=None,
        error_message=None,
        exit_code=None,
    )
    ExecutionRepository(workspace.db).save(running)
    TaskRepository(workspace.db).update_status(evidence_task.id, TaskStatus.RUNNING)

    def _publish_from(stage: Path) -> None:
        publication_mod.publish_candidate_evidence_from_staging(
            handlers,
            workflow,
            evidence_task,
            running,
            snapshot_before=snapshot_before,
            staging_container=stage,
            d_ready=d_ready,
        )

    outcomes = race_publication_from_prepared_stages(_publish_from, stage_a, stage_b)
    assert sum(exc is None for exc in outcomes) == 1
    losers = [exc for exc in outcomes if exc is not None]
    assert len(losers) == 1
    assert isinstance(losers[0], ValidationError)
    assert "completion marker already published" in str(losers[0])
    assert_single_c2_export_triplet(workspace.db, workflow_id)
    final_root = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id / running.id
    assert final_root.is_dir()
    surviving_stages = [stage for stage in (stage_a, stage_b) if stage.exists()]
    assert len(surviving_stages) == 1


def test_post_rename_payload_mutation_rolls_back_without_artifact_rows(tmp_path: Path) -> None:
    from gamefactory.adapters.assets import v08_candidate_evidence as evidence_mod
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    original_publish = evidence_mod.atomic_publish_staged_container

    def _publish_then_mutate(staging_container, publish_parent, execution_id):
        final = original_publish(staging_container, publish_parent, execution_id)
        manifest = final / "bundle" / "manifest.json"
        manifest.write_bytes(manifest.read_bytes() + b" ")
        return final

    with patch(
        "gamefactory.workflows.v08_candidate_evidence_publication.atomic_publish_staged_container",
        _publish_then_mutate,
    ):
        _fail_evidence_run(handlers, engine, workspace, workflow_id)
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    orphan = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id
    assert orphan.exists() and any(orphan.iterdir())
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_engine_finalization_failure_after_publication_blocks_readiness(tmp_path: Path) -> None:
    from gamefactory.workflows.engine import WorkflowEngine
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    original_finalize = WorkflowEngine._finalize_execution

    def _fail_evidence_finalize(self, task, execution, event) -> None:
        if task.task_type == "v08_candidate_evidence":
            raise ValidationError("simulated engine finalization failure after publication")
        return original_finalize(self, task, execution, event)

    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    saw_finalize_failure = False
    with patch.object(WorkflowEngine, "_finalize_execution", _fail_evidence_finalize):
        for _ in range(12):
            try:
                engine.run_workflow(workflow_id)
            except ValidationError as exc:
                if "simulated engine finalization failure after publication" in str(exc):
                    saw_finalize_failure = True
                    break
                raise
    assert saw_finalize_failure
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence_task.id)
    assert latest is not None
    assert latest.status != ExecutionStatus.COMPLETED
    assert_single_c2_export_triplet(workspace.db, workflow_id)
    with pytest.raises(CandidateCurrentnessError):
        candidate_evidence_readiness(handlers, workflow_id)


def test_atomic_publish_rejects_existing_empty_destination_directory(tmp_path: Path) -> None:
    from gamefactory.core.domain.errors import ArtifactError

    publish_parent = tmp_path / "pub"
    publish_parent.mkdir()
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "bundle").mkdir()
    (staging / "bundle" / "manifest.json").write_text("{}", encoding="utf-8")
    dest = publish_parent / "EXEC-1"
    dest.mkdir()
    with pytest.raises(ArtifactError, match="already exists"):
        atomic_publish_staged_container(staging, publish_parent, "EXEC-1")
    assert dest.is_dir()
    assert staging.is_dir()


@pytest.mark.parametrize(
    ("injector", "preserve_label"),
    [
        (inject_empty_destination_directory, "empty_dir"),
        (inject_nonempty_destination_directory, "nonempty_dir"),
        (inject_destination_file, "file"),
        (inject_destination_directory_junction, "junction"),
    ],
)
def test_atomic_publish_syscall_race_preserves_stage_and_foreign_destination(
    tmp_path: Path,
    injector: object,
    preserve_label: str,
) -> None:
    from gamefactory.core.domain.errors import ArtifactError

    publish_parent = tmp_path / "pub"
    publish_parent.mkdir()
    staging = tmp_path / "stage"
    staging.mkdir()
    (staging / "bundle").mkdir()
    manifest = staging / "bundle" / "manifest.json"
    manifest.write_text("{}", encoding="utf-8")
    stage_digest = manifest.read_bytes()
    foreign_probe: dict[str, str] = {}

    def _capture_foreign(_src: str, dst: str) -> None:
        injector(_src, dst)
        if preserve_label == "file":
            foreign_probe["value"] = Path(dst).read_text(encoding="utf-8")
        elif preserve_label == "junction":
            foreign_probe["value"] = Path(dst).joinpath("inside.txt").read_text(encoding="utf-8")
        else:
            foreign_probe["value"] = "dir"

    with inject_destination_immediately_before_atomic_rename(_capture_foreign):
        with pytest.raises(ArtifactError, match="already exists|rename failed"):
            atomic_publish_staged_container(staging, publish_parent, "EXEC-1")
    assert staging.is_dir()
    assert manifest.read_bytes() == stage_digest
    dest = publish_parent / "EXEC-1"
    assert dest.exists()
    if preserve_label == "file":
        assert dest.read_text(encoding="utf-8") == foreign_probe["value"]
    elif preserve_label == "junction":
        assert (dest / "inside.txt").read_text(encoding="utf-8") == foreign_probe["value"]
    elif preserve_label == "nonempty_dir":
        assert (dest / "foreign.txt").read_text(encoding="utf-8") == "occupied"
    else:
        assert dest.is_dir() and not any(dest.iterdir())


def test_revision_processed_hash_baseline_completes_without_mutation(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    _run_full_candidate_evidence(handlers, engine, workspace, workflow_id)
    assert_single_c2_export_triplet(workspace.db, workflow_id)


def test_revision_processed_hash_null_to_nonempty_during_publication_fails(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    revisions = AssetRevisionRepository(workspace.db)
    row = revisions.list_by_workflow(workflow_id)[0]
    revisions.save(replace(row, processed_glb_hash=None))
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    workflow = WorkflowRepository(workspace.db).get(workflow_id)
    assert workflow is not None
    prepare = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    before = handlers._bound_snapshot(workflow, prepare)
    assert before.payload.get("asset_revision_processed_glb_hash") is None
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_set_processed(request):
        mutation["hit"] = True
        container = Path(base(request))
        processed = next(
            a
            for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
            if a.artifact_type == "candidate-processed-glb"
        )
        live = revisions.list_by_workflow(workflow_id)[0]
        revisions.save(replace(live, processed_glb_hash=processed.content_hash))
        return container

    handlers.c2_export_callback = _stage_then_set_processed
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    assert evidence.status == TaskStatus.FAILED
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert (
        "snapshot changed" in latest.error_message or "processed_glb_hash" in latest.error_message
    )
    assert "locked" not in latest.error_message.lower()


def test_revision_processed_hash_drift_during_publication_fails(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    revisions = AssetRevisionRepository(workspace.db)
    row = revisions.list_by_workflow(workflow_id)[0]
    processed = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-processed-glb"
    )
    revisions.save(replace(row, processed_glb_hash=processed.content_hash))
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    workflow = WorkflowRepository(workspace.db).get(workflow_id)
    assert workflow is not None
    prepare = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_prepare"
    )
    before = handlers._bound_snapshot(workflow, prepare)
    assert before.payload.get("asset_revision_processed_glb_hash") == processed.content_hash
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_drift(request):
        mutation["hit"] = True
        container = Path(base(request))
        live = revisions.list_by_workflow(workflow_id)[0]
        with workspace.db.transaction() as conn:
            conn.execute(
                """
                UPDATE asset_revisions
                SET processed_glb_hash = NULL
                WHERE workflow_id = ? AND asset_id = ? AND revision_number = ?;
                """,
                (workflow_id, live.asset_id, live.revision_number),
            )
        return container

    handlers.c2_export_callback = _stage_then_drift
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    assert evidence.status == TaskStatus.FAILED
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert "snapshot changed" in latest.error_message


def test_revision_processed_hash_mismatch_during_publication_fails(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    revisions = AssetRevisionRepository(workspace.db)
    row = revisions.list_by_workflow(workflow_id)[0]
    processed = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-processed-glb"
    )
    revisions.save(replace(row, processed_glb_hash=processed.content_hash))
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_mismatch(request):
        mutation["hit"] = True
        container = Path(base(request))
        live = revisions.list_by_workflow(workflow_id)[0]
        with workspace.db.transaction() as conn:
            conn.execute(
                """
                UPDATE asset_revisions
                SET processed_glb_hash = ?
                WHERE workflow_id = ? AND asset_id = ? AND revision_number = ?;
                """,
                ("e" * 64, workflow_id, live.asset_id, live.revision_number),
            )
        return container

    handlers.c2_export_callback = _stage_then_mismatch
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    assert mutation["hit"] is True
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    assert final.status != WorkflowStatus.COMPLETED


def test_db_insert_failure_rolls_back_with_insert_semantics(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    insert_flags: dict[str, object] = {}
    partial_denier = make_partial_insert_denier(workflow_id, insert_flags)

    with patch.object(ArtifactRepository, "save_many_on_connection", partial_denier):
        latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)

    assert insert_flags.get("reached_first_insert") is True
    assert insert_flags.get("denied_subsequent_insert") is True
    assert insert_flags.get("uncommitted_c2_count_after_first") == 1
    assert isinstance(insert_flags.get("insert_exception"), sqlite3.DatabaseError)
    assert latest.error_message is not None
    assert (
        "not authorized" in latest.error_message.lower()
        or "unique constraint" in latest.error_message.lower()
        or "sqlite" in latest.error_message.lower()
    )
    assert "simulated insert failure" not in latest.error_message
    assert "database is locked" not in latest.error_message.lower()
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    published = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id
    assert published.exists() and any(published.iterdir())


@pytest.mark.parametrize("task_flip", [TaskStatus.FAILED, TaskStatus.COMPLETED])
def test_evidence_task_terminal_flip_during_cold_blocks_publication(
    tmp_path: Path, task_flip: TaskStatus
) -> None:
    from gamefactory.workflows import v08_candidate_evidence_publication as publication_mod
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )

    mutation = {"hit": False}
    original_assert = publication_mod._assert_active_evidence_execution

    def _flip_task_under_lock(conn, handlers_arg, workflow, **kwargs):
        mutation["hit"] = True
        conn.execute(
            "UPDATE tasks SET status = ? WHERE id = ?;",
            (task_flip.value, evidence_task.id),
        )
        return original_assert(conn, handlers_arg, workflow, **kwargs)

    with patch.object(publication_mod, "_assert_active_evidence_execution", _flip_task_under_lock):
        latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert "candidate evidence task must remain RUNNING" in latest.error_message
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


@pytest.mark.parametrize(
    "attempt_status",
    [
        ExecutionStatus.FAILED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.UNCERTAIN,
        ExecutionStatus.COMPLETED,
    ],
)
def test_newer_evidence_attempt_during_cold_blocks_publication(
    tmp_path: Path, attempt_status: ExecutionStatus
) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_new_attempt(request):
        mutation["hit"] = True
        container = Path(base(request))
        ExecutionRepository(workspace.db).save(
            Execution(
                id=generate_id("EXEC"),
                task_id=evidence_task.id,
                attempt_number=99,
                status=attempt_status,
                started_at=utc_now_iso(),
                completed_at=utc_now_iso() if attempt_status != ExecutionStatus.RUNNING else None,
                external_op_id=None,
                pid=None,
                host=None,
                error_message="adversarial newer attempt"
                if attempt_status == ExecutionStatus.FAILED
                else None,
                exit_code=1 if attempt_status == ExecutionStatus.FAILED else None,
                stdout="",
                stderr="",
                cost=0,
                estimated_cost=0,
                cost_unit="usd",
                provider=None,
                retryable=False,
            )
        )
        return container

    handlers.c2_export_callback = _stage_then_new_attempt
    _fail_evidence_run(handlers, engine, workspace, workflow_id)
    assert mutation["hit"] is True
    publication_run = next(
        row
        for row in ExecutionRepository(workspace.db).list_by_task(evidence_task.id)
        if row.attempt_number == 1
    )
    assert publication_run.status == ExecutionStatus.FAILED
    assert publication_run.error_message is not None
    assert (
        "newer candidate evidence attempt" in publication_run.error_message
        or "no longer the authoritative latest attempt" in publication_run.error_message
        or "blocking status" in publication_run.error_message
    )
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_source_glb_byte_mutation_during_cold_blocks_publication(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_source_tamper(request):
        mutation["hit"] = True
        container = Path(base(request))
        glb = workspace.root / "source.glb"
        glb.write_bytes(glb.read_bytes() + b"TAMPER")
        return container

    handlers.c2_export_callback = _stage_then_source_tamper
    latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert "snapshot changed" in latest.error_message
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_registered_artifact_path_drift_during_cold_blocks_publication(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    processed = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-processed-glb"
    )
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_path_drift(request):
        mutation["hit"] = True
        container = Path(base(request))
        stray = workspace.root / "stray-processed.glb"
        shutil.copy2(workspace.root / processed.relative_path, stray)
        stray_rel = stray.relative_to(workspace.root).as_posix()
        with workspace.db.transaction() as conn:
            conn.execute(
                "UPDATE artifacts SET relative_path = ? WHERE id = ?;",
                (stray_rel, processed.id),
            )
        return container

    handlers.c2_export_callback = _stage_then_path_drift
    latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert "snapshot changed" in latest.error_message
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_registered_artifact_hash_drift_during_cold_blocks_publication(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    processed = next(
        a
        for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-processed-glb"
    )
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_hash_drift(request):
        mutation["hit"] = True
        container = Path(base(request))
        ArtifactRepository(workspace.db).save(replace(processed, content_hash="c" * 64))
        return container

    handlers.c2_export_callback = _stage_then_hash_drift
    latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert "snapshot changed" in latest.error_message
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_receipt_identity_drift_during_stage_blocks_live_binding_while_approval_stays_approved(
    tmp_path: Path,
) -> None:
    from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    approval_before = next(
        a
        for a in ApprovalRepository(workspace.db).list_by_workflow(workflow_id)
        if a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
    )
    assert approval_before.status == ApprovalStatus.APPROVED
    mutation: dict[str, object] = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _drift_receipt_identity_then_stage(request):
        mutation["hit"] = True
        receipt_art = next(
            a
            for a in ArtifactRepository(workspace.db).list_by_workflow(workflow_id)
            if a.artifact_type == "candidate-test-only-receipt"
        )
        receipt_path = workspace.root / receipt_art.relative_path
        receipt_doc = json.loads(receipt_path.read_text(encoding="utf-8"))
        mutation["before_snapshot_fingerprint"] = receipt_doc.get("snapshot_fingerprint")
        drifted_fp = "c0ffee" + ("0" * 58)
        receipt_doc["snapshot_fingerprint"] = drifted_fp
        mutation["after_snapshot_fingerprint"] = drifted_fp
        receipt_path.write_text(
            json.dumps(receipt_doc, sort_keys=True, indent=2) + "\n", encoding="utf-8"
        )
        receipt_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
        ArtifactRepository(workspace.db).save(
            replace(
                receipt_art,
                content_hash=receipt_hash,
                file_size=receipt_path.stat().st_size,
            )
        )
        still_approved = ApprovalRepository(workspace.db).get(approval_before.id)
        assert still_approved is not None
        assert still_approved.status == ApprovalStatus.APPROVED
        return Path(base(request))

    handlers.c2_export_callback = _drift_receipt_identity_then_stage
    engine.run_workflow(workflow_id)
    final = engine.run_workflow(workflow_id)
    evidence = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    latest = ExecutionRepository(workspace.db).get_latest_attempt(evidence.id)
    assert latest is not None
    approval_after = ApprovalRepository(workspace.db).get(approval_before.id)
    assert approval_after is not None
    assert approval_after.status == ApprovalStatus.APPROVED
    assert mutation["hit"] is True
    assert mutation["before_snapshot_fingerprint"] != mutation["after_snapshot_fingerprint"]
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    assert final.status != WorkflowStatus.COMPLETED
    assert evidence.status == TaskStatus.FAILED
    assert latest.error_message is not None
    assert "receipt snapshot_fingerprint" in latest.error_message
    assert "database is locked" not in latest.error_message.lower()
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
    published = workspace.root / ".gamefactory" / "candidate-evidence" / workflow_id
    assert not published.exists() or not any(published.iterdir())


def test_approval_identity_drift_during_cold_blocks_publication(tmp_path: Path) -> None:
    from gamefactory.workflows.v08_candidate_snapshot import CANDIDATE_TEST_ONLY_APPROVAL
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    mutation = {"hit": False}
    base = handlers.c2_export_callback
    assert base is not None

    def _stage_then_revoke_approval(request):
        mutation["hit"] = True
        container = Path(base(request))
        pending = next(
            a
            for a in ApprovalRepository(workspace.db).list_by_workflow(workflow_id)
            if a.approval_type == CANDIDATE_TEST_ONLY_APPROVAL
        )
        with workspace.db.transaction() as conn:
            conn.execute(
                "UPDATE approvals SET status = ? WHERE id = ?;",
                ("PENDING", pending.id),
            )
        return container

    handlers.c2_export_callback = _stage_then_revoke_approval
    latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert "snapshot changed" in latest.error_message or "approval" in latest.error_message.lower()
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)


def test_foreign_coherent_cold_bundle_fails_live_binding(tmp_path: Path) -> None:
    from gamefactory.adapters.assets.v08_candidate_evidence import (
        COLD_BUNDLE_DIRNAME,
        trusted_cold_verify_candidate_bundle,
    )
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    foreign = run_workflow_to_evidence_publication_gate(tmp_path / "foreign")
    foreign_stage = Path(foreign[6]["kwargs"]["staging_container"])
    foreign_bundle = foreign_stage / COLD_BUNDLE_DIRNAME
    cold_only = trusted_cold_verify_candidate_bundle(foreign_bundle)
    assert cold_only.get("outcome") == "CONSISTENT_BUT_UNAUTHENTICATED"

    workspace = create_fresh_v08_candidate_workspace(tmp_path / "live")
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    copied = copy_prepared_stage_to_managed_namespace(
        workspace.root, foreign_stage, "foreign_coherent"
    )
    mutation = {"hit": False}

    def _return_foreign_stage(_request):
        mutation["hit"] = True
        return copied

    handlers.c2_export_callback = _return_foreign_stage
    latest = _fail_evidence_run(handlers, engine, workspace, workflow_id)
    assert mutation["hit"] is True
    assert latest.error_message is not None
    assert latest.error_message is not None
    assert (
        "workflow_id" in latest.error_message
        or "bundled snapshot" in latest.error_message
        or "live" in latest.error_message.lower()
    )
    _assert_no_c2_export_artifacts(workspace.db, workflow_id)
