from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_asset_workflow import _at_paid_gate, _decision, _setup

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.asset_installation import AssetInstallationSnapshot
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    ApprovalStatus,
    CostClass,
    Execution,
    ExecutionStatus,
    Project,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    utc_now_iso,
)
from gamefactory.workflows import asset_installation as install_module
from gamefactory.workflows.asset_installation import (
    AssetInstallationHandlers,
    _InstallationLock,
    build_asset_installation_workflow,
    register_asset_installation_handlers,
)


def test_install_builder_never_guesses_from_untrusted_glb_files(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    untrusted = root / "energy_crate.glb"
    untrusted.write_bytes(b"not an accepted asset")
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(
        Project(id="project-1", name="Game", engine_type="godot", root_path=str(root))
    )

    with pytest.raises(ValidationError, match="completed"):
        build_asset_installation_workflow("project-1", root, "missing-source", 1, db=db)

    assert untrusted.read_bytes() == b"not an accepted asset"
    assert not (root / ".gamefactory" / "asset-installations").exists()


def _install_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, baseline: bool = False):
    root = tmp_path / "game"
    root.mkdir()
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    project = Project(id="project-1", name="Game", engine_type="godot", root_path=str(root))
    ProjectRepository(db).save(project)
    source_workflow = Workflow(
        id="source-workflow",
        project_id=project.id,
        name="accepted source",
        status=WorkflowStatus.COMPLETED,
    )
    source_task = Task(
        id="source-final-review",
        workflow_id=source_workflow.id,
        name="accepted review",
        task_type="asset_final_review",
        status=TaskStatus.COMPLETED,
    )
    WorkflowRepository(db).save_with_tasks(source_workflow, [source_task])
    source = root / ".gamefactory" / "source" / "asset.glb"
    source.parent.mkdir(parents=True)
    source_bytes = b"human-accepted-glb-bytes"
    source.write_bytes(source_bytes)
    manager = ArtifactManager(root)
    artifact = manager.register_file_artifact(
        source_workflow.id,
        source_task.id,
        "asset-processed-glb",
        "fixture",
        source.relative_to(root).as_posix(),
    )
    ArtifactRepository(db).save(artifact)
    target_dir = root / "assets" / "generated" / "props"
    wrapper = b"accepted-scene-with-collider-and-lod-contract"
    bundle = {
        "crate.glb": (hashlib.sha256(source_bytes).hexdigest(), len(source_bytes)),
        "crate.tscn": (hashlib.sha256(wrapper).hexdigest(), len(wrapper)),
    }
    approved_baseline = None
    if baseline:
        target_dir.mkdir(parents=True)
        (target_dir / "crate.glb").write_bytes(b"old-user-glb")
        (target_dir / "crate.tscn").write_bytes(b"old-user-scene")
        approved_baseline = {
            "crate.glb": hashlib.sha256(b"old-user-glb").hexdigest(),
            "crate.tscn": hashlib.sha256(b"old-user-scene").hexdigest(),
        }
    snapshot = AssetInstallationSnapshot(
        project.id,
        str(root),
        source_workflow.id,
        "crate",
        1,
        "a" * 64,
        "assets/generated/props/",
        artifact.id,
        artifact.content_hash,
        "approval-1",
        "c" * 64,
        approved_baseline,
    )
    spec = SimpleNamespace(lod_policy="lod0_lod1")
    components = {
        "revision": None,
        "artifact": artifact,
        "source": source,
        "bundle": bundle,
        "wrapper": wrapper,
    }
    monkeypatch.setattr(
        install_module, "_accepted_context", lambda *args, **kwargs: (snapshot, spec, components)
    )
    workflow = Workflow(
        id="install-workflow", project_id=project.id, name="install exact accepted asset"
    )
    task = Task(
        id="install-task",
        workflow_id=workflow.id,
        name="install",
        task_type="asset_install_revision" if baseline else "asset_install",
        cost_class=CostClass.LOCAL,
        parameters={"snapshot": snapshot.to_dict(), "snapshot_sha256": snapshot.fingerprint()},
    )
    WorkflowRepository(db).save_with_tasks(workflow, [task])
    repositories = (
        ArtifactRepository(db),
        EvidenceRepository(db),
        QualityGateRepository(db),
        ExecutionRepository(db),
    )
    handlers = AssetInstallationHandlers(root, db, *repositories, manager)
    execution = Execution(id="install-attempt-1", task_id=task.id, attempt_number=1)
    return root, db, manager, artifact, snapshot, workflow, task, handlers, execution, bundle


