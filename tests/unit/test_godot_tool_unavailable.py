"""Unit tests for pre-launch Godot executable availability validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from gamefactory.adapters.persistence.repositories import TaskRepository
from gamefactory.core.domain.errors import (
    EngineImportFailedError,
    ToolExecutionError,
    ToolUnavailableError,
)
from gamefactory.core.domain.models import (
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.execution.process_runner import CommandResult
from gamefactory.workflows.asset_production import run_asset_in_godot
from tests.integration.test_asset_workflow import _at_paid_gate, _decision, _setup


class FailingRunner:
    """Runner double that fails tests if any process execution is attempted."""

    def run(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("ProcessRunner.run() must not be called before pre-launch validation")


class ImportFailingRunner:
    """Runner double returning non-zero exit for the Godot import command."""

    def __init__(self) -> None:
        self.calls: list[Any] = []

    def run(self, request: Any) -> CommandResult:
        self.calls.append(request)
        return CommandResult(
            exit_code=1,
            stdout="Fake Godot initializing editor...\n",
            stderr="ERROR: Failed to import 3D asset\n",
            duration_seconds=0.05,
        )


@pytest.mark.parametrize("missing_path", [None, ""])
def test_missing_godot_path_raises_tool_unavailable(
    tmp_path: Path, missing_path: str | None
) -> None:
    root = tmp_path / "project"
    root.mkdir()
    workflow = Workflow(
        id="WF-TEST-1", project_id="asset-test", name="Asset", status=WorkflowStatus.RUNNING
    )
    task = Task(
        id="WF-TEST-1-GODOT",
        workflow_id="WF-TEST-1",
        name="Asset Godot Stage",
        task_type="asset_godot",
        status=TaskStatus.RUNNING,
    )
    execution = Execution(
        id=generate_id("EXEC"),
        task_id=task.id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
    )

    with pytest.raises(ToolUnavailableError) as exc_info:
        run_asset_in_godot(
            root,
            artifacts=None,
            artifact_manager=None,
            workflow=workflow,
            task=task,
            execution=execution,
            godot_path=missing_path,
            runner=FailingRunner(),
        )

    err = exc_info.value
    assert isinstance(err, ToolExecutionError)
    assert err.code == "TOOL_UNAVAILABLE"
    assert err.tool == "godot"
    assert err.reason == "executable_missing"
    assert err.configured_path is None
    assert err.task_id == task.id
    assert err.details["tool"] == "godot"
    assert err.details["reason"] == "executable_missing"
    assert err.details["task_id"] == task.id
    assert "configured_path" not in err.details
    assert "Godot executable is required for asset runtime verification" in str(err)

    scratch = root / ".gamefactory/scratch"
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_nonexistent_configured_path_raises_tool_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    workflow = Workflow(
        id="WF-TEST-1", project_id="asset-test", name="Asset", status=WorkflowStatus.RUNNING
    )
    task = Task(
        id="WF-TEST-1-GODOT",
        workflow_id="WF-TEST-1",
        name="Asset Godot Stage",
        task_type="asset_godot",
        status=TaskStatus.RUNNING,
    )
    execution = Execution(
        id=generate_id("EXEC"),
        task_id=task.id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
    )
    nonexistent = str(tmp_path / "missing_dir" / "godot.exe")

    with pytest.raises(ToolUnavailableError) as exc_info:
        run_asset_in_godot(
            root,
            artifacts=None,
            artifact_manager=None,
            workflow=workflow,
            task=task,
            execution=execution,
            godot_path=nonexistent,
            runner=FailingRunner(),
        )

    err = exc_info.value
    assert isinstance(err, ToolExecutionError)
    assert err.code == "TOOL_UNAVAILABLE"
    assert err.tool == "godot"
    assert err.reason == "executable_not_found"
    assert err.configured_path == nonexistent
    assert err.task_id == task.id
    assert err.details["tool"] == "godot"
    assert err.details["reason"] == "executable_not_found"
    assert err.details["configured_path"] == nonexistent
    assert err.details["task_id"] == task.id

    scratch = root / ".gamefactory/scratch"
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_directory_configured_path_raises_tool_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    fake_godot_dir = tmp_path / "Godot.exe"
    fake_godot_dir.mkdir()
    workflow = Workflow(
        id="WF-TEST-1", project_id="asset-test", name="Asset", status=WorkflowStatus.RUNNING
    )
    task = Task(
        id="WF-TEST-1-GODOT",
        workflow_id="WF-TEST-1",
        name="Asset Godot Stage",
        task_type="asset_godot",
        status=TaskStatus.RUNNING,
    )
    execution = Execution(
        id=generate_id("EXEC"),
        task_id=task.id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
    )

    with pytest.raises(ToolUnavailableError) as exc_info:
        run_asset_in_godot(
            root,
            artifacts=None,
            artifact_manager=None,
            workflow=workflow,
            task=task,
            execution=execution,
            godot_path=str(fake_godot_dir),
            runner=FailingRunner(),
        )

    err = exc_info.value
    assert isinstance(err, ToolExecutionError)
    assert err.code == "TOOL_UNAVAILABLE"
    assert err.tool == "godot"
    assert err.reason == "executable_not_file"
    assert err.configured_path == str(fake_godot_dir)
    assert err.task_id == task.id
    assert err.details["tool"] == "godot"
    assert err.details["reason"] == "executable_not_file"
    assert err.details["configured_path"] == str(fake_godot_dir)
    assert err.details["task_id"] == task.id

    scratch = root / ".gamefactory/scratch"
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_non_executable_configured_path_raises_tool_unavailable(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    fake_godot_file = tmp_path / "Godot.exe"
    fake_godot_file.write_text("mock executable")
    workflow = Workflow(
        id="WF-TEST-1", project_id="asset-test", name="Asset", status=WorkflowStatus.RUNNING
    )
    task = Task(
        id="WF-TEST-1-GODOT",
        workflow_id="WF-TEST-1",
        name="Asset Godot Stage",
        task_type="asset_godot",
        status=TaskStatus.RUNNING,
    )
    execution = Execution(
        id=generate_id("EXEC"),
        task_id=task.id,
        attempt_number=1,
        status=ExecutionStatus.RUNNING,
    )

    with patch("os.access", return_value=False):
        with pytest.raises(ToolUnavailableError) as exc_info:
            run_asset_in_godot(
                root,
                artifacts=None,
                artifact_manager=None,
                workflow=workflow,
                task=task,
                execution=execution,
                godot_path=str(fake_godot_file),
                runner=FailingRunner(),
            )

    err = exc_info.value
    assert isinstance(err, ToolExecutionError)
    assert err.code == "TOOL_UNAVAILABLE"
    assert err.tool == "godot"
    assert err.reason == "executable_not_executable"
    assert err.configured_path == str(fake_godot_file)
    assert err.task_id == task.id
    assert err.details["tool"] == "godot"
    assert err.details["reason"] == "executable_not_executable"
    assert err.details["configured_path"] == str(fake_godot_file)
    assert err.details["task_id"] == task.id

    scratch = root / ".gamefactory/scratch"
    assert not scratch.exists() or list(scratch.iterdir()) == []


def test_post_launch_import_failure_raises_engine_import_failed(tmp_path: Path) -> None:
    engine, db, workflow_id, fake, handlers = _setup(tmp_path, stub_downstream=True)
    paid_id = _at_paid_gate(engine, db, workflow_id)
    _decision(engine, db, paid_id, approve=True)

    # Run through validate to ensure processed GLB and validation report artifacts exist
    engine.run_workflow(workflow_id)

    workflow = engine.wf_repo.get(workflow_id)
    assert workflow is not None
    godot_task = next(
        t for t in TaskRepository(db).list_by_workflow(workflow_id) if t.task_type == "asset_godot"
    )
    execution = Execution(
        id=generate_id("EXEC"),
        task_id=godot_task.id,
        attempt_number=2,
        status=ExecutionStatus.RUNNING,
    )
    engine.exec_repo.save(execution)

    valid_godot = tmp_path / "real_godot.exe"
    valid_godot.write_text("mock executable")
    try:
        valid_godot.chmod(0o755)
    except OSError:
        pass

    runner = ImportFailingRunner()
    with pytest.raises(EngineImportFailedError) as exc_info:
        run_asset_in_godot(
            handlers.root,
            handlers.artifacts,
            handlers.artifact_manager,
            workflow,
            godot_task,
            execution,
            str(valid_godot),
            runner,
        )

    err = exc_info.value
    assert not isinstance(err, ToolExecutionError)
    assert not issubclass(EngineImportFailedError, ToolExecutionError)
    assert err.code == "ENGINE_IMPORT_FAILED"
    assert len(runner.calls) == 1
    assert "--import" in runner.calls[0].args
