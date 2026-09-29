"""Evidence-driven failure recovery and classification module.

Provides structured classification of workflow and task failures, proving
pre-launch tool configuration failures from stored evidence and governing safe
reclassification and retry.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from gamefactory.adapters.persistence.repositories import ProviderOperationIntent
from gamefactory.core.domain.models import (
    Artifact,
    AuditEvent,
    CostClass,
    Execution,
    ExecutionStatus,
    Task,
    TaskStatus,
)

RECOVERY_POLICY = "recovery-policy-0.6.0"


class RecoveryCategory(StrEnum):
    PRE_EXECUTION_TOOL_CONFIGURATION = "PRE_EXECUTION_TOOL_CONFIGURATION"
    TOOL_EXECUTION_FAILURE = "TOOL_EXECUTION_FAILURE"
    ENGINE_IMPORT_FAILURE = "ENGINE_IMPORT_FAILURE"
    RUNTIME_VALIDATION_FAILURE = "RUNTIME_VALIDATION_FAILURE"
    ASSET_VALIDATION_FAILURE = "ASSET_VALIDATION_FAILURE"
    PRODUCTION_READINESS_FAILURE = "PRODUCTION_READINESS_FAILURE"
    PAID_SUBMISSION_UNCERTAIN = "PAID_SUBMISSION_UNCERTAIN"
    PROVIDER_TERMINAL_FAILURE = "PROVIDER_TERMINAL_FAILURE"
    PROVIDER_RESULT_REUSABLE = "PROVIDER_RESULT_REUSABLE"
    PRE_SUBMISSION_FAILURE = "PRE_SUBMISSION_FAILURE"
    APPROVAL_DECISION = "APPROVAL_DECISION"
    UNKNOWN = "UNKNOWN"


Category = RecoveryCategory

PRE_LAUNCH_SIGNATURES: tuple[str, ...] = (
    r"Godot executable is required",
    r"Configured Godot executable does not exist",
    r"Configured Godot path is not a regular file",
    r"Configured Godot executable is not executable",
    r"Blender executable not available",
    r"Executable not found",
)


@dataclass
class RecoveryAssessment:
    category: RecoveryCategory
    evidence: dict[str, Any]
    eligible_for_reclassification: bool
    refusal_reasons: list[str]
    safe_actions: list[str]
    provider_safety: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category.value,
            "evidence": self.evidence,
            "eligible_for_reclassification": self.eligible_for_reclassification,
            "refusal_reasons": self.refusal_reasons,
            "safe_actions": self.safe_actions,
            "provider_safety": self.provider_safety,
        }


def classify(
    task: Task,
    execution: Execution | None,
    intent: ProviderOperationIntent | None,
    audit_events: Sequence[AuditEvent],
    task_artifacts: Sequence[Artifact],
    root: Path | None = None,
) -> RecoveryAssessment:
    """Classify a task/execution failure against recovery policy from durable evidence."""
    # 1. Extract error code from matching FAILED audit event
    error_code: str | None = None
    if execution is not None:
        for event in audit_events:
            if (
                event.action == "FAILED"
                and event.details.get("attempt") == execution.attempt_number
            ):
                val = event.details.get("error_code")
                if val is not None:
                    error_code = str(val)
                break

    # 2. Extract process and artifact evidence
    stdout_present = bool(execution and execution.stdout)
    stderr_present = bool(execution and execution.stderr)
    evidence: dict[str, Any] = {
        "error_code": error_code,
        "pid": execution.pid if execution else None,
        "exit_code": execution.exit_code if execution else None,
        "stdout_present": stdout_present,
        "stderr_present": stderr_present,
        "stdout": execution.stdout if execution else None,
        "stderr": execution.stderr if execution else None,
    }

    stage_dir_exists = False
    stage_dir_path: Path | None = None
    matching_log_artifacts: list[str] = []
    has_import_or_render_log = False

    if execution is not None:
        if root is not None:
            stage_dir_path = (
                root / ".gamefactory" / "scratch" / f"asset-{task.workflow_id}-{execution.id}"
            )
            stage_dir_exists = stage_dir_path.exists()

        for art in task_artifacts:
            if art.artifact_type in {"asset-godot-import-log", "asset-godot-render-log"}:
                if execution.id in art.relative_path:
                    matching_log_artifacts.append(art.relative_path)
        has_import_or_render_log = bool(matching_log_artifacts)

    if task.task_type == "asset_godot":
        evidence["stage_dir_exists"] = stage_dir_exists
        evidence["stage_dir"] = str(stage_dir_path) if stage_dir_path else None
        evidence["import_or_render_logs"] = matching_log_artifacts
        evidence["has_import_or_render_log"] = has_import_or_render_log

    is_paid = task.cost_class in (
        CostClass.PAID,
        CostClass.METERED,
        CostClass.EXPENSIVE,
    ) or task.task_type in {"asset_paid_generation", "paid_generation"}

    # 3. Determine Category and Provider Safety
    safe_actions: list[str] = []
    category: RecoveryCategory
    provider_safety: str

    matches_signature = bool(
        execution
        and execution.error_message
        and any(re.search(pat, execution.error_message) for pat in PRE_LAUNCH_SIGNATURES)
    )
    no_process_launched = bool(
        execution
        and execution.pid is None
        and execution.exit_code is None
        and not execution.stdout
        and not execution.stderr
    )

    if is_paid:
        if intent is None:
            category = RecoveryCategory.PRE_SUBMISSION_FAILURE
            provider_safety = "Failed prior to provider submission; zero provider impact."
        elif intent.status in ("SUBMITTING", "UNCERTAIN") and not intent.external_task_id:
            category = RecoveryCategory.PAID_SUBMISSION_UNCERTAIN
            provider_safety = "BLOCKED: provider state uncertain; reclassification could allow a second provider submission."
            safe_actions.append(
                "provider-side reconciliation required; reservation stays held; gamefactory accounting reconcile will refuse"
            )
        elif intent.status in ("SUBMITTED", "SUCCEEDED") and intent.external_task_id:
            category = RecoveryCategory.PROVIDER_RESULT_REUSABLE
            provider_safety = (
                "SAFE_QUERY_ONLY: provider result exists; resume will query without regenerating."
            )
            safe_actions.append(f"gamefactory resume {task.workflow_id}")
        elif intent.status == "FAILED":
            category = RecoveryCategory.PROVIDER_TERMINAL_FAILURE
            provider_safety = "TERMINAL: provider operation failed; reclassification could allow a second provider submission."
            safe_actions.append("allocate a new asset revision")
        elif intent.status in ("SUBMITTING", "UNCERTAIN"):
            category = RecoveryCategory.PAID_SUBMISSION_UNCERTAIN
            provider_safety = "BLOCKED: provider state uncertain; reclassification could allow a second provider submission."
            safe_actions.append(
                "provider-side reconciliation required; reservation stays held; gamefactory accounting reconcile will refuse"
            )
        else:
            category = RecoveryCategory.UNKNOWN
            provider_safety = "reclassification could allow a second provider submission"
    else:
        provider_safety = "Not a paid task; zero provider impact."
        if (
            matches_signature
            and no_process_launched
            and task.task_type in {"asset_godot", "asset_process"}
            and error_code in (None, "TOOL_UNAVAILABLE", "ENGINE_IMPORT_FAILED", "FACTORY_ERROR")
            and (
                task.task_type != "asset_godot"
                or (not stage_dir_exists and not has_import_or_render_log)
            )
        ):
            category = RecoveryCategory.PRE_EXECUTION_TOOL_CONFIGURATION
        elif error_code == "RUNTIME_VALIDATION_FAILED":
            category = RecoveryCategory.RUNTIME_VALIDATION_FAILURE
        elif error_code == "ASSET_VALIDATION_FAILED":
            category = RecoveryCategory.ASSET_VALIDATION_FAILURE
        elif (
            error_code == "PRODUCTION_READINESS_FAILED"
            or task.task_type == "asset_production_readiness"
        ):
            category = RecoveryCategory.PRODUCTION_READINESS_FAILURE
        elif error_code == "ENGINE_IMPORT_FAILED":
            category = RecoveryCategory.ENGINE_IMPORT_FAILURE
        elif error_code in {"TOOL_UNAVAILABLE", "TOOL_EXECUTION_ERROR"}:
            category = RecoveryCategory.TOOL_EXECUTION_FAILURE
        elif (
            task.task_type in {"asset_concept_review", "asset_final_review"}
            or error_code == "VISUAL_REVIEW_REJECTED"
        ):
            category = RecoveryCategory.APPROVAL_DECISION
        elif task.status == TaskStatus.BLOCKED:
            category = RecoveryCategory.APPROVAL_DECISION
        elif matches_signature:
            category = RecoveryCategory.TOOL_EXECUTION_FAILURE
        else:
            category = RecoveryCategory.UNKNOWN

    # 4. Determine Refusal Reasons & Eligibility for reclassification
    refusal_reasons: list[str] = []

    if is_paid:
        refusal_reasons.append("reclassification could allow a second provider submission")

    if error_code in {"RUNTIME_VALIDATION_FAILED", "ASSET_VALIDATION_FAILED"}:
        refusal_reasons.append(f"deterministic failure by configuration policy ({error_code})")

    if execution is None:
        refusal_reasons.append("No execution attempt exists for task")
    else:
        if execution.retryable:
            refusal_reasons.append("already retryable; reclassification is not applicable")

        if execution.status != ExecutionStatus.FAILED:
            refusal_reasons.append(f"Execution status is {execution.status.value}, expected FAILED")

        if (
            execution.pid is not None
            or execution.exit_code is not None
            or stdout_present
            or stderr_present
        ):
            refusal_reasons.append("a launched process (pid or exit_code set, or output present)")

        if not matches_signature:
            refusal_reasons.append("Error message does not match a pre-launch signature")

        if error_code not in (None, "TOOL_UNAVAILABLE", "ENGINE_IMPORT_FAILED", "FACTORY_ERROR"):
            refusal_reasons.append(
                f"Audit error code '{error_code}' is not in allowed pre-execution error codes"
            )

    if task.task_type not in {"asset_godot", "asset_process"}:
        refusal_reasons.append(
            f"Task type '{task.task_type}' is not in eligible recovery types ('asset_godot', 'asset_process')"
        )

    if task.task_type == "asset_godot":
        if stage_dir_exists:
            refusal_reasons.append(f"Stage directory exists: {stage_dir_path}")
        if has_import_or_render_log:
            refusal_reasons.append("Import or render log artifact exists for this execution")

    eligible = len(refusal_reasons) == 0

    # 5. Populate safe actions for non-paid tasks
    if not is_paid:
        if (
            execution is not None
            and execution.retryable
            and (execution.attempt_number < task.max_retries + 1)
        ):
            safe_actions.append(f"gamefactory retry {task.workflow_id} {task.id}")
        elif eligible and execution is not None:
            safe_actions.append(
                f"gamefactory recovery reclassify --execution {execution.id} --apply --actor <name> --reason <text>"
            )

    return RecoveryAssessment(
        category=category,
        evidence=evidence,
        eligible_for_reclassification=eligible,
        refusal_reasons=refusal_reasons,
        safe_actions=safe_actions,
        provider_safety=provider_safety,
    )