def test_accepted_revision_install_registers_exact_bundle_and_recovers_partial_attempt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, manager, accepted, snapshot, workflow, task, handlers, execution, bundle = (
        _install_case(tmp_path, monkeypatch)
    )
    intent_dir = root / ".gamefactory" / "asset-installations" / snapshot.fingerprint()
    targets = {name: (Path(snapshot.target_import_path) / name).as_posix() for name in bundle}
    intent_dir.mkdir(parents=True)
    (intent_dir / "intent.json").write_text(
        json.dumps(
            {
                "snapshot_sha256": snapshot.fingerprint(),
                "source_sha256": accepted.content_hash,
                "targets": targets,
                "baseline_sha256": None,
            }
        ),
        encoding="utf-8",
    )
    glb = root / targets["crate.glb"]
    glb.parent.mkdir(parents=True)
    glb.write_bytes((root / accepted.relative_path).read_bytes())

    result = handlers.install(workflow, task, execution)
    manifest_artifact = next(
        a
        for a in ArtifactRepository(db).list_by_task(task.id)
        if a.artifact_type == "asset-installation-manifest"
    )
    manifest = json.loads(
        manager.path_guard.resolve_safe_path(manifest_artifact.relative_path).read_text(
            encoding="utf-8"
        )
    )
    assert manifest["source_artifact_id"] == accepted.id
    assert manifest["source_sha256"] == snapshot.accepted_artifact_sha256
    assert len(result.artifact_ids) == 3
    assert (
        hashlib.sha256((root / targets["crate.glb"]).read_bytes()).hexdigest()
        == bundle["crate.glb"][0]
    )
    assert manifest["components"][0]["recovered"] is True
    assert manifest["components"][1]["recovered"] is False


