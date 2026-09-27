"""Unit tests for AssetRevisionRepository and ProviderOperationIntentRepository."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    AssetRevisionRepository,
    ProjectRepository,
    ProviderOperationIntent,
    ProviderOperationIntentRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.models import Project, Task, Workflow


def _setup_parents(db: Database, tmp_path: Path) -> tuple[str, str]:
    proj_repo = ProjectRepository(db)
    proj_repo.save(Project(id="p1", name="P1", engine_type="godot", root_path=str(tmp_path)))
    wf_repo = WorkflowRepository(db)
    wf_repo.save(Workflow(id="wf1", project_id="p1", name="WF1"))
    task_repo = TaskRepository(db)
    task_repo.save(Task(id="t1", workflow_id="wf1", name="T1", task_type="paid_generation"))
    return "wf1", "t1"


def test_asset_revision_monotonic_allocation(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    wf_id, _ = _setup_parents(db, tmp_path)

    repo = AssetRevisionRepository(db)
    # Monotonically allocate revision 1
    rev1 = repo.allocate_revision(
        asset_id="prop_energy_crate_01",
        workflow_id=wf_id,
        spec_hash="spec_hash_1",
    )
    assert rev1.revision_number == 1
    assert rev1.revision_id == "r001"
    assert rev1.workflow_id == wf_id
    assert rev1.spec_hash == "spec_hash_1"

    # Monotonically allocate revision 2
    rev2 = repo.allocate_revision(
        asset_id="prop_energy_crate_01",
        workflow_id=wf_id,
        spec_hash="spec_hash_2",
    )
    assert rev2.revision_number == 2
    assert rev2.revision_id == "r002"

    latest = repo.get_latest("prop_energy_crate_01")
    assert latest is not None
    assert latest.revision_number == 2

    all_revs = repo.list_by_asset("prop_energy_crate_01")
    assert len(all_revs) == 2
    assert [r.revision_number for r in all_revs] == [1, 2]

    wf_revs = repo.list_by_workflow(wf_id)
    assert len(wf_revs) == 2


def test_asset_revision_immutability_and_artifacts(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    wf_id, _ = _setup_parents(db, tmp_path)

    repo = AssetRevisionRepository(db)
    rev1 = repo.allocate_revision(
        asset_id="prop_energy_crate_01",
        workflow_id=wf_id,
        spec_hash="spec_hash_1",
    )

    # Attach artifact hashes safely
    rev1.concept_hash = "concept_hash_abc"
    rev1.raw_glb_hash = "raw_hash_def"
    rev1.processed_glb_hash = "processed_hash_ghi"
    rev1.validation_report_hash = "validation_hash_jkl"
    repo.save(rev1)

    fetched = repo.get("prop_energy_crate_01", 1)
    assert fetched is not None
    assert fetched.concept_hash == "concept_hash_abc"
    assert fetched.raw_glb_hash == "raw_hash_def"

    # Overwriting spec_hash on an existing revision is strictly forbidden.
    rev1.spec_hash = "different_spec_hash"
    with pytest.raises(ValueError, match="Cannot overwrite immutable spec_hash"):
        repo.save(rev1)

    # Overwriting workflow_id on an existing revision is strictly forbidden
    rev1.spec_hash = "spec_hash_1"
    rev1.workflow_id = "different_workflow_id"
    with pytest.raises(ValueError, match="Cannot overwrite workflow_id"):
        repo.save(rev1)

    fields = (
        "concept_hash",
        "raw_glb_hash",
        "processed_glb_hash",
        "validation_report_hash",
    )
    for field_name in fields:
        saved = repo.get("prop_energy_crate_01", 1)
        assert saved is not None
        original = getattr(saved, field_name)
        assert original is not None

        setattr(saved, field_name, None)
        with pytest.raises(ValueError, match=f"Cannot overwrite immutable {field_name}"):
            repo.save(saved)

        setattr(saved, field_name, f"changed-{field_name}")
        with pytest.raises(ValueError, match=f"Cannot overwrite immutable {field_name}"):
            repo.save(saved)

        setattr(saved, field_name, original)
        repo.save(saved)  # equal values are idempotent


def test_asset_revision_runtime_evidence_is_append_only(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    wf_id, _ = _setup_parents(db, tmp_path)
    repo = AssetRevisionRepository(db)
    revision = repo.allocate_revision(
        asset_id="prop_energy_crate_01", workflow_id=wf_id, spec_hash="spec_hash"
    )
    revision.runtime_evidence_hashes = ["runtime-a"]
    repo.save(revision)

    for changed in ([], ["runtime-b"]):
        attempted = repo.get("prop_energy_crate_01", 1)
        assert attempted is not None
        attempted.runtime_evidence_hashes = changed
        with pytest.raises(ValueError, match="runtime evidence hashes"):
            repo.save(attempted)

    revision.runtime_evidence_hashes = ["runtime-a", "runtime-c"]
    repo.save(revision)
    fetched = repo.get("prop_energy_crate_01", 1)
    assert fetched is not None
    assert fetched.runtime_evidence_hashes == ["runtime-a", "runtime-c"]
    fetched.runtime_evidence_hashes = ["runtime-a", "runtime-b"]
    with pytest.raises(ValueError, match="runtime evidence hashes"):
        repo.save(fetched)


def test_asset_revision_allocation_is_concurrent_and_monotonic(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    wf_id, _ = _setup_parents(db, tmp_path)
    repo = AssetRevisionRepository(db)

    def allocate(_: int) -> int:
        return repo.allocate_revision("prop_concurrent_crate", wf_id, "spec").revision_number

    with ThreadPoolExecutor(max_workers=8) as pool:
        numbers = list(pool.map(allocate, range(8)))
    assert sorted(numbers) == list(range(1, 9))
    assert [item.revision_number for item in repo.list_by_asset("prop_concurrent_crate")] == list(
        range(1, 9)
    )


def test_provider_operation_intent_repository(tmp_path: Path) -> None:
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    wf_id, task_id = _setup_parents(db, tmp_path)

    intent_repo = ProviderOperationIntentRepository(db)
    # Intent with honest None estimated_cost
    intent = ProviderOperationIntent(
        id="intent-001",
        workflow_id=wf_id,
        task_id=task_id,
        asset_id="prop_energy_crate_01",
        revision_number=1,
        provider="meshy",
        operation="image-to-3d",
        concept_hash="chash123",
        request_fingerprint="fp123",
        approval_id="app123",
        estimated_cost=None,
        status="INTENDED",
    )
    intent_repo.save(intent)

    fetched = intent_repo.get("intent-001")
    assert fetched is not None
    assert fetched.provider == "meshy"
    assert fetched.status == "INTENDED"
    assert fetched.estimated_cost is None
    assert fetched.actual_cost is None

    by_task = intent_repo.get_by_task(task_id)
    assert by_task is not None
    assert by_task.id == "intent-001"

    by_fp = intent_repo.get_by_fingerprint("fp123")
    assert by_fp is not None
    assert by_fp.id == "intent-001"

    # Persist external task ID immediately upon submission
    intent.status = "SUBMITTED"
    intent.external_task_id = "meshy-task-999"
    intent.actual_cost = 4.5
    intent_repo.save(intent)

    fetched_updated = intent_repo.get("intent-001")
    assert fetched_updated is not None
    assert fetched_updated.status == "SUBMITTED"
    assert fetched_updated.external_task_id == "meshy-task-999"
    assert fetched_updated.actual_cost == 4.5
