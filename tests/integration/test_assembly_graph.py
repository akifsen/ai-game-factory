from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.assembly_graph import AssemblyGraphRepository
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import ProjectRepository
from gamefactory.core.domain.asset_contracts import AssetRevision
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import CostClass, Project, Task, TaskStatus, Workflow

STAGES = (
    ("prepare", "asset_v07_assembly_prepare"),
    ("concept_review", "asset_v07_assembly_concept_review"),
    ("source_review", "asset_v07_assembly_source_review"),
    ("process", "asset_v07_assembly_process"),
    ("validate", "asset_v07_assembly_validate"),
    ("godot", "asset_v07_assembly_godot"),
    ("final_review", "asset_v07_assembly_final_review"),
    ("evidence", "asset_v07_assembly_evidence"),
)
SPEC: dict[str, str | int] = {
    "schema_version": "0.7.0",
    "asset_id": "atomic_prop",
    "source_kind": "local_operator_assembly",
    "profile": "modular_prop",
    "profile_version": 1,
}
ASSET_ID = "atomic_prop"
PROFILE_ID = "modular_prop"
PROFILE_VERSION = 1
SPEC_HASH = hashlib.sha256(
    json.dumps(SPEC, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
).hexdigest()
CONCEPT_HASH = "a" * 64
SOURCE_HASH = "b" * 64


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "assembly.sqlite3")
    MigrationRunner(database).apply_all()
    ProjectRepository(database).save(Project("project-1", "Test project", "godot", str(tmp_path)))
    return database


def workflow(workflow_id: str = "workflow-1") -> Workflow:
    return Workflow(workflow_id, "project-1", "temporary")


def task_factory(
    wf: Workflow, *, corrupt: str | None = None
) -> Callable[[AssetRevision], list[Task]]:
    def build(revision: AssetRevision) -> list[Task]:
        parameters = {
            "graph_version": "0.7.0-local-assembly",
            "workflow_id": wf.id,
            "asset_id": revision.asset_id,
            "source_version": revision.revision_number,
            "specification": SPEC,
            "specification_sha256": revision.spec_hash,
            "profile_id": revision.profile_id,
            "profile_version": revision.profile_version,
            "source_glb_sha256": revision.raw_glb_hash,
            "concept_image_sha256": revision.concept_hash,
        }
        if corrupt == "workflow":
            parameters["workflow_id"] = "wrong-workflow"
        if corrupt == "revision":
            parameters["source_version"] = revision.revision_number + 1
        tasks = []
        previous = None
        for stage, task_type in STAGES:
            tasks.append(
                Task(
                    id=f"{wf.id}-{stage.upper()}",
                    workflow_id=wf.id,
                    name=f"V0.7 assembly {stage.replace('_', ' ')}",
                    task_type=task_type,
                    cost_class=CostClass.LOCAL,
                    depends_on=[previous] if previous else [],
                    status=TaskStatus.PENDING,
                    parameters=parameters.copy(),
                    max_retries=1,
                    timeout_seconds=900.0,
                )
            )
            previous = tasks[-1].id
        if corrupt == "missing":
            return tasks[:-1]
        if corrupt == "duplicate":
            tasks[-1] = tasks[-2]
        return tasks

    return build


def create(
    repo: AssemblyGraphRepository,
    wf: Workflow,
    *,
    factory: Callable[[AssetRevision], list[Task]] | None = None,
) -> tuple[AssetRevision, list[Task], bool]:
    return repo.create_graph(
        workflow=wf,
        asset_id=ASSET_ID,
        spec_hash=SPEC_HASH,
        profile_id=PROFILE_ID,
        profile_version=PROFILE_VERSION,
        concept_hash=CONCEPT_HASH,
        raw_glb_hash=SOURCE_HASH,
        task_factory=factory or task_factory(wf),
    )


def counts(db: Database) -> tuple[int, int, int, int]:
    with db.transaction() as conn:
        values = tuple(
            conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("workflows", "asset_revisions", "tasks", "audit_events")
        )
    return (int(values[0]), int(values[1]), int(values[2]), int(values[3]))


def test_create_graph_is_atomic_and_exact_retry_rehydrates(db: Database) -> None:
    repo = AssemblyGraphRepository(db)
    wf = workflow()
    revision, tasks, created = create(repo, wf)
    assert created is True
    assert revision.revision_number == 1
    assert len(tasks) == 8
    assert wf.name == "Local assembly: atomic_prop r001"
    assert counts(db) == (1, 1, 8, 1)

    retry_workflow = workflow()
    retry_revision, retry_tasks, created = create(repo, retry_workflow)
    assert created is False
    assert retry_revision == revision
    assert retry_tasks == tasks
    assert retry_workflow.name == wf.name
    assert counts(db) == (1, 1, 8, 1)