def test_failed_partial_install_retries_after_terminal_owner_with_audit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, manager, _, snapshot, workflow, task, handlers, first_execution, bundle = (
        _install_case(tmp_path, monkeypatch)
    )
    original_copy = handlers._copy_component
    calls = 0

    def fail_second_component(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated interruption after first component")
        return original_copy(*args, **kwargs)

    monkeypatch.setattr(handlers, "_copy_component", fail_second_component)
    with pytest.raises(OSError):
        handlers.install(workflow, task, first_execution)
    first_target = root / snapshot.target_import_path / "crate.glb"
    assert hashlib.sha256(first_target.read_bytes()).hexdigest() == bundle["crate.glb"][0]
    first_execution.status = ExecutionStatus.FAILED
    first_execution.completed_at = utc_now_iso()
    ExecutionRepository(db).save(first_execution)

    monkeypatch.setattr(handlers, "_copy_component", original_copy)
    retry = Execution(id="install-attempt-2", task_id=task.id, attempt_number=2)
    handlers.install(workflow, task, retry)
    second_target = root / snapshot.target_import_path / "crate.tscn"
    assert hashlib.sha256(second_target.read_bytes()).hexdigest() == bundle["crate.tscn"][0]
    assert list((root / ".gamefactory" / "locks").rglob("recovery-*.json"))


def test_approved_revision_replacement_keeps_durable_baseline_and_new_hashes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, _, _, snapshot, workflow, task, handlers, execution, bundle = _install_case(
        tmp_path, monkeypatch, baseline=True
    )
    handlers.install(workflow, task, execution)
    for filename, expected in bundle.items():
        target = root / snapshot.target_import_path / filename
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected[0]
        backup = (
            root
            / ".gamefactory"
            / "asset-installations"
            / snapshot.fingerprint()
            / "baseline-backups"
            / f"{filename}.sha256-{snapshot.target_baseline_sha256[filename]}"
        )
        assert (
            hashlib.sha256(backup.read_bytes()).hexdigest()
            == snapshot.target_baseline_sha256[filename]
        )


def test_source_artifact_drift_after_approval_fails_before_game_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, db, _, accepted, snapshot, workflow, task, handlers, execution, _ = _install_case(
        tmp_path, monkeypatch
    )
    (root / accepted.relative_path).write_bytes(b"changed after approval")
    with pytest.raises(ArtifactError):
        handlers.install(workflow, task, execution)
    assert not (root / snapshot.target_import_path).exists()


def test_revision_baseline_hash_drift_is_preserved_without_replacement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _, snapshot, workflow, task, handlers, execution, _ = _install_case(
        tmp_path, monkeypatch, baseline=True
    )
    target = root / snapshot.target_import_path / "crate.glb"
    drift = b"operator changed approved baseline before execution"
    target.write_bytes(drift)
    with pytest.raises(ValidationError, match="approved baseline"):
        handlers.install(workflow, task, execution)
    assert target.read_bytes() == drift


def test_concurrent_target_edit_is_quarantined_and_never_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root, _, _, _, snapshot, workflow, task, handlers, execution, _ = _install_case(
        tmp_path, monkeypatch, baseline=True
    )
    target = root / snapshot.target_import_path / "crate.glb"
    original_rename = install_module.os.rename
    concurrent = b"user edit racing approved replacement"

    def racing_rename(source: Path, destination: Path) -> None:
        if Path(source) == target:
            target.write_bytes(concurrent)
        original_rename(source, destination)

    monkeypatch.setattr(install_module.os, "rename", racing_rename)
    with pytest.raises(ValidationError, match="preserved"):
        handlers.install(workflow, task, execution)
    saved = list(target.parent.glob(".crate.glb.gamefactory-recovery-*"))
    assert len(saved) == 1 and saved[0].read_bytes() == concurrent


def test_stable_os_lock_prevents_stale_owner_recovery_race(tmp_path: Path) -> None:
    lock_path = tmp_path / "target.lock"
    first, second = _InstallationLock(lock_path), _InstallationLock(lock_path)
    first.acquire()
    with pytest.raises(ValidationError, match="currently owns"):
        second.acquire()
    first.release()
    second.acquire()
    second.release()


def _complete_real_accepted_asset(tmp_path: Path):
    """Produce a completed accepted revision using the real source workflow and records."""
    engine, db, source_id, _, production_handlers = _setup(
        tmp_path, stub_downstream=True, validate_real=True, processed_mutation="valid_fixture"
    )
    paid_approval_id = _at_paid_gate(engine, db, source_id)
    _decision(engine, db, paid_approval_id, approve=True)
    review_result = engine.run_workflow(source_id)
    assert review_result.status == WorkflowStatus.BLOCKED
    review_approval = ApprovalRepository(db).get(review_result.pending_approval_id or "")
    assert review_approval is not None and review_approval.approval_type == "final_visual_review"
    _decision(engine, db, review_approval.id, approve=True)
    assert engine.run_workflow(source_id).status == WorkflowStatus.COMPLETED

    revision = AssetRevisionRepository(db).list_by_workflow(source_id)[0]
    artifact = next(
        item
        for item in engine.art_repo.list_by_workflow(source_id)
        if item.artifact_type == "asset-processed-glb"
        and item.content_hash == revision.processed_glb_hash
    )
    engine.artifact_mgr.verify_artifact_integrity(artifact)
    return engine, db, source_id, revision, artifact, production_handlers


def test_real_accepted_revision_is_human_approved_and_installed_with_exact_hashes(
    tmp_path: Path,
) -> None:
    engine, db, source_id, revision, accepted_artifact, production_handlers = (
        _complete_real_accepted_asset(tmp_path)
    )
    root = production_handlers.root
    workflow, tasks = build_asset_installation_workflow(
        "asset-test",
        root,
        source_id,
        revision.revision_number,
        db=db,
    )
    register_asset_installation_handlers(
        engine.handler_registry,
        root,
        db,
        engine.art_repo,
        engine.evi_repo,
        engine.gate_repo,
        engine.exec_repo,
        engine.artifact_mgr,
    )
    engine.register_workflow(workflow, tasks)

    pending = engine.run_workflow(workflow.id)
    assert pending.status == WorkflowStatus.BLOCKED
    write_approval = ApprovalRepository(db).get(pending.pending_approval_id or "")
    assert write_approval is not None and write_approval.approval_type == "HUMAN_GAME_WRITE"
    _decision(engine, db, write_approval.id, approve=True)
    assert engine.run_workflow(workflow.id).status == WorkflowStatus.COMPLETED

    installed_glb = next(
        item
        for item in engine.art_repo.list_by_workflow(workflow.id)
        if item.artifact_type == "installed-asset-glb"
    )
    installed_scene = next(
        item
        for item in engine.art_repo.list_by_workflow(workflow.id)
        if item.artifact_type == "installed-asset-scene"
    )
    engine.artifact_mgr.verify_artifact_integrity(installed_glb)
    engine.artifact_mgr.verify_artifact_integrity(installed_scene)
    assert (
        installed_glb.content_hash == accepted_artifact.content_hash == revision.processed_glb_hash
    )
    scene_text = (root / installed_scene.relative_path).read_text(encoding="utf-8")
    assert "ACCEPTED_LOD0_NAMES" in scene_text
    assert "CollisionShape3D" in scene_text
    assert (
        f"res://{tasks[0].parameters['snapshot']['target_import_path'].strip('/')}/{revision.asset_id}.glb"
        in scene_text
    )


@pytest.mark.parametrize(
    "tamper", ["wrong_revision", "rejected_final_approval", "accepted_artifact_drift"]
)
def test_real_install_builder_rejects_unqualified_accepted_workflow_inputs(
    tmp_path: Path, tamper: str
) -> None:
    engine, db, source_id, revision, accepted_artifact, production_handlers = (
        _complete_real_accepted_asset(tmp_path)
    )
    if tamper == "wrong_revision":
        with pytest.raises(ValidationError, match="different asset revision"):
            build_asset_installation_workflow(
                "asset-test",
                production_handlers.root,
                source_id,
                revision.revision_number + 1,
                db=db,
            )
    elif tamper == "rejected_final_approval":
        review = next(
            task
            for task in TaskRepository(db).list_by_workflow(source_id)
            if task.task_type == "asset_final_review"
        )
        approval = next(
            item
            for item in ApprovalRepository(db).list_by_workflow(source_id)
            if item.task_id == review.id and item.approval_type == "final_visual_review"
        )
        approval.status = ApprovalStatus.REJECTED
        ApprovalRepository(db).save(approval)
        with pytest.raises(ValidationError, match="not approved"):
            build_asset_installation_workflow(
                "asset-test", production_handlers.root, source_id, revision.revision_number, db=db
            )
    else:
        accepted_path = production_handlers.root / accepted_artifact.relative_path
        accepted_path.write_bytes(b"changed after final human approval")
        with pytest.raises(ArtifactError):
            build_asset_installation_workflow(
                "asset-test", production_handlers.root, source_id, revision.revision_number, db=db
            )
