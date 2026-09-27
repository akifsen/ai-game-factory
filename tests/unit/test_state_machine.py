"""Unit tests for Task and Workflow state machines."""

import pytest

from gamefactory.core.domain.errors import WorkflowError
from gamefactory.core.domain.models import TaskStatus, WorkflowStatus
from gamefactory.core.domain.state_machine import TaskStateMachine, WorkflowStateMachine


class TestTaskStateMachine:
    def test_valid_transitions(self) -> None:
        valid_pairs = [
            (TaskStatus.PENDING, TaskStatus.RUNNING),
            (TaskStatus.PENDING, TaskStatus.BLOCKED),
            (TaskStatus.RUNNING, TaskStatus.COMPLETED),
            (TaskStatus.RUNNING, TaskStatus.FAILED),
            (TaskStatus.RUNNING, TaskStatus.BLOCKED),
            (TaskStatus.BLOCKED, TaskStatus.RUNNING),
            (TaskStatus.BLOCKED, TaskStatus.FAILED),
            (TaskStatus.FAILED, TaskStatus.RUNNING),  # retry
            (TaskStatus.FAILED, TaskStatus.PENDING),  # retry
        ]
        for src, dst in valid_pairs:
            assert TaskStateMachine.can_transition(src, dst)
            TaskStateMachine.validate_transition("T-1", src, dst)

    def test_invalid_transitions(self) -> None:
        invalid_pairs = [
            (TaskStatus.PENDING, TaskStatus.COMPLETED),
            (TaskStatus.PENDING, TaskStatus.FAILED),
            (TaskStatus.COMPLETED, TaskStatus.RUNNING),
            (TaskStatus.COMPLETED, TaskStatus.PENDING),
            (TaskStatus.COMPLETED, TaskStatus.FAILED),
            (TaskStatus.COMPLETED, TaskStatus.BLOCKED),
            (TaskStatus.FAILED, TaskStatus.COMPLETED),
            (TaskStatus.FAILED, TaskStatus.BLOCKED),
            (TaskStatus.BLOCKED, TaskStatus.COMPLETED),
        ]
        for src, dst in invalid_pairs:
            assert not TaskStateMachine.can_transition(src, dst)
            with pytest.raises(WorkflowError):
                TaskStateMachine.validate_transition("T-1", src, dst)


class TestWorkflowStateMachine:
    def test_valid_transitions(self) -> None:
        valid_pairs = [
            (WorkflowStatus.PENDING, WorkflowStatus.RUNNING),
            (WorkflowStatus.PENDING, WorkflowStatus.BLOCKED),
            (WorkflowStatus.RUNNING, WorkflowStatus.COMPLETED),
            (WorkflowStatus.RUNNING, WorkflowStatus.FAILED),
            (WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED),
            (WorkflowStatus.BLOCKED, WorkflowStatus.RUNNING),
            (WorkflowStatus.BLOCKED, WorkflowStatus.FAILED),
            (WorkflowStatus.FAILED, WorkflowStatus.RUNNING),  # retry
        ]
        for src, dst in valid_pairs:
            assert WorkflowStateMachine.can_transition(src, dst)
            WorkflowStateMachine.validate_transition("WF-1", src, dst)

    def test_invalid_transitions(self) -> None:
        invalid_pairs = [
            (WorkflowStatus.PENDING, WorkflowStatus.COMPLETED),
            (WorkflowStatus.PENDING, WorkflowStatus.FAILED),
            (WorkflowStatus.COMPLETED, WorkflowStatus.RUNNING),
            (WorkflowStatus.COMPLETED, WorkflowStatus.PENDING),
            (WorkflowStatus.COMPLETED, WorkflowStatus.BLOCKED),
            (WorkflowStatus.BLOCKED, WorkflowStatus.COMPLETED),
        ]
        for src, dst in invalid_pairs:
            assert not WorkflowStateMachine.can_transition(src, dst)
            with pytest.raises(WorkflowError):
                WorkflowStateMachine.validate_transition("WF-1", src, dst)
