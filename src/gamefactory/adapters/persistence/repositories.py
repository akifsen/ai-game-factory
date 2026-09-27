"""Repository implementations for durable entity storage in SQLite.

Translates domain models to and from relational tables without exposing database
details to the domain layer.
"""

import json
from dataclasses import replace
from typing import Any

from gamefactory.adapters.persistence.database import Database
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    Artifact,
    AuditEvent,
    CostClass,
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
    generate_id,
    utc_now_iso,
)
from gamefactory.core.domain.state_machine import TaskStateMachine
from gamefactory.core.execution.redaction import (
    _SENSITIVE_ENV_SUBSTRINGS,
    redactor,
)


def _is_identity_key(key: Any) -> bool:
    """Keep database identities and hashes byte-for-byte stable."""
    normalized = str(key).strip().lower().replace("-", "_")
    return normalized in {"id", "hash", "fingerprint"} or normalized.endswith(
        ("_id", "_hash", "_fingerprint")
    )


def _sanitize_persisted_data(value: Any) -> Any:
    """Redact payload values while preserving structural key values and keys."""
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_identity_key(key):
                cleaned[key] = item
            elif any(part in str(key).upper() for part in _SENSITIVE_ENV_SUBSTRINGS):
                cleaned[key] = "[REDACTED]"
            else:
                cleaned[key] = _sanitize_persisted_data(item)
        return cleaned
    if isinstance(value, (list, tuple)):
        return [_sanitize_persisted_data(item) for item in value]
    if isinstance(value, str):
        return redactor.redact_text(value)
    return value


def _contains_task_secret(value: Any) -> bool:
    """Detect sensitive task input keys or known credentials without mutating inputs."""
    if isinstance(value, dict):
        for key, item in value.items():
            if not _is_identity_key(key) and any(
                part in str(key).upper() for part in _SENSITIVE_ENV_SUBSTRINGS
            ):
                return True
            if _contains_task_secret(item):
                return True
        return False
    if isinstance(value, (list, tuple)):
        return any(_contains_task_secret(item) for item in value)
    return isinstance(value, str) and redactor.redact_text(value) != value


def _validate_task_parameters(task: Task) -> None:
    if _contains_task_secret(task.parameters):
        raise ValidationError(
            "Task parameters contain a sensitive field or known credential; remove it before persistence",
            details={"task_id": task.id},
        )


def _redact_text(value: str | None) -> str | None:
    return redactor.redact_text(value) if value is not None else None


def _insert_audit_event(conn: Any, event: AuditEvent) -> None:
    """Persist an audit event through the same text/payload redaction boundary."""
    conn.execute(
        "INSERT INTO audit_events (id, entity_type, entity_id, action, actor, timestamp, previous_state, new_state, details_json) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            event.id or generate_id("AUDIT"),
            event.entity_type,
            event.entity_id,
            _redact_text(event.action),
            _redact_text(event.actor),
            event.timestamp,
            _redact_text(event.previous_state),
            _redact_text(event.new_state),
            json.dumps(_sanitize_persisted_data(event.details)),
        ),
    )


def _append_status_event(
    conn: Any,
    entity_type: str,
    entity_id: str,
    previous: str,
    current: str,
    details: dict[str, Any] | None = None,
) -> None:
    _insert_audit_event(
        conn,
        AuditEvent(
            id=generate_id("AUDIT"),
            entity_type=entity_type,
            entity_id=entity_id,
            action="STATE_CHANGED",
            actor="WorkflowEngine",
            timestamp=utc_now_iso(),
            previous_state=previous,
            new_state=current,
            details=details or {},
        ),
    )


class ProjectRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, project: Project) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO projects (id, name, engine_type, root_path, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name = excluded.name,
                    engine_type = excluded.engine_type,
                    root_path = excluded.root_path,
                    updated_at = excluded.updated_at;
                """,
                (
                    project.id,
                    project.name,
                    project.engine_type,
                    project.root_path,
                    project.created_at,
                    project.updated_at,
                ),
            )

    def get(self, project_id: str) -> Project | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM projects WHERE id = ?;", (project_id,)).fetchone()
            if not row:
                return None
            return Project(
                id=row["id"],
                name=row["name"],
                engine_type=row["engine_type"],
                root_path=row["root_path"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        finally:
            conn.close()

    def list_all(self) -> list[Project]:
        conn = self.db.connect()
        try:
            cursor = conn.execute("SELECT * FROM projects ORDER BY created_at DESC;")
            return [
                Project(
                    id=row["id"],
                    name=row["name"],
                    engine_type=row["engine_type"],
                    root_path=row["root_path"],
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()


class WorkflowRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, workflow: Workflow) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO workflows (id, project_id, name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    updated_at = excluded.updated_at;
                """,
                (
                    workflow.id,
                    workflow.project_id,
                    workflow.name,
                    workflow.status.value,
                    workflow.created_at,
                    workflow.updated_at,
                ),
            )

    def save_with_tasks(
        self, workflow: Workflow, tasks: list[Task], event: AuditEvent | None = None
    ) -> None:
        """Atomically register a workflow, its complete task set, and audit trail."""
        for task in tasks:
            _validate_task_parameters(task)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO workflows (id, project_id, name, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    workflow.id,
                    workflow.project_id,
                    workflow.name,
                    workflow.status.value,
                    workflow.created_at,
                    workflow.updated_at,
                ),
            )
            for task in tasks:
                conn.execute(
                    "INSERT INTO tasks (id, workflow_id, name, task_type, cost_class, depends_on_json, status, parameters_json, max_retries, timeout_seconds, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task.id,
                        task.workflow_id,
                        task.name,
                        task.task_type,
                        task.cost_class.value,
                        json.dumps(task.depends_on),
                        task.status.value,
                        json.dumps(task.parameters),
                        task.max_retries,
                        task.timeout_seconds,
                        task.created_at,
                        task.updated_at,
                    ),
                )
            if event is not None:
                _insert_audit_event(conn, event)

    def get(self, workflow_id: str) -> Workflow | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM workflows WHERE id = ?;", (workflow_id,)).fetchone()
            if not row:
                return None
            return Workflow(
                id=row["id"],
                project_id=row["project_id"],
                name=row["name"],
                status=WorkflowStatus(row["status"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        finally:
            conn.close()

    def list_by_project(self, project_id: str) -> list[Workflow]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM workflows WHERE project_id = ? ORDER BY created_at DESC;",
                (project_id,),
            )
            return [
                Workflow(
                    id=row["id"],
                    project_id=row["project_id"],
                    name=row["name"],
                    status=WorkflowStatus(row["status"]),
                    created_at=row["created_at"],
                    updated_at=row["updated_at"],
                )
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()

    def update_status(self, workflow_id: str, status: WorkflowStatus) -> None:
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM workflows WHERE id=?", (workflow_id,)).fetchone()
            if row is None or row["status"] == status.value:
                return
            conn.execute(
                "UPDATE workflows SET status=?, updated_at=? WHERE id=?",
                (status.value, utc_now_iso(), workflow_id),
            )
            _append_status_event(conn, "Workflow", workflow_id, row["status"], status.value)


class TaskRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, task: Task) -> None:
        _validate_task_parameters(task)
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO tasks (
                    id, workflow_id, name, task_type, cost_class, depends_on_json,
                    status, parameters_json, max_retries, timeout_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    updated_at = excluded.updated_at;
                """,
                (
                    task.id,
                    task.workflow_id,
                    task.name,
                    task.task_type,
                    task.cost_class.value,
                    json.dumps(task.depends_on),
                    task.status.value,
                    json.dumps(task.parameters),
                    task.max_retries,
                    task.timeout_seconds,
                    task.created_at,
                    task.updated_at,
                ),
            )

    def get(self, task_id: str) -> Task | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM tasks WHERE id = ?;", (task_id,)).fetchone()
            if not row:
                return None
            return self._row_to_task(row)
        finally:
            conn.close()

    def list_by_workflow(self, workflow_id: str) -> list[Task]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC;",
                (workflow_id,),
            )
            return [self._row_to_task(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def update_status(self, task_id: str, status: TaskStatus) -> None:
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None or row["status"] == status.value:
                return
            conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                (status.value, utc_now_iso(), task_id),
            )
            _append_status_event(conn, "Task", task_id, row["status"], status.value)

    def claim_execution(
        self, task_id: str, execution: Execution, project_id: str, project_budget: float
    ) -> bool:
        """Conditionally change PENDING/BLOCKED to RUNNING and insert attempt atomically."""
        with self.db.transaction() as conn:
            # Serialize budget reads with reservations from other workflow locks.
            conn.execute("BEGIN IMMEDIATE")
            spent = conn.execute(
                "SELECT COALESCE(SUM(e.cost), 0) AS total FROM executions e JOIN tasks t ON t.id=e.task_id JOIN workflows w ON w.id=t.workflow_id WHERE w.project_id=?",
                (project_id,),
            ).fetchone()["total"]
            if float(spent) + execution.cost > project_budget:
                from gamefactory.core.domain.errors import BudgetExceeded

                raise BudgetExceeded(
                    "Atomic project budget reservation would be exceeded",
                    details={
                        "spent": float(spent),
                        "estimate": execution.cost,
                        "budget": project_budget,
                    },
                )
            task_row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
            changed = conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND status IN (?, ?)",
                (
                    TaskStatus.RUNNING.value,
                    utc_now_iso(),
                    task_id,
                    TaskStatus.PENDING.value,
                    TaskStatus.BLOCKED.value,
                ),
            ).rowcount
            if changed != 1:
                return False
            conn.execute(
                "INSERT INTO executions (id, task_id, attempt_number, status, started_at, completed_at, external_op_id, pid, host, error_message, exit_code, stdout, stderr, cost, estimated_cost, cost_unit, provider, retryable) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    execution.id,
                    execution.task_id,
                    execution.attempt_number,
                    execution.status.value,
                    execution.started_at,
                    execution.completed_at,
                    execution.external_op_id,
                    execution.pid,
                    execution.host,
                    _redact_text(execution.error_message),
                    execution.exit_code,
                    _redact_text(execution.stdout),
                    _redact_text(execution.stderr),
                    execution.cost,
                    execution.estimated_cost,
                    execution.cost_unit,
                    execution.provider,
                    int(execution.retryable),
                ),
            )
            _append_status_event(
                conn,
                "Task",
                task_id,
                task_row["status"],
                TaskStatus.RUNNING.value,
                {"execution_id": execution.id, "attempt": execution.attempt_number},
            )
            return True

    def _row_to_task(self, row: Any) -> Task:
        return Task(
            id=row["id"],
            workflow_id=row["workflow_id"],
            name=row["name"],
            task_type=row["task_type"],
            cost_class=CostClass(row["cost_class"]),
            depends_on=json.loads(row["depends_on_json"]),
            status=TaskStatus(row["status"]),
            parameters=json.loads(row["parameters_json"]),
            max_retries=row["max_retries"],
            timeout_seconds=row["timeout_seconds"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )


class ExecutionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, execution: Execution) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO executions (
                    id, task_id, attempt_number, status, started_at, completed_at,
                    external_op_id, pid, host, error_message, exit_code, stdout, stderr, cost,
                    estimated_cost, cost_unit, provider, retryable
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    completed_at = excluded.completed_at,
                    external_op_id = excluded.external_op_id,
                    pid = excluded.pid,
                    host = excluded.host,
                    error_message = excluded.error_message,
                    exit_code = excluded.exit_code,
                    stdout = excluded.stdout,
                    stderr = excluded.stderr,
                    cost = excluded.cost,
                    estimated_cost = excluded.estimated_cost,
                    cost_unit = excluded.cost_unit,
                    provider = excluded.provider,
                    retryable = excluded.retryable;
                """,
                (
                    execution.id,
                    execution.task_id,
                    execution.attempt_number,
                    execution.status.value,
                    execution.started_at,
                    execution.completed_at,
                    execution.external_op_id,
                    execution.pid,
                    execution.host,
                    _redact_text(execution.error_message),
                    execution.exit_code,
                    _redact_text(execution.stdout),
                    _redact_text(execution.stderr),
                    execution.cost,
                    execution.estimated_cost,
                    execution.cost_unit,
                    execution.provider,
                    int(execution.retryable),
                ),
            )

    def finalize_task(
        self, task: Task, execution: Execution, event: AuditEvent | None = None
    ) -> None:
        """Commit attempt outcome and task terminal/block state as one unit."""
        TaskStateMachine.validate_transition(task.id, TaskStatus.RUNNING, task.status)
        if event is not None:
            event = replace(
                event,
                previous_state=TaskStatus.RUNNING.value,
                new_state=task.status.value,
                details={**event.details, "execution_id": execution.id},
            )
        with self.db.transaction() as conn:
            changed_attempt = conn.execute(
                "UPDATE executions SET status=?, completed_at=?, external_op_id=?, error_message=?, exit_code=?, stdout=?, stderr=?, cost=?, estimated_cost=?, cost_unit=?, provider=?, retryable=? WHERE id=? AND status=?",
                (
                    execution.status.value,
                    execution.completed_at,
                    execution.external_op_id,
                    _redact_text(execution.error_message),
                    execution.exit_code,
                    _redact_text(execution.stdout),
                    _redact_text(execution.stderr),
                    execution.cost,
                    execution.estimated_cost,
                    execution.cost_unit,
                    execution.provider,
                    int(execution.retryable),
                    execution.id,
                    ExecutionStatus.RUNNING.value,
                ),
            ).rowcount
            if changed_attempt != 1:
                raise ValueError(f"Execution {execution.id} was not RUNNING during finalization")
            changed = conn.execute(
                "UPDATE tasks SET status=?, updated_at=? WHERE id=? AND status=?",
                (task.status.value, utc_now_iso(), task.id, TaskStatus.RUNNING.value),
            ).rowcount
            if changed != 1:
                raise ValueError(f"Task {task.id} was not RUNNING during finalization")
            if event is not None:
                _insert_audit_event(conn, event)

    def get(self, execution_id: str) -> Execution | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM executions WHERE id = ?;", (execution_id,)).fetchone()
            if not row:
                return None
            return self._row_to_execution(row)
        finally:
            conn.close()

    def list_by_task(self, task_id: str) -> list[Execution]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM executions WHERE task_id = ? ORDER BY attempt_number ASC;",
                (task_id,),
            )
            return [self._row_to_execution(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_latest_attempt(self, task_id: str) -> Execution | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM executions WHERE task_id = ? ORDER BY attempt_number DESC LIMIT 1;",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_execution(row)
        finally:
            conn.close()

    def _row_to_execution(self, row: Any) -> Execution:
        return Execution(
            id=row["id"],
            task_id=row["task_id"],
            attempt_number=row["attempt_number"],
            status=ExecutionStatus(row["status"]),
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            external_op_id=row["external_op_id"],
            pid=row["pid"],
            host=row["host"],
            error_message=row["error_message"],
            exit_code=row["exit_code"],
            stdout=row["stdout"],
            stderr=row["stderr"],
            cost=row["cost"],
            estimated_cost=row["estimated_cost"],
            cost_unit=row["cost_unit"],
            provider=row["provider"],
            retryable=bool(row["retryable"]),
        )


class ApprovalRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, approval: ApprovalRequest) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO approvals (
                    id, workflow_id, task_id, approval_type, status, reason, cost_class,
                    operation_hash, actor, comment, requested_at, decided_at, artifact_ids_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    actor = excluded.actor,
                    comment = excluded.comment,
                    decided_at = excluded.decided_at;
                """,
                (
                    approval.id,
                    approval.workflow_id,
                    approval.task_id,
                    approval.approval_type,
                    approval.status.value,
                    _redact_text(approval.reason),
                    approval.cost_class.value,
                    approval.operation_hash,
                    _redact_text(approval.actor),
                    _redact_text(approval.comment),
                    approval.requested_at,
                    approval.decided_at,
                    json.dumps(approval.artifact_ids),
                ),
            )

    def decide_if_pending(self, approval: ApprovalRequest, event: AuditEvent) -> bool:
        """Compare-and-set approval decision and audit event atomically."""
        with self.db.transaction() as conn:
            changed = conn.execute(
                "UPDATE approvals SET status=?, actor=?, comment=?, decided_at=? WHERE id=? AND status=?",
                (
                    approval.status.value,
                    _redact_text(approval.actor),
                    _redact_text(approval.comment),
                    approval.decided_at,
                    approval.id,
                    ApprovalStatus.PENDING.value,
                ),
            ).rowcount
            if changed != 1:
                return False
            _insert_audit_event(conn, event)
            return True

    def get(self, approval_id: str) -> ApprovalRequest | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM approvals WHERE id = ?;", (approval_id,)).fetchone()
            if not row:
                return None
            return self._row_to_approval(row)
        finally:
            conn.close()

    def list_by_workflow(self, workflow_id: str) -> list[ApprovalRequest]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM approvals WHERE workflow_id = ? ORDER BY requested_at DESC;",
                (workflow_id,),
            )
            return [self._row_to_approval(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def list_pending(self, workflow_id: str | None = None) -> list[ApprovalRequest]:
        conn = self.db.connect()
        try:
            if workflow_id:
                cursor = conn.execute(
                    "SELECT * FROM approvals WHERE workflow_id = ? AND status = ? ORDER BY requested_at ASC;",
                    (workflow_id, ApprovalStatus.PENDING.value),
                )
            else:
                cursor = conn.execute(
                    "SELECT * FROM approvals WHERE status = ? ORDER BY requested_at ASC;",
                    (ApprovalStatus.PENDING.value,),
                )
            return [self._row_to_approval(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _row_to_approval(self, row: Any) -> ApprovalRequest:
        return ApprovalRequest(
            id=row["id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            approval_type=row["approval_type"],
            status=ApprovalStatus(row["status"]),
            reason=row["reason"],
            cost_class=CostClass(row["cost_class"]),
            operation_hash=row["operation_hash"],
            actor=row["actor"],
            comment=row["comment"],
            requested_at=row["requested_at"],
            decided_at=row["decided_at"],
            artifact_ids=json.loads(row["artifact_ids_json"]),
        )


class ArtifactRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, artifact: Artifact) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                    id, workflow_id, task_id, artifact_type, producer, relative_path,
                    content_hash, file_size, validation_state, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    validation_state = excluded.validation_state,
                    content_hash = excluded.content_hash,
                    file_size = excluded.file_size;
                """,
                (
                    artifact.id,
                    artifact.workflow_id,
                    artifact.task_id,
                    artifact.artifact_type,
                    artifact.producer,
                    artifact.relative_path,
                    artifact.content_hash,
                    artifact.file_size,
                    artifact.validation_state,
                    artifact.created_at,
                ),
            )

    def get(self, artifact_id: str) -> Artifact | None:
        conn = self.db.connect()
        try:
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?;", (artifact_id,)).fetchone()
            if not row:
                return None
            return self._row_to_artifact(row)
        finally:
            conn.close()

    def list_by_workflow(self, workflow_id: str) -> list[Artifact]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM artifacts WHERE workflow_id = ? ORDER BY created_at ASC;",
                (workflow_id,),
            )
            return [self._row_to_artifact(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def list_by_task(self, task_id: str) -> list[Artifact]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at ASC;",
                (task_id,),
            )
            return [self._row_to_artifact(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def _row_to_artifact(self, row: Any) -> Artifact:
        return Artifact(
            id=row["id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            artifact_type=row["artifact_type"],
            producer=row["producer"],
            relative_path=row["relative_path"],
            content_hash=row["content_hash"],
            file_size=row["file_size"],
            validation_state=row["validation_state"],
            created_at=row["created_at"],
        )


class EvidenceRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, evidence: Evidence) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO evidences (id, task_id, execution_id, evidence_type, summary, raw_data_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    evidence.id,
                    evidence.task_id,
                    evidence.execution_id,
                    evidence.evidence_type,
                    _redact_text(evidence.summary),
                    json.dumps(_sanitize_persisted_data(evidence.raw_data)),
                    evidence.created_at,
                ),
            )

    def list_by_task(self, task_id: str) -> list[Evidence]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM evidences WHERE task_id = ? ORDER BY created_at ASC;",
                (task_id,),
            )
            return [
                Evidence(
                    id=row["id"],
                    task_id=row["task_id"],
                    execution_id=row["execution_id"],
                    evidence_type=row["evidence_type"],
                    summary=row["summary"],
                    raw_data=json.loads(row["raw_data_json"]),
                    created_at=row["created_at"],
                )
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()


class QualityGateRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, gate: QualityGate) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO quality_gates (id, task_id, gate_type, status, evaluated_at, reason)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    evaluated_at = excluded.evaluated_at,
                    reason = excluded.reason;
                """,
                (
                    gate.id,
                    gate.task_id,
                    gate.gate_type,
                    gate.status.value,
                    gate.evaluated_at,
                    _redact_text(gate.reason),
                ),
            )

    def list_by_task(self, task_id: str) -> list[QualityGate]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM quality_gates WHERE task_id = ? ORDER BY id ASC;",
                (task_id,),
            )
            return [
                QualityGate(
                    id=row["id"],
                    task_id=row["task_id"],
                    gate_type=row["gate_type"],
                    status=GateStatus(row["status"]),
                    evaluated_at=row["evaluated_at"],
                    reason=row["reason"],
                )
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()


class AuditLogRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def append(self, event: AuditEvent) -> None:
        with self.db.transaction() as conn:
            _insert_audit_event(conn, event)

    def list_by_entity(self, entity_type: str, entity_id: str) -> list[AuditEvent]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM audit_events WHERE entity_type = ? AND entity_id = ? ORDER BY timestamp ASC;",
                (entity_type, entity_id),
            )
            return [
                AuditEvent(
                    id=row["id"],
                    entity_type=row["entity_type"],
                    entity_id=row["entity_id"],
                    action=row["action"],
                    actor=row["actor"],
                    timestamp=row["timestamp"],
                    previous_state=row["previous_state"],
                    new_state=row["new_state"],
                    details=json.loads(row["details_json"]),
                )
                for row in cursor.fetchall()
            ]
        finally:
            conn.close()


class ProviderInvocationRepository:
    """Append-only ledger of actual calls initiated through a provider adapter."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def record(self, workflow_id: str, task_id: str, provider: str, operation_hash: str) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO provider_invocations (id, workflow_id, task_id, provider, operation_hash, invoked_at) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    generate_id("CALL"),
                    workflow_id,
                    task_id,
                    provider,
                    operation_hash,
                    utc_now_iso(),
                ),
            )

    def count(self, workflow_id: str | None = None) -> int:
        conn = self.db.connect()
        try:
            if workflow_id is None:
                row = conn.execute("SELECT COUNT(*) AS n FROM provider_invocations").fetchone()
            else:
                row = conn.execute(
                    "SELECT COUNT(*) AS n FROM provider_invocations WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
            return int(row["n"])
        finally:
            conn.close()
