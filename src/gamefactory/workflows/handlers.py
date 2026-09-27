"""Adapter-neutral, versioned contracts for bounded workflow task handlers."""

from dataclasses import dataclass, field
from typing import Protocol

from gamefactory.core.domain.models import Execution, Task, Workflow


@dataclass(frozen=True)
class TaskHandlerResult:
    """Untrusted handler output; the engine must independently verify its artifacts."""

    schema_version: int
    summary: str
    artifact_ids: list[str] = field(default_factory=list)

    def validate(self) -> None:
        if self.schema_version != 1:
            raise ValueError(f"Unsupported handler result schema version: {self.schema_version}")
        if not isinstance(self.summary, str) or not self.summary.strip():
            raise ValueError("Handler result summary must be a non-empty string")
        if not isinstance(self.artifact_ids, list) or any(
            not isinstance(item, str) or not item for item in self.artifact_ids
        ):
            raise ValueError("Handler result artifact_ids must be a list of non-empty strings")


class TaskHandler(Protocol):
    def __call__(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> TaskHandlerResult: ...


class TaskHandlerRegistry:
    """Explicit extension registry with reserved core task names protected."""

    _RESERVED = frozenset(
        {
            "inspect_project",
            "generate_concept",
            "validate_artifact",
            "paid_generation",
            "controlled_command",
            "record_evidence",
            "simulated_failure",
        }
    )

    def __init__(self) -> None:
        self._handlers: dict[str, TaskHandler] = {}

    def register(self, task_type: str, handler: TaskHandler) -> None:
        if not task_type or task_type in self._RESERVED or task_type in self._handlers:
            raise ValueError(f"Invalid, reserved, or duplicate task handler type: {task_type}")
        self._handlers[task_type] = handler

    def get(self, task_type: str) -> TaskHandler | None:
        return self._handlers.get(task_type)
