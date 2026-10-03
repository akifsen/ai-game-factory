"""Fresh-process bridge acceptance against exported V0.8-8 review-set bytes.

Uses managed evidence and ``_copy_review_set_fixture`` from
``test_v08_candidate_animation_review_set``. Each bridge action runs in a new
Python subprocess with ``PYTHONPATH`` rooted at the imported ``gamefactory`` package;
bridge/workflow/public-gate
code is not monkeypatched in-process.
"""

# ruff: noqa: F811

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from gamefactory.cli.animation_review_session_bridge import (
    BRIDGE_SCHEMA_VERSION,
    CONTEXT_SCHEMA_VERSION,
    BridgePaths,
)
from gamefactory.workflows.v08_candidate_workspace import (
    CANDIDATE_DB_FILENAME,
    CANDIDATE_STATE_DIR,
)
from tests.unit.test_v08_candidate_animation_review_set import (
    _copy_review_set_fixture,
    _sources_ab,
    acceptance_clip_a_path,  # noqa: F401
    acceptance_clip_b_path,  # noqa: F401
    authored_arm_reverse_02_clip_raw_bytes,
    build_authored_arm_reverse_02_clip_document,
    managed_review_set_evidence,  # noqa: F401
    shared_clip_preview_a,  # noqa: F401
    shared_clip_preview_b,  # noqa: F401
    shared_preview,  # noqa: F401
    shared_review_set,  # noqa: F401
)
from tests.unit.v08_candidate_c2b_readiness_fixtures import CompletedEvidenceContext


def _imported_gamefactory_pythonpath_root() -> Path:
    import gamefactory

    return Path(gamefactory.__file__).resolve().parent.parent


def _review_set_tree_digest(review_set: Path) -> dict[str, str]:
    return {
        str(path.relative_to(review_set)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(review_set.rglob("*"))
        if path.is_file()
    }


def _context_document(
    *,
    project_root: Path,
    workflow_id: str,
    preview: Path,
    review_set: Path,
    sources: tuple[Any, ...],
    session_path: Path,
    exchange_dir: Path,
) -> dict[str, Any]:
    clip_packages = [
        {
            "animation_dir": str(source.animation_dir.resolve()),
            "clip_path": str(source.clip_path.resolve()),
        }
        for source in sources
    ]
    return {
        "schema_version": CONTEXT_SCHEMA_VERSION,
        "project_root": str(project_root.resolve()),
        "workflow_id": workflow_id,
        "preview_dir": str(preview.resolve()),
        "review_dir": str(review_set.resolve()),
        "clip_packages": clip_packages,
        "session_path": str(session_path.resolve()),
        "exchange_dir": str(exchange_dir.resolve()),
    }


def _bridge_env() -> dict[str, str]:
    return {**os.environ, "PYTHONPATH": str(_imported_gamefactory_pythonpath_root())}


def _run_bridge_subprocess(paths: BridgePaths) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
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
        env=_bridge_env(),
    )


def _clip_record(session: dict[str, Any], clip_id: str) -> dict[str, Any]:
    for record in session["clip_records"]:
        if record["clip_id"] == clip_id:
            return record
    raise KeyError(clip_id)


