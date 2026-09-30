"""Atomic publication and stale-attempt fencing for V0.7 assembly work."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.assembly_publication import AssemblyPublicationRepository
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    ProjectRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.asset_contracts import AssetRevision
from gamefactory.core.domain.models import (
    Artifact,
    CostClass,
    Execution,
    ExecutionStatus,
    Project,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)

_RAW = "1" * 64
_CONCEPT = "2" * 64
_PROCESSED = "3" * 64
_VALIDATION = "4" * 64


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _fixture(
    tmp_path: Path,
    *,
    stage: str = "prepare",
    seeded_state: str = "VERIFIED",
    seed_validation: bool = True,
) -> tuple[Database, Task, Execution, AssetRevision, dict[str, str]]:
    db = Database(tmp_path / "assembly.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(Project("P-PUBLISH", "Publication", "godot", str(tmp_path)))
    workflow = Workflow("WF-PUBLISH", "P-PUBLISH", "Assembly", WorkflowStatus.RUNNING)
    profile_id = "vehicle_tank_v07"
    specification = {
        "schema_version": "0.7.0",
        "asset_id": "test_tank",
        "profile": profile_id,
        "profile_version": 1,
        "source_kind": "local_operator_assembly",
    }
    spec_hash = _digest(
        json.dumps(specification, sort_keys=True, separators=(",", ":"), allow_nan=False)
    )
    parameters = {
        "graph_version": "0.7.0-local-assembly",
        "workflow_id": workflow.id,
        "asset_id": "test_tank",
        "source_version": 1,
        "specification": specification,
        "specification_sha256": spec_hash,
        "profile_id": profile_id,
        "profile_version": 1,
        "source_glb_sha256": _RAW,
        "concept_image_sha256": _CONCEPT,
    }
    stage_types = {
        "prepare": "asset_v07_assembly_prepare",
        "process": "asset_v07_assembly_process",
        "validate": "asset_v07_assembly_validate",
        "godot": "asset_v07_assembly_godot",
    }
    if stage not in stage_types:
        raise ValueError(f"Unknown test stage {stage!r}")
    tasks = {
        name: Task(
            f"TASK-PUBLISH-{name.upper()}",
            workflow.id,
            f"{name} assembly",
            task_type,
            CostClass.LOCAL,
            status=TaskStatus.PENDING,
            parameters=parameters,
        )
        for name, task_type in stage_types.items()
    }
    WorkflowRepository(db).save_with_tasks(workflow, list(tasks.values()))
    revision = AssetRevision(
        "test_tank",
        1,
        workflow.id,
        spec_hash,
        profile_id=profile_id,
        profile_version=1,
        concept_hash=_CONCEPT,
        raw_glb_hash=_RAW,
    )
    AssetRevisionRepository(db).save(revision)
    task = tasks[stage]
    artifact_repo = ArtifactRepository(db)

    def seed(artifact: Artifact) -> None:
        artifact_repo.save(artifact)

    if stage != "prepare":
        seed(
            _artifact(
                tasks["prepare"],
                "ART-SEED-RAW",
                "assembly-source-glb",
                "source/model.glb",
                _RAW,
                state=seeded_state,
            )
        )
        seed(
            _artifact(
                tasks["prepare"],
                "ART-SEED-CONCEPT",
                "asset-concept",
                "concept/concept.png",
                _CONCEPT,
                state=seeded_state,
            )
        )
    if stage in ("validate", "godot"):
        revision.processed_glb_hash = _PROCESSED
        seed(
            _artifact(
                tasks["process"],
                "ART-SEED-PROCESSED",
                "processed-assembly-glb",
                "out/processed.glb",
                _PROCESSED,
                state=seeded_state,
            )
        )
    if stage == "godot" and seed_validation:
        revision.validation_report_hash = _VALIDATION
        seed(
            _artifact(
                tasks["validate"],
                "ART-SEED-VALIDATION",
                "assembly-validation-report",
                "out/validation.json",
                _VALIDATION,
                state=seeded_state,
            )
        )
    if revision.processed_glb_hash is not None or revision.validation_report_hash is not None:
        AssetRevisionRepository(db).save(revision)

    execution = Execution(f"EXEC-PUBLISH-{stage.upper()}-1", task.id, 1)
    assert TaskRepository(db).claim_execution(task.id, execution, "P-PUBLISH", 0.0)
    task.status = TaskStatus.RUNNING
    return db, task, execution, revision, parameters


def _artifact(
    task: Task,
    artifact_id: str,
    artifact_type: str,
    relative_path: str,
    content_hash: str,
    *,
    file_size: int = 10,
    state: str = "VERIFIED",
) -> Artifact:
    return Artifact(
        artifact_id,
        task.workflow_id,
        task.id,
        artifact_type,
        "local_operator_assembly",
        relative_path,
        content_hash,
        file_size,
        state,
        "2026-09-30T00:00:00+00:00",
    )


def _base_artifacts(task: Task) -> list[Artifact]:
    return [
        _artifact(task, "ART-RAW", "assembly-source-glb", "source/model.glb", _RAW),
        _artifact(task, "ART-CONCEPT", "asset-concept", "concept/concept.png", _CONCEPT),
    ]


def _row_counts(db: Database) -> tuple[int, str | None, str | None]:
    with db.connect() as conn:
        artifacts = int(conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0])
        row = conn.execute(
            "SELECT processed_glb_hash, validation_report_hash FROM asset_revisions "
            "WHERE asset_id='test_tank' AND revision_number=1"
        ).fetchone()
        return artifacts, row["processed_glb_hash"], row["validation_report_hash"]


def test_publish_is_atomic_and_exact_retry_is_idempotent(tmp_path: Path) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path)
    repo = AssemblyPublicationRepository(db)
    artifacts = _base_artifacts(task)
    repo.publish_attempt(task=task, execution=execution, revision=revision, artifacts=artifacts)
    repo.publish_attempt(task=task, execution=execution, revision=revision, artifacts=artifacts)
    with db.connect() as conn:
        rows = conn.execute("SELECT id FROM artifacts ORDER BY id").fetchall()
        assert [row["id"] for row in rows] == ["ART-CONCEPT", "ART-RAW"]
        assert conn.execute("SELECT raw_glb_hash, concept_hash FROM asset_revisions").fetchone()[
            :
        ] == (_RAW, _CONCEPT)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda task, rows: (
            rows + [_artifact(task, "ART-RAW-ALIAS", "assembly-source-glb", "alias.glb", _RAW)]
        ),
        lambda task, rows: [
            rows[0],
            _artifact(task, "ART-DUP-PATH", "asset-concept", rows[0].relative_path, _CONCEPT),
        ],
        lambda task, rows: [
            rows[0],
            _artifact(task, "ART-BAD-PATH", "asset-concept", "../escape.png", _CONCEPT),
        ],
    ],
    ids=["duplicate-singleton-role", "duplicate-path", "path-traversal"],
)
def test_invalid_batch_is_rejected_without_partial_rows(tmp_path: Path, mutate: object) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path)
    artifacts = mutate(task, _base_artifacts(task))  # type: ignore[operator]
    with pytest.raises(ValueError):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=artifacts
        )
    assert _row_counts(db) == (0, None, None)


@pytest.mark.parametrize("state", ["VALID", "PENDING", "FAILED_TAMPERED", "UNKNOWN"])
def test_new_artifact_requires_verified_state(tmp_path: Path, state: str) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path)
    raw = _artifact(
        task,
        "ART-RAW",
        "assembly-source-glb",
        "source/model.glb",
        _RAW,
        state=state,
    )
    with pytest.raises(ValueError, match="identity and state"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task,
            execution=execution,
            revision=revision,
            artifacts=[raw, _base_artifacts(task)[1]],
        )
    assert _row_counts(db) == (0, None, None)


@pytest.mark.parametrize("state", ["VALID", "VERIFIED"])
def test_later_stage_can_reuse_valid_canonical_pins(tmp_path: Path, state: str) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="process", seeded_state=state)
    revision.processed_glb_hash = _PROCESSED
    rows = [
        _artifact(
            task,
            "ART-PROCESSED",
            "processed-assembly-glb",
            "out/processed.glb",
            _PROCESSED,
        ),
        _artifact(
            task,
            "ART-BLENDER-REPORT",
            "assembly-blender-report",
            "out/blender-report.json",
            _digest("report"),
        ),
    ]
    AssemblyPublicationRepository(db).publish_attempt(
        task=task, execution=execution, revision=revision, artifacts=rows
    )
    assert _row_counts(db)[0] == 4


@pytest.mark.parametrize("state", ["PENDING", "FAILED_TAMPERED", "UNKNOWN"])
def test_later_stage_rejects_invalid_existing_pin_rows(tmp_path: Path, state: str) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="process", seeded_state=state)
    revision.processed_glb_hash = _PROCESSED
    rows = [
        _artifact(
            task,
            "ART-PROCESSED",
            "processed-assembly-glb",
            "out/processed.glb",
            _PROCESSED,
        )
    ]
    with pytest.raises(ValueError, match="invalid or noncanonical"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=rows
        )
    assert _row_counts(db) == (2, None, None)


def test_new_source_pin_from_later_task_is_rejected(tmp_path: Path) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="process")
    graft = _artifact(task, "ART-GRAFT-RAW", "assembly-source-glb", "graft/model.glb", _RAW)
    with pytest.raises(ValueError, match="canonical stage|wrong owning task"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=[graft]
        )
    assert _row_counts(db)[0] == 2


def test_new_validation_pin_from_godot_task_is_rejected(tmp_path: Path) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="godot", seed_validation=False)
    revision.validation_report_hash = _VALIDATION
    graft = _artifact(
        task,
        "ART-GRAFT-VALIDATION",
        "assembly-validation-report",
        "graft/validation.json",
        _VALIDATION,
    )
    with pytest.raises(ValueError, match="canonical stage"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=[graft]
        )
    assert _row_counts(db)[0] == 3


def test_existing_processed_pin_cannot_be_grafted_from_godot_task(tmp_path: Path) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="godot")
    with db.transaction() as conn:
        conn.execute("UPDATE artifacts SET task_id = ? WHERE id = 'ART-SEED-PROCESSED'", (task.id,))
    unrelated = _artifact(
        task,
        "ART-RUNTIME-INDEX",
        "assembly-runtime-index",
        "runtime/index.json",
        _digest("runtime index"),
    )
    with pytest.raises(ValueError, match="invalid or noncanonical"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=[unrelated]
        )
    assert _row_counts(db)[0] == 4


def test_new_processed_pin_cannot_be_published_by_later_task_from_existing_row(
    tmp_path: Path,
) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="godot")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE asset_revisions SET processed_glb_hash = NULL "
            "WHERE asset_id = 'test_tank' AND revision_number = 1"
        )
    unrelated = _artifact(
        task,
        "ART-LATE-OUTPUT",
        "assembly-runtime-index",
        "runtime/new-index.json",
        _digest("runtime index"),
    )
    with pytest.raises(ValueError, match="canonical stage"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=[unrelated]
        )
    assert _row_counts(db)[0] == 4


def test_new_runtime_hash_cannot_be_claimed_by_non_godot_stage(tmp_path: Path) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="process")
    capture_hash = _digest("already saved capture")
    godot_task = TaskRepository(db).get("TASK-PUBLISH-GODOT")
    ArtifactRepository(db).save(
        _artifact(
            godot_task,
            "ART-EXISTING-CAPTURE",
            "assembly-review-capture",
            "review/old.png",
            capture_hash,
        )
    )
    revision.processed_glb_hash = _PROCESSED
    revision.runtime_evidence_hashes = [capture_hash]
    outputs = [
        _artifact(
            task,
            "ART-PROCESSED",
            "processed-assembly-glb",
            "out/processed.glb",
            _PROCESSED,
        ),
        _artifact(
            task,
            "ART-BLENDER-REPORT",
            "assembly-blender-report",
            "out/report.json",
            _digest("blender report"),
        ),
    ]
    with pytest.raises(ValueError, match="canonical stage"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=outputs
        )
    assert _row_counts(db)[0] == 3


def test_pin_must_match_registered_artifact_and_existing_path_is_immutable(
    tmp_path: Path,
) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path)
    revision.processed_glb_hash = "3" * 64
    with pytest.raises(ValueError, match="processed_glb_hash"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=_base_artifacts(task)
        )
    assert _row_counts(db) == (0, None, None)

    revision.processed_glb_hash = None
    original = _base_artifacts(task)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO artifacts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            tuple(original[0].to_dict().values()),
        )
    changed = [
        _artifact(task, "ART-RAW", "assembly-source-glb", "source/model.glb", "4" * 64),
        original[1],
    ]
    with pytest.raises(ValueError, match="conflicts"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=changed
        )
    assert _row_counts(db)[0] == 1


def test_existing_revision_pin_cannot_be_changed_even_with_new_matching_artifact(
    tmp_path: Path,
) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="process")
    repo = AssemblyPublicationRepository(db)
    first_hash = "8" * 64
    revision.processed_glb_hash = first_hash
    first = _artifact(
        task, "ART-PROCESSED-1", "processed-assembly-glb", "out/first.glb", first_hash
    )
    report = _artifact(
        task,
        "ART-PROCESS-REPORT-1",
        "assembly-blender-report",
        "out/first.json",
        _digest("report"),
    )
    repo.publish_attempt(
        task=task,
        execution=execution,
        revision=revision,
        artifacts=[first, report],
    )

    revision.processed_glb_hash = "9" * 64
    changed = _artifact(
        task,
        "ART-PROCESSED-2",
        "processed-assembly-glb",
        "out/second.glb",
        revision.processed_glb_hash,
    )
    with pytest.raises(ValueError, match="immutable V0.7 processed_glb_hash"):
        repo.publish_attempt(task=task, execution=execution, revision=revision, artifacts=[changed])
    with db.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 4
        assert (
            conn.execute("SELECT processed_glb_hash FROM asset_revisions").fetchone()[0]
            == first_hash
        )


def test_pin_hash_must_match_role_artifact_digest(tmp_path: Path) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path)
    artifacts = _base_artifacts(task)
    artifacts[0].content_hash = "a" * 64
    with pytest.raises(ValueError, match="raw_glb_hash"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=artifacts
        )
    assert _row_counts(db) == (0, None, None)


@pytest.mark.parametrize("mismatch", ["task", "execution", "profile", "source", "revision"])
def test_publication_rejects_wrong_task_attempt_or_revision_binding(
    tmp_path: Path, mismatch: str
) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path)
    if mismatch == "task":
        task.id = "OTHER-TASK"
    elif mismatch == "execution":
        execution.task_id = "OTHER-TASK"
    elif mismatch == "profile":
        revision.profile_id = "different_profile"
    elif mismatch == "source":
        revision.raw_glb_hash = "5" * 64
    else:
        revision.revision_number = 2
    with pytest.raises(ValueError):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=_base_artifacts(task)
        )
    assert _row_counts(db) == (0, None, None)


def test_trigger_failure_rolls_back_prior_artifact_inserts_and_revision_update(
    tmp_path: Path,
) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="process")
    processed_hash = "6" * 64
    revision.processed_glb_hash = processed_hash
    artifacts = [
        _artifact(
            task, "ART-PROCESSED", "processed-assembly-glb", "out/processed.glb", processed_hash
        ),
        _artifact(
            task,
            "ART-PROCESS-REPORT",
            "assembly-blender-report",
            "out/report.json",
            _digest("process report"),
        ),
    ]
    with db.transaction() as conn:
        conn.execute(
            "CREATE TRIGGER injected_publication_failure BEFORE INSERT ON artifacts "
            "WHEN NEW.artifact_type = 'assembly-blender-report' "
            "BEGIN SELECT RAISE(ABORT, 'injected insert failure'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="injected insert failure"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task, execution=execution, revision=revision, artifacts=artifacts
        )
    assert _row_counts(db) == (2, None, None)


def test_runtime_hashes_are_append_only_and_bound_to_capture_artifacts(tmp_path: Path) -> None:
    db, task, execution, revision, _ = _fixture(tmp_path, stage="godot")
    repo = AssemblyPublicationRepository(db)
    first_hash = _digest("capture one")
    first = _artifact(task, "ART-CAPTURE-1", "assembly-review-capture", "review/1.png", first_hash)
    repo.publish_attempt(
        task=task,
        execution=execution,
        revision=revision,
        artifacts=[first],
    )

    revision.runtime_evidence_hashes = [first_hash, _digest("capture two")]
    second = _artifact(
        task,
        "ART-CAPTURE-2",
        "assembly-review-capture",
        "review/2.png",
        revision.runtime_evidence_hashes[1],
    )
    repo.publish_attempt(task=task, execution=execution, revision=revision, artifacts=[second])
    assert AssetRevisionRepository(db).get("test_tank", 1).runtime_evidence_hashes == [
        first_hash,
        revision.runtime_evidence_hashes[1],
    ]

    revision.runtime_evidence_hashes = [revision.runtime_evidence_hashes[1]]
    with pytest.raises(ValueError, match="remove or reorder"):
        repo.publish_attempt(task=task, execution=execution, revision=revision, artifacts=[second])


def test_forced_takeover_fences_stale_publish_and_finalize(tmp_path: Path) -> None:
    db, task_a, execution_a, revision, _ = _fixture(tmp_path)
    tasks = TaskRepository(db)
    executions = ExecutionRepository(db)
    publication = AssemblyPublicationRepository(db)
    # Emulate recovery outside the normal OS-locked DAG runner: A stays RUNNING,
    # while the persisted task is made claimable and B claims the next attempt.
    tasks.update_status(task_a.id, TaskStatus.PENDING)
    task_b = tasks.get(task_a.id)
    execution_b = Execution("EXEC-PUBLISH-2", task_a.id, 2)
    assert tasks.claim_execution(task_a.id, execution_b, "P-PUBLISH", 0.0)
    task_b.status = TaskStatus.RUNNING
    publication.publish_attempt(
        task=task_b,
        execution=execution_b,
        revision=revision,
        artifacts=_base_artifacts(task_b),
    )

    stale_rows = [
        _artifact(task_a, "ART-STALE", "assembly-blender-report", "stale/report.json", _digest("A"))
    ]
    with pytest.raises(ValueError, match="stale"):
        publication.publish_attempt(
            task=task_a, execution=execution_a, revision=revision, artifacts=stale_rows
        )
    task_b.status = TaskStatus.COMPLETED
    execution_b.status = ExecutionStatus.COMPLETED
    execution_b.completed_at = "2026-09-30T00:01:00+00:00"
    executions.finalize_task(task_b, execution_b)

    task_a.status = TaskStatus.COMPLETED
    execution_a.status = ExecutionStatus.COMPLETED
    execution_a.completed_at = "2026-09-30T00:02:00+00:00"
    with pytest.raises(ValueError, match="no longer owns"):
        executions.finalize_task(task_a, execution_a)
    with db.connect() as conn:
        assert (
            conn.execute("SELECT status FROM tasks WHERE id=?", (task_a.id,)).fetchone()[0]
            == "COMPLETED"
        )
        assert conn.execute("SELECT COUNT(*) FROM artifacts").fetchone()[0] == 2


def test_attempt_a_cannot_publish_after_b_claims_even_before_b_publishes(tmp_path: Path) -> None:
    db, task_a, execution_a, revision, _ = _fixture(tmp_path)
    tasks = TaskRepository(db)
    tasks.update_status(task_a.id, TaskStatus.PENDING)
    task_b = tasks.get(task_a.id)
    execution_b = Execution("EXEC-PUBLISH-2", task_a.id, 2)
    assert tasks.claim_execution(task_a.id, execution_b, "P-PUBLISH", 0.0)
    task_b.status = TaskStatus.RUNNING
    with pytest.raises(ValueError, match="stale"):
        AssemblyPublicationRepository(db).publish_attempt(
            task=task_a,
            execution=execution_a,
            revision=revision,
            artifacts=_base_artifacts(task_a),
        )
    assert _row_counts(db) == (0, None, None)
