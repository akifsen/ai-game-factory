"""Fresh isolated workspace + DB entry for V0.8-3C candidate workflows."""

from __future__ import annotations

import secrets
from dataclasses import dataclass
from pathlib import Path

from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import AssetRevisionRepository, ProjectRepository
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import Project
from gamefactory.core.execution.path_guard import PathGuard

CANDIDATE_STATE_DIR = ".gamefactory/state"
CANDIDATE_DB_FILENAME = "candidate-factory.db"
PRODUCTION_DB_FILENAME = "factory.db"
CANDIDATE_WORKSPACE_ROOT_NAME = "c"


@dataclass(frozen=True)
class _ManagedWorkspaceBinding:
    root: Path
    db_path: Path
    db: Database
    project_id: str


_MANAGED_WORKSPACE_REGISTRY: dict[str, _ManagedWorkspaceBinding] = {}


def _lexical_absolute(path: Path) -> Path:
    return Path(path).absolute()


def _assert_lexical_path_has_no_links(path: Path) -> None:
    lexical = _lexical_absolute(path)
    for ancestor in [lexical, *lexical.parents]:
        if path_crosses_link(ancestor):
            raise ValidationError("Candidate workspace path crosses a symlink or junction")
        if ancestor.anchor == ancestor:
            break


def _register_managed_workspace_binding(
    managed_workspace_id: str,
    *,
    root: Path,
    db: Database,
    project_id: str,
) -> None:
    _MANAGED_WORKSPACE_REGISTRY[managed_workspace_id] = _ManagedWorkspaceBinding(
        root=_lexical_absolute(root),
        db_path=_lexical_absolute(db.db_path),
        db=db,
        project_id=project_id,
    )


def _assert_bound_managed_workspace_identity(workspace: V08CandidateWorkspace) -> None:
    binding = _MANAGED_WORKSPACE_REGISTRY.get(workspace.managed_workspace_id)
    if binding is None:
        raise ValidationError(
            "V08CandidateWorkspace must be created via create_fresh_v08_candidate_workspace"
        )
    _assert_lexical_path_has_no_links(workspace.root)
    _assert_lexical_path_has_no_links(workspace.db.db_path)
    if workspace.project_id != binding.project_id:
        raise ValidationError("Candidate workspace project identity does not match factory binding")
    if _lexical_absolute(workspace.root) != binding.root:
        raise ValidationError("Candidate workspace root does not match factory-managed binding")
    if workspace.db is not binding.db:
        raise ValidationError(
            "Candidate workflow database must be the factory-managed workspace database"
        )
    if _lexical_absolute(workspace.db.db_path) != binding.db_path:
        raise ValidationError(
            "Candidate workflow database path does not match factory-managed binding"
        )


@dataclass(frozen=True)
class V08CandidateWorkspace:
    """Ephemeral candidate project root and database; not production factory.db."""

    project_id: str
    root: Path
    db: Database
    managed_workspace_id: str

    def __post_init__(self) -> None:
        _assert_bound_managed_workspace_identity(self)

    @property
    def revision_repository(self) -> AssetRevisionRepository:
        return AssetRevisionRepository(self.db)

    def assert_managed_database(self) -> None:
        _assert_bound_managed_workspace_identity(self)
        expected = (self.root / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME).resolve()
        if self.db.db_path.resolve() != expected:
            raise ValidationError(
                "Candidate workflow database must be the workspace candidate-factory.db"
            )
        if self.db.db_path.name.casefold() == PRODUCTION_DB_FILENAME.casefold():
            raise ValidationError("Candidate workflow must not use production factory.db")


def _assert_safe_workspace_parent(parent: Path) -> Path:
    _assert_lexical_path_has_no_links(parent)
    resolved = parent.resolve(strict=False)
    if path_crosses_link(resolved):
        raise ValidationError("Candidate workspace parent crosses a symlink or junction")
    for ancestor in [resolved, *resolved.parents]:
        if path_crosses_link(ancestor):
            raise ValidationError("Candidate workspace ancestor crosses a symlink or junction")
        if ancestor.anchor == ancestor:
            break
    return resolved


def create_fresh_v08_candidate_workspace(
    parent: Path | str,
    *,
    project_id: str = "cand",
    project_name: str = "Candidate",
    engine_type: str = "godot",
) -> V08CandidateWorkspace:
    """Create a new candidate project directory and empty candidate-factory.db."""
    parent_path = _assert_safe_workspace_parent(Path(parent))
    root = parent_path / CANDIDATE_WORKSPACE_ROOT_NAME
    if root.exists():
        raise ValidationError(
            "Candidate workspace root already exists; use a fresh parent directory"
        )
    db_path = root / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME
    if db_path.exists():
        raise ValidationError("Candidate database path already exists before workspace creation")
    root.mkdir(parents=True, exist_ok=False)
    PathGuard(root).ensure_safe_parent(CANDIDATE_STATE_DIR)
    _assert_lexical_path_has_no_links(root)
    if path_crosses_link(root):
        raise ValidationError("Candidate workspace root crosses a symlink or junction")
    (root / "project.godot").write_text(
        'config_version=5\n[application]\nconfig/name="Candidate"\n', encoding="utf-8"
    )
    db = Database(db_path)
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(
        Project(
            id=project_id,
            name=project_name,
            engine_type=engine_type,
            root_path=str(root.resolve()),
        )
    )
    managed_id = secrets.token_hex(16)
    resolved_root = root.resolve()
    _register_managed_workspace_binding(
        managed_id,
        root=resolved_root,
        db=db,
        project_id=project_id,
    )
    workspace = V08CandidateWorkspace(
        project_id=project_id,
        root=resolved_root,
        db=db,
        managed_workspace_id=managed_id,
    )
    workspace.assert_managed_database()
    return workspace


def reject_unmanaged_candidate_database(
    db: Database,
    *,
    workspace: V08CandidateWorkspace,
    project_root: Path | None = None,
) -> None:
    """Reject production or pre-existing unmanaged databases before candidate writes."""
    workspace.assert_managed_database()
    if db is not workspace.db:
        raise ValidationError(
            "Candidate workflow database must be the factory-managed workspace database"
        )
    db_path = db.db_path.resolve()
    if db_path.name.casefold() == PRODUCTION_DB_FILENAME.casefold():
        raise ValidationError(
            "Refusing candidate workflow on production factory.db; "
            "open a fresh V08CandidateWorkspace instead"
        )
    if db_path.name.casefold() != CANDIDATE_DB_FILENAME.casefold():
        raise ValidationError(
            "Refusing candidate workflow on an unmanaged database file",
            details={"database": str(db_path)},
        )
    if project_root is not None:
        expected = (project_root.resolve() / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME).resolve()
        if db_path != expected:
            raise ValidationError(
                "Candidate database path must live under the workspace state directory"
            )


__all__ = [
    "CANDIDATE_DB_FILENAME",
    "CANDIDATE_WORKSPACE_ROOT_NAME",
    "V08CandidateWorkspace",
    "create_fresh_v08_candidate_workspace",
    "reject_unmanaged_candidate_database",
]
