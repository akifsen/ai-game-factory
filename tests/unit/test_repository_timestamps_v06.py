"""Unit tests for V0.6 repository timestamp semantics: created_at immutability and updated_at advancement."""

from __future__ import annotations

from pathlib import Path

import pytest

from gamefactory.adapters.persistence import repositories
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


@pytest.fixture
def db(tmp_path: Path) -> Database:
    database = Database(tmp_path / "timestamps.db")
    MigrationRunner(database).apply_all()
    return database


def _setup_parents(db: Database, tmp_path: Path) -> tuple[str, str]:
    proj_repo = ProjectRepository(db)
    proj_repo.save(Project(id="p1", name="P1", engine_type="godot", root_path=str(tmp_path)))
    wf_repo = WorkflowRepository(db)
    wf_repo.save(Workflow(id="wf1", project_id="p1", name="WF1"))
    task_repo = TaskRepository(db)
    task_repo.save(Task(id="t1", workflow_id="wf1", name="T1", task_type="paid_generation"))
    return "wf1", "t1"


def test_provider_operation_intent_timestamp_semantics(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf_id, task_id = _setup_parents(db, tmp_path)
    intent_repo = ProviderOperationIntentRepository(db)

    t0 = "2026-01-01T00:00:00+00:00"
    t1 = "2026-01-01T00:01:00+00:00"

    # Deterministic, controllable clock (audit events read it too).
    clock = {"now": t0}
    monkeypatch.setattr(repositories, "utc_now_iso", lambda: clock["now"])

    # 1. claim_intent insert: created_at == updated_at
    intent = ProviderOperationIntent(
        id="INTENT-001",
        workflow_id=wf_id,
        task_id=task_id,
        asset_id="asset-01",
        revision_number=1,
        provider="meshy",
        operation="image-to-3d",
        concept_hash="a" * 64,
        request_fingerprint="f" * 64,
        approval_id="app-01",
        status="SUBMITTING",
        created_at=t0,
        updated_at=t0,
    )
    claimed, is_winner = intent_repo.claim_intent(intent)
    assert is_winner is True
    assert claimed.created_at == t0
    assert claimed.updated_at == t0

    loaded = intent_repo.get("INTENT-001")
    assert loaded is not None
    assert loaded.created_at == t0
    assert loaded.updated_at == t0

    # 2. save on update (conflict): created_at stays unchanged, updated_at advances strictly
    intent.status = "SUBMITTED"
    intent.external_task_id = "ext-task-123"
    clock["now"] = t1
    intent_repo.save(intent)

    assert intent.created_at == t0
    assert intent.updated_at == t1

    loaded2 = intent_repo.get("INTENT-001")
    assert loaded2 is not None
    assert loaded2.created_at == t0
    assert loaded2.updated_at == t1
    assert loaded2.status == "SUBMITTED"
    assert loaded2.external_task_id == "ext-task-123"


def test_asset_revision_timestamp_semantics(
    db: Database, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wf_id, _ = _setup_parents(db, tmp_path)
    rev_repo = AssetRevisionRepository(db)

    t0 = "2026-01-01T00:00:00+00:00"
    t1 = "2026-01-01T00:01:00+00:00"
    t2 = "2026-01-01T00:02:00+00:00"

    current_time = t0
    monkeypatch.setattr(repositories, "utc_now_iso", lambda: current_time)

    # 1. Allocate initial revision: created_at == updated_at == t0
    revision = rev_repo.allocate_revision(
        asset_id="asset_test",
        workflow_id=wf_id,
        spec_hash="s" * 64,
        concept_hash="c" * 64,
    )
    assert revision.created_at == t0
    assert revision.updated_at == t0

    # 2. Save without changing mutable fields: updated_at and created_at stay unchanged
    current_time = t1
    rev_repo.save(revision)
    assert revision.created_at == t0
    assert revision.updated_at == t0

    loaded = rev_repo.get("asset_test", 1)
    assert loaded is not None
    assert loaded.created_at == t0
    assert loaded.updated_at == t0

    # 3. Update mutable field (raw_glb_hash): created_at unchanged, updated_at advances to t2
    current_time = t2
    revision.raw_glb_hash = "r" * 64
    rev_repo.save(revision)

    assert revision.created_at == t0
    assert revision.updated_at == t2

    loaded2 = rev_repo.get("asset_test", 1)
    assert loaded2 is not None
    assert loaded2.created_at == t0
    assert loaded2.updated_at == t2
    assert loaded2.raw_glb_hash == "r" * 64
