"""Test-only helpers for C2-B publication matrix cases."""

from __future__ import annotations

import os
import shutil
import sqlite3
import subprocess
import sys
import threading
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from gamefactory.adapters.assets.v08_candidate_evidence import (
    bind_production_candidate_evidence_exporter,
)
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import Artifact, Task, Workflow
from gamefactory.workflows import v08_candidate_evidence_publication as publication_mod
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers
from tests.unit.test_v08_candidate_workflow import _approve, _handlers, _run_to_test_only_gate

C2_EXPORT_ARTIFACT_TYPES = (
    "candidate-c2-export-marker",
    "candidate-c2-evidence-result",
    "candidate-c2-evidence-manifest",
)


def count_c2_artifacts_on_connection(conn: sqlite3.Connection, workflow_id: str) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS c FROM artifacts
        WHERE workflow_id = ?
          AND artifact_type IN (?, ?, ?);
        """,
        (workflow_id, *C2_EXPORT_ARTIFACT_TYPES),
    ).fetchone()
    return int(row["c"])


def make_commit_denier(
    workflow_id: str,
    flags: dict[str, Any],
) -> Callable[[sqlite3.Connection], None]:
    """Deny COMMIT on param1 after exactly three uncommitted C2 rows are inserted."""

    def _commit_denied_after_inserts(conn: sqlite3.Connection) -> None:
        flags.clear()
        flags["reached_inserts"] = count_c2_artifacts_on_connection(conn, workflow_id) == 3
        flags["denied_commit"] = False
        flags["commit_exception"] = None

        def _authorizer(
            action: int,
            param1: str | None,
            _param2: str | None,
            _dbname: str | None,
            _source: str | None,
        ) -> int:
            if action == sqlite3.SQLITE_TRANSACTION and param1 == "COMMIT":
                flags["denied_commit"] = True
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        conn.set_authorizer(_authorizer)
        try:
            conn.commit()
        except sqlite3.DatabaseError as exc:
            flags["commit_exception"] = exc
            raise

    return _commit_denied_after_inserts


def make_partial_insert_denier(
    workflow_id: str,
    flags: dict[str, Any],
) -> Callable[[ArtifactRepository, sqlite3.Connection, list[Artifact]], None]:
    """Insert one C2 artifact on the held connection, then fail the next INSERT (duplicate PK)."""

    _real_save_many = ArtifactRepository.save_many_on_connection

    def _save_many_with_partial_insert_failure(
        self: ArtifactRepository,
        conn: sqlite3.Connection,
        artifacts: list[Artifact],
    ) -> None:
        flags.clear()
        flags["reached_first_insert"] = False
        flags["denied_subsequent_insert"] = False
        flags["insert_exception"] = None
        flags["uncommitted_c2_count_after_first"] = None
        if len(artifacts) < 2:
            _real_save_many(self, conn, artifacts)
            return
        _real_save_many(self, conn, [artifacts[0]])
        flags["reached_first_insert"] = True
        flags["uncommitted_c2_count_after_first"] = count_c2_artifacts_on_connection(
            conn, workflow_id
        )
        collision = replace(artifacts[1], id=artifacts[0].id)
        try:
            _real_save_many(self, conn, [collision])
        except sqlite3.DatabaseError as exc:
            flags["insert_exception"] = exc
            flags["denied_subsequent_insert"] = True
            raise

    return _save_many_with_partial_insert_failure


def copy_prepared_stage_to_managed_namespace(
    project_root: Path,
    source: Path,
    token: str,
) -> Path:
    """Copy a D-ready stage into a fresh managed staging slot (no publish dest cleanup)."""
    dest = project_root / ".gf" / "candidate_evidence_staging" / f"stage_{token}"
    if dest.exists():
        raise AssertionError(f"managed staging slot already exists: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, dest)
    return dest


def run_workflow_to_evidence_publication_gate(
    tmp_path: Path,
) -> tuple[Any, str, CandidateWorkflowHandlers, Workflow, Task, Any, dict[str, Any]]:
    """Approve TEST_ONLY gate and capture publication inputs before CAS."""
    from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace

    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    handlers = _handlers(workspace.root, workspace.db)
    bind_production_candidate_evidence_exporter(handlers)
    engine, workflow_id, handlers = _run_to_test_only_gate(workspace, handlers=handlers)
    captured: dict[str, Any] = {}

    def _capture_publish(*args: Any, **kwargs: Any) -> Any:
        captured["args"] = args
        captured["kwargs"] = kwargs
        raise ValidationError("test harness: stop before publication commit")

    blocked = engine.run_workflow(workflow_id)
    _approve(engine, workspace.db, blocked.pending_approval_id or "")
    with patch.object(
        publication_mod,
        "publish_candidate_evidence_from_staging",
        _capture_publish,
    ):
        for _ in range(12):
            engine.run_workflow(workflow_id)
            if captured:
                break
    assert "kwargs" in captured
    workflow = WorkflowRepository(workspace.db).get(workflow_id)
    assert workflow is not None
    evidence_task = next(
        t
        for t in TaskRepository(workspace.db).list_by_workflow(workflow_id)
        if t.task_type == "v08_candidate_evidence"
    )
    execution = ExecutionRepository(workspace.db).get_latest_attempt(evidence_task.id)
    assert execution is not None
    return workspace, workflow_id, handlers, workflow, evidence_task, execution, captured


def duplicate_prepared_staging_container(source: Path, dest: Path) -> None:
    if dest.exists():
        raise AssertionError(f"refusing to replace existing prepared stage copy: {dest}")
    shutil.copytree(source, dest)


def race_publication_from_prepared_stages(
    publish_callable: Callable[[Path], None],
    stage_a: Path,
    stage_b: Path,
) -> list[BaseException | None]:
    barrier = threading.Barrier(2)
    outcomes: list[BaseException | None] = [None, None]

    def _runner(index: int, stage: Path) -> None:
        barrier.wait()
        try:
            publish_callable(stage)
            outcomes[index] = None
        except BaseException as exc:
            outcomes[index] = exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(_runner, 0, stage_a),
            pool.submit(_runner, 1, stage_b),
        ]
        for fut in as_completed(futures):
            fut.result()
    return outcomes


def assert_single_c2_export_triplet(db: Any, workflow_id: str) -> None:
    markers = [
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-export-marker"
    ]
    results = [
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-evidence-result"
    ]
    manifests = [
        a
        for a in ArtifactRepository(db).list_by_workflow(workflow_id)
        if a.artifact_type == "candidate-c2-evidence-manifest"
    ]
    assert len(markers) == 1
    assert len(results) == 1
    assert len(manifests) == 1


@contextmanager
def inject_destination_immediately_before_atomic_rename(
    injector: Callable[[str, str], None],
) -> Iterator[None]:
    """Run injector with (src, dst) immediately before the real no-replace rename syscall."""
    from gamefactory.adapters.assets import v08_candidate_evidence as evidence_mod

    original_rename = os.rename
    original_linux = evidence_mod._linux_rename_noreplace_directory

    def _rename_with_injection(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        injector(os.fspath(src), os.fspath(dst))
        return original_rename(src, dst)

    def _linux_with_injection(src: Path, dst: Path) -> None:
        injector(os.fspath(src), os.fspath(dst))
        return original_linux(src, dst)

    with patch.object(evidence_mod.os, "rename", _rename_with_injection):
        with patch.object(evidence_mod, "_linux_rename_noreplace_directory", _linux_with_injection):
            yield


def inject_empty_destination_directory(_src: str, dst: str) -> None:
    Path(dst).mkdir(parents=True, exist_ok=True)


def inject_nonempty_destination_directory(_src: str, dst: str) -> None:
    root = Path(dst)
    root.mkdir(parents=True, exist_ok=True)
    (root / "foreign.txt").write_text("occupied", encoding="utf-8")


def inject_destination_file(_src: str, dst: str) -> None:
    Path(dst).write_text("foreign-file", encoding="utf-8")


def inject_destination_directory_junction(_src: str, dst: str) -> None:
    target = Path(dst).with_name(Path(dst).name + "_junction_target")
    target.mkdir(parents=True, exist_ok=True)
    (target / "inside.txt").write_text("junction-target", encoding="utf-8")
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", dst, str(target)],
            check=True,
            capture_output=True,
        )
        return
    if sys.platform == "linux":
        os.symlink(target, dst, target_is_directory=True)
        return
    raise AssertionError("junction race injection requires Windows or Linux")
