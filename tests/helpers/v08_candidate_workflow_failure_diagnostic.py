"""Bounded CI diagnostics for V0.8 candidate workflow runtime log artifacts."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.v08_candidate_evidence import candidate_evidence_lexical_unsafe
from gamefactory.adapters.assets.v08_candidate_runtime_paths import path_crosses_link
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    TaskRepository,
)
from gamefactory.core.domain.models import Artifact, Execution, TaskStatus, WorkflowStatus
from gamefactory.workflows.engine import WorkflowExecutionResult
from gamefactory.workflows.v08_candidate_currentness import artifact_bound_to_execution
from gamefactory.workflows.v08_candidate_gates import MAX_CANDIDATE_JSON_ARTIFACT_BYTES

RUNTIME_LOG_ARTIFACT_TYPES = (
    "candidate-runtime-import-log",
    "candidate-runtime-render-log",
)

# Hard cap on runtime-log diagnostic entries reported per workflow failure.
MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES = 8

# Cold verifier patterns (scripts/verify_candidate_bundle.py); diagnostic-only mirror.
# More specific identities are listed first so line attribution stays stable.
_COLD_ENGINE_ERROR_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("SCRIPT ERROR", re.compile(r"SCRIPT ERROR", re.IGNORECASE)),
    ("Parse Error", re.compile(r"Parse Error", re.IGNORECASE)),
    ("ERROR:", re.compile(r"ERROR:", re.IGNORECASE)),
)

_DEFAULT_MAX_EXCERPT_LINES = 8
_DEFAULT_MAX_EXCERPT_CHARS = 300
_DEFAULT_CONTEXT_LINES = 1


def cold_engine_error_line(line: str) -> bool:
    return cold_engine_error_rule_identity(line) is not None


def cold_engine_error_rule_identity(line: str) -> str | None:
    for identity, pattern in _COLD_ENGINE_ERROR_PATTERNS:
        if pattern.search(line):
            return identity
    return None


def extract_bounded_engine_error_bounded_matches(
    text: str,
    *,
    max_lines: int = _DEFAULT_MAX_EXCERPT_LINES,
    max_chars: int = _DEFAULT_MAX_EXCERPT_CHARS,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
) -> list[dict[str, Any]]:
    """Return bounded excerpt rows with 1-based line numbers and cold rule identities."""
    if max_lines <= 0 or max_chars <= 0:
        return []
    lines = text.splitlines()
    selected_indices: set[int] = set()
    for index, line in enumerate(lines):
        if not cold_engine_error_line(line):
            continue
        start = max(0, index - context_lines)
        end = min(len(lines), index + context_lines + 1)
        for ctx_index in range(start, end):
            selected_indices.add(ctx_index)
    ordered = sorted(selected_indices)
    matches: list[dict[str, Any]] = []
    total_chars = 0
    for index in ordered:
        if len(matches) >= max_lines:
            break
        sanitized = _sanitize_excerpt_line(lines[index])
        if not sanitized:
            continue
        next_total = total_chars + len(sanitized) + (1 if matches else 0)
        if next_total > max_chars:
            remaining = max_chars - total_chars - (1 if matches else 0)
            if remaining <= 0:
                break
            if remaining > 3:
                sanitized = sanitized[: remaining - 3] + "..."
            else:
                sanitized = sanitized[:remaining]
        row: dict[str, Any] = {
            "line_number": index + 1,
            "text": sanitized,
        }
        rule_identity = cold_engine_error_rule_identity(lines[index])
        if rule_identity is not None:
            row["rule_identity"] = rule_identity
        matches.append(row)
        total_chars = sum(len(item["text"]) for item in matches) + max(0, len(matches) - 1)
    return matches


def extract_bounded_engine_error_excerpts(
    text: str,
    *,
    max_lines: int = _DEFAULT_MAX_EXCERPT_LINES,
    max_chars: int = _DEFAULT_MAX_EXCERPT_CHARS,
    context_lines: int = _DEFAULT_CONTEXT_LINES,
) -> list[str]:
    """Return sanitized excerpt lines around cold engine-error pattern hits."""
    return [
        row["text"]
        for row in extract_bounded_engine_error_bounded_matches(
            text,
            max_lines=max_lines,
            max_chars=max_chars,
            context_lines=context_lines,
        )
    ]


def _sanitize_excerpt_line(line: str, *, per_line_limit: int = 200) -> str:
    cleaned = "".join(ch if ch >= " " or ch == "\t" else " " for ch in line)
    cleaned = cleaned.strip()
    if len(cleaned) > per_line_limit:
        return cleaned[: per_line_limit - 3] + "..."
    return cleaned


def _binary_read_flags() -> int:
    return os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _lexical_relative_unsafe(relative_path: str) -> bool:
    rel_norm = relative_path.replace("\\", "/")
    parts = Path(rel_norm).parts
    if not parts:
        return True
    return any(part in ("..", "") for part in parts)


def resolve_bounded_runtime_log_path(
    project_root: Path,
    relative_path: str,
) -> tuple[Path | None, str | None]:
    """Resolve a registered artifact path under project_root without following links."""
    if _lexical_relative_unsafe(relative_path):
        return None, "relative_path_not_lexically_safe"
    rel_norm = relative_path.replace("\\", "/")
    lexical = project_root / rel_norm
    if candidate_evidence_lexical_unsafe(lexical):
        return None, "lexical_path_unsafe"
    try:
        if path_crosses_link(lexical):
            return None, "path_crosses_link"
    except OSError as exc:
        return None, f"path_crosses_link_failed:{type(exc).__name__}"
    try:
        resolved = lexical.resolve(strict=True)
        root_resolved = project_root.resolve(strict=True)
        resolved.relative_to(root_resolved)
    except (OSError, ValueError):
        return None, "path_not_under_project_root"
    return resolved, None


def bounded_read_runtime_log_bytes(
    project_root: Path,
    relative_path: str,
    *,
    max_bytes: int = MAX_CANDIDATE_JSON_ARTIFACT_BYTES,
) -> tuple[bytes | None, str | None]:
    target, resolve_reason = resolve_bounded_runtime_log_path(project_root, relative_path)
    if target is None:
        return None, resolve_reason
    try:
        if not target.is_file():
            return None, "not_a_regular_file"
        size = target.stat().st_size
    except OSError as exc:
        return None, f"stat_failed:{type(exc).__name__}"
    if size <= 0:
        return None, "empty_file"
    if size > max_bytes:
        return None, "exceeds_bounded_byte_limit"
    try:
        descriptor = os.open(target, _binary_read_flags())
    except OSError as exc:
        return None, f"open_failed:{type(exc).__name__}"
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return None, "not_a_regular_file"
        chunks = bytearray()
        while len(chunks) <= max_bytes:
            part = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        if len(chunks) > max_bytes:
            return None, "exceeds_bounded_stream_limit"
        if len(chunks) != size:
            return None, "size_changed_during_read"
        return bytes(chunks), None
    except OSError as exc:
        return None, f"read_failed:{type(exc).__name__}"
    finally:
        os.close(descriptor)


def _producing_execution_for_artifact(
    artifact: Artifact,
    executions_by_task: dict[str, list[Execution]],
) -> tuple[dict[str, Any] | None, str | None]:
    attempts = executions_by_task.get(artifact.task_id, [])
    matches = [
        execution for execution in attempts if artifact_bound_to_execution(artifact, execution)
    ]
    if len(matches) == 1:
        execution = matches[0]
        return (
            {
                "execution_id": execution.id,
                "attempt_number": execution.attempt_number,
                "task_id": execution.task_id,
            },
            None,
        )
    if not matches:
        return None, "no_producing_execution_match"
    return None, f"ambiguous_producing_execution:{len(matches)}"


def diagnose_registered_runtime_log_artifact(
    project_root: Path,
    artifact: Artifact,
    executions_by_task: dict[str, list[Execution]],
) -> dict[str, Any]:
    rel_norm = artifact.relative_path.replace("\\", "/")
    producing, producing_reason = _producing_execution_for_artifact(artifact, executions_by_task)
    entry: dict[str, Any] = {
        "artifact_id": artifact.id,
        "artifact_type": artifact.artifact_type,
        "task_id": artifact.task_id,
        "relative_path": rel_norm,
        "registered_content_hash": artifact.content_hash,
        "producing_execution": producing,
        "producing_execution_diagnostic": producing_reason,
        "observed_content_hash": None,
        "read_diagnostic": None,
        "observed_log_byte_size": None,
        "engine_error_excerpts": [],
        "engine_error_bounded_matches": [],
    }
    raw, read_reason = bounded_read_runtime_log_bytes(project_root, rel_norm)
    if raw is None:
        entry["read_diagnostic"] = read_reason
        return entry
    entry["observed_log_byte_size"] = len(raw)
    observed = hashlib.sha256(raw).hexdigest()
    entry["observed_content_hash"] = observed
    if observed != artifact.content_hash:
        entry["read_diagnostic"] = "registered_hash_mismatch"
    try:
        text = raw.decode("utf-8", errors="replace")
    except UnicodeDecodeError:
        entry["read_diagnostic"] = entry["read_diagnostic"] or "utf8_decode_failed"
        return entry
    bounded_matches = extract_bounded_engine_error_bounded_matches(text)
    entry["engine_error_bounded_matches"] = bounded_matches
    entry["engine_error_excerpts"] = [row["text"] for row in bounded_matches]
    return entry


def collect_registered_runtime_log_diagnostics(
    project_root: Path,
    db: object,
    workflow_id: str,
) -> dict[str, Any]:
    artifacts = ArtifactRepository(db).list_by_workflow(workflow_id)
    selected = [
        artifact for artifact in artifacts if artifact.artifact_type in RUNTIME_LOG_ARTIFACT_TYPES
    ]
    selected.sort(key=lambda item: (item.artifact_type, item.relative_path, item.id))
    total_matching = len(selected)
    truncated = total_matching > MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES
    if truncated:
        selected = selected[:MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES]
    exec_repo = ExecutionRepository(db)
    executions_by_task: dict[str, list[Execution]] = {}
    for artifact in selected:
        if artifact.task_id not in executions_by_task:
            executions_by_task[artifact.task_id] = exec_repo.list_by_task(artifact.task_id)
    entries = [
        diagnose_registered_runtime_log_artifact(project_root, artifact, executions_by_task)
        for artifact in selected
    ]
    result: dict[str, Any] = {"entries": entries}
    if truncated:
        result["truncation"] = {
            "max_entries": MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES,
            "total_matching": total_matching,
            "omitted_count": total_matching - len(entries),
        }
    return result


_OPTIONAL_DIAGNOSTIC_ERRORS_KEY = "optional_diagnostic_errors"

_LOGGER = logging.getLogger(__name__)


def _ci_candidate_real_evidence_dir(project_root: Path) -> Path | None:
    explicit = os.environ.get("GAMEFACTORY_CI_CANDIDATE_REAL_EVIDENCE_DIR")
    if explicit:
        return Path(explicit)
    if os.environ.get("CI"):
        return project_root / ".verification" / "ci-candidate-real"
    return None


def _sanitize_workflow_failure_message(message: str | None, *, limit: int = 400) -> str | None:
    if message is None:
        return None
    trimmed = message.strip()
    if len(trimmed) > limit:
        return trimmed[: limit - 3] + "..."
    return trimmed


def _bounded_optional_diagnostic_error_marker(stage: str, exc: BaseException) -> str:
    return f"{stage}:{type(exc).__name__}"


def _record_optional_diagnostic_error(payload: dict[str, object], marker: str) -> None:
    existing = payload.get(_OPTIONAL_DIAGNOSTIC_ERRORS_KEY)
    if isinstance(existing, list):
        errors = existing
    else:
        errors = []
        payload[_OPTIONAL_DIAGNOSTIC_ERRORS_KEY] = errors
    errors.append(marker)


def _log_optional_diagnostic_error(marker: str) -> None:
    _LOGGER.warning("candidate workflow failure optional diagnostic skipped: %s", marker)


def _maybe_write_optional_diagnostic_error_marker(
    project_root: Path,
    markers: list[str],
) -> None:
    dest_root = _ci_candidate_real_evidence_dir(project_root)
    if dest_root is None or not markers:
        return
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        marker_path = dest_root / "workflow-failure-diagnostic-errors.json"
        marker_path.write_text(
            json.dumps({"optional_diagnostic_errors": markers}, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (PermissionError, OSError):
        return


def _collect_registered_runtime_log_diagnostics_best_effort(
    project_root: Path,
    db: object,
    workflow_id: str,
) -> tuple[dict[str, Any], str | None]:
    try:
        return collect_registered_runtime_log_diagnostics(project_root, db, workflow_id), None
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        marker = _bounded_optional_diagnostic_error_marker(
            "registered_runtime_log_artifacts",
            exc,
        )
        _log_optional_diagnostic_error(marker)
        return {"entries": []}, marker


def workflow_failure_diagnostics(
    project_root: Path,
    db: object,
    workflow_id: str,
    result: WorkflowExecutionResult,
) -> dict[str, object]:
    tasks = TaskRepository(db).list_by_workflow(workflow_id)
    exec_repo = ExecutionRepository(db)
    failed_summaries: list[dict[str, object]] = []
    for task in tasks:
        if task.status != TaskStatus.FAILED:
            continue
        attempts = exec_repo.list_by_task(task.id)
        latest = attempts[-1] if attempts else None
        failed_summaries.append(
            {
                "task_id": task.id,
                "task_type": task.task_type,
                "task_status": task.status.value,
                "latest_execution_status": latest.status.value if latest else None,
                "error_message": _sanitize_workflow_failure_message(
                    latest.error_message if latest else None
                ),
            }
        )
    runtime_log_diagnostics, runtime_log_error = (
        _collect_registered_runtime_log_diagnostics_best_effort(
            project_root,
            db,
            workflow_id,
        )
    )
    payload: dict[str, object] = {
        "workflow_id": workflow_id,
        "workflow_status": result.status.value,
        "workflow_error_message": _sanitize_workflow_failure_message(result.error_message),
        "workflow_error_code": result.error_code,
        "failed_task_count": len(failed_summaries),
        "failed_tasks": failed_summaries,
        "registered_runtime_log_artifacts": runtime_log_diagnostics["entries"],
    }
    truncation = runtime_log_diagnostics.get("truncation")
    if truncation is not None:
        payload["registered_runtime_log_artifacts_truncation"] = truncation
    if runtime_log_error is not None:
        _record_optional_diagnostic_error(payload, runtime_log_error)
    return payload


def maybe_write_ci_workflow_failure_report(
    project_root: Path,
    payload: dict[str, object],
) -> str | None:
    dest_root = _ci_candidate_real_evidence_dir(project_root)
    if dest_root is None:
        return None
    try:
        dest_root.mkdir(parents=True, exist_ok=True)
        report_path = dest_root / "workflow-failure-diagnostic.json"
        report_path.write_text(
            json.dumps(payload, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except (KeyboardInterrupt, SystemExit):
        raise
    except (PermissionError, OSError) as exc:
        marker = _bounded_optional_diagnostic_error_marker("ci_workflow_failure_report", exc)
        _log_optional_diagnostic_error(marker)
        return marker
    return None


def assert_workflow_completed_or_diagnose(
    db: object,
    project_root: Path,
    workflow_id: str,
    result: WorkflowExecutionResult,
) -> None:
    if result.status == WorkflowStatus.COMPLETED:
        return
    diagnostics = workflow_failure_diagnostics(project_root, db, workflow_id, result)
    report_error = maybe_write_ci_workflow_failure_report(project_root, diagnostics)
    if report_error is not None:
        _record_optional_diagnostic_error(diagnostics, report_error)
    optional_errors = diagnostics.get(_OPTIONAL_DIAGNOSTIC_ERRORS_KEY)
    if isinstance(optional_errors, list) and optional_errors:
        _maybe_write_optional_diagnostic_error_marker(project_root, optional_errors)
    optional_suffix = ""
    if isinstance(optional_errors, list) and optional_errors:
        optional_suffix = f" optional_diagnostic_errors={optional_errors!r}"
    raise AssertionError(
        "candidate workflow did not reach COMPLETED after TEST_ONLY approval: "
        f"status={result.status.value} "
        f"error_message={result.error_message!r} "
        f"error_code={result.error_code!r} "
        f"failed_tasks={diagnostics['failed_tasks']}"
        f"{optional_suffix}"
    )
