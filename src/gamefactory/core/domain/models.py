"""Domain models for AI Game Factory.

Defines core entities: Project, Workflow, Task, Execution, ApprovalRequest,
Artifact, Evidence, QualityGate, and AuditEvent.
No external database, vendor, or CLI dependencies are imported here.
"""

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


def utc_now_iso() -> str:
    """Return current UTC timestamp in ISO 8601 format."""
    return datetime.now(UTC).isoformat()


def generate_id(prefix: str) -> str:
    """Generate a unique prefixed identifier."""
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


class TaskStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class WorkflowStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class ApprovalStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"


class CostClass(StrEnum):
    LOCAL = "LOCAL"
    FREE_EXTERNAL = "FREE_EXTERNAL"
    METERED = "METERED"
    PAID = "PAID"
    EXPENSIVE = "EXPENSIVE"


class GateStatus(StrEnum):
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"


class ExecutionStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"


@dataclass
class Project:
    """Represents a game project managed by AI Game Factory."""

    id: str
    name: str
    engine_type: str
    root_path: str
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Task:
    """A bounded unit of work within a workflow DAG."""

    id: str
    workflow_id: str
    name: str
    task_type: str
    cost_class: CostClass = CostClass.LOCAL
    depends_on: list[str] = field(default_factory=list)
    status: TaskStatus = TaskStatus.PENDING
    parameters: dict[str, Any] = field(default_factory=dict)
    max_retries: int = 1
    timeout_seconds: float = 60.0
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def __post_init__(self) -> None:
        if not self.id.strip() or not self.workflow_id.strip() or not self.task_type.strip():
            raise ValueError("Task id, workflow_id, and task_type are required")
        if not isinstance(self.cost_class, CostClass) or not isinstance(self.status, TaskStatus):
            raise ValueError("Task cost_class and status must use their declared enums")
        if self.max_retries < 0 or self.timeout_seconds <= 0:
            raise ValueError("Task retry limit must be non-negative and timeout must be positive")
        json.dumps(self.parameters, allow_nan=False)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["cost_class"] = self.cost_class.value
        data["status"] = self.status.value
        return data


@dataclass
class Workflow:
    """A workflow DAG coordinating a set of tasks."""

    id: str
    project_id: str
    name: str
    status: WorkflowStatus = WorkflowStatus.PENDING
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class Execution:
    """A single execution attempt for a specific task."""

    id: str
    task_id: str
    attempt_number: int
    status: ExecutionStatus = ExecutionStatus.RUNNING
    started_at: str = field(default_factory=utc_now_iso)
    completed_at: str | None = None
    external_op_id: str | None = None
    pid: int | None = None
    host: str | None = None
    error_message: str | None = None
    exit_code: int | None = None
    stdout: str | None = None
    stderr: str | None = None
    cost: float = 0.0
    estimated_cost: float = 0.0
    cost_unit: str = "provider_units"
    provider: str | None = None
    retryable: bool = False

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class ApprovalRequest:
    """A human-in-the-loop approval record binding immutable task inputs."""

    id: str
    workflow_id: str
    task_id: str
    approval_type: str
    status: ApprovalStatus = ApprovalStatus.PENDING
    reason: str = ""
    cost_class: CostClass = CostClass.LOCAL
    operation_hash: str = ""
    actor: str | None = None
    comment: str | None = None
    requested_at: str = field(default_factory=utc_now_iso)
    decided_at: str | None = None
    artifact_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        data["cost_class"] = self.cost_class.value
        return data


@dataclass
class Artifact:
    """A durable asset, report, or file generated during workflow execution."""

    id: str
    workflow_id: str
    task_id: str
    artifact_type: str
    producer: str
    relative_path: str
    content_hash: str
    file_size: int = 0
    validation_state: str = "PENDING"
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Evidence:
    """Verification evidence associated with an execution attempt or gate."""

    id: str
    task_id: str
    execution_id: str
    evidence_type: str
    summary: str
    raw_data: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utc_now_iso)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualityGate:
    """A quality checkpoint evaluated before marking a task COMPLETED."""

    id: str
    task_id: str
    gate_type: str
    status: GateStatus = GateStatus.PENDING
    evaluated_at: str | None = None
    reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        return data


@dataclass
class AuditEvent:
    """Append-only record of important actions and transitions."""

    id: str
    entity_type: str
    entity_id: str
    action: str
    actor: str
    timestamp: str = field(default_factory=utc_now_iso)
    previous_state: str | None = None
    new_state: str | None = None
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
