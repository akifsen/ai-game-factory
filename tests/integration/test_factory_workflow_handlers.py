"""Cross-contract DAG invariants; no providers or game engines are invoked."""

import pytest

from gamefactory.core.domain.factory_workflow import FactoryWorkflowManifest
from gamefactory.workflows.factory_workflow import (
    ExecutorRegistry,
    GateExecutorRegistry,
    _artifact_matches_completed_attempt,
    _build_agent_contract,
    _factory_journal_relative,
    _read_factory_journal,
    build_workflow,
    register_factory_handlers,
)


def test_accept_interleaving_preserves_baseline_and_editor_bytes_and_marks_recovery_required(
    tmp_path, monkeypatch
):
    """An edit between snapshot and atomic capture must not be reported rolled back."""
    import json
    from pathlib import Path

    from gamefactory.agents.registry import AgentRegistry
    from gamefactory.core.domain.errors import ValidationError
    from gamefactory.core.domain.models import Execution, ExecutionStatus
    from gamefactory.workflows.factory_workflow import _read_factory_journal
    from gamefactory.workflows.handlers import TaskHandlerRegistry
    from tests.integration.test_factory_apply_recovery import (
        _BASE,
        _case,
    )

    case = _case(tmp_path, journal_status="COMMITTED", target_state="before")
    case["journal_path"].unlink()
    target = case["game_target"]
    concurrent_bytes = b"editor change between approval snapshot and capture"
    handlers = TaskHandlerRegistry()
    register_factory_handlers(
        handlers,
        project_root=case["root"],
        db=case["db"],
        executor_registry=ExecutorRegistry(),
        agent_registry=AgentRegistry(),
        gate_registry=GateExecutorRegistry(),
    )
    execution = Execution("accept-race-run", case["task"].id, 2, ExecutionStatus.RUNNING)

    import gamefactory.workflows.factory_workflow as factory_workflow

    real_replace = factory_workflow.os.replace
    raced = False

    def replace_with_concurrent_edit(source, destination):
        nonlocal raced
        if Path(source) == target and not raced:
            raced = True
            target.write_bytes(concurrent_bytes)
        real_replace(source, destination)

    monkeypatch.setattr(factory_workflow.os, "replace", replace_with_concurrent_edit)
    with pytest.raises(ValidationError, match="changed during acceptance"):
        handlers.get("factory_acceptance")(case["workflow"], case["task"], execution)

    journal_candidates = list(
        (case["root"] / ".gamefactory" / "operations" / "factory-apply").glob("*.json")
    )
    journal_path = next(
        path
        for path in journal_candidates
        if json.loads(path.read_text(encoding="utf-8")).get("execution_id") == execution.id
    )
    journal_relative = journal_path.relative_to(case["root"]).as_posix()
    journal_path, journal, _ = _read_factory_journal(case["root"], journal_relative)
    backup_path = case["root"] / Path(*journal["files"][0]["backup_path"].split("/"))
    assert raced
    assert journal["status"] == "ROLLBACK_REQUIRED"
    assert target.read_bytes() == concurrent_bytes
    assert backup_path.read_bytes() == _BASE


def test_combined_candidate_capture_precedes_vision_provider_and_final_visual_gate():
    manifest = FactoryWorkflowManifest.model_validate_json(
        """{
          "schema_version":"factory-workflow-1.0.0",
          "workflow_id":"wf-visual",
          "project_id":"project-visual",
          "name":"Combined candidate review",
          "tasks":[
            {"task_id":"writer","name":"Writer","family":"feature","kind":"code","executor_id":"local-code","agent_id":"code-agent","objective":"Propose level changes","prompt_template_id":"feature","prompt_template_version":"1.0.0","game_write":true,"output_scopes":["levels/arena.json"],"expected_artifacts":["levels/arena.json"],"required_gates":["code"]},
            {"task_id":"vision","name":"Visual review","family":"vision","kind":"vision","executor_id":"vision-api","agent_id":"vision-agent","objective":"Review the captured candidate","prompt_template_id":"visual","prompt_template_version":"1.0.0","dependencies":["writer"],"required_gates":["visual"],"inputs":[{"path":"evidence/current.png","source_task_id":"writer","source_gate":"visual","source_gate_scope":"combined_candidate","evidence_name":"candidate.png","purpose":"candidate screenshot"}]}
          ]
        }"""
    )
    workflow, tasks = build_workflow(manifest)
    by_id = {task.id: task for task in tasks}
    capture_id = "vision:combined-capture:visual"
    assert capture_id in by_id
    assert set(by_id[capture_id].depends_on) == {"writer"}
    assert capture_id in by_id["vision"].depends_on
    final_visual = next(
        task
        for task in tasks
        if task.task_type == "factory_quality_gate"
        and task.parameters.get("final_candidate")
        and task.parameters.get("gate") == "visual"
    )
    assert set(final_visual.parameters["candidate_writer_tasks"]) == {"writer"}
    assert any(
        task.task_type == "factory_visual_decision"
        and task.parameters.get("gate_task_id") == final_visual.id
        for task in tasks
    )
    vision_contract = _build_agent_contract(manifest.tasks[1], manifest.project_id, ())
    assert vision_contract.allowed_output_paths == ()
    assert vision_contract.max_output_files == 0
    assert vision_contract.max_output_bytes == 0


