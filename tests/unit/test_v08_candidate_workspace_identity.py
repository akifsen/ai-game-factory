"""Managed workspace token binding: reject forged V08CandidateWorkspace contexts."""

from __future__ import annotations

import gc
import hashlib
import weakref
from dataclasses import fields, replace
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.core.domain.errors import ValidationError
from gamefactory.workflows.v08_candidate_workspace import (
    V08CandidateWorkspace,
    create_fresh_v08_candidate_workspace,
    reject_unmanaged_candidate_database,
)


def _forged_workspace_without_post_init(
    genuine: V08CandidateWorkspace, **overrides: object
) -> V08CandidateWorkspace:
    """Construct a forged context that bypasses constructor validation."""
    forged = object.__new__(V08CandidateWorkspace)
    for field in fields(V08CandidateWorkspace):
        object.__setattr__(
            forged, field.name, overrides.get(field.name, getattr(genuine, field.name))
        )
    return forged


def test_genuine_managed_workspace_identity_accepted(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workspace.assert_managed_database()
    reject_unmanaged_candidate_database(
        workspace.db, workspace=workspace, project_root=workspace.root
    )


def _foreign_candidate_database(tmp_path: Path) -> tuple[Path, Database]:
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    foreign_db_path = foreign_root / ".gamefactory/state/candidate-factory.db"
    foreign_db_path.parent.mkdir(parents=True)
    db = Database(foreign_db_path)
    MigrationRunner(db).apply_all()
    return foreign_root, db


def test_forged_workspace_reusing_valid_token_and_foreign_root_rejected(tmp_path: Path) -> None:
    genuine = create_fresh_v08_candidate_workspace(tmp_path / "genuine")
    foreign_root, foreign_db = _foreign_candidate_database(tmp_path)
    foreign_db_path = foreign_db.db_path
    before = hashlib.sha256(foreign_db_path.read_bytes()).hexdigest()

    forged = _forged_workspace_without_post_init(
        genuine,
        root=foreign_root.resolve(),
        db=foreign_db,
    )
    with pytest.raises(ValidationError):
        forged.assert_managed_database()
    after_assert = hashlib.sha256(foreign_db_path.read_bytes()).hexdigest()
    assert before == after_assert

    with pytest.raises(ValidationError):
        reject_unmanaged_candidate_database(foreign_db, workspace=forged, project_root=foreign_root)
    after_reject = hashlib.sha256(foreign_db_path.read_bytes()).hexdigest()
    assert before == after_reject


def test_forged_workspace_same_db_path_new_database_object_rejected(tmp_path: Path) -> None:
    genuine = create_fresh_v08_candidate_workspace(tmp_path)
    second_db = Database(genuine.db.db_path)
    assert second_db is not genuine.db
    forged = _forged_workspace_without_post_init(genuine, db=second_db)
    with pytest.raises(ValidationError, match="factory-managed"):
        forged.assert_managed_database()


def test_factory_binding_retains_database_object_same_path_replacement_rejected(
    tmp_path: Path,
) -> None:
    """Registry keeps the factory Database instance; same-path reopen cannot substitute."""
    genuine = create_fresh_v08_candidate_workspace(tmp_path)
    managed_id = genuine.managed_workspace_id
    root = genuine.root
    project_id = genuine.project_id
    db_path = genuine.db.db_path
    bound_db_ref = weakref.ref(genuine.db)

    del genuine
    gc.collect()

    retained = bound_db_ref()
    assert retained is not None

    replacement_db = Database(db_path)
    assert replacement_db is not retained

    forged = object.__new__(V08CandidateWorkspace)
    object.__setattr__(forged, "project_id", project_id)
    object.__setattr__(forged, "root", root)
    object.__setattr__(forged, "db", replacement_db)
    object.__setattr__(forged, "managed_workspace_id", managed_id)
    with pytest.raises(ValidationError, match="factory-managed"):
        forged.assert_managed_database()


def test_forged_workspace_changed_project_id_rejected(tmp_path: Path) -> None:
    genuine = create_fresh_v08_candidate_workspace(tmp_path)
    forged = _forged_workspace_without_post_init(genuine, project_id="not-the-bound-project")
    with pytest.raises(ValidationError, match="project identity"):
        forged.assert_managed_database()


def test_dataclasses_replace_with_forged_fields_rejected_at_construction(tmp_path: Path) -> None:
    genuine = create_fresh_v08_candidate_workspace(tmp_path)
    foreign_root, foreign_db = _foreign_candidate_database(tmp_path)
    with pytest.raises(ValidationError):
        replace(genuine, root=foreign_root.resolve(), db=foreign_db)
