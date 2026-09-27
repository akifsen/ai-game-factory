"""State machines for Task and Workflow lifecycles.

Enforces deterministic transitions centrally. Tasks and workflows cannot
mutate themselves into arbitrary states without validation.
"""

from typing import ClassVar

from gamefactory.core.domain.errors import WorkflowError
from gamefactory.core.domain.models import TaskStatus, WorkflowStatus


class TaskStateMachine:
    """Validates and governs task state transitions."""

    _VALID_TRANSITIONS: ClassVar[dict[TaskStatus, set[TaskStatus]]] = {
        TaskStatus.PENDING: {TaskStatus.RUNNING, TaskStatus.BLOCKED},
        TaskStatus.RUNNING: {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED},
        TaskStatus.BLOCKED: {TaskStatus.RUNNING, TaskStatus.FAILED},
        TaskStatus.FAILED: {TaskStatus.PENDING, TaskStatus.RUNNING},  # Allowed via explicit retry
        TaskStatus.COMPLETED: set(),  # Terminal unless explicit re-run policy
    }

    @classmethod
    def can_transition(cls, current: TaskStatus, target: TaskStatus) -> bool:
        """Check if transition from current to target is allowed."""
        return target in cls._VALID_TRANSITIONS.get(current, set())

    @classmethod
    def validate_transition(cls, task_id: str, current: TaskStatus, target: TaskStatus) -> None:
        """Validate transition, raising WorkflowError if invalid."""
        if not cls.can_transition(current, target):
            raise WorkflowError(
                f"Invalid task transition for '{task_id}': cannot transition from {current.value} to {target.value}",
                details={"task_id": task_id, "current": current.value, "target": target.value},
            )


class WorkflowStateMachine:
    """Validates and governs workflow state transitions."""

    _VALID_TRANSITIONS: ClassVar[dict[WorkflowStatus, set[WorkflowStatus]]] = {
        WorkflowStatus.PENDING: {WorkflowStatus.RUNNING, WorkflowStatus.BLOCKED},
        WorkflowStatus.RUNNING: {
            WorkflowStatus.COMPLETED,
            WorkflowStatus.FAILED,
            WorkflowStatus.BLOCKED,
        },
        WorkflowStatus.BLOCKED: {WorkflowStatus.RUNNING, WorkflowStatus.FAILED},
        WorkflowStatus.FAILED: {WorkflowStatus.RUNNING},  # Allowed via retry
        WorkflowStatus.COMPLETED: set(),
    }

    @classmethod
    def can_transition(cls, current: WorkflowStatus, target: WorkflowStatus) -> bool:
        """Check if transition from current to target is allowed."""
        return target in cls._VALID_TRANSITIONS.get(current, set())

    @classmethod
    def validate_transition(
        cls, workflow_id: str, current: WorkflowStatus, target: WorkflowStatus
    ) -> None:
        """Validate transition, raising WorkflowError if invalid."""
        if not cls.can_transition(current, target):
            raise WorkflowError(
                f"Invalid workflow transition for '{workflow_id}': cannot transition from {current.value} to {target.value}",
                details={
                    "workflow_id": workflow_id,
                    "current": current.value,
                    "target": target.value,
                },
            )