def _invoke_bridge(
    *,
    exchange_dir: Path,
    context_doc: dict[str, Any],
    request_doc: dict[str, Any],
) -> tuple[int, dict[str, Any]]:
    context_path = exchange_dir / "context.json"
    request_path = exchange_dir / "request.json"
    response_path = exchange_dir / "response.json"
    context_path.write_text(
        json.dumps(context_doc, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    request_path.write_text(
        json.dumps(request_doc, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    if response_path.exists():
        response_path.unlink()
    paths = BridgePaths(
        context_file=context_path,
        request_file=request_path,
        response_file=response_path,
    )
    proc = _run_bridge_subprocess(paths)
    if not response_path.is_file():
        return proc.returncode, {}
    return proc.returncode, json.loads(response_path.read_text(encoding="utf-8"))


@pytest.mark.candidate_slow
def test_unmocked_bridge_session_lifecycle_fresh_subprocess(
    managed_review_set_evidence: CompletedEvidenceContext,
    shared_preview: Path,
    shared_clip_preview_a: Path,
    shared_clip_preview_b: Path,
    shared_review_set: Path,
    acceptance_clip_a_path: Path,
    acceptance_clip_b_path: Path,
    tmp_path: Path,
) -> None:
    preview, clip_a, clip_b, clip_a_path, clip_b_path, review_set = _copy_review_set_fixture(
        shared_preview,
        shared_clip_preview_a,
        shared_clip_preview_b,
        shared_review_set,
        acceptance_clip_a_path,
        acceptance_clip_b_path,
        tmp_path,
        "bridge-public-gate",
    )
    sources = _sources_ab(clip_a, clip_a_path, clip_b, clip_b_path)
    ctx = managed_review_set_evidence
    project_root = ctx.workspace.root
    session_path = tmp_path / "bridge-public-gate-sidecar" / "session.json"
    session_path.parent.mkdir(parents=True, exist_ok=True)
    exchange_dir = tmp_path / "bridge-public-gate-exchange"
    exchange_dir.mkdir(parents=True, exist_ok=True)
    context_doc = _context_document(
        project_root=project_root,
        workflow_id=ctx.workflow_id,
        preview=preview,
        review_set=review_set,
        sources=sources,
        session_path=session_path,
        exchange_dir=exchange_dir,
    )
    review_bytes_before = _review_set_tree_digest(review_set)
    shared_bytes_before = _review_set_tree_digest(shared_review_set)

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    assert code == 0
    assert doc["ok"] is True
    assert doc["stored"] is None
    assert doc["current"] is True

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "create"},
    )
    assert code == 0 and doc["ok"] is True and doc["committed"] is True
    create_sha = doc["stored"]["raw_sha256"]

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": create_sha,
            "operation": {"op": "SetNote", "clip_id": "arm_wave_01", "note": "review A"},
        },
    )
    assert code == 0 and doc["ok"] is True
    note_sha = doc["stored"]["raw_sha256"]

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": note_sha,
            "operation": {"op": "SetStatus", "clip_id": "arm_wave_01", "status": "revise"},
        },
    )
    assert code == 0 and doc["ok"] is True
    revise_sha = doc["stored"]["raw_sha256"]

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": revise_sha,
            "operation": {"op": "AddBookmark", "clip_id": "arm_wave_01", "timestamp": 0.75},
        },
    )
    assert code == 0 and doc["ok"] is True
    bookmark_sha = doc["stored"]["raw_sha256"]

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": bookmark_sha,
            "operation": {"op": "SetStatus", "clip_id": "arm_reverse_02", "status": "keep"},
        },
    )
    assert code == 0 and doc["ok"] is True
    assert doc["stored"]["session"]["revision"] == 4
    kept_sha = doc["stored"]["raw_sha256"]

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    assert code == 0 and doc["ok"] is True
    assert doc["stored"]["session"]["revision"] == 4
    assert doc["stored"]["raw_sha256"] == kept_sha
    assert doc["current"] is True

    candidate_db = project_root / CANDIDATE_STATE_DIR / CANDIDATE_DB_FILENAME
    owned_backup = tmp_path / "candidate-factory.db.owned-backup"
    session_bytes_before = session_path.read_bytes()
    candidate_db.rename(owned_backup)
    try:
        code, doc = _invoke_bridge(
            exchange_dir=exchange_dir,
            context_doc=context_doc,
            request_doc={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
        )
        assert code == 0 and doc["ok"] is True
        assert doc["current"] is False
        assert doc["stored"]["raw_sha256"] == kept_sha
        session_doc = doc["stored"]["session"]
        wave = _clip_record(session_doc, "arm_wave_01")
        reverse = _clip_record(session_doc, "arm_reverse_02")
        assert wave["note"] == "review A"
        assert wave["status"] == "revise"
        assert wave["bookmarks"] == [0.75]
        assert reverse["status"] == "keep"

        code, doc = _invoke_bridge(
            exchange_dir=exchange_dir,
            context_doc=context_doc,
            request_doc={
                "schema_version": BRIDGE_SCHEMA_VERSION,
                "action": "update",
                "expected_raw_sha256": kept_sha,
                "operation": {"op": "SetStatus", "clip_id": "arm_reverse_02", "status": "revise"},
            },
        )
        assert code != 0 and doc["ok"] is False
        assert doc["error"]["code"] == "DATABASE_UNAVAILABLE"
        assert session_path.read_bytes() == session_bytes_before
    finally:
        if owned_backup.is_file():
            owned_backup.rename(candidate_db)

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": create_sha,
            "operation": {"op": "SetStatus", "clip_id": "arm_reverse_02", "status": "revise"},
        },
    )
    assert doc["error"]["code"] == "SESSION_CONFLICT"
    assert doc["committed"] is False

    drift_clip_path = tmp_path / "bridge-public-gate-drift-b.json"
    drift_doc = build_authored_arm_reverse_02_clip_document()
    drift_doc["duration_seconds"] = 2.25
    drift_clip_path.write_bytes(
        json.dumps(drift_doc, sort_keys=True, separators=(",", ":"), allow_nan=False).encode(
            "utf-8"
        )
    )
    assert drift_clip_path.read_bytes() != authored_arm_reverse_02_clip_raw_bytes()
    clip_b_path.write_bytes(drift_clip_path.read_bytes())

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    assert code == 0 and doc["ok"] is True
    assert doc["current"] is False
    assert doc["stored"]["raw_sha256"] == kept_sha

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={
            "schema_version": BRIDGE_SCHEMA_VERSION,
            "action": "update",
            "expected_raw_sha256": kept_sha,
            "operation": {"op": "SetStatus", "clip_id": "arm_reverse_02", "status": "revise"},
        },
    )
    assert doc["error"]["code"] == "SESSION_STALE"
    assert doc["stored"] is None

    code, doc = _invoke_bridge(
        exchange_dir=exchange_dir,
        context_doc=context_doc,
        request_doc={"schema_version": BRIDGE_SCHEMA_VERSION, "action": "read"},
    )
    assert doc["stored"]["raw_sha256"] == kept_sha
    assert _review_set_tree_digest(review_set) == review_bytes_before
    assert _review_set_tree_digest(shared_review_set) == shared_bytes_before
