"""Repository implementations for durable entity storage in SQLite.

Translates domain models to and from relational tables without exposing database
details to the domain layer.
"""

import json
import sqlite3
from dataclasses import asdict, dataclass, field, replace
from typing import Any

from gamefactory.adapters.persistence.database import Database
from gamefactory.core.accounting.ledger import (
    EntryType,
    LedgerEntry,
    OperationAccount,
)
from gamefactory.core.domain.asset_contracts import AssetRevision
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
    _is_identity_key,
    redactor,
)


def _sanitize_persisted_data(value: Any) -> Any:
    """Redact payload values while preserving structural key values and keys."""
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            if _is_identity_key(key):
                cleaned[key] = item
            elif any(part in str(key).upper() for part in _SENSITIVE_ENV_SUBSTRINGS):
                if item is None or isinstance(item, bool):
                    cleaned[key] = item
                else:
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


def _insert_ledger_entry(conn: Any, entry: LedgerEntry) -> None:
    """Append one ledger row on a caller-owned connection (no commit)."""
    conn.execute(
        """
        INSERT INTO cost_ledger (
            id, project_id, workflow_id, task_id, execution_id, intent_id,
            request_fingerprint, entry_type, amount, cost_unit, reason,
            source, actor, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            entry.id or generate_id("LEDGER"),
            entry.project_id,
            entry.workflow_id,
            entry.task_id,
            entry.execution_id,
            entry.intent_id,
            entry.request_fingerprint,
            entry.entry_type.value if hasattr(entry.entry_type, "value") else str(entry.entry_type),
            float(entry.amount),
            entry.cost_unit,
            _redact_text(entry.reason),
            _redact_text(entry.source),
            _redact_text(entry.actor),
            entry.created_at or utc_now_iso(),
        ),
    )


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
        self,
        workflow: Workflow,
        tasks: list[Task],
        event: AuditEvent | None = None,
        *,
        allow_existing_empty_placeholder: bool = False,
    ) -> None:
        """Atomically register a workflow, its complete task set, and audit trail."""
        for task in tasks:
            _validate_task_parameters(task)
        with self.db.transaction() as conn:
            existing = conn.execute(
                "SELECT project_id, status FROM workflows WHERE id = ?", (workflow.id,)
            ).fetchone()
            if existing is not None:
                existing_tasks = conn.execute(
                    "SELECT 1 FROM tasks WHERE workflow_id = ? LIMIT 1", (workflow.id,)
                ).fetchone()
                if (
                    not allow_existing_empty_placeholder
                    or existing["project_id"] != workflow.project_id
                    or existing["status"] != WorkflowStatus.PENDING.value
                    or existing_tasks is not None
                ):
                    raise sqlite3.IntegrityError("workflow ID already exists")
                conn.execute(
                    "UPDATE workflows SET name=?, updated_at=? WHERE id=?",
                    (workflow.name, workflow.updated_at, workflow.id),
                )
            else:
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

    def list_by_workflow(
        self, workflow_id: str, conn: sqlite3.Connection | None = None
    ) -> list[Task]:
        active = conn or self.db.connect()
        try:
            cursor = active.execute(
                "SELECT * FROM tasks WHERE workflow_id = ? ORDER BY created_at ASC;",
                (workflow_id,),
            )
            return [self._row_to_task(row) for row in cursor.fetchall()]
        finally:
            if conn is None:
                active.close()

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
            spent_row = conn.execute(
                "SELECT COALESCE(SUM(CASE WHEN entry_type = 'RELEASE' THEN -amount ELSE amount END), 0.0) AS total FROM cost_ledger WHERE project_id = ?",
                (project_id,),
            ).fetchone()
            spent = float(spent_row["total"])
            # A paid operation (task) is reserved once. A settled operation needs no new
            # hold, and an unsettled one only tops up to the requested reservation, so a
            # retry can never stack a second, never-released reservation.
            account_row = conn.execute(
                "SELECT "
                "COALESCE(SUM(CASE WHEN entry_type = 'SETTLE' THEN 1 ELSE 0 END), 0) AS settles, "
                "COALESCE(SUM(CASE WHEN entry_type = 'RESERVE' THEN amount "
                "WHEN entry_type = 'RELEASE' THEN -amount ELSE 0 END), 0.0) AS held "
                "FROM cost_ledger WHERE task_id = ?",
                (task_id,),
            ).fetchone()
            reservation = (
                0.0
                if int(account_row["settles"]) > 0
                else max(0.0, float(execution.cost) - max(0.0, float(account_row["held"])))
            )
            if spent + reservation > project_budget:
                from gamefactory.core.domain.errors import BudgetExceeded

                raise BudgetExceeded(
                    "Atomic project budget reservation would be exceeded",
                    details={
                        "spent": spent,
                        "estimate": reservation,
                        "budget": project_budget,
                    },
                )
            task_row = conn.execute(
                "SELECT workflow_id, status FROM tasks WHERE id=?", (task_id,)
            ).fetchone()
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
            if reservation > 0:
                conn.execute(
                    """
                    INSERT INTO cost_ledger (
                        id, project_id, workflow_id, task_id, execution_id, intent_id,
                        request_fingerprint, entry_type, amount, cost_unit, reason,
                        source, actor, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        generate_id("LEDGER"),
                        project_id,
                        task_row["workflow_id"],
                        task_id,
                        execution.id,
                        None,
                        None,
                        "RESERVE",
                        reservation,
                        execution.cost_unit or "credits",
                        "Reservation held for dispatch",
                        "execution_claim",
                        "WorkflowEngine",
                        execution.started_at or utc_now_iso(),
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

    def update_parameters(self, task_id: str, parameters: dict[str, Any]) -> None:
        """Persist a refreshed immutable input snapshot before policy/approval evaluation."""
        if _contains_task_secret(parameters):
            raise ValidationError(
                "Task parameters contain a sensitive field or known credential; remove it before persistence",
                details={"task_id": task_id},
            )
        payload = json.dumps(parameters, sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE tasks SET parameters_json = ?, updated_at = ? WHERE id = ?",
                (payload, utc_now_iso(), task_id),
            )
            if cursor.rowcount != 1:
                raise ValidationError(f"Task '{task_id}' not found while updating parameters")

    def reopen_for_concept_replacement(
        self, workflow_id: str, task_ids: list[str], audit_event: AuditEvent | None = None
    ) -> None:
        """Bypass TaskStateMachine to reset pre-paid tasks to PENDING and workflow to BLOCKED."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for task_id in task_ids:
                row = conn.execute(
                    "SELECT status FROM tasks WHERE id = ? AND workflow_id = ?",
                    (task_id, workflow_id),
                ).fetchone()
                if row is None:
                    continue
                old_status = row["status"]
                conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ? AND workflow_id = ?",
                    (TaskStatus.PENDING.value, now, task_id, workflow_id),
                )
                actor = audit_event.actor if audit_event else "AssetConceptService"
                details = dict(audit_event.details) if (audit_event and audit_event.details) else {}
                details["workflow_id"] = workflow_id
                _insert_audit_event(
                    conn,
                    AuditEvent(
                        id=generate_id("AUDIT"),
                        entity_type="Task",
                        entity_id=task_id,
                        action="TASK_REOPENED_FOR_CONCEPT_REPLACEMENT",
                        actor=actor,
                        timestamp=now,
                        previous_state=old_status,
                        new_state=TaskStatus.PENDING.value,
                        details=details,
                    ),
                )
            wf_row = conn.execute(
                "SELECT status FROM workflows WHERE id = ?", (workflow_id,)
            ).fetchone()
            if wf_row is not None:
                old_wf_status = wf_row["status"]
                conn.execute(
                    "UPDATE workflows SET status = ?, updated_at = ? WHERE id = ?",
                    (WorkflowStatus.BLOCKED.value, now, workflow_id),
                )
                if old_wf_status != WorkflowStatus.BLOCKED.value:
                    actor = audit_event.actor if audit_event else "AssetConceptService"
                    _insert_audit_event(
                        conn,
                        AuditEvent(
                            id=generate_id("AUDIT"),
                            entity_type="Workflow",
                            entity_id=workflow_id,
                            action="STATE_CHANGED",
                            actor=actor,
                            timestamp=now,
                            previous_state=old_wf_status,
                            new_state=WorkflowStatus.BLOCKED.value,
                            details={"reason": "reopened_for_concept_replacement"},
                        ),
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
            # A worker can outlive an operator or recovery process that starts a
            # newer attempt. A task-status CAS alone lets that stale worker
            # finalize the replacement attempt's task state, so bind the update
            # to the persisted task owner and unique latest execution as well.
            persisted_task = conn.execute(
                "SELECT workflow_id, status FROM tasks WHERE id = ?", (task.id,)
            ).fetchone()
            persisted_execution = conn.execute(
                "SELECT task_id, attempt_number, status FROM executions WHERE id = ?",
                (execution.id,),
            ).fetchone()
            latest_execution = conn.execute(
                "SELECT id, attempt_number FROM executions WHERE task_id = ? "
                "ORDER BY attempt_number DESC LIMIT 1",
                (task.id,),
            ).fetchone()
            if (
                persisted_task is None
                or persisted_task["workflow_id"] != task.workflow_id
                or persisted_task["status"] != TaskStatus.RUNNING.value
                or execution.task_id != task.id
                or persisted_execution is None
                or persisted_execution["task_id"] != task.id
                or persisted_execution["attempt_number"] != execution.attempt_number
                or persisted_execution["status"] != ExecutionStatus.RUNNING.value
                or latest_execution is None
                or latest_execution["id"] != execution.id
                or latest_execution["attempt_number"] != execution.attempt_number
            ):
                raise ValueError(
                    f"Execution {execution.id} no longer owns the latest running task attempt"
                )
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

    def list_by_task(self, task_id: str, conn: sqlite3.Connection | None = None) -> list[Execution]:
        """List attempts; pass ``conn`` to read inside a caller-owned transaction."""
        active = conn or self.db.connect()
        try:
            cursor = active.execute(
                "SELECT * FROM executions WHERE task_id = ? ORDER BY attempt_number ASC;",
                (task_id,),
            )
            return [self._row_to_execution(row) for row in cursor.fetchall()]
        finally:
            if conn is None:
                active.close()

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

    def reclassify_retryable(self, execution_id: str, audit_event: AuditEvent) -> bool:
        """Atomically update retryable 0->1 for a failed execution and insert audit event."""
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            changed = conn.execute(
                "UPDATE executions SET retryable = 1 WHERE id = ? AND retryable = 0 AND status = 'FAILED'",
                (execution_id,),
            ).rowcount
            if changed != 1:
                return False
            _insert_audit_event(conn, audit_event)
            return True

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
                    operation_hash, actor, comment, requested_at, decided_at, artifact_ids_json,
                    paid_request_snapshot_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    actor = excluded.actor,
                    comment = excluded.comment,
                    decided_at = excluded.decided_at,
                    paid_request_snapshot_hash = excluded.paid_request_snapshot_hash;
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
                    approval.paid_request_snapshot_hash,
                ),
            )

    def rebind_pending_inputs(
        self,
        approval_id: str,
        operation_hash: str,
        artifact_ids: list[str],
        event: AuditEvent,
    ) -> bool:
        """Point a still-pending approval at corrected inputs without deciding it."""
        if len(operation_hash) != 64 or any(
            char not in "0123456789abcdef" for char in operation_hash
        ):
            raise ValidationError("operation hash must be a sha256 digest")
        with self.db.transaction() as conn:
            changed = conn.execute(
                """
                UPDATE approvals
                SET operation_hash = ?, artifact_ids_json = ?
                WHERE id = ? AND status = ?
                """,
                (
                    operation_hash,
                    json.dumps(artifact_ids),
                    approval_id,
                    ApprovalStatus.PENDING.value,
                ),
            ).rowcount
            if changed != 1:
                return False
            _insert_audit_event(conn, event)
            return True

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
            paid_request_snapshot_hash=(
                row["paid_request_snapshot_hash"]
                if "paid_request_snapshot_hash" in row.keys()
                else None
            ),
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

    def count(self, workflow_id: str | None = None, conn: sqlite3.Connection | None = None) -> int:
        active = conn or self.db.connect()
        try:
            if workflow_id is None:
                row = active.execute("SELECT COUNT(*) AS n FROM provider_invocations").fetchone()
            else:
                row = active.execute(
                    "SELECT COUNT(*) AS n FROM provider_invocations WHERE workflow_id = ?",
                    (workflow_id,),
                ).fetchone()
            return int(row["n"])
        finally:
            if conn is None:
                active.close()


@dataclass
class ProviderOperationIntent:
    """Durable record of intent to invoke an external or paid provider operation."""

    id: str
    workflow_id: str
    task_id: str
    asset_id: str
    revision_number: int
    provider: str
    operation: str
    concept_hash: str
    request_fingerprint: str
    approval_id: str
    estimated_cost: float | None = None
    actual_cost: float | None = None
    cost_unit: str = "credits"
    external_task_id: str | None = None
    status: str = "INTENDED"  # INTENDED, SUBMITTING, SUBMITTED, SUCCEEDED, FAILED, UNCERTAIN
    created_at: str = field(default_factory=utc_now_iso)
    updated_at: str = field(default_factory=utc_now_iso)
    paid_request_snapshot_hash: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class AssetRevisionRepository:
    """Persistence repository for monotonic, immutable asset revisions.

    Workflow tasks maintain authoritative lifecycle phase; AssetRevision tracks
    workflow_id and immutable spec/artifact relationships. Monotonic revisions
    are allocated atomically under SQLite transactions.
    """

    def __init__(self, db: Database) -> None:
        self.db = db

    def allocate_revision(
        self,
        asset_id: str,
        workflow_id: str,
        spec_hash: str,
        concept_hash: str | None = None,
        raw_glb_hash: str | None = None,
        processed_glb_hash: str | None = None,
        validation_report_hash: str | None = None,
        runtime_evidence_hashes: list[str] | None = None,
        profile_id: str | None = None,
        profile_version: int | None = None,
    ) -> AssetRevision:
        """Atomically allocate the next monotonic revision for an asset."""
        now = utc_now_iso()
        runtime_hashes = runtime_evidence_hashes or []
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT COALESCE(MAX(revision_number), 0) AS max_rev FROM asset_revisions WHERE asset_id = ?;",
                (asset_id,),
            ).fetchone()
            next_rev = int(row["max_rev"]) + 1

            conn.execute(
                """
                INSERT INTO asset_revisions (
                    asset_id, revision_number, workflow_id, spec_hash,
                    concept_hash, raw_glb_hash, processed_glb_hash,
                    validation_report_hash, runtime_evidence_hashes_json,
                    created_at, updated_at, profile_id, profile_version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    asset_id,
                    next_rev,
                    workflow_id,
                    spec_hash,
                    concept_hash,
                    raw_glb_hash,
                    processed_glb_hash,
                    validation_report_hash,
                    json.dumps(runtime_hashes),
                    now,
                    now,
                    profile_id,
                    profile_version,
                ),
            )

            return AssetRevision(
                asset_id=asset_id,
                revision_number=next_rev,
                workflow_id=workflow_id,
                spec_hash=spec_hash,
                profile_id=profile_id,
                profile_version=profile_version,
                concept_hash=concept_hash,
                raw_glb_hash=raw_glb_hash,
                processed_glb_hash=processed_glb_hash,
                validation_report_hash=validation_report_hash,
                runtime_evidence_hashes=runtime_hashes,
                created_at=now,
                updated_at=now,
            )

    def save(self, revision: AssetRevision) -> None:
        """Save a revision, inserting or updating artifact hashes without overwriting spec_hash or workflow_id."""
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT spec_hash, workflow_id, concept_hash, raw_glb_hash, processed_glb_hash, validation_report_hash, runtime_evidence_hashes_json, profile_id, profile_version, created_at, updated_at FROM asset_revisions WHERE asset_id = ? AND revision_number = ?;",
                (revision.asset_id, revision.revision_number),
            ).fetchone()

            if existing is None:
                conn.execute(
                    """
                    INSERT INTO asset_revisions (
                        asset_id, revision_number, workflow_id, spec_hash,
                        concept_hash, raw_glb_hash, processed_glb_hash,
                        validation_report_hash, runtime_evidence_hashes_json,
                        created_at, updated_at, profile_id, profile_version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        revision.asset_id,
                        revision.revision_number,
                        revision.workflow_id,
                        revision.spec_hash,
                        revision.concept_hash,
                        revision.raw_glb_hash,
                        revision.processed_glb_hash,
                        revision.validation_report_hash,
                        json.dumps(revision.runtime_evidence_hashes),
                        revision.created_at,
                        revision.updated_at,
                        revision.profile_id,
                        revision.profile_version,
                    ),
                )
            else:
                if existing["spec_hash"] != revision.spec_hash:
                    raise ValueError(
                        f"Cannot overwrite immutable spec_hash for revision {revision.revision_number} of '{revision.asset_id}'"
                    )
                if existing["workflow_id"] != revision.workflow_id:
                    raise ValueError(
                        f"Cannot overwrite workflow_id for revision {revision.revision_number} of '{revision.asset_id}'"
                    )
                if existing["profile_id"] is not None and revision.profile_id not in (
                    None,
                    existing["profile_id"],
                ):
                    raise ValueError(
                        f"Cannot overwrite immutable profile_id for revision {revision.revision_number} of '{revision.asset_id}'"
                    )
                if existing["profile_version"] is not None and revision.profile_version not in (
                    None,
                    existing["profile_version"],
                ):
                    raise ValueError(
                        "Cannot overwrite immutable profile_version for revision "
                        f"{revision.revision_number} of '{revision.asset_id}'"
                    )
                immutable_artifacts = (
                    "concept_hash",
                    "raw_glb_hash",
                    "processed_glb_hash",
                    "validation_report_hash",
                )
                for field_name in immutable_artifacts:
                    saved_value = existing[field_name]
                    proposed_value = getattr(revision, field_name)
                    if saved_value is not None and proposed_value != saved_value:
                        raise ValueError(
                            f"Cannot overwrite immutable {field_name} for revision "
                            f"{revision.revision_number} of '{revision.asset_id}'"
                        )
                saved_runtime_hashes = json.loads(existing["runtime_evidence_hashes_json"])
                proposed_runtime_hashes = revision.runtime_evidence_hashes
                if (
                    len(proposed_runtime_hashes) < len(saved_runtime_hashes)
                    or proposed_runtime_hashes[: len(saved_runtime_hashes)] != saved_runtime_hashes
                ):
                    raise ValueError(
                        f"Cannot remove or change immutable runtime evidence hashes for revision "
                        f"{revision.revision_number} of '{revision.asset_id}'"
                    )

                changed = (
                    existing["concept_hash"] != revision.concept_hash
                    or existing["raw_glb_hash"] != revision.raw_glb_hash
                    or existing["processed_glb_hash"] != revision.processed_glb_hash
                    or existing["validation_report_hash"] != revision.validation_report_hash
                    or saved_runtime_hashes != proposed_runtime_hashes
                )

                revision.created_at = existing["created_at"]
                if changed:
                    now = utc_now_iso()
                    revision.updated_at = now
                else:
                    revision.updated_at = existing["updated_at"]

                conn.execute(
                    """
                    UPDATE asset_revisions SET
                        concept_hash = ?,
                        raw_glb_hash = ?,
                        processed_glb_hash = ?,
                        validation_report_hash = ?,
                        runtime_evidence_hashes_json = ?,
                        updated_at = ?
                    WHERE asset_id = ? AND revision_number = ?;
                    """,
                    (
                        revision.concept_hash,
                        revision.raw_glb_hash,
                        revision.processed_glb_hash,
                        revision.validation_report_hash,
                        json.dumps(revision.runtime_evidence_hashes),
                        revision.updated_at,
                        revision.asset_id,
                        revision.revision_number,
                    ),
                )

    def supersede_concept_hash(
        self, asset_id: str, revision_number: int, expected_old_hash: str, new_hash: str
    ) -> bool:
        """Atomically update concept_hash only when raw_glb_hash is NULL and concept_hash matches expected_old_hash."""
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE asset_revisions
                SET concept_hash = ?, updated_at = ?
                WHERE asset_id = ?
                  AND revision_number = ?
                  AND raw_glb_hash IS NULL
                  AND concept_hash = ?;
                """,
                (new_hash, now, asset_id, revision_number, expected_old_hash),
            )
            return cursor.rowcount == 1

    def get(self, asset_id: str, revision_number: int) -> AssetRevision | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM asset_revisions WHERE asset_id = ? AND revision_number = ?;",
                (asset_id, revision_number),
            ).fetchone()
            if not row:
                return None
            return self._row_to_revision(row)
        finally:
            conn.close()

    def get_latest(self, asset_id: str) -> AssetRevision | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM asset_revisions WHERE asset_id = ? ORDER BY revision_number DESC LIMIT 1;",
                (asset_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_revision(row)
        finally:
            conn.close()

    def list_by_asset(self, asset_id: str) -> list[AssetRevision]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM asset_revisions WHERE asset_id = ? ORDER BY revision_number ASC;",
                (asset_id,),
            ).fetchall()
            return [self._row_to_revision(row) for row in rows]
        finally:
            conn.close()

    def list_by_workflow(self, workflow_id: str) -> list[AssetRevision]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM asset_revisions WHERE workflow_id = ? ORDER BY created_at ASC;",
                (workflow_id,),
            ).fetchall()
            return [self._row_to_revision(row) for row in rows]
        finally:
            conn.close()

    def _row_to_revision(self, row: Any) -> AssetRevision:
        return AssetRevision(
            asset_id=row["asset_id"],
            revision_number=row["revision_number"],
            workflow_id=row["workflow_id"],
            spec_hash=row["spec_hash"],
            concept_hash=row["concept_hash"],
            raw_glb_hash=row["raw_glb_hash"],
            processed_glb_hash=row["processed_glb_hash"],
            validation_report_hash=row["validation_report_hash"],
            runtime_evidence_hashes=json.loads(row["runtime_evidence_hashes_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            profile_id=row["profile_id"] if "profile_id" in row.keys() else None,
            profile_version=row["profile_version"] if "profile_version" in row.keys() else None,
        )


class ProviderOperationIntentRepository:
    """Persistence repository for pre-submission durable intent and recovery."""

    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def _audit(
        conn: Any,
        intent: ProviderOperationIntent,
        action: str,
        previous: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Record a provider lifecycle transition in the intent's own transaction."""
        _insert_audit_event(
            conn,
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="ProviderOperationIntent",
                entity_id=intent.id,
                action=action,
                actor="ProviderAdapter",
                timestamp=utc_now_iso(),
                previous_state=previous,
                new_state=intent.status,
                details={
                    "workflow_id": intent.workflow_id,
                    "task_id": intent.task_id,
                    "provider": intent.provider,
                    "operation": intent.operation,
                    "request_fingerprint": intent.request_fingerprint,
                    "external_task_id": intent.external_task_id,
                    **(details or {}),
                },
            ),
        )

    def save(self, intent: ProviderOperationIntent) -> None:
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT created_at, updated_at, status, external_task_id, actual_cost "
                "FROM provider_operation_intents WHERE id = ?;",
                (intent.id,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO provider_operation_intents (
                        id, workflow_id, task_id, asset_id, revision_number,
                        provider, operation, concept_hash, request_fingerprint,
                        approval_id, estimated_cost, actual_cost, cost_unit,
                        external_task_id, status, created_at, updated_at,
                        paid_request_snapshot_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        intent.id,
                        intent.workflow_id,
                        intent.task_id,
                        intent.asset_id,
                        intent.revision_number,
                        intent.provider,
                        intent.operation,
                        intent.concept_hash,
                        intent.request_fingerprint,
                        intent.approval_id,
                        intent.estimated_cost,
                        intent.actual_cost,
                        intent.cost_unit,
                        intent.external_task_id,
                        intent.status,
                        intent.created_at,
                        intent.updated_at,
                        intent.paid_request_snapshot_hash,
                    ),
                )
                self._audit(conn, intent, "PROVIDER_INTENT_RECORDED")
            else:
                now = utc_now_iso()
                intent.created_at = existing["created_at"]
                intent.updated_at = now
                conn.execute(
                    """
                    UPDATE provider_operation_intents SET
                        external_task_id = ?,
                        actual_cost = ?,
                        status = ?,
                        updated_at = ?
                    WHERE id = ?;
                    """,
                    (
                        intent.external_task_id,
                        intent.actual_cost,
                        intent.status,
                        now,
                        intent.id,
                    ),
                )
                if intent.external_task_id and not existing["external_task_id"]:
                    self._audit(conn, intent, "PROVIDER_TASK_ID_PERSISTED", existing["status"])
                if intent.status != existing["status"]:
                    self._audit(
                        conn,
                        intent,
                        "PROVIDER_STATUS_CHANGED",
                        existing["status"],
                        {"actual_cost": intent.actual_cost},
                    )
                elif intent.actual_cost != existing["actual_cost"]:
                    self._audit(
                        conn,
                        intent,
                        "PROVIDER_ACTUAL_COST_RECORDED",
                        existing["status"],
                        {"actual_cost": intent.actual_cost},
                    )

    def claim_intent(self, intent: ProviderOperationIntent) -> tuple[ProviderOperationIntent, bool]:
        """Atomically claim intent for an operation using BEGIN IMMEDIATE.

        Returns (claimed_intent, True) if newly claimed (winner).
        Returns (existing_intent, False) if an intent with the same task_id,
        request_fingerprint, or (asset_id, revision_number, provider, operation)
        already exists.
        """
        intent.updated_at = intent.created_at
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT * FROM provider_operation_intents
                WHERE task_id = ?
                   OR request_fingerprint = ?
                   OR (asset_id = ? AND revision_number = ? AND provider = ? AND operation = ?)
                ORDER BY created_at ASC LIMIT 1;
                """,
                (
                    intent.task_id,
                    intent.request_fingerprint,
                    intent.asset_id,
                    intent.revision_number,
                    intent.provider,
                    intent.operation,
                ),
            ).fetchone()
            if row is not None:
                return self._row_to_intent(row), False

            try:
                conn.execute(
                    """
                    INSERT INTO provider_operation_intents (
                        id, workflow_id, task_id, asset_id, revision_number,
                        provider, operation, concept_hash, request_fingerprint,
                        approval_id, estimated_cost, actual_cost, cost_unit,
                        external_task_id, status, created_at, updated_at,
                        paid_request_snapshot_hash
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                    """,
                    (
                        intent.id,
                        intent.workflow_id,
                        intent.task_id,
                        intent.asset_id,
                        intent.revision_number,
                        intent.provider,
                        intent.operation,
                        intent.concept_hash,
                        intent.request_fingerprint,
                        intent.approval_id,
                        intent.estimated_cost,
                        intent.actual_cost,
                        intent.cost_unit,
                        intent.external_task_id,
                        intent.status,
                        intent.created_at,
                        intent.updated_at,
                        intent.paid_request_snapshot_hash,
                    ),
                )
                self._audit(
                    conn,
                    intent,
                    "PROVIDER_INTENT_CLAIMED",
                    details={"paid_request_snapshot_hash": intent.paid_request_snapshot_hash},
                )
                return intent, True
            except sqlite3.IntegrityError:
                existing_row = conn.execute(
                    """
                    SELECT * FROM provider_operation_intents
                    WHERE task_id = ?
                       OR request_fingerprint = ?
                       OR (asset_id = ? AND revision_number = ? AND provider = ? AND operation = ?)
                    ORDER BY created_at ASC LIMIT 1;
                    """,
                    (
                        intent.task_id,
                        intent.request_fingerprint,
                        intent.asset_id,
                        intent.revision_number,
                        intent.provider,
                        intent.operation,
                    ),
                ).fetchone()
                if existing_row is not None:
                    return self._row_to_intent(existing_row), False
                raise

    atomic_claim = claim_intent

    def get(self, intent_id: str) -> ProviderOperationIntent | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM provider_operation_intents WHERE id = ?;", (intent_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_intent(row)
        finally:
            conn.close()

    def get_by_task(
        self, task_id: str, conn: sqlite3.Connection | None = None
    ) -> ProviderOperationIntent | None:
        active = conn or self.db.connect()
        try:
            row = active.execute(
                "SELECT * FROM provider_operation_intents WHERE task_id = ? ORDER BY created_at DESC LIMIT 1;",
                (task_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_intent(row)
        finally:
            if conn is None:
                active.close()

    def get_by_fingerprint(self, fingerprint: str) -> ProviderOperationIntent | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM provider_operation_intents WHERE request_fingerprint = ? ORDER BY created_at DESC LIMIT 1;",
                (fingerprint,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_intent(row)
        finally:
            conn.close()

    def get_by_asset_revision(
        self,
        asset_id: str,
        revision_number: int,
        provider: str = "meshy",
        operation: str = "image-to-3d",
    ) -> ProviderOperationIntent | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                """
                SELECT * FROM provider_operation_intents
                WHERE asset_id = ? AND revision_number = ? AND provider = ? AND operation = ?
                ORDER BY created_at DESC LIMIT 1;
                """,
                (asset_id, revision_number, provider, operation),
            ).fetchone()
            if not row:
                return None
            return self._row_to_intent(row)
        finally:
            conn.close()

    def list_by_workflow(self, workflow_id: str) -> list[ProviderOperationIntent]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM provider_operation_intents WHERE workflow_id = ? ORDER BY created_at ASC;",
                (workflow_id,),
            ).fetchall()
            return [self._row_to_intent(row) for row in rows]
        finally:
            conn.close()

    def _row_to_intent(self, row: Any) -> ProviderOperationIntent:
        return ProviderOperationIntent(
            id=row["id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            asset_id=row["asset_id"],
            revision_number=row["revision_number"],
            provider=row["provider"],
            operation=row["operation"],
            concept_hash=row["concept_hash"],
            request_fingerprint=row["request_fingerprint"],
            approval_id=row["approval_id"],
            estimated_cost=row["estimated_cost"],
            actual_cost=row["actual_cost"],
            cost_unit=row["cost_unit"],
            external_task_id=row["external_task_id"],
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            paid_request_snapshot_hash=(
                row["paid_request_snapshot_hash"]
                if "paid_request_snapshot_hash" in row.keys()
                else None
            ),
        )


class CostLedgerRepository:
    """Repository for appending and reading append-only cost ledger records."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def append(
        self, entries: list[LedgerEntry], audit_event: AuditEvent | None = None
    ) -> list[LedgerEntry]:
        """Atomically append ledger entries and an optional audit event in one transaction."""
        if not entries and audit_event is None:
            return []
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            for entry in entries:
                _insert_ledger_entry(conn, entry)
            if audit_event is not None:
                _insert_audit_event(conn, audit_event)
        return entries

    def list_by_workflow(self, workflow_id: str) -> list[LedgerEntry]:
        conn = self.db.connect()
        try:
            cursor = conn.execute(
                "SELECT * FROM cost_ledger WHERE workflow_id = ? ORDER BY rowid ASC;",
                (workflow_id,),
            )
            return [self._row_to_entry(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def list_by_task(
        self, task_id: str, conn: sqlite3.Connection | None = None
    ) -> list[LedgerEntry]:
        active = conn or self.db.connect()
        try:
            cursor = active.execute(
                "SELECT * FROM cost_ledger WHERE task_id = ? ORDER BY rowid ASC;",
                (task_id,),
            )
            return [self._row_to_entry(row) for row in cursor.fetchall()]
        finally:
            if conn is None:
                active.close()

    def project_net(self, project_id: str, conn: sqlite3.Connection | None = None) -> float:
        """Project committed spend; pass ``conn`` to read inside a caller-owned transaction."""
        active = conn or self.db.connect()
        try:
            row = active.execute(
                """
                SELECT COALESCE(SUM(
                    CASE
                        WHEN entry_type = 'RELEASE' THEN -amount
                        ELSE amount
                    END
                ), 0.0) AS net
                FROM cost_ledger
                WHERE project_id = ?;
                """,
                (project_id,),
            ).fetchone()
            return float(row["net"]) if row else 0.0
        finally:
            if conn is None:
                active.close()

    def operation_account(
        self, task_id: str, conn: sqlite3.Connection | None = None
    ) -> OperationAccount:
        entries = self.list_by_task(task_id, conn)
        reserved_total = sum(e.amount for e in entries if e.entry_type == EntryType.RESERVE)
        released_total = sum(e.amount for e in entries if e.entry_type == EntryType.RELEASE)
        settled_total = sum(e.amount for e in entries if e.entry_type == EntryType.SETTLE)
        adjustments = sum(e.amount for e in entries if e.entry_type == EntryType.ADJUSTMENT)
        settled = any(e.entry_type == EntryType.SETTLE for e in entries)
        project_id = entries[0].project_id if entries else ""
        workflow_id = entries[0].workflow_id if entries else ""
        cost_unit = entries[0].cost_unit if entries else "credits"
        if not project_id or not workflow_id:
            active = conn or self.db.connect()
            try:
                row = active.execute(
                    "SELECT t.workflow_id, w.project_id FROM tasks t JOIN workflows w ON w.id = t.workflow_id WHERE t.id = ?",
                    (task_id,),
                ).fetchone()
                if row:
                    project_id = row["project_id"]
                    workflow_id = row["workflow_id"]
            finally:
                if conn is None:
                    active.close()
        return OperationAccount(
            task_id=task_id,
            reserved_total=reserved_total,
            released_total=released_total,
            settled_total=settled_total,
            adjustments=adjustments,
            settled=settled,
            project_id=project_id,
            workflow_id=workflow_id,
            cost_unit=cost_unit,
        )

    def _row_to_entry(self, row: Any) -> LedgerEntry:
        return LedgerEntry(
            id=row["id"],
            project_id=row["project_id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            execution_id=row["execution_id"],
            intent_id=row["intent_id"],
            request_fingerprint=row["request_fingerprint"],
            entry_type=EntryType(row["entry_type"]),
            amount=float(row["amount"]),
            cost_unit=row["cost_unit"],
            reason=row["reason"],
            source=row["source"],
            actor=row["actor"],
            created_at=row["created_at"],
        )


@dataclass
class PaidRequestSnapshotRecord:
    id: str
    workflow_id: str
    task_id: str
    asset_id: str
    revision_number: int
    concept_version: int
    schema_version: str
    snapshot_sha256: str
    canonical_json: str
    status: str = "ACTIVE"  # ACTIVE, SUPERSEDED
    artifact_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    superseded_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PaidRequestSnapshotRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, record: PaidRequestSnapshotRecord) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO paid_request_snapshots (
                    id, workflow_id, task_id, asset_id, revision_number,
                    concept_version, schema_version, snapshot_sha256,
                    canonical_json, artifact_id, status, created_at, superseded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    superseded_at = excluded.superseded_at,
                    artifact_id = excluded.artifact_id;
                """,
                (
                    record.id,
                    record.workflow_id,
                    record.task_id,
                    record.asset_id,
                    record.revision_number,
                    record.concept_version,
                    record.schema_version,
                    record.snapshot_sha256,
                    record.canonical_json,
                    record.artifact_id,
                    record.status,
                    record.created_at,
                    record.superseded_at,
                ),
            )

    def get_active_for_workflow(self, workflow_id: str) -> PaidRequestSnapshotRecord | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM paid_request_snapshots WHERE workflow_id = ? AND status = 'ACTIVE' ORDER BY created_at DESC LIMIT 1;",
                (workflow_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_record(row)
        finally:
            conn.close()

    def list_by_workflow(self, workflow_id: str) -> list[PaidRequestSnapshotRecord]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM paid_request_snapshots WHERE workflow_id = ? ORDER BY created_at ASC;",
                (workflow_id,),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]
        finally:
            conn.close()

    def mark_superseded(self, ids: list[str], at: str | None = None) -> int:
        if not ids:
            return 0
        now = at or utc_now_iso()
        placeholders = ",".join("?" for _ in ids)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE paid_request_snapshots SET status = 'SUPERSEDED', superseded_at = ? WHERE id IN ({placeholders}) AND status = 'ACTIVE';",
                [now, *ids],
            )
            return cursor.rowcount

    def _row_to_record(self, row: Any) -> PaidRequestSnapshotRecord:
        return PaidRequestSnapshotRecord(
            id=row["id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            asset_id=row["asset_id"],
            revision_number=row["revision_number"],
            concept_version=row["concept_version"],
            schema_version=row["schema_version"],
            snapshot_sha256=row["snapshot_sha256"],
            canonical_json=row["canonical_json"],
            status=row["status"],
            artifact_id=row["artifact_id"],
            created_at=row["created_at"],
            superseded_at=row["superseded_at"],
        )


@dataclass
class ProductionReadinessRecord:
    id: str
    workflow_id: str
    task_id: str
    snapshot_sha256: str
    result: str  # PASS, FAIL
    schema_version: str
    report_json: str
    report_sha256: str
    status: str = "ACTIVE"  # ACTIVE, SUPERSEDED
    artifact_id: str | None = None
    created_at: str = field(default_factory=utc_now_iso)
    superseded_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ProductionReadinessRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def save(self, record: ProductionReadinessRecord) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO production_readiness_reports (
                    id, workflow_id, task_id, snapshot_sha256, result,
                    schema_version, report_json, report_sha256, artifact_id,
                    status, created_at, superseded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    status = excluded.status,
                    superseded_at = excluded.superseded_at,
                    artifact_id = excluded.artifact_id;
                """,
                (
                    record.id,
                    record.workflow_id,
                    record.task_id,
                    record.snapshot_sha256,
                    record.result,
                    record.schema_version,
                    record.report_json,
                    record.report_sha256,
                    record.artifact_id,
                    record.status,
                    record.created_at,
                    record.superseded_at,
                ),
            )

    def get_active_for_workflow(self, workflow_id: str) -> ProductionReadinessRecord | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM production_readiness_reports WHERE workflow_id = ? AND status = 'ACTIVE' ORDER BY created_at DESC LIMIT 1;",
                (workflow_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_record(row)
        finally:
            conn.close()

    def list_by_workflow(self, workflow_id: str) -> list[ProductionReadinessRecord]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM production_readiness_reports WHERE workflow_id = ? ORDER BY created_at ASC;",
                (workflow_id,),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]
        finally:
            conn.close()

    def mark_superseded(self, ids: list[str], at: str | None = None) -> int:
        if not ids:
            return 0
        now = at or utc_now_iso()
        placeholders = ",".join("?" for _ in ids)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                f"UPDATE production_readiness_reports SET status = 'SUPERSEDED', superseded_at = ? WHERE id IN ({placeholders}) AND status = 'ACTIVE';",
                [now, *ids],
            )
            return cursor.rowcount

    def _row_to_record(self, row: Any) -> ProductionReadinessRecord:
        return ProductionReadinessRecord(
            id=row["id"],
            workflow_id=row["workflow_id"],
            task_id=row["task_id"],
            snapshot_sha256=row["snapshot_sha256"],
            result=row["result"],
            schema_version=row["schema_version"],
            report_json=row["report_json"],
            report_sha256=row["report_sha256"],
            status=row["status"],
            artifact_id=row["artifact_id"],
            created_at=row["created_at"],
            superseded_at=row["superseded_at"],
        )


@dataclass
class ConceptVersionRecord:
    id: str
    workflow_id: str
    asset_id: str
    revision_number: int
    version: int
    artifact_id: str
    content_hash: str
    actor: str
    reason: str
    provenance_artifact_id: str | None = None
    provenance_hash: str | None = None
    provenance_type: str | None = None
    source_type: str | None = None
    status: str = "ACTIVE"
    created_at: str = field(default_factory=utc_now_iso)
    superseded_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class ConceptVersionRepository:
    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, record: ConceptVersionRecord) -> None:
        with self.db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO concept_versions (
                    id, workflow_id, asset_id, revision_number, version,
                    artifact_id, content_hash, provenance_artifact_id, provenance_hash,
                    provenance_type, source_type, status, actor, reason,
                    created_at, superseded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    record.id,
                    record.workflow_id,
                    record.asset_id,
                    record.revision_number,
                    record.version,
                    record.artifact_id,
                    record.content_hash,
                    record.provenance_artifact_id,
                    record.provenance_hash,
                    record.provenance_type,
                    record.source_type,
                    record.status,
                    record.actor,
                    record.reason,
                    record.created_at,
                    record.superseded_at,
                ),
            )

    def active_for_workflow(self, workflow_id: str) -> ConceptVersionRecord | None:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM concept_versions WHERE workflow_id = ? AND status = 'ACTIVE' ORDER BY version DESC LIMIT 1;",
                (workflow_id,),
            ).fetchone()
            if not row:
                return None
            return self._row_to_record(row)
        finally:
            conn.close()

    def list_for_revision(self, asset_id: str, revision_number: int) -> list[ConceptVersionRecord]:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM concept_versions WHERE asset_id = ? AND revision_number = ? ORDER BY version ASC;",
                (asset_id, revision_number),
            ).fetchall()
            return [self._row_to_record(row) for row in rows]
        finally:
            conn.close()

    def supersede_and_add(self, old_id: str | None, new_record: ConceptVersionRecord) -> None:
        now = new_record.created_at or utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if old_id:
                conn.execute(
                    "UPDATE concept_versions SET status = 'SUPERSEDED', superseded_at = ? WHERE id = ? AND status = 'ACTIVE';",
                    (now, old_id),
                )
            conn.execute(
                """
                INSERT INTO concept_versions (
                    id, workflow_id, asset_id, revision_number, version,
                    artifact_id, content_hash, provenance_artifact_id, provenance_hash,
                    provenance_type, source_type, status, actor, reason,
                    created_at, superseded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);
                """,
                (
                    new_record.id,
                    new_record.workflow_id,
                    new_record.asset_id,
                    new_record.revision_number,
                    new_record.version,
                    new_record.artifact_id,
                    new_record.content_hash,
                    new_record.provenance_artifact_id,
                    new_record.provenance_hash,
                    new_record.provenance_type,
                    new_record.source_type,
                    new_record.status,
                    new_record.actor,
                    new_record.reason,
                    new_record.created_at,
                    new_record.superseded_at,
                ),
            )

    def _row_to_record(self, row: Any) -> ConceptVersionRecord:
        return ConceptVersionRecord(
            id=row["id"],
            workflow_id=row["workflow_id"],
            asset_id=row["asset_id"],
            revision_number=row["revision_number"],
            version=row["version"],
            artifact_id=row["artifact_id"],
            content_hash=row["content_hash"],
            provenance_artifact_id=row["provenance_artifact_id"],
            provenance_hash=row["provenance_hash"],
            provenance_type=row["provenance_type"],
            source_type=row["source_type"],
            status=row["status"],
            actor=row["actor"],
            reason=row["reason"],
            created_at=row["created_at"],
            superseded_at=row["superseded_at"],
        )
