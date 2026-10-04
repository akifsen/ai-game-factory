from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    QualityGateRepository,
    WorkflowRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import (
    Artifact,
    Evidence,
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
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.handlers import TaskHandlerRegistry
from gamefactory.workflows.project_operations import (
    _release_payload,
    build_project_operation_workflow,
    register_project_operation_handlers,
)


def test_project_workflow_registers_and_runs_read_only_discovery(tmp_path: Path) -> None:
    project = tmp_path / "game"
    project.mkdir()
    (project / "project.godot").write_text("config_version=5\n")
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(
        Project(id="project-1", name="Game", engine_type="godot", root_path=str(tmp_path))
    )
    registry = TaskHandlerRegistry()
    artifacts, evidence = ArtifactRepository(db), EvidenceRepository(db)
    gates, executions = QualityGateRepository(db), ExecutionRepository(db)
    manager = ArtifactManager(tmp_path)
    register_project_operation_handlers(
        registry, tmp_path, artifacts, evidence, gates, executions, manager
    )
    engine = WorkflowEngine(tmp_path, db, handler_registry=registry)
    workflow, tasks = build_project_operation_workflow(
        "project-1", project, "discover", {"include_git": False}
    )
    engine.register_workflow(workflow, tasks)
    assert len(tasks) == 2 and tasks[-1].task_type == "record_evidence"
    assert engine.run_workflow(workflow.id).status == WorkflowStatus.COMPLETED
    with pytest.raises(ValidationError):
        build_project_operation_workflow("project-1", project, "publish")


def test_release_rejects_receipt_whose_attempt_has_no_own_handler_evidence(tmp_path: Path) -> None:
    root = tmp_path / "game"
    root.mkdir()
    db = Database(tmp_path / "factory.db")
    MigrationRunner(db).apply_all()
    ProjectRepository(db).save(
        Project(id="project-1", name="Game", engine_type="godot", root_path=str(root))
    )
    manager, artifact_repo = ArtifactManager(root), ArtifactRepository(db)
    evidence_repo, gates, executions = (
        EvidenceRepository(db),
        QualityGateRepository(db),
        ExecutionRepository(db),
    )

    source_wf = Workflow(
        id="wf-source",
        project_id="project-1",
        name="source export",
        status=WorkflowStatus.COMPLETED,
    )
    source_task = Task(
        id="task-source",
        workflow_id=source_wf.id,
        name="export",
        task_type="godot_export",
        status=TaskStatus.COMPLETED,
    )
    WorkflowRepository(db).save_with_tasks(source_wf, [source_task])
    source_execution = Execution(
        id="attempt-source",
        task_id=source_task.id,
        attempt_number=1,
        status=ExecutionStatus.COMPLETED,
    )
    executions.save(source_execution)

    def create_attempt(attempt_id: str, workflow_id: str, task: Task) -> dict[str, object]:
        output_dir = root / ".gamefactory" / "attempts" / attempt_id
        output_dir.mkdir(parents=True)
        binary = output_dir / "game.bin"
        binary.write_bytes(b"exported" + attempt_id.encode())
        receipt: dict[str, object] = {
            "schema_version": 1,
            "operation": "export",
            "attempt_id": attempt_id,
            "execution_id": attempt_id,
            "workflow_id": workflow_id,
            "task_id": task.id,
            "exit_code": 0,
            "timed_out": False,
            "process": {"cleanup_completed": True},
            "source_manifest_sha256": "a" * 64,
            "executable_sha256": "b" * 64,
            "preset": "Linux Release",
            "status": "succeeded",
            "output": {
                "files": [
                    {
                        "path": binary.relative_to(output_dir).as_posix(),
                        "size": binary.stat().st_size,
                        "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
                    }
                ]
            },
        }
        (output_dir / "operation-result.json").write_text(json.dumps(receipt), encoding="utf-8")
        return receipt

    source_receipt = create_attempt(source_execution.id, source_wf.id, source_task)
    result_path = root / ".gamefactory" / "artifact-records" / "source-result.json"
    result_path.parent.mkdir(parents=True)
    result_path.write_text(json.dumps(source_receipt), encoding="utf-8")
    source_artifact = Artifact(
        id="source-result-artifact",
        workflow_id=source_wf.id,
        task_id=source_task.id,
        artifact_type="godot_export-result",
        producer="test",
        relative_path=result_path.relative_to(root).as_posix(),
        content_hash=hashlib.sha256(result_path.read_bytes()).hexdigest(),
        file_size=result_path.stat().st_size,
    )
    artifact_repo.save(source_artifact)
    verified = manager.verify_artifact_integrity(source_artifact)
    source_gate = QualityGate(
        id="source-handler-gate",
        task_id=source_task.id,
        gate_type="handler:godot_export",
        status=GateStatus.PASSED,
    )
    gates.save(source_gate)
    evidence_repo.save(
        Evidence(
            id="source-handler-evidence",
            task_id=source_task.id,
            execution_id=source_execution.id,
            evidence_type="handler:godot_export",
            summary="verified source attempt",
            raw_data={"gate_id": source_gate.id, "artifacts": [verified]},
        )
    )

    other_wf = Workflow(
        id="wf-other", project_id="project-1", name="other export", status=WorkflowStatus.COMPLETED
    )
    other_task = Task(
        id="task-other",
        workflow_id=other_wf.id,
        name="export",
        task_type="godot_export",
        status=TaskStatus.COMPLETED,
    )
    WorkflowRepository(db).save_with_tasks(other_wf, [other_task])
    other_execution = Execution(
        id="attempt-other",
        task_id=other_task.id,
        attempt_number=1,
        status=ExecutionStatus.COMPLETED,
    )
    executions.save(other_execution)
    create_attempt(other_execution.id, other_wf.id, other_task)

    with pytest.raises(ValidationError, match="execution-bound handler evidence"):
        _release_payload(root, other_execution.id, executions, gates, artifact_repo, manager)
