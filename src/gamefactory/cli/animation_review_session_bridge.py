"""One-shot file-exchange bridge for V0.8-9 animation review sessions (V0.8-9b).

Invoked by Godot or a launcher with isolated context/request/response JSON files.
Uses read-only candidate workflow state and public review-session workflow APIs only.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sqlite3
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.animation_review_session_store import (
    AnimationReviewSessionStore,
    AnimationReviewSessionStoreConflictError,
    StoredAnimationReviewSession,
)
from gamefactory.adapters.assets.v08_candidate_evidence import candidate_evidence_lexical_unsafe
from gamefactory.adapters.assets.v08_candidate_runtime_json import (
    CandidateRuntimeJsonError,
    parse_strict_runtime_json_object,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.persistence.read_only_database import ReadOnlyDatabase
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AssetRevisionRepository,
    ExecutionRepository,
    ProjectRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.cli.exit_codes import EXIT_CONFIG_ERROR, EXIT_SUCCESS, EXIT_WORKFLOW_FAILURE
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.animation_review_session import animation_review_session_to_document
from gamefactory.core.domain.errors import FactoryError, ValidationError
from gamefactory.workflows.animation_review_session import (
    AnimationReviewSessionWorkflowContext,
    animation_review_session_current,
    create_animation_review_session,
    update_animation_review_session,
)
from gamefactory.workflows.v08_candidate_animation_review_set import (
    MAX_REVIEW_SET_SOURCES,
    MIN_REVIEW_SET_SOURCES,
    CandidateAnimationReviewSetSource,
    animation_review_set_current,
)
from gamefactory.workflows.v08_candidate_currentness import CandidateCurrentnessError
from gamefactory.workflows.v08_candidate_snapshot import is_v08_candidate_graph
from gamefactory.workflows.v08_candidate_workflow import CandidateWorkflowHandlers
from gamefactory.workflows.v08_candidate_workspace import (
    CANDIDATE_DB_FILENAME,
    CANDIDATE_STATE_DIR,
    PRODUCTION_DB_FILENAME,
)

BRIDGE_SCHEMA_VERSION = "animation-review-session-bridge-0.8.0"
CONTEXT_SCHEMA_VERSION = "animation-review-session-context-0.8.0"

_MAX_INPUT_BYTES = 64 * 1024
_MAX_RESPONSE_BYTES = 72 * 1024

_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")

_CONTEXT_KEYS = frozenset(
    {
        "schema_version",
        "project_root",
        "workflow_id",
        "preview_dir",
        "review_dir",
        "clip_packages",
        "session_path",
        "exchange_dir",
    }
)
_CLIP_PACKAGE_KEYS = frozenset({"animation_dir", "clip_path"})
_REQUEST_BASE_KEYS = frozenset({"schema_version", "action"})
_REQUEST_UPDATE_KEYS = frozenset({"schema_version", "action", "expected_raw_sha256", "operation"})
_RESPONSE_KEYS = frozenset({"schema_version", "ok", "committed", "current", "stored", "error"})
_ERROR_KEYS = frozenset({"code", "message"})
_STORED_KEYS = frozenset({"session", "raw_sha256"})

_ACTION_READ = "read"
_ACTION_CREATE = "create"
_ACTION_UPDATE = "update"
_ALLOWED_ACTIONS = frozenset({_ACTION_READ, _ACTION_CREATE, _ACTION_UPDATE})

_MSG_REQUEST_INVALID = "The bridge request is invalid."
_MSG_CONTEXT_INVALID = "The bridge context is invalid."
_MSG_PATH_UNSAFE = "A bridge file path is not allowed."
_MSG_WORKFLOW_NOT_FOUND = "The candidate workflow was not found for this project."
_MSG_WORKFLOW_PROJECT_MISMATCH = "The candidate workflow does not belong to this project."
_MSG_DATABASE_UNAVAILABLE = "Candidate workflow state is not available for read-only access."
_MSG_SESSION_CONFLICT = "The review session changed since it was last read. Reload and try again."
_MSG_SESSION_STALE = "The animation review set is no longer current."
_MSG_SESSION_INVALID = "The saved review session file is invalid."
_MSG_SESSION_MISSING = "No review session exists yet."
_MSG_OPERATION_REJECTED = "The review session operation was rejected."
_MSG_RESPONSE_TOO_LARGE = "The bridge response exceeded the allowed size."
_MSG_INTERNAL = "The bridge could not complete the request."


@dataclass(frozen=True)
class BridgeContext:
    project_root: Path
    workflow_id: str
    preview_dir: Path
    review_dir: Path
    clip_packages: tuple[CandidateAnimationReviewSetSource, ...]
    session_path: Path | None
    exchange_dir: Path


@dataclass(frozen=True)
class BridgePaths:
    context_file: Path
    request_file: Path
    response_file: Path


def _same_absolute_path(left: Path, right: Path) -> bool:
    if sys.platform == "win32":
        return os.path.normcase(str(left)) == os.path.normcase(str(right))
    return left == right


def _resolved_path_inside_root(path_resolved: Path, root_resolved: Path) -> bool:
    if sys.platform == "win32":
        path_s = os.path.normcase(str(path_resolved))
        root_s = os.path.normcase(str(root_resolved))
        if path_s == root_s:
            return True
        return path_s.startswith(root_s + os.sep)
    try:
        path_resolved.relative_to(root_resolved)
        return True
    except ValueError:
        return False


def _not_regular_file_message(label: str) -> str:
    return f"{label} must be a regular file, not a link, junction, or reparse point"


def _assert_stat_regular_single_link(info: os.stat_result, *, label: str) -> None:
    if stat.S_ISLNK(info.st_mode):
        raise ValidationError(_not_regular_file_message(label))
    reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
    if reparse:
        raise ValidationError(_not_regular_file_message(label))
    if stat.S_ISDIR(info.st_mode):
        raise ValidationError(_not_regular_file_message(label))
    if not stat.S_ISREG(info.st_mode):
        raise ValidationError(_not_regular_file_message(label))
    if info.st_nlink > 1:
        raise ValidationError(f"{label} must not be a hard link")


def _assert_regular_single_link_file(path: Path, *, label: str) -> os.stat_result:
    if path_crosses_link(path):
        raise ValidationError(f"{label} crosses a symlink or junction")
    if candidate_evidence_lexical_unsafe(path):
        raise ValidationError(f"{label} is not a bounded lexical path")
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise ValidationError(f"{label} does not exist") from None
    except OSError as exc:
        raise ValidationError(f"{label} cannot be inspected: {exc}") from exc
    _assert_stat_regular_single_link(info, label=label)
    return info


def _read_bounded_regular_file(path: Path, *, label: str, max_bytes: int) -> bytes:
    entry = _assert_regular_single_link_file(path, label=label)
    size = entry.st_size
    if size > max_bytes:
        raise ValidationError(f"{label} exceeds {max_bytes} bytes")
    with path.open("rb") as handle:
        fd = handle.fileno()
        opened = os.fstat(fd)
        _assert_stat_regular_single_link(opened, label=label)
        if opened.st_nlink > 1:
            raise ValidationError(f"{label} must not be a hard link")
        entry_now = path.lstat()
        if (opened.st_dev, opened.st_ino) != (entry_now.st_dev, entry_now.st_ino):
            raise ValidationError(f"{label} identity changed during read")
        if opened.st_size != size:
            raise ValidationError(f"{label} size changed during read")
        payload = handle.read(max_bytes + 1)
        if len(payload) > max_bytes:
            raise ValidationError(f"{label} exceeds {max_bytes} bytes")
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError(f"{label} identity changed during read")
        if after.st_size != opened.st_size:
            raise ValidationError(f"{label} grew during bounded read")
        if len(payload) != after.st_size:
            raise ValidationError(f"{label} grew during bounded read")
        leaf_after = path.lstat()
        if (leaf_after.st_dev, leaf_after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValidationError(f"{label} identity changed during read")
    return payload


def _reject_unsafe_lexical_path(path: Path, *, label: str) -> Path:
    if not path.is_absolute():
        raise ValidationError(f"{label} must be an absolute path")
    lexical = path
    if candidate_evidence_lexical_unsafe(lexical):
        raise ValidationError(f"{label} is not a bounded lexical path")
    if path_crosses_link(lexical):
        raise ValidationError(f"{label} crosses a symlink or junction")
    try:
        resolved = lexical.resolve(strict=False)
    except OSError as exc:
        raise ValidationError(f"{label} cannot be resolved: {exc}") from exc
    if candidate_evidence_lexical_unsafe(resolved) or path_crosses_link(resolved):
        raise ValidationError(f"{label} resolves through a link or reparse point")
    return resolved


def _require_exact_keys(data: dict[str, Any], expected: frozenset[str], *, label: str) -> None:
    keys = set(data.keys())
    if keys != expected:
        raise ValidationError(f"{label} fields do not match the contract")


def _require_nonempty_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty string")
    return value


def _require_absolute_path_field(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{field} must be a non-empty absolute path string")
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError(f"{field} must be an absolute path")
    return _reject_unsafe_lexical_path(path, label=field)


def _parse_context_document(document: dict[str, Any]) -> BridgeContext:
    _require_exact_keys(document, _CONTEXT_KEYS, label="bridge context")
    if document.get("schema_version") != CONTEXT_SCHEMA_VERSION:
        raise ValidationError("bridge context schema_version mismatch")
    project_root = _require_absolute_path_field(document.get("project_root"), "project_root")
    if not project_root.is_dir():
        raise ValidationError("project_root is not a directory")
    workflow_id = _require_nonempty_str(document.get("workflow_id"), "workflow_id")
    preview_dir = _require_absolute_path_field(document.get("preview_dir"), "preview_dir")
    review_dir = _require_absolute_path_field(document.get("review_dir"), "review_dir")
    exchange_dir = _require_absolute_path_field(document.get("exchange_dir"), "exchange_dir")
    if not exchange_dir.is_dir():
        raise ValidationError("exchange_dir is not a directory")
    session_raw = document.get("session_path")
    session_path: Path | None
    if session_raw is None:
        session_path = None
    else:
        session_path = _require_absolute_path_field(session_raw, "session_path")
    packages_raw = document.get("clip_packages")
    if not isinstance(packages_raw, list):
        raise ValidationError("clip_packages must be a list")
    count = len(packages_raw)
    if count < MIN_REVIEW_SET_SOURCES or count > MAX_REVIEW_SET_SOURCES:
        raise ValidationError("clip_packages length is out of range")
    clip_packages: list[CandidateAnimationReviewSetSource] = []
    for index, entry in enumerate(packages_raw):
        if not isinstance(entry, dict):
            raise ValidationError(f"clip_packages[{index}] must be an object")
        _require_exact_keys(entry, _CLIP_PACKAGE_KEYS, label=f"clip_packages[{index}]")
        animation_dir = _require_absolute_path_field(
            entry.get("animation_dir"),
            f"clip_packages[{index}].animation_dir",
        )
        clip_path = _require_absolute_path_field(
            entry.get("clip_path"),
            f"clip_packages[{index}].clip_path",
        )
        clip_packages.append(CandidateAnimationReviewSetSource(animation_dir, clip_path))
    ctx = BridgeContext(
        project_root=project_root,
        workflow_id=workflow_id,
        preview_dir=preview_dir,
        review_dir=review_dir,
        clip_packages=tuple(clip_packages),
        session_path=session_path,
        exchange_dir=exchange_dir,
    )
    _assert_exchange_paths_safe(ctx)
    return ctx


def _canonical_review_roots(ctx: BridgeContext) -> list[Path]:
    roots: list[Path] = [ctx.review_dir, ctx.preview_dir]
    for package in ctx.clip_packages:
        roots.append(Path(package.animation_dir).absolute())
        roots.append(Path(package.clip_path).absolute())
    return roots


def _mutation_forbidden_roots(ctx: BridgeContext) -> list[Path]:
    forbidden = list(_canonical_review_roots(ctx))
    store = AnimationReviewSessionStore(
        review_root=ctx.review_dir,
        session_path=ctx.session_path,
    )
    session_file = store.session_path
    sidecar_dir = session_file.parent
    lock_file = sidecar_dir / "session.lock"
    forbidden.extend((session_file, sidecar_dir, lock_file))
    if ctx.session_path is not None:
        forbidden.append(ctx.session_path)
    return forbidden


def _assert_path_outside_roots(path: Path, roots: list[Path], *, label: str) -> None:
    resolved = _reject_unsafe_lexical_path(path, label=label)
    for root in roots:
        root_resolved = _reject_unsafe_lexical_path(root, label="forbidden root")
        if _resolved_path_inside_root(resolved, root_resolved):
            raise ValidationError(f"{label} must stay outside canonical review and source paths")
        if _same_absolute_path(resolved, root_resolved):
            raise ValidationError(f"{label} must stay outside canonical review and source paths")


def _assert_exchange_paths_safe(ctx: BridgeContext) -> None:
    canonical = _canonical_review_roots(ctx)
    mutation_forbidden = _mutation_forbidden_roots(ctx)
    _assert_path_outside_roots(ctx.exchange_dir, mutation_forbidden, label="exchange_dir")
    store = AnimationReviewSessionStore(
        review_root=ctx.review_dir,
        session_path=ctx.session_path,
    )
    effective = store.session_path
    session_label = "session_path" if ctx.session_path is not None else "default session_path"
    _assert_path_outside_roots(effective, canonical, label=session_label)


def _validate_exchange_leaf_path(
    path: Path,
    *,
    exchange_dir: Path,
    label: str,
    must_exist: bool,
    must_be_new: bool,
    peer_paths: list[Path],
    forbidden_roots: list[Path],
) -> Path:
    resolved = _reject_unsafe_lexical_path(path, label=label)
    exchange_resolved = _reject_unsafe_lexical_path(exchange_dir, label="exchange_dir")
    if not _same_absolute_path(resolved.parent, exchange_resolved):
        raise ValidationError(f"{label} must be a direct child of exchange_dir")
    if resolved.name.startswith("."):
        raise ValidationError(f"{label} must not be a hidden leaf name")
    for peer in peer_paths:
        if _same_absolute_path(resolved, _reject_unsafe_lexical_path(peer, label="peer path")):
            raise ValidationError(f"{label} must be distinct from other bridge paths")
    _assert_path_outside_roots(resolved, forbidden_roots, label=label)
    if must_exist:
        _assert_regular_single_link_file(resolved, label=label)
    elif must_be_new and resolved.exists():
        raise ValidationError(f"{label} must not already exist")
    return resolved


def _parse_request_document(document: dict[str, Any], action: str) -> dict[str, Any]:
    if action == _ACTION_UPDATE:
        _require_exact_keys(document, _REQUEST_UPDATE_KEYS, label="bridge request")
        expected = document.get("expected_raw_sha256")
        if not isinstance(expected, str) or not _SHA256_HEX_RE.fullmatch(expected):
            raise ValidationError("expected_raw_sha256 must be a 64-character lowercase hex digest")
        operation = document.get("operation")
        if not isinstance(operation, dict):
            raise ValidationError("operation must be a JSON object")
        return {
            "expected_raw_sha256": expected,
            "operation": operation,
        }
    _require_exact_keys(document, _REQUEST_BASE_KEYS, label="bridge request")
    if action not in {_ACTION_READ, _ACTION_CREATE}:
        raise ValidationError("unsupported bridge action")
    return {}


def _load_json_object(path: Path, *, label: str) -> dict[str, Any]:
    raw = _read_bounded_regular_file(path, label=label, max_bytes=_MAX_INPUT_BYTES)
    try:
        return parse_strict_runtime_json_object(raw, max_bytes=_MAX_INPUT_BYTES)
    except (CandidateRuntimeJsonError, ValueError, RecursionError) as exc:
        raise ValidationError(str(exc)) from exc


def _stored_payload(stored: StoredAnimationReviewSession) -> dict[str, Any]:
    return {
        "session": animation_review_session_to_document(stored.session),
        "raw_sha256": stored.raw_sha256,
    }


def _response_document(
    *,
    ok: bool,
    committed: bool,
    current: bool,
    stored: dict[str, Any] | None,
    error: dict[str, str] | None,
) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "schema_version": BRIDGE_SCHEMA_VERSION,
        "ok": ok,
        "committed": committed,
        "current": current,
        "stored": stored,
        "error": error,
    }
    _require_exact_keys(doc, _RESPONSE_KEYS, label="bridge response")
    if stored is not None:
        _require_exact_keys(stored, _STORED_KEYS, label="stored")
    if error is not None:
        _require_exact_keys(error, _ERROR_KEYS, label="error")
    return doc


def _encode_response(document: dict[str, Any]) -> bytes:
    try:
        payload = json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
            ensure_ascii=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise ValidationError("bridge response is not JSON-encodable") from exc
    if len(payload) > _MAX_RESPONSE_BYTES:
        raise ValidationError(_MSG_RESPONSE_TOO_LARGE)
    return payload


def _stage_response_payload(parent: Path, leaf_name: str, payload: bytes) -> Path:
    token = secrets.token_hex(8)
    temp_path = parent / f".{leaf_name}.{os.getpid()}.{token}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temp_path, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    return temp_path


def _unlink_owned_temp(temp_path: Path) -> None:
    if temp_path.exists():
        try:
            temp_path.unlink()
        except OSError:
            pass


def _commit_new_response_file(target: Path, staged: Path) -> None:
    owned_temp: Path | None = staged
    try:
        if target.exists():
            raise ValidationError("response file already exists")
        os.rename(staged, target)
        owned_temp = None
    finally:
        if owned_temp is not None:
            _unlink_owned_temp(owned_temp)


def _unlink_owned_claim(claim_path: Path) -> None:
    if claim_path.exists():
        try:
            claim_path.unlink()
        except OSError:
            pass


def _acquire_response_publication_claim(parent: Path, leaf_name: str) -> Path | None:
    claim_path = parent / f".{leaf_name}.publish.claim"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(claim_path, flags, 0o600)
    except FileExistsError:
        return None
    except OSError:
        return None
    try:
        os.close(descriptor)
    except OSError:
        _unlink_owned_claim(claim_path)
        return None
    return claim_path


def _write_response_if_safe(response_path: Path, document: dict[str, Any]) -> bool:
    owned_claim: Path | None = None
    staged: Path | None = None
    try:
        resolved = _reject_unsafe_lexical_path(response_path, label="response file")
        if resolved.exists():
            return False
        payload = _encode_response(document)
        parent = resolved.parent
        _reject_unsafe_lexical_path(parent, label="response parent")
        parent.mkdir(parents=True, exist_ok=True)
        _reject_unsafe_lexical_path(resolved, label="response file")
        _reject_unsafe_lexical_path(parent, label="response parent")
        if resolved.exists():
            return False
        acquired = _acquire_response_publication_claim(parent, resolved.name)
        if acquired is None:
            return False
        owned_claim = acquired
        if resolved.exists():
            return False
        staged = _stage_response_payload(parent, resolved.name, payload)
        _reject_unsafe_lexical_path(resolved, label="response file")
        if resolved.exists():
            return False
        _commit_new_response_file(resolved, staged)
        staged = None
    except (ValidationError, OSError):
        if staged is not None:
            _unlink_owned_temp(staged)
        return False
    finally:
        if owned_claim is not None:
            _unlink_owned_claim(owned_claim)
    return True


def _assert_candidate_database_path(project_root: Path) -> Path:
    state_dir = project_root / CANDIDATE_STATE_DIR
    db_path = state_dir / CANDIDATE_DB_FILENAME
    if db_path.name.casefold() == PRODUCTION_DB_FILENAME.casefold():
        raise ValidationError("production factory.db is not allowed")
    if db_path.name.casefold() != CANDIDATE_DB_FILENAME.casefold():
        raise ValidationError("candidate database file name is invalid")
    _assert_regular_single_link_file(db_path, label="candidate database")
    expected_parent = (project_root / CANDIDATE_STATE_DIR).resolve(strict=False)
    if db_path.parent.resolve(strict=False) != expected_parent:
        raise ValidationError("candidate database must live under project state directory")
    return db_path


def build_readonly_candidate_handlers(project_root: Path) -> CandidateWorkflowHandlers:
    """Reconstruct workflow handlers from read-only candidate-factory.db (no workspace registry)."""
    root = _reject_unsafe_lexical_path(project_root, label="project_root")
    if not root.is_dir():
        raise ValidationError("project_root is not a directory")
    db_path = _assert_candidate_database_path(root)
    db = ReadOnlyDatabase(db_path)
    return CandidateWorkflowHandlers(
        root,
        ArtifactRepository(db),
        AssetRevisionRepository(db),
        ApprovalRepository(db),
        ExecutionRepository(db),
        TaskRepository(db),
        ArtifactManager(root),
        godot_path=None,
        runner=None,
    )


def _assert_workflow_belongs_to_project(
    handlers: CandidateWorkflowHandlers,
    workflow_id: str,
    project_root: Path,
) -> None:
    db = handlers.artifacts.db
    workflow = WorkflowRepository(db).get(workflow_id)
    if workflow is None:
        raise ValidationError("workflow not found")
    project = ProjectRepository(db).get(workflow.project_id)
    if project is None:
        raise ValidationError("workflow project is missing")
    recorded_root = Path(project.root_path).resolve(strict=False)
    if recorded_root != project_root.resolve(strict=False):
        raise ValidationError("workflow project root mismatch")
    tasks = handlers.tasks.list_by_workflow(workflow_id)
    if not any(is_v08_candidate_graph(task) for task in tasks):
        raise ValidationError("workflow is not a V0.8 candidate graph")


def _workflow_context(
    bridge: BridgeContext,
    handlers: CandidateWorkflowHandlers,
) -> AnimationReviewSessionWorkflowContext:
    return AnimationReviewSessionWorkflowContext(
        handlers=handlers,
        workflow_id=bridge.workflow_id,
        preview_dir=bridge.preview_dir,
        clip_packages=bridge.clip_packages,
        review_dir=bridge.review_dir,
        session_path=bridge.session_path,
    )


def _session_store(bridge: BridgeContext) -> AnimationReviewSessionStore:
    if bridge.session_path is None:
        return AnimationReviewSessionStore(review_root=bridge.review_dir)
    return AnimationReviewSessionStore(
        review_root=bridge.review_dir,
        session_path=bridge.session_path,
    )


def _review_set_gate_current(
    bridge: BridgeContext,
    handlers: CandidateWorkflowHandlers,
) -> bool:
    return animation_review_set_current(
        handlers,
        bridge.workflow_id,
        bridge.preview_dir,
        bridge.clip_packages,
        bridge.review_dir,
    )


def _read_authority_failure(exc: Exception) -> bool:
    return isinstance(
        exc, (FactoryError, OSError, sqlite3.Error, ValidationError, FileNotFoundError)
    )


def _try_readonly_bootstrap(
    bridge: BridgeContext,
) -> tuple[CandidateWorkflowHandlers | None, Exception | None]:
    try:
        handlers = build_readonly_candidate_handlers(bridge.project_root)
        _assert_workflow_belongs_to_project(handlers, bridge.workflow_id, bridge.project_root)
        return handlers, None
    except (FactoryError, OSError, sqlite3.Error, ValidationError, FileNotFoundError) as exc:
        return None, exc


def _read_current_from_authority(
    bridge: BridgeContext,
    handlers: CandidateWorkflowHandlers,
    *,
    stored: StoredAnimationReviewSession | None,
) -> bool | None:
    try:
        if stored is None:
            return _review_set_gate_current(bridge, handlers)
        ctx = _workflow_context(bridge, handlers)
        return animation_review_session_current(ctx)
    except (FactoryError, OSError, sqlite3.Error):
        return None


def _historical_read_response(
    *,
    stored: StoredAnimationReviewSession | None,
) -> dict[str, Any]:
    payload = _stored_payload(stored) if stored is not None else None
    return _response_document(
        ok=True,
        committed=False,
        current=False,
        stored=payload,
        error=None,
    )


def _execute_read(bridge: BridgeContext) -> dict[str, Any]:
    store = _session_store(bridge)
    session_file = store.session_path
    if session_file.exists() and not session_file.is_file():
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_INVALID", "message": _MSG_SESSION_INVALID},
        )

    stored: StoredAnimationReviewSession | None = None
    if session_file.is_file():
        try:
            stored = store.load()
        except ValidationError:
            return _response_document(
                ok=False,
                committed=False,
                current=False,
                stored=None,
                error={"code": "SESSION_INVALID", "message": _MSG_SESSION_INVALID},
            )

    handlers, bootstrap_exc = _try_readonly_bootstrap(bridge)
    if handlers is None:
        if bootstrap_exc is not None and _read_authority_failure(bootstrap_exc):
            return _historical_read_response(stored=stored)
        if bootstrap_exc is not None:
            return _map_bootstrap_error(bootstrap_exc)
        return _map_bootstrap_error(ValidationError("candidate workflow bootstrap failed"))

    current = _read_current_from_authority(bridge, handlers, stored=stored)
    if current is None:
        return _historical_read_response(stored=stored)

    stored_payload = _stored_payload(stored) if stored is not None else None
    return _response_document(
        ok=True,
        committed=False,
        current=current,
        stored=stored_payload,
        error=None,
    )


def _handle_create(
    bridge: BridgeContext,
    handlers: CandidateWorkflowHandlers,
) -> dict[str, Any]:
    ctx = _workflow_context(bridge, handlers)
    try:
        result = create_animation_review_session(ctx)
    except AnimationReviewSessionStoreConflictError:
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_CONFLICT", "message": _MSG_SESSION_CONFLICT},
        )
    except CandidateCurrentnessError:
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_STALE", "message": _MSG_SESSION_STALE},
        )
    except ValidationError:
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "OPERATION_REJECTED", "message": _MSG_OPERATION_REJECTED},
        )
    return _response_document(
        ok=True,
        committed=True,
        current=result.current,
        stored=_stored_payload(result.stored),
        error=None,
    )


def _handle_update(
    bridge: BridgeContext,
    handlers: CandidateWorkflowHandlers,
    *,
    expected_raw_sha256: str,
    operation: dict[str, Any],
) -> dict[str, Any]:
    ctx = _workflow_context(bridge, handlers)
    store = _session_store(bridge)
    if not store.session_path.is_file():
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_MISSING", "message": _MSG_SESSION_MISSING},
        )
    try:
        result = update_animation_review_session(
            ctx,
            expected_raw_sha256=expected_raw_sha256,
            operation=operation,
        )
    except AnimationReviewSessionStoreConflictError:
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_CONFLICT", "message": _MSG_SESSION_CONFLICT},
        )
    except CandidateCurrentnessError:
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_STALE", "message": _MSG_SESSION_STALE},
        )
    except ValidationError:
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "OPERATION_REJECTED", "message": _MSG_OPERATION_REJECTED},
        )
    return _response_document(
        ok=True,
        committed=True,
        current=result.current,
        stored=_stored_payload(result.stored),
        error=None,
    )


def _map_workflow_transport_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, AnimationReviewSessionStoreConflictError):
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_CONFLICT", "message": _MSG_SESSION_CONFLICT},
        )
    if isinstance(exc, CandidateCurrentnessError):
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "SESSION_STALE", "message": _MSG_SESSION_STALE},
        )
    if isinstance(exc, ValidationError):
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "OPERATION_REJECTED", "message": _MSG_OPERATION_REJECTED},
        )
    if isinstance(exc, (FactoryError, OSError, sqlite3.Error)):
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "INTERNAL_ERROR", "message": _MSG_INTERNAL},
        )
    raise exc


def _map_bootstrap_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, FileNotFoundError):
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "DATABASE_UNAVAILABLE", "message": _MSG_DATABASE_UNAVAILABLE},
        )
    if isinstance(exc, ValidationError):
        message = exc.message
        if "workflow not found" in message:
            code = "WORKFLOW_NOT_FOUND"
            ui = _MSG_WORKFLOW_NOT_FOUND
        elif "project root mismatch" in message or "workflow is not" in message:
            code = "WORKFLOW_PROJECT_MISMATCH"
            ui = _MSG_WORKFLOW_PROJECT_MISMATCH
        else:
            code = "DATABASE_UNAVAILABLE"
            ui = _MSG_DATABASE_UNAVAILABLE
        return _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": code, "message": ui},
        )
    return _response_document(
        ok=False,
        committed=False,
        current=False,
        stored=None,
        error={"code": "INTERNAL_ERROR", "message": _MSG_INTERNAL},
    )


def _require_cli_absolute_path(value: str, *, label: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValidationError(f"{label} must be an absolute path")
    return path


def run_bridge(paths: BridgePaths) -> tuple[int, dict[str, Any] | None]:
    """Execute one bridge exchange; returns (exit_code, response document if encoded)."""
    try:
        context_path = _require_cli_absolute_path(
            str(paths.context_file),
            label="context file",
        )
        context_path = _reject_unsafe_lexical_path(context_path, label="context file")
        _assert_regular_single_link_file(context_path, label="context file")
    except ValidationError:
        return EXIT_CONFIG_ERROR, None

    try:
        context_doc = _load_json_object(context_path, label="context file")
        bridge = _parse_context_document(context_doc)
    except ValidationError:
        return EXIT_CONFIG_ERROR, None

    forbidden = _mutation_forbidden_roots(bridge)
    peer_extra = [paths.context_file]
    if bridge.session_path is not None:
        peer_extra.append(bridge.session_path)
    try:
        _assert_path_outside_roots(context_path, forbidden, label="context file")
        request_path = _validate_exchange_leaf_path(
            paths.request_file,
            exchange_dir=bridge.exchange_dir,
            label="request file",
            must_exist=True,
            must_be_new=False,
            peer_paths=[paths.response_file, *peer_extra],
            forbidden_roots=forbidden,
        )
        response_path = _validate_exchange_leaf_path(
            paths.response_file,
            exchange_dir=bridge.exchange_dir,
            label="response file",
            must_exist=False,
            must_be_new=True,
            peer_paths=[paths.request_file, *peer_extra],
            forbidden_roots=forbidden,
        )
    except ValidationError:
        return EXIT_CONFIG_ERROR, None

    try:
        request_doc = _load_json_object(request_path, label="request file")
        if request_doc.get("schema_version") != BRIDGE_SCHEMA_VERSION:
            raise ValidationError("bridge request schema_version mismatch")
        action = request_doc.get("action")
        if not isinstance(action, str) or action not in _ALLOWED_ACTIONS:
            raise ValidationError("unsupported bridge action")
        update_fields = _parse_request_document(request_doc, action)
    except ValidationError:
        doc = _response_document(
            ok=False,
            committed=False,
            current=False,
            stored=None,
            error={"code": "REQUEST_INVALID", "message": _MSG_REQUEST_INVALID},
        )
        if _write_response_if_safe(response_path, doc):
            return EXIT_WORKFLOW_FAILURE, doc
        return EXIT_CONFIG_ERROR, None

    if action == _ACTION_READ:
        try:
            doc = _execute_read(bridge)
        except (
            AnimationReviewSessionStoreConflictError,
            CandidateCurrentnessError,
            ValidationError,
            FactoryError,
            OSError,
            sqlite3.Error,
        ) as exc:
            doc = _map_workflow_transport_error(exc)
        exit_code = EXIT_SUCCESS if doc["ok"] else EXIT_WORKFLOW_FAILURE
        if not _write_response_if_safe(response_path, doc):
            return EXIT_CONFIG_ERROR, None
        return exit_code, doc

    try:
        handlers = build_readonly_candidate_handlers(bridge.project_root)
        _assert_workflow_belongs_to_project(handlers, bridge.workflow_id, bridge.project_root)
    except (FactoryError, OSError, sqlite3.Error, ValidationError, FileNotFoundError) as exc:
        doc = _map_bootstrap_error(exc)
        if _write_response_if_safe(response_path, doc):
            return EXIT_WORKFLOW_FAILURE, doc
        return EXIT_CONFIG_ERROR, None

    try:
        if action == _ACTION_CREATE:
            doc = _handle_create(bridge, handlers)
        else:
            doc = _handle_update(
                bridge,
                handlers,
                expected_raw_sha256=update_fields["expected_raw_sha256"],
                operation=update_fields["operation"],
            )
    except (
        AnimationReviewSessionStoreConflictError,
        CandidateCurrentnessError,
        ValidationError,
        FactoryError,
        OSError,
        sqlite3.Error,
    ) as exc:
        doc = _map_workflow_transport_error(exc)

    exit_code = EXIT_SUCCESS if doc["ok"] else EXIT_WORKFLOW_FAILURE
    if not _write_response_if_safe(response_path, doc):
        return EXIT_CONFIG_ERROR, None
    return exit_code, doc


def _parse_cli_paths(args: argparse.Namespace) -> BridgePaths:
    return BridgePaths(
        context_file=_require_cli_absolute_path(args.context_file, label="context file"),
        request_file=_require_cli_absolute_path(args.request_file, label="request file"),
        response_file=_require_cli_absolute_path(args.response_file, label="response file"),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="V0.8-9 animation review session file bridge")
    parser.add_argument("--context-file", required=True)
    parser.add_argument("--request-file", required=True)
    parser.add_argument("--response-file", required=True)
    ns = parser.parse_args(argv)
    try:
        cli_paths = _parse_cli_paths(ns)
    except ValidationError:
        return EXIT_CONFIG_ERROR
    code, _document = run_bridge(cli_paths)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
