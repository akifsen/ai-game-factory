"""Approval selection during normal retry of a recovered Factory apply."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from test_factory_apply_recovery import (
    _ACCEPT,
    _AFTER,
    _WORKFLOW,
    _case,
    _recover,
)

from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    ExecutionRepository,
    ProviderInvocationRepository,
    TaskRepository,
)
from gamefactory.core.domain.models import ApprovalStatus, TaskStatus, WorkflowStatus


def _copy_approval(
    case: dict[str, Any],
    *,
    approval_id: str,
    status: ApprovalStatus,
    requested_at: str,
    operation_hash: str | None = None,
):
    source = case["approval"]
    approval = replace(
        source,
        id=approval_id,
        status=status,
        requested_at=requested_at,
        operation_hash=operation_hash or source.operation_hash,
        actor="historical-operator" if status != ApprovalStatus.PENDING else None,
        comment="Historical decision" if status != ApprovalStatus.PENDING else None,
        decided_at=requested_at if status != ApprovalStatus.PENDING else None,
    )
    ApprovalRepository(case["db"]).save(approval)
    return approval


def _recovered_case(tmp_path: Path) -> dict[str, Any]:
    case = _case(tmp_path, acceptance_task_status=TaskStatus.FAILED)
    recovered = _recover(case)
    assert recovered["retry_ready"] is True
    return case


def _assert_no_extra_apply(case: dict[str, Any]) -> None:
    assert case["game_target"].read_bytes() == _AFTER
    application_artifacts = [
        item
        for item in ArtifactRepository(case["db"]).list_by_workflow(_WORKFLOW)
        if item.artifact_type == "factory_application"
    ]
    assert len(application_artifacts) == 1
    journal = json.loads(case["journal_path"].read_text(encoding="utf-8"))
    assert journal["status"] == "FINALIZATION_READY"
    assert journal["execution_id"] == "accept-old-run"
    assert ProviderInvocationRepository(case["db"]).count(_WORKFLOW) == 0


def test_latest_matching_pending_approval_blocks_recovered_acceptance(tmp_path: Path) -> None:
    case = _recovered_case(tmp_path)
    pending = _copy_approval(
        case,
        approval_id="pending-same-operation",
        status=ApprovalStatus.PENDING,
        requested_at="2099-12-31T23:59:59+00:00",
    )

    result = case["engine"].retry_task(_WORKFLOW, _ACCEPT)

    assert result.status == WorkflowStatus.BLOCKED
    assert result.pending_approval_id == pending.id
    assert TaskRepository(case["db"]).get(_ACCEPT).status == TaskStatus.BLOCKED
    assert len(ExecutionRepository(case["db"]).list_by_task(_ACCEPT)) == 1
    _assert_no_extra_apply(case)


@pytest.mark.parametrize(
    "matching_status", [ApprovalStatus.REJECTED, ApprovalStatus.CHANGES_REQUESTED]
)
def test_matching_rejection_is_not_overridden_by_a_newer_unrelated_approval(
    tmp_path: Path, matching_status: ApprovalStatus
) -> None:
    case = _recovered_case(tmp_path)
    rejection = _copy_approval(
        case,
        approval_id=f"matching-{matching_status.value.lower()}",
        status=matching_status,
        requested_at="2099-12-30T23:59:59+00:00",
    )
    unrelated_approved = _copy_approval(
        case,
        approval_id="newer-unrelated-approved",
        status=ApprovalStatus.APPROVED,
        requested_at="2099-12-31T23:59:59+00:00",
        operation_hash="e" * 64,
    )

    result = case["engine"].retry_task(_WORKFLOW, _ACCEPT)

    expected_status = (
        WorkflowStatus.FAILED
        if matching_status == ApprovalStatus.REJECTED
        else WorkflowStatus.BLOCKED
    )
    expected_task_status = (
        TaskStatus.FAILED if matching_status == ApprovalStatus.REJECTED else TaskStatus.BLOCKED
    )
    assert result.status == expected_status
    assert TaskRepository(case["db"]).get(_ACCEPT).status == expected_task_status
    assert len(ExecutionRepository(case["db"]).list_by_task(_ACCEPT)) == 1
    assert ApprovalRepository(case["db"]).get(rejection.id).status == matching_status
    assert (
        ApprovalRepository(case["db"]).get(unrelated_approved.id).status == ApprovalStatus.APPROVED
    )
    _assert_no_extra_apply(case)


def test_matching_approval_is_used_despite_newer_unrelated_rejection(tmp_path: Path) -> None:
    case = _recovered_case(tmp_path)
    matching_approved = case["approval"]
    unrelated_rejected = _copy_approval(
        case,
        approval_id="newer-unrelated-rejected",
        status=ApprovalStatus.REJECTED,
        requested_at="2099-12-31T23:59:59+00:00",
        operation_hash="f" * 64,
    )

    result = case["engine"].retry_task(_WORKFLOW, _ACCEPT)

    assert result.status == WorkflowStatus.COMPLETED
    assert (
        ApprovalRepository(case["db"]).get(matching_approved.id).status == ApprovalStatus.APPROVED
    )
    assert (
        ApprovalRepository(case["db"]).get(unrelated_rejected.id).status == ApprovalStatus.REJECTED
    )
    assert len(ExecutionRepository(case["db"]).list_by_task(_ACCEPT)) == 2
    _assert_no_extra_apply(case)
