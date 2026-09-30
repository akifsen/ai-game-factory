from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.provider_character_publication import (
    ProviderCharacterEvidencePublicationRepository,
)
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    ProjectRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.models import (
    Artifact,
    CostClass,
    Execution,
    Project,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)

_FINGERPRINT = "a" * 64
_SPEC_HASH = "b" * 64


def _running_publication(tmp_path: Path):
    db = Database(tmp_path / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    project = Project("publication-project", "Publication test", "godot", str(tmp_path))
    workflow = Workflow(
        "publication-workflow", project.id, "Provider evidence", status=WorkflowStatus.RUNNING
    )
    task = Task(
        "publication-task",
        workflow.id,
        "Publish provider evidence",
        "asset_v07_provider_evidence",
        cost_class=CostClass.LOCAL,
        status=TaskStatus.RUNNING,
        parameters={
            "graph_version": "0.7.0",
            "revision_number": 1,
            "profile_id": "character",
            "profile_version": 1,
            "specification_hash": _SPEC_HASH,
            "specification": {
                "asset_id": "character_fixture",
                "profile": "character",
                "profile_version": 1,
            },
        },
    )
    ProjectRepository(db).save(project)
    WorkflowRepository(db).save(workflow)
    TaskRepository(db).save(task)
    AssetRevisionRepository(db).allocate_revision(
        "character_fixture",
        workflow.id,
        _SPEC_HASH,
        profile_id="character",
        profile_version=1,
    )
    execution = Execution("publication-execution", task.id, 1)
    ExecutionRepository(db).save(execution)
    relative_path = (
        ".gamefactory/assets/character_fixture/r001/provider-evidence/"
        "publication-execution-a1/manifest.json"
    )
    artifact = Artifact(
        "publication-artifact",
        workflow.id,
        task.id,
        "provider-character-evidence-manifest",
        "cold_verified_provider_character_evidence",
        relative_path,
        _FINGERPRINT,
        17,
        "VERIFIED",
    )
    repository = ProviderCharacterEvidencePublicationRepository(db)
    return db, workflow, task, execution, artifact, repository


def _publish(repository, workflow, task, execution, artifact, fingerprint=_FINGERPRINT) -> None:
    repository.publish_attempt(
        workflow=workflow,
        task=task,
        execution=execution,
        artifact=artifact,
        expected_snapshot_fingerprint=_FINGERPRINT,
        current_snapshot_fingerprint=lambda: fingerprint,
    )


def test_publishes_manifest_only_after_current_snapshot_recheck(tmp_path: Path) -> None:
    db, workflow, task, execution, artifact, repository = _running_publication(tmp_path)

    with pytest.raises(ValueError, match="snapshot changed"):
        _publish(repository, workflow, task, execution, artifact, "c" * 64)
    assert ArtifactRepository(db).get(artifact.id) is None

    _publish(repository, workflow, task, execution, artifact)
    persisted = ArtifactRepository(db).get(artifact.id)
    assert persisted is not None
    assert persisted.relative_path == artifact.relative_path
    assert persisted.content_hash == artifact.content_hash
    assert persisted.validation_state == "VERIFIED"


def test_manifest_registration_is_strict_insert_and_does_not_upsert(tmp_path: Path) -> None:
    db, workflow, task, execution, artifact, repository = _running_publication(tmp_path)
    _publish(repository, workflow, task, execution, artifact)
    original = ArtifactRepository(db).get(artifact.id)
    assert original is not None

    conflicting = Artifact(
        artifact.id,
        workflow.id,
        task.id,
        artifact.artifact_type,
        artifact.producer,
        artifact.relative_path,
        "d" * 64,
        artifact.file_size,
        "VERIFIED",
    )
    with pytest.raises(ValueError, match="ID or path already exists"):
        _publish(repository, workflow, task, execution, conflicting)

    retained = ArtifactRepository(db).get(artifact.id)
    assert retained is not None
    assert retained.content_hash == original.content_hash


@pytest.mark.parametrize("status", ["FAILED", "RUNNING"])
def test_newer_execution_fences_stale_publication(tmp_path: Path, status: str) -> None:
    db, workflow, task, execution, artifact, repository = _running_publication(tmp_path)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO executions (id, task_id, attempt_number, status, started_at, cost, "
            "estimated_cost, cost_unit, retryable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "newer-provider-evidence-attempt",
                task.id,
                2,
                status,
                execution.started_at,
                0.0,
                0.0,
                "provider_units",
                0,
            ),
        )

    with pytest.raises(ValueError, match="current RUNNING attempt"):
        _publish(repository, workflow, task, execution, artifact)
    assert ArtifactRepository(db).get(artifact.id) is None


@pytest.mark.parametrize("stale_binding", ["workflow", "task", "revision"])
def test_changed_persisted_binding_fences_publication(tmp_path: Path, stale_binding: str) -> None:
    db, workflow, task, execution, artifact, repository = _running_publication(tmp_path)
    with db.transaction() as conn:
        if stale_binding == "workflow":
            conn.execute("UPDATE workflows SET status='FAILED' WHERE id=?", (workflow.id,))
        elif stale_binding == "task":
            conn.execute(
                "UPDATE tasks SET parameters_json='{}' WHERE id=?",
                (task.id,),
            )
        else:
            conn.execute(
                "UPDATE asset_revisions SET spec_hash=? WHERE asset_id=? AND revision_number=1",
                ("e" * 64, "character_fixture"),
            )

    with pytest.raises(ValueError):
        _publish(repository, workflow, task, execution, artifact)
    assert ArtifactRepository(db).get(artifact.id) is None


def test_concurrent_publishers_register_only_one_current_manifest(tmp_path: Path) -> None:
    db, workflow, task, execution, artifact, repository = _running_publication(tmp_path)

    def publish_once() -> str:
        try:
            _publish(repository, workflow, task, execution, artifact)
        except ValueError:
            return "rejected"
        return "published"

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _index: publish_once(), range(2)))
    assert sorted(outcomes) == ["published", "rejected"]
    registered = ArtifactRepository(db).list_by_workflow(workflow.id)
    assert [item.id for item in registered] == [artifact.id]


def test_snapshot_callback_failure_rolls_back_publication_transaction(tmp_path: Path) -> None:
    db, workflow, task, execution, artifact, repository = _running_publication(tmp_path)

    def failed_recheck() -> str:
        raise RuntimeError("snapshot source failed")

    with pytest.raises(RuntimeError, match="snapshot source failed"):
        repository.publish_attempt(
            workflow=workflow,
            task=task,
            execution=execution,
            artifact=artifact,
            expected_snapshot_fingerprint=_FINGERPRINT,
            current_snapshot_fingerprint=failed_recheck,
        )
    assert ArtifactRepository(db).get(artifact.id) is None