def test_recovered_provider_output_is_eligible_only_with_current_execution_hash_proof():
    envelope = {"execution_id": "original-provider-run", "request_fingerprint": "a" * 64}
    proof = {
        "artifact_ids": ["proposal-artifact"],
        "request_fingerprint": "a" * 64,
        "artifact_hashes": {"proposal-artifact": "b" * 64},
    }
    assert _artifact_matches_completed_attempt(
        envelope, "proposal-artifact", "b" * 64, "completed-recovery-attempt", proof
    )
    assert not _artifact_matches_completed_attempt(
        envelope, "proposal-artifact", "c" * 64, "completed-recovery-attempt", proof
    )
    assert not _artifact_matches_completed_attempt(
        envelope, "proposal-artifact", "b" * 64, "failed-latest-attempt", None
    )


def test_apply_journal_private_path_and_schema_are_strict(tmp_path):
    import hashlib
    import json

    from gamefactory.core.domain.errors import ValidationError

    relative = ".gamefactory/operations/factory-apply/task-run.json"
    journal_path = tmp_path / ".gamefactory" / "operations" / "factory-apply" / "task-run.json"
    journal_path.parent.mkdir(parents=True)
    output_hash = hashlib.sha256(b"candidate").hexdigest()
    report_hash = "b" * 64
    payload = {
        "schema_version": "factory-apply-journal-1.0.0",
        "workflow_id": "workflow",
        "task_id": "task",
        "execution_id": "execution",
        "approval_id": "approval",
        "operation_hash": "c" * 64,
        "approved_context": {
            "manifest_sha256": "e" * 64,
            "writer_tasks": ["writer"],
            "required_gate_tasks": ["writer:gate:code"],
            "required_decision_tasks": [],
            "candidate_sha256": "d" * 64,
            "candidate_evidence": [],
        },
        "candidate_sha256": "d" * 64,
        "writer_tasks": ["writer"],
        "required_gate_tasks": ["writer:gate:code"],
        "required_decision_tasks": [],
        "gate_reports": {
            "writer:gate:code": {
                "report_artifact_id": "gate-report",
                "report_sha256": report_hash,
                "candidate_sha256": "d" * 64,
            }
        },
        "status": "PREPARED",
        "files": [
            {
                "path": "levels/a.json",
                "before_sha256": None,
                "after_sha256": output_hash,
                "artifact_id": "output-artifact",
                "backup_path": None,
                "backup_sha256": None,
                "capture_path": None,
                "was_created": True,
            }
        ],
    }
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    path, loaded, digest = _read_factory_journal(tmp_path, _factory_journal_relative(relative))
    assert path == journal_path
    assert loaded["task_id"] == "task"
    assert digest == hashlib.sha256(journal_path.read_bytes()).hexdigest()
    with pytest.raises(ValidationError):
        _factory_journal_relative(".gamefactory/operations/factory-apply/../outside.json")

    payload["files"][0]["backup_path"] = (
        ".gamefactory/operations/factory-apply/other.backups/0000.backup"
    )
    journal_path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValidationError):
        _read_factory_journal(tmp_path, relative)


def test_apply_journal_requires_exact_gate_report_set(tmp_path):
    import json

    from gamefactory.core.domain.errors import ValidationError

    relative = ".gamefactory/operations/factory-apply/run.json"
    journal_path = tmp_path / relative
    journal_path.parent.mkdir(parents=True)
    journal_path.write_text(
        json.dumps({"schema_version": "factory-apply-journal-1.0.0"}), encoding="utf-8"
    )
    with pytest.raises(ValidationError):
        _read_factory_journal(tmp_path, relative)
