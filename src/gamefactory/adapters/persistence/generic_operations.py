"""Durable provider intents for non-asset Factory tasks."""

from __future__ import annotations

import hashlib
import math
from typing import Any

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    CostLedgerRepository,
    _insert_audit_event,
    _insert_ledger_entry,
)
from gamefactory.core.accounting.ledger import plan_settlement
from gamefactory.core.domain.errors import ReconciliationRequired, ValidationError
from gamefactory.core.domain.models import ApprovalStatus, AuditEvent, generate_id, utc_now_iso


class GenericOperationRepository:
    """Append-preserving intent rows separate from asset-specific provider intents."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def claim_intent(
        self,
        workflow_id: str,
        task_id: str,
        executor_id: str,
        request_fingerprint: str,
        estimated_cost: float,
        currency: str,
        unit: str,
        maximum_cost: float,
        execution_id: str,
        approval_id: str,
        operation_hash: str,
    ) -> tuple[dict[str, Any], bool]:
        for label, value, limit in (
            ("workflow_id", workflow_id, 128),
            ("task_id", task_id, 128),
            ("executor_id", executor_id, 128),
            ("execution_id", execution_id, 128),
            ("approval_id", approval_id, 128),
        ):
            if not isinstance(value, str) or not value or len(value) > limit:
                raise ValidationError(f"{label} must be a bounded non-empty string")
        for label, value in (("currency", currency), ("unit", unit)):
            if not isinstance(value, str) or not value or len(value) > 64:
                raise ValidationError(f"{label} must be a bounded non-empty string")
        if len(request_fingerprint) != 64 or any(
            c not in "0123456789abcdef" for c in request_fingerprint
        ):
            raise ValidationError("request_fingerprint must be a SHA-256 digest")
        if len(operation_hash) != 64 or any(c not in "0123456789abcdef" for c in operation_hash):
            raise ValidationError("operation_hash must be a SHA-256 digest")
        if (
            isinstance(estimated_cost, bool)
            or not isinstance(estimated_cost, (int, float))
            or not math.isfinite(estimated_cost)
            or estimated_cost < 0
            or isinstance(maximum_cost, bool)
            or not isinstance(maximum_cost, (int, float))
            or not math.isfinite(maximum_cost)
            or maximum_cost < estimated_cost
        ):
            raise ValidationError("intent costs must be finite and estimated <= approved maximum")
        now = utc_now_iso()
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            approval = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (approval_id,)
            ).fetchone()
            task = conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if (
                approval is None
                or task is None
                or approval["status"] != ApprovalStatus.APPROVED.value
                or approval["task_id"] != task_id
                or approval["workflow_id"] != workflow_id
                or approval["approval_type"] != "factory_provider_call"
                or approval["operation_hash"] != operation_hash
                or approval["cost_class"] != task["cost_class"]
            ):
                raise ValidationError(
                    "Provider intent requires the matching persisted approved decision"
                )
            row = conn.execute(
                "SELECT * FROM generic_operation_intents WHERE task_id = ? ORDER BY created_at DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            if row is not None:
                if (
                    row["workflow_id"] != workflow_id
                    or row["executor_id"] != executor_id
                    or row["request_fingerprint"] != request_fingerprint
                    or row["operation_hash"] != operation_hash
                    or row["approval_id"] != approval_id
                    or row["currency"] != currency
                    or row["unit"] != unit
                    or float(row["maximum_cost"]) != float(maximum_cost)
                ):
                    raise ReconciliationRequired(
                        "A prior provider intent is bound to different approved inputs",
                        task_id=task_id,
                        execution_id=row["execution_id"],
                    )
                return dict(row), False
            intent_id = (
                "GOP-"
                + hashlib.sha256(f"{task_id}:{request_fingerprint}".encode()).hexdigest()[:24]
            )
            conn.execute(
                "INSERT INTO generic_operation_intents (id, workflow_id, task_id, executor_id, request_fingerprint, operation_hash, approval_id, estimated_cost, maximum_cost, currency, unit, actual_cost, cost_unit, external_id, status, execution_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, 'SUBMITTING', ?, ?, ?)",
                (
                    intent_id,
                    workflow_id,
                    task_id,
                    executor_id,
                    request_fingerprint,
                    operation_hash,
                    approval_id,
                    estimated_cost,
                    maximum_cost,
                    currency,
                    unit,
                    f"{currency}:{unit}",
                    execution_id,
                    now,
                    now,
                ),
            )
            return dict(
                conn.execute(
                    "SELECT * FROM generic_operation_intents WHERE id = ?", (intent_id,)
                ).fetchone()
            ), True

    def update_intent(
        self,
        task_id: str,
        request_fingerprint: str,
        status: str,
        external_id: str | None,
        actual_cost: float | None,
    ) -> None:
        if (
            not isinstance(task_id, str)
            or not task_id
            or not isinstance(request_fingerprint, str)
            or len(request_fingerprint) != 64
        ):
            raise ValidationError("task and request fingerprint are required")
        if status not in {"SUBMITTING", "COMPLETED", "FAILED", "UNCERTAIN", "RECONCILED"}:
            raise ValidationError("invalid generic operation status")
        if external_id is not None and (
            not isinstance(external_id, str) or not external_id or len(external_id) > 256
        ):
            raise ValidationError("external id must be null or a bounded non-empty string")
        if actual_cost is not None and (
            isinstance(actual_cost, bool)
            or not isinstance(actual_cost, (int, float))
            or not math.isfinite(actual_cost)
            or actual_cost < 0
        ):
            raise ValidationError("actual_cost must be null or a finite non-negative number")
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM generic_operation_intents WHERE task_id = ?", (task_id,)
            ).fetchone()
            if row is None or row["request_fingerprint"] != request_fingerprint:
                if row is None:
                    raise ValidationError("Generic provider intent is missing")
                raise ReconciliationRequired(
                    "Generic provider intent has a different fingerprint",
                    task_id=task_id,
                    execution_id=row["execution_id"],
                )
            if row["status"] in {"COMPLETED", "FAILED", "RECONCILED"} and row["status"] != status:
                raise ReconciliationRequired(
                    "Terminal generic provider intent cannot be rewritten",
                    task_id=task_id,
                    execution_id=row["execution_id"],
                )
            if row["external_id"] and external_id and row["external_id"] != external_id:
                raise ReconciliationRequired(
                    "Provider external id changed for an existing intent",
                    task_id=task_id,
                    execution_id=row["execution_id"],
                )
            if (
                row["actual_cost"] is not None
                and actual_cost is not None
                and float(row["actual_cost"]) != float(actual_cost)
            ):
                raise ReconciliationRequired(
                    "Provider actual cost changed for an existing intent",
                    task_id=task_id,
                    execution_id=row["execution_id"],
                )
            conn.execute(
                "UPDATE generic_operation_intents SET status = ?, external_id = COALESCE(external_id, ?), actual_cost = COALESCE(actual_cost, ?), updated_at = ? WHERE task_id = ?",
                (status, external_id, actual_cost, utc_now_iso(), task_id),
            )

    def queryable(self, task_id: str, executors: Any) -> bool:
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT executor_id, external_id, status FROM generic_operation_intents WHERE task_id = ?",
                (task_id,),
            ).fetchone()
        finally:
            conn.close()
        if (
            row is None
            or row["status"] not in {"SUBMITTING", "UNCERTAIN"}
            or not row["external_id"]
        ):
            return False
        entry = executors.get(row["executor_id"])
        return bool(entry and callable(getattr(entry[0], "query", None)))

    def verify(
        self, authorization: Any, provider_id: str, request_fingerprint: str, operation_hash: str
    ) -> bool:
        """Adapter callback: require persisted approved intent before provider side effects."""
        conn = self.db.connect()
        try:
            row = conn.execute(
                "SELECT * FROM generic_operation_intents WHERE executor_id = ? AND request_fingerprint = ? ORDER BY created_at DESC LIMIT 1",
                (provider_id, request_fingerprint),
            ).fetchone()
            if row is None:
                return False
            approval = conn.execute(
                "SELECT * FROM approvals WHERE id = ?", (row["approval_id"],)
            ).fetchone()
        finally:
            conn.close()
        if approval is None or approval["status"] != ApprovalStatus.APPROVED.value:
            return False
        return bool(
            row["status"] == "SUBMITTING"
            and row["operation_hash"]
            == operation_hash
            == authorization.operation_hash
            == approval["operation_hash"]
            and row["request_fingerprint"]
            == authorization.request_fingerprint
            == request_fingerprint
            and row["approval_id"] == authorization.approval_id == approval["id"]
            and row["executor_id"] == provider_id
            and approval["task_id"] == row["task_id"]
            and approval["workflow_id"] == row["workflow_id"]
            and approval["approval_type"] == "factory_provider_call"
            and float(row["maximum_cost"]) == float(authorization.max_cost)
            and row["currency"] == authorization.currency
            and row["unit"] == authorization.unit
            and approval["cost_class"] == authorization.cost_class
        )

    def list_operations(
        self, *, workflow_id: str | None = None, project_id: str | None = None
    ) -> list[dict[str, Any]]:
        if workflow_id is None and project_id is None:
            raise ValidationError("workflow_id or project_id is required")
        conn = self.db.connect()
        try:
            if workflow_id is not None:
                rows = conn.execute(
                    "SELECT * FROM generic_operation_intents WHERE workflow_id = ? ORDER BY created_at",
                    (workflow_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT i.* FROM generic_operation_intents i JOIN workflows w ON w.id = i.workflow_id WHERE w.project_id = ? ORDER BY i.created_at",
                    (project_id,),
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def record_unreconciled_charge(
        self,
        task_id: str,
        request_fingerprint: str,
        *,
        actual_cost: float,
        currency: str,
        unit: str,
        external_id: str | None,
    ) -> None:
        """Durably record an observed charge in an incompatible unit without converting it."""
        if (
            isinstance(actual_cost, bool)
            or not isinstance(actual_cost, (int, float))
            or not math.isfinite(actual_cost)
            or actual_cost < 0
        ):
            raise ValidationError("observed actual charge must be finite and non-negative")
        if (
            not isinstance(currency, str)
            or not currency
            or len(currency) > 64
            or not isinstance(unit, str)
            or not unit
            or len(unit) > 64
        ):
            raise ValidationError("observed currency and unit must be bounded strings")
        if external_id is not None and (
            not isinstance(external_id, str) or not external_id or len(external_id) > 256
        ):
            raise ValidationError("external id must be null or bounded")
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM generic_operation_intents WHERE task_id = ?", (task_id,)
            ).fetchone()
            if (
                row is None
                or row["request_fingerprint"] != request_fingerprint
                or row["status"] not in {"SUBMITTING", "UNCERTAIN", "COMPLETED"}
            ):
                if row is None:
                    raise ValidationError("Cannot record a charge without a provider intent")
                raise ReconciliationRequired(
                    "Unreconciled charge does not match an active generic provider intent",
                    task_id=task_id,
                    execution_id=row["execution_id"],
                )
            _insert_audit_event(
                conn,
                AuditEvent(
                    generate_id("AUDIT"),
                    "GenericOperationIntent",
                    row["id"],
                    "GENERIC_PROVIDER_CHARGE_UNIT_MISMATCH",
                    "WorkflowEngine",
                    previous_state=row["status"],
                    new_state=row["status"],
                    details={
                        "task_id": task_id,
                        "request_fingerprint": request_fingerprint,
                        "observed_actual_cost": float(actual_cost),
                        "observed_currency": currency,
                        "observed_unit": unit,
                        "approved_currency": row["currency"],
                        "approved_unit": row["unit"],
                        "external_id": external_id,
                        "settlement": "UNRECONCILED_NO_CONVERSION",
                    },
                ),
            )

    def reconcile(
        self,
        task_id: str,
        *,
        expected_request_fingerprint: str,
        expected_status: str,
        status: str,
        actual_cost: float | None,
        external_id: str | None,
        actor: str,
        comment: str,
        evidence_refs: list[str],
    ) -> dict[str, Any]:
        """Audited compare-and-set reconciliation; never launches or resubmits providers."""
        if status not in {"FAILED", "RECONCILED"} or expected_status not in {
            "SUBMITTING",
            "UNCERTAIN",
            "COMPLETED",
            "FAILED",
        }:
            raise ValidationError("unsupported reconciliation state transition")
        if (
            not isinstance(actor, str)
            or not actor.strip()
            or len(actor) > 128
            or not isinstance(comment, str)
            or not comment.strip()
            or len(comment) > 4000
            or not isinstance(evidence_refs, list)
            or not evidence_refs
            or len(evidence_refs) > 128
            or any(not isinstance(x, str) or not x.strip() or len(x) > 2048 for x in evidence_refs)
        ):
            raise ValidationError("actor, comment, and external evidence references are required")
        if actual_cost is not None and (
            isinstance(actual_cost, bool)
            or not isinstance(actual_cost, (int, float))
            or not math.isfinite(actual_cost)
            or actual_cost < 0
        ):
            raise ValidationError("actual_cost must be null or non-negative")
        with self.db.transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM generic_operation_intents WHERE task_id = ?", (task_id,)
            ).fetchone()
            if (
                row is None
                or row["request_fingerprint"] != expected_request_fingerprint
                or row["status"] != expected_status
            ):
                if row is None:
                    raise ValidationError("Generic provider intent no longer exists")
                raise ReconciliationRequired(
                    "Intent changed since inspection; refresh before reconciling",
                    task_id=task_id,
                    execution_id=row["execution_id"],
                )
            if external_id is not None and row["external_id"] not in (None, external_id):
                raise ValidationError("external id cannot be rewritten during reconciliation")
            if external_id is not None and (
                not isinstance(external_id, str) or not external_id or len(external_id) > 256
            ):
                raise ValidationError("external id must be a bounded non-empty string")
            if (
                row["actual_cost"] is not None
                and actual_cost is not None
                and float(row["actual_cost"]) != float(actual_cost)
            ):
                raise ValidationError(
                    "reconciliation cannot contradict an already recorded actual cost"
                )
            if (
                status == "RECONCILED"
                and row["status"] != "COMPLETED"
                and actual_cost is None
                and row["actual_cost"] is None
            ):
                raise ValidationError("reconciled result requires a known terminal cost")
            if (
                status == "RECONCILED"
                and row["status"] == "COMPLETED"
                and actual_cost is None
                and row["actual_cost"] is None
            ):
                raise ValidationError(
                    "completed provider result requires cost reconciliation evidence"
                )
            previous = row["status"]
            effective_cost = actual_cost if actual_cost is not None else row["actual_cost"]
            if status == "FAILED" and effective_cost is None:
                raise ValidationError(
                    "failed provider intent remains blocked until its actual charge, including zero, is known"
                )
            ledger_account = CostLedgerRepository(self.db).operation_account(task_id, conn)
            if effective_cost is not None and not ledger_account.settled:
                settlement = plan_settlement(
                    ledger_account,
                    float(effective_cost),
                    source="operator_reconciliation",
                    actor=actor.strip(),
                    reason="Audited generic provider cost reconciliation",
                    execution_id=row["execution_id"],
                    intent_id=row["id"],
                    request_fingerprint=expected_request_fingerprint,
                    cost_unit=row["cost_unit"],
                )
                for entry in settlement:
                    _insert_ledger_entry(conn, entry)
            elif (
                effective_cost is not None
                and ledger_account.settled
                and abs(ledger_account.settled_total - float(effective_cost)) > 1e-9
            ):
                raise ValidationError(
                    "reconciliation cost contradicts the already-settled ledger amount"
                )
            updated_count = conn.execute(
                "UPDATE generic_operation_intents SET status = ?, external_id = COALESCE(external_id, ?), actual_cost = COALESCE(actual_cost, ?), updated_at = ? WHERE task_id = ? AND request_fingerprint = ? AND status = ?",
                (
                    status,
                    external_id,
                    actual_cost,
                    utc_now_iso(),
                    task_id,
                    expected_request_fingerprint,
                    expected_status,
                ),
            ).rowcount
            if updated_count != 1:
                raise ReconciliationRequired(
                    "Intent changed during reconciliation",
                    task_id=task_id,
                    execution_id=row["execution_id"],
                )
            _insert_audit_event(
                conn,
                AuditEvent(
                    generate_id("AUDIT"),
                    "GenericOperationIntent",
                    row["id"],
                    "GENERIC_OPERATION_RECONCILED",
                    actor,
                    previous_state=previous,
                    new_state=status,
                    details={
                        "task_id": task_id,
                        "request_fingerprint": expected_request_fingerprint,
                        "external_id": external_id or row["external_id"],
                        "actual_cost": effective_cost,
                        "over_approved_maximum": effective_cost is not None
                        and float(effective_cost) > float(row["maximum_cost"]),
                        "ledger_cost_unit": row["cost_unit"],
                        "comment": comment,
                        "evidence_refs": evidence_refs,
                    },
                ),
            )
            updated = conn.execute(
                "SELECT * FROM generic_operation_intents WHERE task_id = ?", (task_id,)
            ).fetchone()
            return dict(updated)

    def ensure_project_cost_unit(self, project_id: str, cost_unit: str) -> None:
        conn = self.db.connect()
        try:
            rows = conn.execute(
                "SELECT DISTINCT cost_unit FROM cost_ledger WHERE project_id = ? AND entry_type IN ('RESERVE', 'SETTLE', 'ADJUSTMENT') LIMIT 2",
                (project_id,),
            ).fetchall()
            units = {item["cost_unit"] for item in rows}
            if units and units != {cost_unit}:
                raise ValidationError(
                    "Project cost ledger uses a different currency/unit; implicit conversion is forbidden",
                    details={"existing": sorted(units), "requested": cost_unit},
                )
        finally:
            conn.close()


class DatabaseAuthorizationVerifier:
    """Thin typed port adapter passed to provider executors immediately before dispatch."""

    def __init__(self, repository: GenericOperationRepository) -> None:
        self.repository = repository

    def verify(
        self, authorization: Any, provider_id: str, request_fingerprint: str, operation_hash: str
    ) -> bool:
        return self.repository.verify(
            authorization, provider_id, request_fingerprint, operation_hash
        )
