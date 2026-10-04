"""Crash recovery for approved generic game-file application.

These fixtures persist real proposal, output, gate, approval, execution and
journal records. Providers and game engines are not invoked by these tests.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.engines.godot_staging import GodotStager
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AuditLogRepository,
    ExecutionRepository,
    ProjectRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.agents.registry import AgentRegistry
from gamefactory.cli.exit_codes import EXIT_SUCCESS
from gamefactory.cli.factory_commands import dispatch_factory_command
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.domain.errors import LockError, ValidationError
from gamefactory.core.domain.factory_workflow import FactoryWorkflowManifest
from gamefactory.core.domain.models import (
    ApprovalStatus,
    Artifact,
    CostClass,
    Execution,
    ExecutionStatus,
    GateStatus,
    Project,
    QualityGate,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
)
from gamefactory.core.execution.locks import ExecutionLock
from gamefactory.core.policies.policy_engine import PolicyEngine
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.factory_workflow import (
    ExecutorRegistry,
    GateExecutorRegistry,
    _artifact_dir,
    build_workflow,
    inspect_factory_apply_journal,
    recover_factory_apply_journal,
    register_factory_handlers,
)
from gamefactory.workflows.handlers import TaskHandlerRegistry

_RELATIVE = ".gamefactory/operations/factory-apply/accept-old-run.json"
_WORKFLOW = "wf-recovery"
_PROJECT = "project-recovery"
_WRITER = "writer"
_GATE = "writer:gate:code"
_ACCEPT = f"{_WORKFLOW}:accept"
_MANIFEST = "a" * 64
_BASE = b"extends Node\n# approved baseline\n"
_AFTER = b"extends Node\n# accepted candidate\n"


def _generated_acceptance_retry_limit() -> int:
    manifest = FactoryWorkflowManifest.model_validate_json(
        """{
          "schema_version":"factory-workflow-1.0.0",
          "workflow_id":"wf-retry-budget",
          "project_id":"project-retry-budget",
          "name":"Acceptance retry budget",
          "tasks":[{
            "task_id":"writer",
            "name":"Write player controller",
            "family":"feature",
            "kind":"code",
            "executor_id":"executor.local",
            "agent_id":"agent.local",
            "objective":"Propose a player controller change",
            "prompt_template_id":"feature",
            "prompt_template_version":"1.0.0",
            "game_write":true,
            "output_scopes":["scripts/player.gd"],
            "expected_artifacts":["scripts/player.gd"],
            "required_gates":["code"]
          }]
        }"""
    )
    _, generated_tasks = build_workflow(manifest)
    generated_acceptance = next(
        item for item in generated_tasks if item.task_type == "factory_acceptance"
    )
    return generated_acceptance.max_retries


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _put_artifact(
    root: Path,
    db: Database,
    *,
    artifact_id: str,
    task_id: str,
    artifact_type: str,
    relative_path: str,
    content: bytes,
) -> Artifact:
    path = root / Path(*relative_path.split("/"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    artifact = Artifact(
        artifact_id,
        _WORKFLOW,
        task_id,
        artifact_type,
        "recovery-test-fixture",
        relative_path,
        _digest(content),
        len(content),
        "VALID",
    )
    ArtifactRepository(db).save(artifact)
    return artifact


def _case(
    tmp_path: Path,
    *,
    journal_status: str = "COMMITTED",
    target_state: str = "after",
    created_target: bool = False,
    with_application_artifact: bool = False,
    acceptance_execution_status: ExecutionStatus = ExecutionStatus.FAILED,
    acceptance_task_status: TaskStatus = TaskStatus.PENDING,
    acceptance_retryable: bool = False,
) -> dict[str, Any]:
    """Build a DB-bound approved candidate and simulate a journal crash point."""
    root = tmp_path / "project"
    root.mkdir()
    (root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    game_target = root / "scripts" / "player.gd"
    game_target.parent.mkdir(parents=True)
    if not created_target:
        game_target.write_bytes(_BASE)

    db = Database(root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(Project(_PROJECT, "Recovery project", "godot", str(root)))

    workflow_status = (
        WorkflowStatus.FAILED
        if acceptance_task_status == TaskStatus.FAILED
        else WorkflowStatus.BLOCKED
    )
    workflow = Workflow(_WORKFLOW, _PROJECT, "Accepted change", workflow_status)
    writer = Task(
        _WRITER,
        _WORKFLOW,
        "Accepted writer",
        "factory_provider_task",
        CostClass.LOCAL,
        status=TaskStatus.COMPLETED,
        parameters={"manifest_sha256": _MANIFEST},
    )
    gate = Task(
        _GATE,
        _WORKFLOW,
        "Final code gate",
        "factory_quality_gate",
        CostClass.LOCAL,
        depends_on=[_WRITER],
        status=TaskStatus.COMPLETED,
        parameters={
            "manifest_sha256": _MANIFEST,
            "gate": "code",
            "candidate_writer_tasks": [_WRITER],
        },
    )
    acceptance = Task(
        _ACCEPT,
        _WORKFLOW,
        "Human accepted game-file application",
        "factory_acceptance",
        CostClass.LOCAL,
        depends_on=[_GATE],
        status=acceptance_task_status,
        parameters={
            "writer_tasks": [_WRITER],
            "required_gate_tasks": [_GATE],
            "required_decision_tasks": [],
            "manifest_sha256": _MANIFEST,
        },
        max_retries=_generated_acceptance_retry_limit(),
    )
    writer_execution = Execution(
        "writer-run",
        _WRITER,
        1,
        ExecutionStatus.COMPLETED,
        completed_at="2026-10-04T00:00:00+00:00",
    )
    gate_execution = Execution(
        "gate-run", _GATE, 1, ExecutionStatus.COMPLETED, completed_at="2026-10-04T00:00:00+00:00"
    )
    old_acceptance_execution = Execution(
        "accept-old-run",
        _ACCEPT,
        1,
        acceptance_execution_status,
        completed_at="2026-10-04T00:00:00+00:00"
        if acceptance_execution_status != ExecutionStatus.RUNNING
        else None,
        retryable=acceptance_retryable,
    )

    handlers = TaskHandlerRegistry()
    register_factory_handlers(
        handlers,
        project_root=root,
        db=db,
        executor_registry=ExecutorRegistry(),
        agent_registry=AgentRegistry(),
        gate_registry=GateExecutorRegistry(),
    )
    engine = WorkflowEngine(root, db, policy_engine=PolicyEngine(), handler_registry=handlers)
    engine.register_workflow(workflow, [writer, gate, acceptance])
    ExecutionRepository(db).save(writer_execution)
    ExecutionRepository(db).save(gate_execution)
    ExecutionRepository(db).save(old_acceptance_execution)
    QualityGateRepository(db).save(
        QualityGate(
            "gate-passed",
            _GATE,
            "factory:code",
            GateStatus.PASSED,
            "2026-10-04T00:00:00+00:00",
            "Fixture code gate passed.",
        )
    )

    candidate_root = tmp_path / "candidate"
    candidate_root.mkdir()
    (candidate_root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    (candidate_root / "scripts").mkdir()
    (candidate_root / "scripts" / "player.gd").write_bytes(_AFTER)
    _, candidate_sha256 = GodotStager(candidate_root, tmp_path / "unused-scratch").source_manifest()

    writer_artifact_root = _artifact_dir(_WORKFLOW, _WRITER, "writer-run")
    output_rel = f"{writer_artifact_root}/output.bin"
    output_artifact = _put_artifact(
        root,
        db,
        artifact_id="output-artifact",
        task_id=_WRITER,
        artifact_type="factory_output:scripts/player.gd",
        relative_path=output_rel,
        content=_AFTER,
    )
    proposal = {
        "schema_version": "factory-provider-proposal-1.0.0",
        "execution_id": writer_execution.id,
        "files": [
            {
                "path": "scripts/player.gd",
                "sha256": _digest(_AFTER),
                "media_type": "text/x-gdscript",
                "expected_before_sha256": None if created_target else _digest(_BASE),
                "artifact_id": output_artifact.id,
            }
        ],
    }
    proposal_artifact = _put_artifact(
        root,
        db,
        artifact_id="proposal-artifact",
        task_id=_WRITER,
        artifact_type="factory_proposal",
        relative_path=f"{writer_artifact_root}/proposal.json",
        content=json.dumps(proposal, sort_keys=True, separators=(",", ":")).encode(),
    )
    report_payload = {
        "schema_version": "factory-quality-gate-1.0.0",
        "execution_id": gate_execution.id,
        "manifest_sha256": _MANIFEST,
        "gate_task_id": _GATE,
        "gate": "code",
        "candidate_sha256": candidate_sha256,
        "report": {
            "schema_version": "factory-quality-report-1.0.0",
            "status": "PASS",
            "findings": [],
            "visual_advisories": [],
        },
    }
    report_artifact = _put_artifact(
        root,
        db,
        artifact_id="gate-report-artifact",
        task_id=_GATE,
        artifact_type="factory_quality_report",
        relative_path=f"{_artifact_dir(_WORKFLOW, _GATE, 'gate-run')}/quality-code.json",
        content=json.dumps(report_payload, sort_keys=True, separators=(",", ":")).encode(),
    )

    # Build the exact persisted operation inputs that a real acceptance request
    # uses, then approve them through ApprovalService.
    current_workflow = WorkflowRepository(db).get(_WORKFLOW)
    current_acceptance = TaskRepository(db).get(_ACCEPT)
    assert current_workflow is not None and current_acceptance is not None
    operation_inputs = engine.approval_inputs(current_workflow, current_acceptance)
    acceptance_profile = handlers.metadata("factory_acceptance")
    assert acceptance_profile is not None and acceptance_profile.approval_context is not None
    approved_context = acceptance_profile.approval_context(current_workflow, current_acceptance)
    approval = ApprovalService.create_request(
        _WORKFLOW,
        _ACCEPT,
        "factory_game_write",
        "Approve the exact candidate and required gate evidence.",
        CostClass.LOCAL,
        operation_inputs,
        [output_artifact.id, proposal_artifact.id, report_artifact.id],
    )
    approval = ApprovalService.approve(
        approval,
        "integration-operator",
        "Approve exact test candidate",
        current_inputs=operation_inputs,
    )
    ApprovalRepository(db).save(approval)

    journal_path = root / Path(*_RELATIVE.split("/"))
    backup_rel = (
        None
        if created_target
        else f".gamefactory/operations/factory-apply/{journal_path.stem}.backups/0000.backup"
    )
    capture_rel = (
        None
        if created_target
        else f".gamefactory/operations/factory-apply/{journal_path.stem}.backups/0000.capture"
    )
    backup_path = root / Path(*backup_rel.split("/")) if backup_rel else None
    if backup_path is not None:
        backup_path.parent.mkdir(parents=True, exist_ok=True)
        backup_path.write_bytes(_BASE)
    if target_state == "after":
        game_target.write_bytes(_AFTER)
    elif target_state == "missing":
        if game_target.exists():
            game_target.unlink()
    elif target_state == "unknown":
        game_target.write_bytes(b"operator concurrent edit")
    elif target_state != "before":
        raise AssertionError(f"Unsupported fixture target state: {target_state}")

    journal = {
        "schema_version": "factory-apply-journal-1.0.0",
        "workflow_id": _WORKFLOW,
        "task_id": _ACCEPT,
        "execution_id": old_acceptance_execution.id,
        "approval_id": approval.id,
        "operation_hash": approval.operation_hash,
        "approved_context": approved_context,
        "candidate_sha256": candidate_sha256,
        "writer_tasks": [_WRITER],
        "required_gate_tasks": [_GATE],
        "required_decision_tasks": [],
        "gate_reports": {
            _GATE: {
                "report_artifact_id": report_artifact.id,
                "report_sha256": report_artifact.content_hash,
                "candidate_sha256": candidate_sha256,
            },
        },
        "status": journal_status,
        "files": [
            {
                "path": "scripts/player.gd",
                "before_sha256": None if created_target else _digest(_BASE),
                "after_sha256": _digest(_AFTER),
                "artifact_id": output_artifact.id,
                "backup_path": backup_rel,
                "backup_sha256": None if created_target else _digest(_BASE),
                "capture_path": capture_rel,
                "was_created": created_target,
            }
        ],
    }
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_text(
        json.dumps(journal, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )

    existing_application = None
    if with_application_artifact:
        application_rel = (
            f"{_artifact_dir(_WORKFLOW, _ACCEPT, old_acceptance_execution.id)}/application.json"
        )
        # Use the same payload/serializer as the ordinary accept handler.
        ordinary_report = {
            **report_payload,
            "_report_artifact_id": report_artifact.id,
            "_report_sha256": report_artifact.content_hash,
        }
        app_payload = {
            "schema_version": "factory-application-1.0.0",
            "candidate_sha256": candidate_sha256,
            "journal": _RELATIVE,
            "files": [{"path": "scripts/player.gd", "sha256": _digest(_AFTER)}],
            "accepted_writer_tasks": [_WRITER],
            "gate_reports": {_GATE: ordinary_report},
        }
        existing_application = _put_artifact(
            root,
            db,
            artifact_id="application-artifact",
            task_id=_ACCEPT,
            artifact_type="factory_application",
            relative_path=application_rel,
            content=json.dumps(
                app_payload,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode(),
        )

    return {
        "root": root,
        "db": db,
        "engine": engine,
        "workflow": workflow,
        "task": acceptance,
        "approval": approval,
        "journal_path": journal_path,
        "journal": journal,
        "game_target": game_target,
        "backup_path": backup_path,
        "candidate_sha256": candidate_sha256,
        "output_artifact": output_artifact,
        "proposal_artifact": proposal_artifact,
        "report_artifact": report_artifact,
        "application_artifact": existing_application,
    }


def _inspect(case: dict[str, Any]) -> dict[str, Any]:
    return inspect_factory_apply_journal(case["root"], _RELATIVE, db=case["db"])


def _recover(case: dict[str, Any], preview: dict[str, Any] | None = None) -> dict[str, Any]:
    preview = preview or _inspect(case)
    return recover_factory_apply_journal(
        case["root"],
        _RELATIVE,
        db=case["db"],
        expected_journal_sha256=preview["journal_sha256"],
        actor="integration-operator",
        comment="Recover after a simulated process interruption.",
    )


def test_prepared_interruption_rolls_back_promoted_file_and_preserves_original_approval(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, journal_status="PREPARED", target_state="after")
    preview = _inspect(case)
    assert preview["recovery_action"] == "ROLLBACK"
    result = _recover(case, preview)
    assert result["status"] == "ROLLED_BACK"
    assert case["game_target"].read_bytes() == _BASE
    assert case["backup_path"].read_bytes() == _BASE
    assert ApprovalRepository(case["db"]).get(case["approval"].id).status == ApprovalStatus.APPROVED


def test_prepared_interruption_before_write_is_idempotently_rolled_back(tmp_path: Path) -> None:
    case = _case(tmp_path, journal_status="PREPARED", target_state="before")
    preview = _inspect(case)
    assert preview["recovery_action"] == "ROLLBACK"
    first = _recover(case, preview)
    second_preview = _inspect(case)
    second = _recover(case, second_preview)
    assert first["status"] == second["status"] == "ROLLED_BACK"
    assert case["game_target"].read_bytes() == _BASE


def test_interrupted_create_before_target_appears_leaves_it_absent(tmp_path: Path) -> None:
    case = _case(tmp_path, journal_status="PREPARED", target_state="missing", created_target=True)
    assert _inspect(case)["recovery_action"] == "ROLLBACK"
    result = _recover(case)
    assert result["status"] == "ROLLED_BACK"
    assert not case["game_target"].exists()


def test_rollback_captures_and_preserves_replacement_racing_after_preview(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import gamefactory.workflows.factory_workflow as factory_workflow

    case = _case(tmp_path, journal_status="PREPARED", target_state="after")
    preview = _inspect(case)
    concurrent_bytes = b"operator replaced candidate during recovery"
    original_replace = factory_workflow.os.replace
    raced = False

    def replace_with_interleaving(source: Any, destination: Any) -> None:
        nonlocal raced
        if Path(source) == case["game_target"] and not raced:
            raced = True
            case["game_target"].write_bytes(concurrent_bytes)
        original_replace(source, destination)

    monkeypatch.setattr(factory_workflow.os, "replace", replace_with_interleaving)
    result = _recover(case, preview)

    assert raced
    assert result["status"] == "ROLLBACK_REQUIRED"
    assert result["conflicts"] == ["scripts/player.gd"]
    assert case["game_target"].read_bytes() == concurrent_bytes
    assert case["backup_path"].read_bytes() == _BASE


def test_unexpected_concurrent_game_file_is_never_overwritten_or_deleted(tmp_path: Path) -> None:
    case = _case(tmp_path, journal_status="PREPARED", target_state="unknown")
    unexpected = b"operator concurrent edit"
    preview = _inspect(case)
    assert preview["recovery_action"] == "BLOCKED"
    with pytest.raises(ValidationError, match="Unexpected live or backup bytes"):
        _recover(case, preview)
    assert case["game_target"].read_bytes() == unexpected
    assert case["backup_path"].read_bytes() == _BASE


def test_preview_is_read_only_and_stale_cas_does_not_change_files_or_journal(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path)
    before_journal = case["journal_path"].read_bytes()
    before_file = case["game_target"].read_bytes()
    preview = _inspect(case)
    assert preview["recovery_action"] == "FINALIZE_COMMITTED"
    assert case["journal_path"].read_bytes() == before_journal
    assert case["game_target"].read_bytes() == before_file
    with pytest.raises(ValidationError, match="changed after preview"):
        recover_factory_apply_journal(
            case["root"],
            _RELATIVE,
            db=case["db"],
            expected_journal_sha256="f" * 64,
            actor="integration-operator",
            comment="Stale compare-and-swap token.",
        )
    assert case["journal_path"].read_bytes() == before_journal
    assert case["game_target"].read_bytes() == before_file


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("path", "Journal game paths"),
        ("baseline", "Journal game paths"),
        ("candidate", "candidate|gate report"),
        ("gate", "gate report"),
    ],
)
def test_journal_tampering_cannot_change_approved_candidate_or_gate_binding(
    tmp_path: Path, mutation: str, expected_error: str
) -> None:
    case = _case(tmp_path)
    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    if mutation == "path":
        journal["files"][0]["path"] = "scripts/other.gd"
    elif mutation == "baseline":
        journal["files"][0]["before_sha256"] = "b" * 64
    elif mutation == "candidate":
        journal["candidate_sha256"] = "c" * 64
    else:
        journal["gate_reports"][_GATE]["report_artifact_id"] = "another-report"
    case["journal_path"].write_text(
        json.dumps(journal, sort_keys=True, separators=(",", ":")), encoding="utf-8"
    )
    with pytest.raises(ValidationError, match=expected_error):
        _inspect(case)


def test_running_acceptance_execution_cannot_be_taken_over_by_recovery(tmp_path: Path) -> None:
    case = _case(tmp_path, acceptance_execution_status=ExecutionStatus.RUNNING)
    before = case["journal_path"].read_bytes()
    with pytest.raises(ValidationError, match="execution is active"):
        _inspect(case)
    assert case["journal_path"].read_bytes() == before
    assert case["game_target"].read_bytes() == _AFTER


def test_recovery_refuses_while_the_workflow_execution_lock_is_held(tmp_path: Path) -> None:
    case = _case(tmp_path, acceptance_task_status=TaskStatus.FAILED)
    before_journal = case["journal_path"].read_bytes()
    before_game_file = case["game_target"].read_bytes()
    before_artifacts = [
        item.id for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
    ]
    before_task_status = TaskRepository(case["db"]).get(_ACCEPT).status
    before_workflow_status = WorkflowRepository(case["db"]).get(_WORKFLOW).status
    before_task_audit = AuditLogRepository(case["db"]).list_by_entity("Task", _ACCEPT)
    workflow_lock = ExecutionLock(case["engine"].locks_dir, _WORKFLOW)

    with workflow_lock.hold():
        with pytest.raises(LockError):
            _recover(case)

    assert case["journal_path"].read_bytes() == before_journal
    assert case["game_target"].read_bytes() == before_game_file
    assert [
        item.id for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
    ] == before_artifacts
    assert TaskRepository(case["db"]).get(_ACCEPT).status == before_task_status
    assert WorkflowRepository(case["db"]).get(_WORKFLOW).status == before_workflow_status
    assert AuditLogRepository(case["db"]).list_by_entity("Task", _ACCEPT) == before_task_audit


def test_committed_journal_finalizes_missing_application_artifact_without_completing_task(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, acceptance_task_status=TaskStatus.FAILED)
    preview = _inspect(case)
    result = _recover(case, preview)
    assert result["status"] == "FINALIZATION_READY"
    assert result["resume_required"] is True
    assert result["retry_ready"] is True
    assert result["next_step"] == f"gamefactory retry {_WORKFLOW} {_ACCEPT}"
    assert case["game_target"].read_bytes() == _AFTER
    recovered_task = TaskRepository(case["db"]).get(_ACCEPT)
    assert recovered_task.status == TaskStatus.FAILED
    assert ExecutionRepository(case["db"]).get_latest_attempt(_ACCEPT).retryable is True
    assert WorkflowRepository(case["db"]).get(_WORKFLOW).status == WorkflowStatus.FAILED
    assert any(
        event.action == "FACTORY_COMMITTED_APPLY_RETRY_ELIGIBLE"
        for event in AuditLogRepository(case["db"]).list_by_entity("Task", _ACCEPT)
    )


def test_crash_after_application_artifact_save_reuses_exact_artifact_idempotently(
    tmp_path: Path,
) -> None:
    case = _case(tmp_path, with_application_artifact=True, acceptance_task_status=TaskStatus.FAILED)
    before_ids = [
        item.id
        for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
        if item.artifact_type == "factory_application"
    ]
    result = _recover(case)
    after_ids = [
        item.id
        for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
        if item.artifact_type == "factory_application"
    ]
    assert result["status"] == "FINALIZATION_READY"
    assert after_ids == before_ids == [case["application_artifact"].id]
    second = _recover(case)
    assert second["status"] == "FINALIZATION_READY"
    assert [
        item.id
        for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
        if item.artifact_type == "factory_application"
    ] == before_ids


def test_cli_recovery_dispatch_returns_the_normal_retry_command(tmp_path: Path) -> None:
    case = _case(tmp_path, acceptance_task_status=TaskStatus.FAILED)
    preview = _inspect(case)
    args = argparse.Namespace(
        command="factory",
        factory_command="recover-apply",
        journal=_RELATIVE,
        apply=True,
        expected_journal_sha256=preview["journal_sha256"],
        actor="integration-operator",
        comment="Recover and continue through the normal acceptance retry.",
    )

    result, exit_code, message = dispatch_factory_command(args, case["root"], case["db"])

    assert exit_code == EXIT_SUCCESS
    assert result["status"] == "FINALIZATION_READY"
    assert result["retry_ready"] is True
    assert f"gamefactory retry {_WORKFLOW} {_ACCEPT}" in message


def test_engine_retry_finishes_recovered_apply_without_reapplying_or_replacing_approval(
    tmp_path: Path,
) -> None:
    case = _case(
        tmp_path,
        acceptance_task_status=TaskStatus.FAILED,
    )
    assert case["task"].max_retries > 0
    original_approval_id = case["approval"].id
    recovered = _recover(case)
    assert recovered["retry_ready"] is True
    assert ExecutionRepository(case["db"]).get_latest_attempt(_ACCEPT).retryable is True

    result = case["engine"].retry_task(_WORKFLOW, _ACCEPT)

    assert result.status == WorkflowStatus.COMPLETED
    assert sorted(result.completed_tasks) == sorted([_WRITER, _GATE, _ACCEPT])
    assert case["game_target"].read_bytes() == _AFTER
    assert (
        ApprovalRepository(case["db"]).get(original_approval_id).status == ApprovalStatus.APPROVED
    )
    approvals = ApprovalRepository(case["db"]).list_by_workflow(_WORKFLOW)
    assert [item.id for item in approvals if item.task_id == _ACCEPT] == [original_approval_id]
    application_artifacts = [
        item
        for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
        if item.artifact_type == "factory_application"
    ]
    assert len(application_artifacts) == 1
    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert journal["status"] == "FINALIZATION_READY"
    assert journal["execution_id"] == "accept-old-run"  # immutable original apply provenance
    latest_acceptance = ExecutionRepository(case["db"]).get_latest_attempt(_ACCEPT)
    assert latest_acceptance is not None and latest_acceptance.id != journal["execution_id"]


def test_recovery_retry_selects_the_approved_candidate_matching_the_journal_not_newest_record(
    tmp_path: Path,
) -> None:
    case = _case(
        tmp_path,
        acceptance_task_status=TaskStatus.FAILED,
    )
    current_approval = case["approval"]
    stale = ApprovalService.create_request(
        _WORKFLOW,
        _ACCEPT,
        "factory_game_write",
        "An unrelated historical candidate revision.",
        CostClass.LOCAL,
        {"candidate_sha256": "d" * 64, "candidate_revision": "old"},
        [case["proposal_artifact"].id],
    )
    stale = ApprovalService.approve(
        stale,
        "integration-operator",
        "Approve the old revision only.",
        current_inputs={"candidate_sha256": "d" * 64, "candidate_revision": "old"},
    )
    # Make list order actively misleading: this approval is newer in storage,
    # while its operation hash and artifact set belong to a different candidate.
    stale.requested_at = "2099-12-31T23:59:59+00:00"
    ApprovalRepository(case["db"]).save(stale)

    recovered = _recover(case)
    assert recovered["approval_id"] == current_approval.id
    result = case["engine"].retry_task(_WORKFLOW, _ACCEPT)

    assert result.status == WorkflowStatus.COMPLETED
    approval_ids = {
        item.id
        for item in ApprovalRepository(case["db"]).list_by_workflow(_WORKFLOW)
        if item.task_id == _ACCEPT
    }
    assert approval_ids == {current_approval.id, stale.id}
    assert ApprovalRepository(case["db"]).get(current_approval.id).status == ApprovalStatus.APPROVED
    assert case["game_target"].read_bytes() == _AFTER
    assert (
        len(
            [
                item
                for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
                if item.artifact_type == "factory_application"
            ]
        )
        == 1
    )
