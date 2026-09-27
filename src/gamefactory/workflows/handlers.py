"""Adapter-neutral, versioned contracts for bounded workflow task handlers."""

from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

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


class HandlerOperation(StrEnum):
    LOCAL_READ = "LOCAL_READ"
    REPOSITORY_WRITE = "REPOSITORY_WRITE"
    PROCESS_EXECUTION = "PROCESS_EXECUTION"
    VISUAL_REVIEW = "VISUAL_REVIEW"


class HandlerRecovery(StrEnum):
    SAFE_TO_RETRY = "SAFE_TO_RETRY"
    CONSERVATIVE_PROCESS = "CONSERVATIVE_PROCESS"


@dataclass(frozen=True)
class TaskHandlerMetadata:
    """Policy and recovery profile for a registered task handler."""

    operation: HandlerOperation = HandlerOperation.LOCAL_READ
    managed_write: bool = False
    recovery: HandlerRecovery = HandlerRecovery.SAFE_TO_RETRY
    approval_context: Callable[[Workflow, Task], dict[str, Any]] | None = None
    refresh_parameters: Callable[[Workflow, Task], dict[str, Any]] | None = None
    recovery_check: Callable[[Workflow, Task, Execution], bool] | None = None


@dataclass(frozen=True)
class RegisteredTaskHandler:
    handler: TaskHandler
    metadata: TaskHandlerMetadata


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
        self._handlers: dict[str, RegisteredTaskHandler] = {}

    def register(
        self,
        task_type: str,
        handler: TaskHandler,
        metadata: TaskHandlerMetadata | None = None,
    ) -> None:
        if not task_type or task_type in self._RESERVED or task_type in self._handlers:
            raise ValueError(f"Invalid, reserved, or duplicate task handler type: {task_type}")
        self._handlers[task_type] = RegisteredTaskHandler(
            handler, metadata or TaskHandlerMetadata()
        )

    def get(self, task_type: str) -> TaskHandler | None:
        registered = self._handlers.get(task_type)
        return registered.handler if registered else None

    def metadata(self, task_type: str) -> TaskHandlerMetadata | None:
        registered = self._handlers.get(task_type)
        return registered.metadata if registered else None
