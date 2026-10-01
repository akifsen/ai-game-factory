"""Unit tests for bounded V0.8 candidate workflow failure runtime-log diagnostics."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    ExecutionRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.domain.models import (
    Artifact,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    utc_now_iso,
)
from gamefactory.workflows.engine import WorkflowExecutionResult
from gamefactory.workflows.v08_candidate_workspace import create_fresh_v08_candidate_workspace
from tests.helpers.v08_candidate_workflow_failure_diagnostic import (
    MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES,
    RUNTIME_LOG_ARTIFACT_TYPES,
    assert_workflow_completed_or_diagnose,
    bounded_read_runtime_log_bytes,
    cold_engine_error_line,
    cold_engine_error_rule_identity,
    collect_registered_runtime_log_diagnostics,
    diagnose_registered_runtime_log_artifact,
    extract_bounded_engine_error_bounded_matches,
    extract_bounded_engine_error_excerpts,
    resolve_bounded_runtime_log_path,
)


def _stub_workflow_task_execution(
    workspace,
    *,
    workflow_id: str,
    task_id: str,
    execution_id: str,
) -> None:
    WorkflowRepository(workspace.db).save(
        Workflow(
            id=workflow_id,
            project_id="cand",
            name="diagnostic-stub",
            status=WorkflowStatus.RUNNING,
        )
    )
    TaskRepository(workspace.db).save(
        Task(
            id=task_id,
            workflow_id=workflow_id,
            name="capsule capture stub",
            task_type="v08_candidate_capsule_capture",
            status=TaskStatus.COMPLETED,
        )
    )
    ExecutionRepository(workspace.db).save(
        Execution(
            id=execution_id,
            task_id=task_id,
            attempt_number=1,
            status=ExecutionStatus.COMPLETED,
            completed_at=utc_now_iso(),
        )
    )


def _runtime_log_relative_path(execution_id: str, filename: str) -> str:
    return f"assets/generated/character/stub/capsule-runtime-{execution_id}/{filename}"


def _save_runtime_log_artifact(
    workspace,
    *,
    workflow_id: str,
    task_id: str,
    execution_id: str,
    artifact_id: str,
    artifact_type: str,
    filename: str,
    log_text: str,
    content_hash: str | None = None,
) -> Artifact:
    rel = _runtime_log_relative_path(execution_id, filename)
    log_path = workspace.root / rel
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(log_text, encoding="utf-8")
    raw = log_path.read_bytes()
    digest = content_hash or hashlib.sha256(raw).hexdigest()
    artifact = Artifact(
        id=artifact_id,
        workflow_id=workflow_id,
        task_id=task_id,
        artifact_type=artifact_type,
        producer="unit-test",
        relative_path=rel,
        content_hash=digest,
        file_size=len(raw),
        created_at=utc_now_iso(),
    )
    ArtifactRepository(workspace.db).save(artifact)
    return artifact


def test_cold_engine_error_line_matches_verifier_patterns() -> None:
    assert cold_engine_error_line("ERROR: Failed to load scene")
    assert cold_engine_error_line("some prefix SCRIPT ERROR: bad call")
    assert cold_engine_error_line("Parse Error: unexpected token")


def test_cold_engine_error_line_is_case_insensitive() -> None:
    assert cold_engine_error_line("error: lowercase prefix")
    assert cold_engine_error_rule_identity("error: lowercase prefix") == "ERROR:"
    assert cold_engine_error_rule_identity("script error: tail") == "SCRIPT ERROR"
    assert cold_engine_error_rule_identity("parse error: token") == "Parse Error"


def test_cold_engine_error_line_ignores_innocuous_text() -> None:
    assert not cold_engine_error_line("Godot Engine started normally")
    assert not cold_engine_error_line("INFO: importing assets")
    assert not cold_engine_error_line("Finished import with zero warnings")


def test_extract_bounded_engine_error_excerpts_includes_context_and_caps() -> None:
    text = "\n".join(
        [
            "line0",
            "line1 before",
            "ERROR: boom",
            "line3 after",
            "line4",
            "SCRIPT ERROR: tail",
        ]
    )
    excerpts = extract_bounded_engine_error_excerpts(
        text,
        max_lines=8,
        max_chars=300,
        context_lines=1,
    )
    assert "line1 before" in excerpts
    assert "ERROR: boom" in excerpts
    assert "line3 after" in excerpts
    assert "SCRIPT ERROR: tail" in excerpts
    assert "line0" not in excerpts


def test_extract_bounded_engine_error_bounded_matches_line_numbers_and_rules() -> None:
    text = "\n".join(
        [
            "line0",
            "line1 before",
            "ERROR: boom",
            "line3 after",
            "SCRIPT ERROR: tail",
        ]
    )
    matches = extract_bounded_engine_error_bounded_matches(
        text,
        max_lines=8,
        max_chars=300,
        context_lines=1,
    )
    assert [row["text"] for row in matches] == extract_bounded_engine_error_excerpts(
        text,
        max_lines=8,
        max_chars=300,
        context_lines=1,
    )
    by_line = {row["line_number"]: row for row in matches}
    assert by_line[2]["text"] == "line1 before"
    assert "rule_identity" not in by_line[2]
    assert by_line[3]["rule_identity"] == "ERROR:"
    assert by_line[5]["rule_identity"] == "SCRIPT ERROR"


def test_extract_bounded_engine_error_bounded_matches_respects_line_cap() -> None:
    lines = [f"ERROR: hit {index}" for index in range(20)]
    text = "\n".join(lines)
    matches = extract_bounded_engine_error_bounded_matches(text, max_lines=8, max_chars=10_000)
    assert len(matches) == 8


def test_extract_bounded_engine_error_excerpts_respects_char_cap() -> None:
    text = "ERROR: " + ("x" * 400)
    excerpts = extract_bounded_engine_error_excerpts(text, max_lines=8, max_chars=40)
    assert excerpts
    assert sum(len(line) for line in excerpts) + max(0, len(excerpts) - 1) <= 40


def test_resolve_bounded_runtime_log_path_rejects_parent_segments(tmp_path: Path) -> None:
    log = tmp_path / "runtime" / "godot-import.log"
    log.parent.mkdir(parents=True)
    log.write_text("ok\n", encoding="utf-8")
    resolved, reason = resolve_bounded_runtime_log_path(tmp_path, "../runtime/godot-import.log")
    assert resolved is None
    assert reason == "relative_path_not_lexically_safe"


@pytest.mark.skipif(os.name != "posix", reason="symlink escape check uses POSIX symlinks")
def test_bounded_read_runtime_log_bytes_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside.log"
    outside.write_text("secret\n", encoding="utf-8")
    link = tmp_path / "workspace" / "runtime" / "godot-import.log"
    link.parent.mkdir(parents=True)
    os.symlink(outside, link)
    raw, reason = bounded_read_runtime_log_bytes(
        tmp_path / "workspace",
        "runtime/godot-import.log",
    )
    assert raw is None
    assert reason in {"path_crosses_link", "lexical_path_unsafe", "path_not_under_project_root"}


def test_bounded_read_runtime_log_bytes_hashes_observed_content(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    log = root / "runtime" / "godot-render.log"
    log.parent.mkdir(parents=True)
    payload = "ERROR: Failed to instantiate\n"
    log.write_text(payload, encoding="utf-8")
    raw, reason = bounded_read_runtime_log_bytes(root, "runtime/godot-render.log")
    assert reason is None
    assert raw is not None
    assert hashlib.sha256(raw).hexdigest() == hashlib.sha256(log.read_bytes()).hexdigest()


def test_bounded_read_runtime_log_bytes_reports_oversize(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    log = root / "runtime" / "godot-import.log"
    log.parent.mkdir(parents=True)
    log.write_bytes(b"x" * 32)
    raw, reason = bounded_read_runtime_log_bytes(root, "runtime/godot-import.log", max_bytes=16)
    assert raw is None
    assert reason == "exceeds_bounded_byte_limit"


def test_bounded_read_runtime_log_bytes_reports_missing_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(parents=True)
    raw, reason = bounded_read_runtime_log_bytes(root, "runtime/godot-import.log")
    assert raw is None
    assert reason in {"not_a_regular_file", "path_not_under_project_root"}


def test_bounded_read_runtime_log_bytes_reports_open_failure(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "workspace"
    log = root / "runtime" / "godot-import.log"
    log.parent.mkdir(parents=True)
    log.write_text("ERROR: open should fail\n", encoding="utf-8")

    def _raise_os_open(*_args, **_kwargs):
        raise OSError("simulated open failure")

    monkeypatch.setattr(os, "open", _raise_os_open)
    raw, reason = bounded_read_runtime_log_bytes(root, "runtime/godot-import.log")
    assert raw is None
    assert reason is not None
    assert reason.startswith("open_failed:")


def test_diagnose_registered_runtime_log_artifact_binds_execution_and_hash(
    tmp_path: Path,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-DIAG-1"
    task_id = "TASK-CAP-1"
    execution_id = "EXEC-CAP-BIND"
    _stub_workflow_task_execution(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
    )
    artifact = _save_runtime_log_artifact(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        artifact_id="ART-IMPORT-1",
        artifact_type="candidate-runtime-import-log",
        filename="godot-import.log",
        log_text="ERROR: import failed\n",
    )
    executions_by_task = {task_id: ExecutionRepository(workspace.db).list_by_task(task_id)}
    entry = diagnose_registered_runtime_log_artifact(
        workspace.root,
        artifact,
        executions_by_task,
    )
    assert entry["producing_execution"]["execution_id"] == execution_id
    assert entry["observed_content_hash"] == artifact.content_hash
    assert entry["read_diagnostic"] is None
    assert "ERROR: import failed" in entry["engine_error_excerpts"][0]
    assert entry["observed_log_byte_size"] == artifact.file_size
    assert entry["engine_error_bounded_matches"][0]["line_number"] == 1
    assert entry["engine_error_bounded_matches"][0]["rule_identity"] == "ERROR:"


def test_diagnose_registered_runtime_log_artifact_rejects_unbound_execution(
    tmp_path: Path,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-DIAG-2"
    task_id = "TASK-CAP-2"
    bound_execution_id = "EXEC-CAP-BOUND"
    wrong_execution_id = "EXEC-CAP-OTHER"
    _stub_workflow_task_execution(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=wrong_execution_id,
    )
    artifact = _save_runtime_log_artifact(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=bound_execution_id,
        artifact_id="ART-IMPORT-2",
        artifact_type="candidate-runtime-import-log",
        filename="godot-import.log",
        log_text="ERROR: bound path only\n",
    )
    executions_by_task = {task_id: ExecutionRepository(workspace.db).list_by_task(task_id)}
    entry = diagnose_registered_runtime_log_artifact(
        workspace.root,
        artifact,
        executions_by_task,
    )
    assert entry["producing_execution"] is None
    assert entry["producing_execution_diagnostic"] == "no_producing_execution_match"
    assert entry["observed_content_hash"] is not None


def test_collect_registered_runtime_log_diagnostics_filters_types_and_hashes(
    tmp_path: Path,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-COLLECT-1"
    task_id = "TASK-COLLECT-1"
    execution_id = "EXEC-COLLECT-1"
    _stub_workflow_task_execution(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
    )
    _save_runtime_log_artifact(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        artifact_id="ART-IMPORT-C",
        artifact_type="candidate-runtime-import-log",
        filename="godot-import.log",
        log_text="ERROR: collect import\n",
    )
    _save_runtime_log_artifact(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        artifact_id="ART-RENDER-C",
        artifact_type="candidate-runtime-render-log",
        filename="godot-render.log",
        log_text="Godot ok\n",
    )
    ArtifactRepository(workspace.db).save(
        Artifact(
            id="ART-UNRELATED",
            workflow_id=workflow_id,
            task_id=task_id,
            artifact_type="candidate-runtime-request",
            producer="unit-test",
            relative_path="assets/generated/character/stub/request.json",
            content_hash="0" * 64,
            file_size=1,
        )
    )
    collected = collect_registered_runtime_log_diagnostics(
        workspace.root,
        workspace.db,
        workflow_id,
    )
    entries = collected["entries"]
    assert collected.get("truncation") is None
    assert len(entries) == 2
    types = {entry["artifact_type"] for entry in entries}
    assert types == set(RUNTIME_LOG_ARTIFACT_TYPES)
    import_entry = next(
        item for item in entries if item["artifact_type"] == "candidate-runtime-import-log"
    )
    assert import_entry["producing_execution"]["execution_id"] == execution_id
    assert import_entry["read_diagnostic"] is None
    assert import_entry["engine_error_excerpts"]


def test_collect_registered_runtime_log_diagnostics_reports_hash_mismatch(
    tmp_path: Path,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-COLLECT-2"
    task_id = "TASK-COLLECT-2"
    execution_id = "EXEC-COLLECT-2"
    _stub_workflow_task_execution(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
    )
    _save_runtime_log_artifact(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        artifact_id="ART-MISMATCH",
        artifact_type="candidate-runtime-import-log",
        filename="godot-import.log",
        log_text="ERROR: on disk\n",
        content_hash="f" * 64,
    )
    entry = collect_registered_runtime_log_diagnostics(
        workspace.root,
        workspace.db,
        workflow_id,
    )["entries"][0]
    assert entry["read_diagnostic"] == "registered_hash_mismatch"
    assert entry["observed_content_hash"] != entry["registered_content_hash"]


def test_collect_registered_runtime_log_diagnostics_caps_entries(tmp_path: Path) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-CAP"
    task_id = "TASK-CAP"
    execution_id = "EXEC-CAP-LIMIT"
    _stub_workflow_task_execution(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
    )
    over = MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES + 3
    for index in range(over):
        rel = (
            f"assets/generated/character/stub/capsule-runtime-{execution_id}/"
            f"extra-{index:02d}-godot-import.log"
        )
        log_path = workspace.root / rel
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log_path.write_text(f"ERROR: cap {index}\n", encoding="utf-8")
        raw = log_path.read_bytes()
        ArtifactRepository(workspace.db).save(
            Artifact(
                id=f"ART-CAP-{index:02d}",
                workflow_id=workflow_id,
                task_id=task_id,
                artifact_type="candidate-runtime-import-log",
                producer="unit-test",
                relative_path=rel,
                content_hash=hashlib.sha256(raw).hexdigest(),
                file_size=len(raw),
            )
        )
    collected = collect_registered_runtime_log_diagnostics(
        workspace.root,
        workspace.db,
        workflow_id,
    )
    assert len(collected["entries"]) == MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES
    truncation = collected["truncation"]
    assert truncation is not None
    assert truncation["max_entries"] == MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES
    assert truncation["total_matching"] == over
    assert truncation["omitted_count"] == over - MAX_REGISTERED_RUNTIME_LOG_DIAGNOSTIC_ENTRIES


def test_collect_registered_runtime_log_diagnostics_survives_open_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-OPEN-FAIL"
    task_id = "TASK-OPEN"
    execution_id = "EXEC-OPEN-FAIL"
    _stub_workflow_task_execution(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
    )
    _save_runtime_log_artifact(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        artifact_id="ART-OPEN",
        artifact_type="candidate-runtime-import-log",
        filename="godot-import.log",
        log_text="ERROR: should not read\n",
    )

    def _raise_os_open(*_args, **_kwargs):
        raise OSError("simulated open failure")

    monkeypatch.setattr(os, "open", _raise_os_open)
    collected = collect_registered_runtime_log_diagnostics(
        workspace.root,
        workspace.db,
        workflow_id,
    )
    entry = collected["entries"][0]
    assert entry["read_diagnostic"] is not None
    assert entry["read_diagnostic"].startswith("open_failed:")
    assert entry["observed_content_hash"] is None


def _stub_failed_workflow_with_execution_error(
    workspace,
    *,
    workflow_id: str,
    task_id: str,
    execution_id: str,
    task_error_message: str,
    workflow_error_message: str = "stub workflow failed",
    workflow_error_code: str | None = "STUB_WORKFLOW_FAILED",
) -> WorkflowExecutionResult:
    WorkflowRepository(workspace.db).save(
        Workflow(
            id=workflow_id,
            project_id="cand",
            name="diagnostic-failure-stub",
            status=WorkflowStatus.FAILED,
        )
    )
    TaskRepository(workspace.db).save(
        Task(
            id=task_id,
            workflow_id=workflow_id,
            name="capsule capture stub",
            task_type="v08_candidate_capsule_capture",
            status=TaskStatus.FAILED,
        )
    )
    ExecutionRepository(workspace.db).save(
        Execution(
            id=execution_id,
            task_id=task_id,
            attempt_number=1,
            status=ExecutionStatus.FAILED,
            error_message=task_error_message,
            completed_at=utc_now_iso(),
        )
    )
    return WorkflowExecutionResult(
        workflow_id=workflow_id,
        status=WorkflowStatus.FAILED,
        completed_tasks=[],
        failed_tasks=[task_id],
        blocked_tasks=[],
        error_message=workflow_error_message,
        error_code=workflow_error_code,
    )


def test_assert_workflow_completed_or_diagnose_raises_with_failed_task_when_report_write_denied(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-ASSERT-REPORT"
    task_id = "TASK-ASSERT-REPORT"
    execution_id = "EXEC-ASSERT-REPORT"
    task_error = "simulated failed task error for report denial"
    result = _stub_failed_workflow_with_execution_error(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        task_error_message=task_error,
    )
    evidence_dir = tmp_path / "ci-evidence"
    evidence_dir.mkdir()
    monkeypatch.setenv("GAMEFACTORY_CI_CANDIDATE_REAL_EVIDENCE_DIR", str(evidence_dir))
    original_write_text = Path.write_text

    def _deny_diagnostic_report_write(self: Path, *args, **kwargs):
        if self.name == "workflow-failure-diagnostic.json":
            raise PermissionError("simulated report write denial")
        return original_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _deny_diagnostic_report_write)
    with pytest.raises(AssertionError) as raised:
        assert_workflow_completed_or_diagnose(
            workspace.db,
            workspace.root,
            workflow_id,
            result,
        )
    message = str(raised.value)
    assert "status=FAILED" in message
    assert task_id in message
    assert task_error in message
    assert "optional_diagnostic_errors" in message
    assert "ci_workflow_failure_report:PermissionError" in message


def test_assert_workflow_completed_or_diagnose_raises_with_failed_task_when_runtime_log_db_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = create_fresh_v08_candidate_workspace(tmp_path)
    workflow_id = "WF-ASSERT-DB"
    task_id = "TASK-ASSERT-DB"
    execution_id = "EXEC-ASSERT-DB"
    task_error = "simulated failed task error for db collector failure"
    result = _stub_failed_workflow_with_execution_error(
        workspace,
        workflow_id=workflow_id,
        task_id=task_id,
        execution_id=execution_id,
        task_error_message=task_error,
    )

    def _raise_db_error(self, workflow_id: str):
        raise RuntimeError("simulated artifact repository db failure")

    monkeypatch.setattr(ArtifactRepository, "list_by_workflow", _raise_db_error)
    with pytest.raises(AssertionError) as raised:
        assert_workflow_completed_or_diagnose(
            workspace.db,
            workspace.root,
            workflow_id,
            result,
        )
    message = str(raised.value)
    assert "status=FAILED" in message
    assert task_id in message
    assert task_error in message
    assert "registered_runtime_log_artifacts:RuntimeError" in message
