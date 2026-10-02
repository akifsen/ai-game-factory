"""V0.8-9b animation review session bridge (fast unit coverage)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import gamefactory.cli.animation_review_session_bridge as bridge
from gamefactory.adapters.assets.animation_review_session_store import StoredAnimationReviewSession
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.read_only_database import ReadOnlyDatabase
from gamefactory.adapters.persistence.repositories import TaskRepository, WorkflowRepository
from gamefactory.cli.animation_review_session_bridge import (
    BRIDGE_SCHEMA_VERSION,
    CONTEXT_SCHEMA_VERSION,
    BridgePaths,
    build_readonly_candidate_handlers,
    run_bridge,
)
from gamefactory.core.domain.animation_review_session import create_animation_review_session
from gamefactory.core.domain.models import CostClass, Task, TaskStatus, Workflow, WorkflowStatus
from gamefactory.workflows.animation_review_session import AnimationReviewSessionMutationResult
from gamefactory.workflows.v08_candidate_workspace import (
    CANDIDATE_DB_FILENAME,
    CANDIDATE_STATE_DIR,
    create_fresh_v08_candidate_workspace,
)
from tests.unit.test_animation_review_session_store import (
    _binding_for_review_root,
    _seed_review_tree,
    _store_for,
)
from tests.unit.test_animation_review_session_workflow import _write_review_tree

_DIGEST = "a" * 64


def _clip_packages(tmp_path: Path) -> list[dict[str, str]]:
    pkg_a = tmp_path / "pkg-a"
    pkg_b = tmp_path / "pkg-b"
    pkg_a.mkdir(parents=True, exist_ok=True)
    pkg_b.mkdir(parents=True, exist_ok=True)
    clip_a = tmp_path / "clip-a.json"
    clip_b = tmp_path / "clip-b.json"
    clip_a.write_text("{}", encoding="utf-8")
    clip_b.write_text("{}", encoding="utf-8")
    return [
        {"animation_dir": str(pkg_a.resolve()), "clip_path": str(clip_a.resolve())},
        {"animation_dir": str(pkg_b.resolve()), "clip_path": str(clip_b.resolve())},
    ]


def _minimal_clip(clip_id: str) -> dict[str, Any]:
    return {
        "schema_version": "rig-animation-clip-0.8.0",
        "clip_id": clip_id,
        "duration_seconds": 1.0,
        "loop": False,
        "tracks": [
            {
                "bone": "Spine",
                "keyframes": [{"time": 0.0, "rotation_xyzw": [0.0, 0.0, 0.0, 1.0]}],
            }
        ],
    }


def _layout(tmp_path: Path) -> dict[str, Any]:
    workspace = create_fresh_v08_candidate_workspace(tmp_path / "parent")
    exchange = tmp_path / "exchange"
    exchange.mkdir(parents=True, exist_ok=True)
    preview = tmp_path / "preview"
    preview.mkdir()
    review = tmp_path / "review-set"
    _write_review_tree(
        review,
        clips=[("IdleA", _minimal_clip("IdleA")), ("WalkB", _minimal_clip("WalkB"))],
    )
    session_sidecar = tmp_path / "session-sidecar" / "session.json"
    session_sidecar.parent.mkdir(parents=True, exist_ok=True)
    workflow_id = _seed_workflow(workspace)
    return {
        "workspace": workspace.root,
        "exchange": exchange,
        "preview": preview,
        "review": review,
        "session_sidecar": session_sidecar,
        "workflow_id": workflow_id,  # type: ignore[dict-item]
    }


def _seed_workflow(workspace: Any) -> str:
    wf_id = "WF-BRIDGE-UNIT"
    WorkflowRepository(workspace.db).save(
        Workflow(
            id=wf_id,
            project_id=workspace.project_id,
            name="bridge unit",
            status=WorkflowStatus.PENDING,
        )
    )
    TaskRepository(workspace.db).save(
        Task(
            id=f"{wf_id}-PREPARE",
            workflow_id=wf_id,
            name="prepare",
            task_type="v08_candidate_prepare",
            cost_class=CostClass.LOCAL,
            depends_on=[],
            status=TaskStatus.PENDING,
            parameters={
                "graph_version": "0.8.0-candidate",
                "source_kind": "local_verified_rig",
            },
        )
    )
    return wf_id


def _context_document(layout: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "project_root": str(layout["workspace"].resolve()),
        "workflow_id": layout["workflow_id"],
        "preview_dir": str(layout["preview"].resolve()),
        "review_dir": str(layout["review"].resolve()),
        "clip_packages": _clip_packages(layout["workspace"].parent.parent),
        "session_path": str(layout["session_sidecar"].resolve()),
        "exchange_dir": str(layout["exchange"].resolve()),
    }


def _write_exchange_files(
    layout: dict[str, Any],
    *,
    request: dict[str, Any],
    context: dict[str, Any] | None = None,
) -> BridgePaths:
    exchange = layout["exchange"]
    context_path = exchange / "context.json"
    request_path = exchange / "request.json"
    response_path = exchange / "response.json"
    context_path.write_text(
        json.dumps(context or _context_document(layout), separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    request_path.write_text(
        json.dumps(request, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return BridgePaths(
        context_file=context_path,
        request_file=request_path,
        response_file=response_path,
    )


def _read_response(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _candidate_db_path(layout: dict[str, Any]) -> Path:
    return layout["workspace"] / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME


def _seed_valid_session_sidecar(layout: dict[str, Any]) -> StoredAnimationReviewSession:
    review_root = layout["review"]
    _seed_review_tree(review_root)
    store = _store_for(review_root, session_path=layout["session_sidecar"])
    return store.create(_binding_for_review_root(review_root))


def test_response_closed_fields_and_revision_is_int(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    binding = _binding_for_review_root(layout["review"])
    session = create_animation_review_session(binding)
    stored = StoredAnimationReviewSession(
        session=session,
        raw_sha256=_DIGEST,
    )
    monkeypatch.setattr(
        bridge,
        "build_readonly_candidate_handlers",
        lambda _root: MagicMock(),
    )
    monkeypatch.setattr(bridge, "_assert_workflow_belongs_to_project", lambda *a, **k: None)
    monkeypatch.setattr(
        bridge,
        "_execute_read",
        lambda *_a, **_k: bridge._response_document(
            ok=True,
            committed=False,
            current=False,
            stored=bridge._stored_payload(stored),
            error=None,
        ),
    )
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_SUCCESS
    doc = _read_response(paths.response_file)
    assert set(doc.keys()) == {
        "schema_version",
        "ok",
        "committed",
        "current",
        "stored",
        "error",
    }
    assert doc["schema_version"] == BRIDGE_SCHEMA_VERSION
    assert doc["ok"] is True
    assert doc["committed"] is False
    assert isinstance(doc["current"], bool)
    assert doc["error"] is None
    assert isinstance(doc["stored"]["session"]["revision"], int)


def test_malformed_request_emits_bounded_response(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "not-an-action"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_WORKFLOW_FAILURE
    doc = _read_response(paths.response_file)
    assert doc["ok"] is False
    assert doc["error"]["code"] == "REQUEST_INVALID"


def test_non_string_action_is_request_invalid_not_type_error(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": ["read"]},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_WORKFLOW_FAILURE
    doc = _read_response(paths.response_file)
    assert doc["error"]["code"] == "REQUEST_INVALID"


def test_rejects_response_inside_review_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    exchange = layout["exchange"]
    context_path = exchange / "context.json"
    request_path = exchange / "request.json"
    response_path = layout["review"] / "response.json"
    context_path.write_text(json.dumps(_context_document(layout)), encoding="utf-8")
    request_path.write_text(
        json.dumps({"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"}),
        encoding="utf-8",
    )
    code, doc = run_bridge(
        BridgePaths(
            context_file=context_path, request_file=request_path, response_file=response_path
        )
    )
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None
    assert not response_path.exists()


def test_rejects_overwriting_existing_response(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    paths.response_file.write_text("{}", encoding="utf-8")
    code, doc = run_bridge(paths)
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None


def test_build_handlers_uses_read_only_database_without_migrations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    root = layout["workspace"]
    migration_calls: list[str] = []

    class _SpyMigrationRunner:
        def __init__(self, _db: Database) -> None:
            migration_calls.append("init")

        def apply_all(self) -> None:
            migration_calls.append("apply")

    monkeypatch.setattr(
        "gamefactory.adapters.persistence.migrations.MigrationRunner", _SpyMigrationRunner
    )
    handlers = build_readonly_candidate_handlers(root)
    assert handlers.root == root.resolve()
    assert isinstance(handlers.artifacts.db, ReadOnlyDatabase)
    assert migration_calls == []


def test_read_no_session_uses_public_review_set_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    calls: list[str] = []

    def _gate(*_a: Any, **_k: Any) -> bool:
        calls.append("gate")
        return True

    monkeypatch.setattr(bridge, "animation_review_set_current", _gate)
    monkeypatch.setattr(
        bridge,
        "animation_review_session_current",
        lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("must not call session_current without a session file")
        ),
    )
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_SUCCESS
    doc = _read_response(paths.response_file)
    assert doc["stored"] is None
    assert doc["committed"] is False
    assert doc["current"] is True
    assert calls == ["gate"]


def test_read_no_session_returns_null_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setattr(
        bridge,
        "animation_review_set_current",
        lambda *_a, **_k: False,
    )
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_SUCCESS
    doc = _read_response(paths.response_file)
    assert doc["stored"] is None
    assert doc["committed"] is False


def test_read_malformed_session_file_is_session_invalid(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    layout["session_sidecar"].write_text("{not-json", encoding="utf-8")
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_WORKFLOW_FAILURE
    doc = _read_response(paths.response_file)
    assert doc["error"]["code"] == "SESSION_INVALID"
    assert doc["stored"] is None


def test_read_session_path_existing_non_file_leaf_is_session_invalid(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    sidecar = layout["session_sidecar"]
    if sidecar.exists():
        sidecar.unlink()
    sidecar.mkdir(parents=True, exist_ok=True)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_WORKFLOW_FAILURE
    doc = _read_response(paths.response_file)
    assert doc["error"]["code"] == "SESSION_INVALID"


def test_create_success_monkeypatched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path)
    review_root = layout["review"]
    _seed_review_tree(review_root)
    store = _store_for(review_root, session_path=layout["session_sidecar"])
    created = store.create(_binding_for_review_root(review_root))
    result = AnimationReviewSessionMutationResult(
        committed=True,
        current=False,
        stored=created,
    )

    def _fake_create(_ctx: Any) -> AnimationReviewSessionMutationResult:
        return result

    monkeypatch.setattr(bridge, "create_animation_review_session", _fake_create)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "create"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_SUCCESS
    doc = _read_response(paths.response_file)
    assert doc["committed"] is True
    assert doc["current"] is False
    assert doc["stored"]["raw_sha256"] == created.raw_sha256


def test_update_sha_conflict(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gamefactory.adapters.assets.animation_review_session_store import (
        AnimationReviewSessionStoreConflictError,
    )

    layout = _layout(tmp_path)
    layout["session_sidecar"].write_bytes(b"{}")

    def _boom(*_a: Any, **_k: Any) -> None:
        raise AnimationReviewSessionStoreConflictError("conflict")

    monkeypatch.setattr(bridge, "update_animation_review_session", _boom)
    paths = _write_exchange_files(
        layout,
        request={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": _DIGEST,
            "operation": {"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        },
    )
    code, _ = run_bridge(paths)
    doc = _read_response(paths.response_file)
    assert doc["error"]["code"] == "SESSION_CONFLICT"
    assert doc["committed"] is False
    assert doc["current"] is False
    assert code == bridge.EXIT_WORKFLOW_FAILURE


def test_update_stale_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError

    layout = _layout(tmp_path)
    layout["session_sidecar"].write_bytes(b"{}")
    monkeypatch.setattr(
        bridge,
        "update_animation_review_session",
        lambda *_a, **_k: (_ for _ in ()).throw(CandidateCurrentnessError("stale")),
    )
    paths = _write_exchange_files(
        layout,
        request={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": _DIGEST,
            "operation": {"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        },
    )
    code, _ = run_bridge(paths)
    doc = _read_response(paths.response_file)
    assert doc["error"]["code"] == "SESSION_STALE"
    assert code == bridge.EXIT_WORKFLOW_FAILURE


def test_invalid_context_writes_no_response(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    broken_context = _context_document(layout)
    broken_context["schema_version"] = "wrong"
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
        context=broken_context,
    )
    code, doc = run_bridge(paths)
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None
    assert not paths.response_file.exists()


def test_cli_rejects_relative_paths(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "gamefactory.cli.animation_review_session_bridge",
            "--context-file",
            "relative/context.json",
            "--request-file",
            str(paths.request_file),
            "--response-file",
            str(paths.response_file),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert proc.returncode == bridge.EXIT_CONFIG_ERROR
    assert not paths.response_file.exists()


def test_response_publication_claim_blocks_second_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    doc = bridge._response_document(
        ok=True,
        committed=False,
        current=False,
        stored=None,
        error=None,
    )
    monkeypatch.setattr(
        bridge,
        "_acquire_response_publication_claim",
        lambda *_a, **_k: None,
    )
    assert bridge._write_response_if_safe(paths.response_file, doc) is False
    assert not paths.response_file.exists()


def test_response_temp_cleanup_on_encoding_failure(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )

    class _Huge:
        def __getitem__(self, _key: str) -> Any:
            return "x" * 80_000

    broken = bridge._response_document(
        ok=True,
        committed=False,
        current=False,
        stored={"session": _Huge(), "raw_sha256": _DIGEST},  # type: ignore[arg-type]
        error=None,
    )
    parent = paths.response_file.parent
    before = set(parent.glob(".*.tmp"))
    assert bridge._write_response_if_safe(paths.response_file, broken) is False
    after = set(parent.glob(".*.tmp"))
    assert before == after
    assert not list(parent.glob(".*.claim"))


def test_cli_module_entrypoint_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    layout = _layout(tmp_path)
    monkeypatch.setattr(
        bridge,
        "animation_review_set_current",
        lambda *_a, **_k: False,
    )
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "gamefactory.cli.animation_review_session_bridge",
            "--context-file",
            str(paths.context_file),
            "--request-file",
            str(paths.request_file),
            "--response-file",
            str(paths.response_file),
        ],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2] / "src")},
    )
    assert proc.returncode == bridge.EXIT_SUCCESS
    assert _read_response(paths.response_file)["ok"] is True


def test_rejects_session_path_under_preview_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    nested = layout["preview"] / "sidecar" / "session.json"
    nested.parent.mkdir(parents=True, exist_ok=True)
    broken = _context_document(layout)
    broken["session_path"] = str(nested.resolve())
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
        context=broken,
    )
    code, doc = run_bridge(paths)
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None
    assert not paths.response_file.exists()


def test_rejects_session_path_under_animation_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    packages = _clip_packages(layout["workspace"].parent.parent)
    broken = _context_document(layout)
    broken["session_path"] = str((Path(packages[0]["animation_dir"]) / "session.json").resolve())
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
        context=broken,
    )
    code, doc = run_bridge(paths)
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None


def test_rejects_session_path_nested_under_review_dir(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    nested = layout["review"] / "nested" / "session.json"
    broken = _context_document(layout)
    broken["session_path"] = str(nested.resolve())
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
        context=broken,
    )
    code, doc = run_bridge(paths)
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None


def test_rejects_exchange_dir_under_session_sidecar(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    sidecar_dir = layout["session_sidecar"].parent
    broken = _context_document(layout)
    broken["exchange_dir"] = str(sidecar_dir.resolve())
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
        context=broken,
    )
    code, doc = run_bridge(paths)
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None


def test_rejects_context_file_under_session_sidecar(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    sidecar_dir = layout["session_sidecar"].parent
    context_path = sidecar_dir / "context.json"
    request_path = layout["exchange"] / "request.json"
    response_path = layout["exchange"] / "response.json"
    context_path.write_text(json.dumps(_context_document(layout)), encoding="utf-8")
    request_path.write_text(
        json.dumps({"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"}),
        encoding="utf-8",
    )
    code, doc = run_bridge(
        BridgePaths(
            context_file=context_path,
            request_file=request_path,
            response_file=response_path,
        )
    )
    assert code == bridge.EXIT_CONFIG_ERROR
    assert doc is None


def test_read_valid_session_missing_candidate_db_returns_historical_history(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    created = _seed_valid_session_sidecar(layout)
    session_bytes_before = layout["session_sidecar"].read_bytes()
    _candidate_db_path(layout).unlink()
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_SUCCESS
    doc = _read_response(paths.response_file)
    assert doc["ok"] is True
    assert doc["committed"] is False
    assert doc["current"] is False
    assert doc["error"] is None
    assert doc["stored"]["raw_sha256"] == created.raw_sha256
    assert layout["session_sidecar"].read_bytes() == session_bytes_before


def test_read_valid_session_workflow_mismatch_returns_historical_history(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    created = _seed_valid_session_sidecar(layout)
    broken = _context_document(layout)
    broken["workflow_id"] = "WF-MISMATCH-NOT-SEEDED"
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
        context=broken,
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_SUCCESS
    doc = _read_response(paths.response_file)
    assert doc["ok"] is True
    assert doc["current"] is False
    assert doc["stored"]["raw_sha256"] == created.raw_sha256


def test_read_valid_session_gate_failure_returns_historical_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    layout = _layout(tmp_path)
    created = _seed_valid_session_sidecar(layout)

    def _gate_unavailable(*_a: Any, **_k: Any) -> bool:
        raise OSError("review gate unavailable")

    monkeypatch.setattr(bridge, "animation_review_session_current", _gate_unavailable)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_SUCCESS
    doc = _read_response(paths.response_file)
    assert doc["current"] is False
    assert doc["stored"]["raw_sha256"] == created.raw_sha256


def test_create_rejects_when_candidate_db_missing(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    _candidate_db_path(layout).unlink()
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "create"},
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_WORKFLOW_FAILURE
    doc = _read_response(paths.response_file)
    assert doc["ok"] is False
    assert doc["error"]["code"] == "DATABASE_UNAVAILABLE"


def test_update_rejects_when_candidate_db_missing_without_session_change(
    tmp_path: Path,
) -> None:
    layout = _layout(tmp_path)
    created = _seed_valid_session_sidecar(layout)
    session_bytes_before = layout["session_sidecar"].read_bytes()
    _candidate_db_path(layout).unlink()
    paths = _write_exchange_files(
        layout,
        request={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": created.raw_sha256,
            "operation": {"op": "SetStatus", "clip_id": "IdleA", "status": "keep"},
        },
    )
    code, _ = run_bridge(paths)
    assert code == bridge.EXIT_WORKFLOW_FAILURE
    doc = _read_response(paths.response_file)
    assert doc["ok"] is False
    assert doc["error"]["code"] == "DATABASE_UNAVAILABLE"
    assert layout["session_sidecar"].read_bytes() == session_bytes_before


def test_simultaneous_managed_response_writers_one_winner(tmp_path: Path) -> None:
    layout = _layout(tmp_path)
    paths = _write_exchange_files(
        layout,
        request={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    doc = bridge._response_document(
        ok=True,
        committed=False,
        current=False,
        stored=None,
        error=None,
    )
    barrier = threading.Barrier(2)
    results: list[bool] = []
    lock = threading.Lock()

    def _worker() -> None:
        barrier.wait()
        ok = bridge._write_response_if_safe(paths.response_file, doc)
        with lock:
            results.append(ok)

    threads = [threading.Thread(target=_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sum(results) == 1
    assert paths.response_file.is_file()
    assert _read_response(paths.response_file)["ok"] is True
    assert not list(paths.response_file.parent.glob("*.publish.claim"))
    assert not list(paths.response_file.parent.glob(".*.tmp"))