@pytest.mark.parametrize("field", ["source_version", "profile_version"])
def test_exact_retry_rejects_boolean_integer_substitution_without_writes(
    db: Database, field: str
) -> None:
    repo = AssemblyGraphRepository(db)
    create(repo, workflow())
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT parameters_json FROM tasks WHERE id = 'workflow-1-PREPARE'"
        ).fetchone()
        parameters = json.loads(row["parameters_json"])
        parameters[field] = True
        conn.execute(
            "UPDATE tasks SET parameters_json = ? WHERE id = 'workflow-1-PREPARE'",
            (json.dumps(parameters),),
        )
    before = counts(db)

    with pytest.raises(ValidationError, match="complete canonical graph"):
        create(repo, workflow())

    assert counts(db) == before == (1, 1, 8, 1)
    with db.transaction() as conn:
        row = conn.execute(
            "SELECT parameters_json FROM tasks WHERE id = 'workflow-1-PREPARE'"
        ).fetchone()
    assert json.loads(row["parameters_json"])[field] is True


@pytest.mark.parametrize(
    ("table", "when", "condition"),
    [
        ("workflows", "AFTER INSERT", ""),
        ("asset_revisions", "AFTER INSERT", ""),
        ("tasks", "AFTER INSERT", "WHEN NEW.id LIKE '%-PROCESS'"),
        ("audit_events", "AFTER INSERT", ""),
    ],
)
def test_each_write_failure_rolls_back_complete_graph_and_revision(
    db: Database, table: str, when: str, condition: str
) -> None:
    with db.transaction() as conn:
        conn.execute(
            f"CREATE TRIGGER fail_graph_write {when} ON {table} {condition} "
            "BEGIN SELECT RAISE(ABORT, 'injected graph failure'); END"
        )
    repo = AssemblyGraphRepository(db)
    with pytest.raises(sqlite3.IntegrityError):
        create(repo, workflow())
    assert counts(db) == (0, 0, 0, 0)

    with db.transaction() as conn:
        conn.execute("DROP TRIGGER fail_graph_write")
    revision, _, created = create(repo, workflow())
    assert created is True
    assert revision.revision_number == 1


@pytest.mark.parametrize("corrupt", ["workflow", "revision", "missing", "duplicate"])
def test_malformed_canonical_graph_is_rejected_before_any_commit(
    db: Database, corrupt: str
) -> None:
    repo = AssemblyGraphRepository(db)
    wf = workflow()
    with pytest.raises(ValidationError):
        create(repo, wf, factory=task_factory(wf, corrupt=corrupt))
    assert counts(db) == (0, 0, 0, 0)


def test_missing_project_and_conflicting_workflow_identity_fail_closed(db: Database) -> None:
    repo = AssemblyGraphRepository(db)
    wf = workflow()
    wf.project_id = "missing-project"
    with pytest.raises(ValidationError, match="project"):
        create(repo, wf)
    assert counts(db) == (0, 0, 0, 0)

    valid = workflow()
    create(repo, valid)
    wrong_project = workflow()
    wrong_project.project_id = "missing-project"
    with pytest.raises(ValidationError, match="project"):
        create(repo, wrong_project)
    assert counts(db) == (1, 1, 8, 1)


def test_concurrent_asset_graphs_receive_distinct_monotonic_revisions(db: Database) -> None:
    repo = AssemblyGraphRepository(db)

    def run(index: int) -> int:
        wf = workflow(f"workflow-{index}")
        return int(create(repo, wf)[0].revision_number)

    with ThreadPoolExecutor(max_workers=2) as pool:
        numbers = list(pool.map(run, (1, 2)))
    assert sorted(numbers) == [1, 2]
    assert counts(db) == (2, 2, 16, 2)


def test_orphan_workflow_id_is_not_reconstructed_or_deleted(db: Database) -> None:
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO workflows (id, project_id, name, status, created_at, updated_at) "
            "VALUES ('workflow-1', 'project-1', 'orphan', 'PENDING', 'now', 'now')"
        )
    with pytest.raises(ValidationError, match="orphaned or incomplete"):
        create(AssemblyGraphRepository(db), workflow())
    assert counts(db) == (1, 0, 0, 0)
