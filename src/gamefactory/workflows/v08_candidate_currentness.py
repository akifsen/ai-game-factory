"""Latest-attempt currentness guards for V0.8-3C candidate workflow tasks."""

from __future__ import annotations

from typing import Any

from gamefactory.adapters.persistence.repositories import ExecutionRepository
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import Artifact, Execution, ExecutionStatus, Task

BLOCKING_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.FAILED,
        ExecutionStatus.RUNNING,
        ExecutionStatus.UNCERTAIN,
    }
)


class CandidateCurrentnessError(ArtifactError):
    """Bound artifact or execution attempt is not current for candidate gates."""


def assert_latest_attempt_completed(
    executions: ExecutionRepository,
    task: Task,
    *,
    purpose: str,
) -> Execution:
    latest = executions.get_latest_attempt(task.id)
    if latest is None:
        raise CandidateCurrentnessError(f"{purpose}: task '{task.name}' has no execution attempts")
    if latest.status in BLOCKING_EXECUTION_STATUSES:
        raise CandidateCurrentnessError(
            f"{purpose}: latest attempt {latest.id} for '{task.name}' "
            f"has status {latest.status.value}"
        )
    if latest.status != ExecutionStatus.COMPLETED:
        raise CandidateCurrentnessError(
            f"{purpose}: latest attempt {latest.id} for '{task.name}' "
            f"is not COMPLETED ({latest.status.value})"
        )
    return latest


def verify_artifact_bytes_and_hash(
    artifact: Artifact,
    *,
    root_path: Any,
    artifact_manager: Any,
) -> None:
    artifact_manager.verify_artifact_integrity(artifact)
    path = root_path / artifact.relative_path
    if not path.is_file():
        raise CandidateCurrentnessError(
            f"artifact {artifact.id} path missing: {artifact.relative_path}"
        )
    from gamefactory.adapters.engines.godot_staging import sha256_file

    if sha256_file(path) != artifact.content_hash:
        raise CandidateCurrentnessError(
            f"artifact {artifact.id} on-disk hash does not match registered content_hash"
        )


def artifact_bound_to_execution(artifact: Artifact, execution: Execution) -> bool:
    """True when the artifact path is bound to the exact producing execution id."""
    normalized = artifact.relative_path.replace("\\", "/")
    execution_id = execution.id
    parts = normalized.split("/")
    if execution_id in parts:
        return True
    leaf = parts[-1]
    if leaf == execution_id:
        return True
    exact_leaf_names = (
        f"source-retained-{execution_id}.glb",
        f"processed-{execution_id}.glb",
        f"raw-{execution_id}.glb",
        f"identity-{execution_id}.json",
        f"static-report-{execution_id}.json",
        f"rig-attempt-{execution_id}.json",
        f"test-only-receipt-{execution_id}.json",
        f"c2-export-marker-{execution_id}.json",
        f"specification-{execution_id}.json",
        f"profile-document-{execution_id}.json",
    )
    if leaf in exact_leaf_names:
        return True
    for segment in parts:
        if segment == f"capsule-runtime-{execution_id}":
            return True
        if segment == execution_id:
            return True
    return False


def select_artifact_for_execution(
    artifacts: list[Artifact],
    *,
    task_id: str,
    artifact_type: str,
    execution: Execution,
) -> Artifact:
    matches = [
        item
        for item in artifacts
        if item.task_id == task_id
        and item.artifact_type == artifact_type
        and artifact_bound_to_execution(item, execution)
    ]
    if len(matches) != 1:
        raise CandidateCurrentnessError(
            f"expected exactly one {artifact_type} bound to execution {execution.id}, "
            f"found {len(matches)}"
        )
    return matches[0]


def load_json_artifact(root: Any, artifact: Artifact) -> dict[str, Any]:
    from gamefactory.workflows.v08_candidate_gates import load_strict_json_artifact

    return load_strict_json_artifact(root, artifact)


def require_observation_execution_match(
    observation: dict[str, Any],
    execution: Execution,
    *,
    purpose: str,
) -> None:
    obs_exec = observation.get("execution_id")
    if obs_exec != execution.id:
        raise CandidateCurrentnessError(
            f"{purpose}: observation execution_id {obs_exec!r} "
            f"does not match latest completed {execution.id}"
        )
    attempt = observation.get("strict_attempt_number")
    if attempt != execution.attempt_number:
        raise CandidateCurrentnessError(
            f"{purpose}: observation strict_attempt_number {attempt!r} "
            f"does not match latest completed attempt {execution.attempt_number}"
        )


def assert_review_still_current(
    *,
    approval_operation_hash: str,
    current_fingerprint: str,
    purpose: str,
) -> None:
    if approval_operation_hash != current_fingerprint:
        raise CandidateCurrentnessError(
            f"{purpose}: approved review fingerprint is stale "
            f"(approved {approval_operation_hash}, current {current_fingerprint})"
        )


def assert_zero_provider_activity(db: Any, workflow_id: str) -> None:
    from gamefactory.adapters.persistence.repositories import (
        CostLedgerRepository,
        ProviderInvocationRepository,
        ProviderOperationIntentRepository,
    )

    if ProviderInvocationRepository(db).count(workflow_id) != 0:
        raise ValidationError(
            f"candidate workflow {workflow_id} must not record provider invocations"
        )
    intents = ProviderOperationIntentRepository(db).list_by_workflow(workflow_id)
    if intents:
        raise ValidationError(
            f"candidate workflow {workflow_id} must not record provider operation intents"
        )
    ledger = CostLedgerRepository(db).list_by_workflow(workflow_id)
    if ledger:
        raise ValidationError(
            f"candidate workflow {workflow_id} must not record cost ledger entries"
        )
