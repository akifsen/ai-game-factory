"""End-to-end generic Factory wiring tests using explicitly marked local fakes."""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.migrations import MigrationRunner
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    ExecutionRepository,
    ProjectRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.agents.registry import AgentRegistry
from gamefactory.core.approvals.approval_service import ApprovalService
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.agent_contracts import (
    AgentDefinition,
    AgentKind,
    AgentOutcome,
    AgentResultProposal,
    CostConstraints,
    ProposedFile,
)
from gamefactory.core.domain.factory_workflow import FactoryWorkflowManifest
from gamefactory.core.domain.game_quality import QualityFinding, QualityReport
from gamefactory.core.domain.models import Project, Task, Workflow, WorkflowStatus
from gamefactory.core.domain.provider_execution import (
    ProviderExecutionStatus,
    ProviderOutputFile,
    ProviderRun,
)
from gamefactory.core.policies.policy_engine import PolicyEngine
from gamefactory.workflows.engine import WorkflowEngine
from gamefactory.workflows.factory_workflow import (
    ExecutorRegistry,
    GateExecutorRegistry,
    build_workflow,
    register_factory_handlers,
)
from gamefactory.workflows.handlers import TaskHandlerRegistry

_BASE = b"extends Node\n# baseline from the fixture\n"
_AFTER = b"extends Node\n# proposed by the fixture executor\n"
_EXECUTOR_HASH = hashlib.sha256(b"test-only-factory-executor-v1").hexdigest()
_GATE_HASH = hashlib.sha256(b"test-only-code-gate-v1").hexdigest()


class _TestOnlyExecutor:
    """Deterministic test adapter; never contacts or represents a real provider."""

    provider_id = "fixture-executor"
    config_fingerprint = _EXECUTOR_HASH
    is_configured = True

    def __init__(self) -> None:
        self.dispatches = 0
        self.before_return: Any | None = None

    def execute(
        self,
        contract: Any,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: Any,
        authorization_verifier: Any,
    ) -> ProviderRun:
        assert authorization_verifier.verify(
            authorization,
            self.provider_id,
            request_fingerprint,
            authorization.operation_hash,
        )
        assert any(source.path == "scripts/player.gd" for source in context.sources)
        self.dispatches += 1
        if self.before_return is not None:
            self.before_return(contract)
        encoded = base64.b64encode(_AFTER).decode("ascii")
        before_hash = hashlib.sha256(_BASE).hexdigest()
        after_hash = hashlib.sha256(_AFTER).hexdigest()
        proposal = AgentResultProposal(
            schema_version="1.0.0",
            task_id=contract.task_id,
            agent_id=contract.selected_agent_id,
            outcome=AgentOutcome.PROPOSED,
            summary="Test fixture proposes a single bounded script update.",
            proposed_files=(
                ProposedFile(
                    path="scripts/player.gd",
                    operation="UPDATE",
                    content_base64=encoded,
                    before_sha256=before_hash,
                    output_sha256=after_hash,
                ),
            ),
        )
        staged = ProviderOutputFile(
            path="scripts/player.gd",
            content_base64=encoded,
            sha256=after_hash,
            media_type="text/x-gdscript",
            expected_before_sha256=before_hash,
        )
        return ProviderRun(
            schema_version="provider-run-1.0.0",
            status=ProviderExecutionStatus.COMPLETED,
            proposal=proposal,
            files=(staged,),
            actual_cost=0.0,
            cost_currency="USD",
            cost_unit="request",
        )


class _TestOnlyCodeGate:
    """Test wiring stub, not a Godot validator or production quality claim."""

    config_fingerprint = _GATE_HASH

    def __init__(self, *, passes: bool) -> None:
        self.passes = passes
        self.calls = 0

    def evaluate(
        self,
        candidate_workspace: Path,
        candidate_sha256: str,
        *,
        gate: str,
        task_spec: Any,
        parameters: dict[str, Any],
    ) -> dict[str, Any]:
        assert gate == "code"
        assert (candidate_workspace / "scripts" / "player.gd").read_bytes() == _AFTER
        receipt = parameters["record_process_intent"](
            "test-only-candidate-fixture",
            {
                "candidate_sha256": candidate_sha256,
                "runner_config_sha256": self.config_fingerprint,
                "purpose": "test-only gate wiring; no operating-system process is launched",
            },
        )
        self.calls += 1
        evidence = json.dumps(
            {"test_fixture": True, "candidate_sha256": candidate_sha256},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        report = QualityReport(
            subject="code",
            findings=(
                QualityFinding(
                    rule_id="test-fixture-candidate-bytes",
                    status="PASS" if self.passes else "FAIL",
                    observed={"fixture_bytes_match": True},
                    expected={"test_only": True},
                    artifact_sha256=candidate_sha256,
                    evidence_refs=(),
                    message="TEST FIXTURE ONLY: checks workflow wiring, not product quality.",
                ),
            ),
        )
        return {
            "candidate_sha256": candidate_sha256,
            "process_receipt_ids": [receipt],
            "evidence_files": [
                {
                    "name": "fixture-validator-evidence.json",
                    "sha256": hashlib.sha256(evidence).hexdigest(),
                    "content_base64": base64.b64encode(evidence).decode("ascii"),
                    "media_type": "application/json",
                    "purpose": "test-only gate adapter evidence",
                }
            ],
            "report": report.to_dict(),
        }


def _workflow_setup(
    tmp_path: Path, *, gate_passes: bool
) -> tuple[
    Path,
    Path,
    Database,
    WorkflowEngine,
    Workflow,
    list[Task],
    _TestOnlyExecutor,
    _TestOnlyCodeGate,
]:
    project_root = tmp_path / "project"
    (project_root / "scripts").mkdir(parents=True)
    (project_root / "project.godot").write_text("config_version=5\n", encoding="utf-8")
    game_file = project_root / "scripts" / "player.gd"
    game_file.write_bytes(_BASE)
    db = Database(project_root / ".gamefactory" / "state" / "factory.db")
    MigrationRunner(db).apply_all()
    project = Project("project-e2e", "Factory E2E fixture", "godot", str(project_root))
    ProjectRepository(db).save(project)
    manifest = FactoryWorkflowManifest.model_validate_json(
        json.dumps(
            {
                "schema_version": "factory-workflow-1.0.0",
                "workflow_id": "workflow-e2e",
                "project_id": project.id,
                "name": "Test-only generic feature workflow",
                "tasks": [
                    {
                        "task_id": "player-change",
                        "name": "Propose player change",
                        "family": "feature",
                        "kind": "code",
                        "executor_id": "fixture-executor",
                        "agent_id": "fixture-agent",
                        "objective": "Produce one bounded, reviewable player script update.",
                        "prompt_template_id": "test-feature",
                        "prompt_template_version": "1.0.0",
                        "capabilities": ["factory.propose"],
                        "inputs": [
                            {"path": "scripts/player.gd", "purpose": "Approved baseline file"}
                        ],
                        "game_write": True,
                        "output_scopes": ["scripts/player.gd"],
                        "expected_artifacts": ["scripts/player.gd"],
                        "required_gates": ["code"],
                    }
                ],
            }
        )
    )
    workflow, tasks = build_workflow(manifest)
    agents = AgentRegistry()
    agents.register(
        AgentDefinition(
            schema_version="1.0.0",
            agent_id="fixture-agent",
            display_name="Test-only feature agent",
            role="Produces deterministic fixture proposals for integration tests.",
            kinds=(AgentKind.CODE,),
            capabilities=("factory.propose",),
            cost=CostConstraints(),
        )
    )
    executor = _TestOnlyExecutor()
    executors = ExecutorRegistry()
    executors.register(
        "fixture-executor",
        executor,
        capabilities={"factory.propose"},
        config_fingerprint=_EXECUTOR_HASH,
    )
    gate = _TestOnlyCodeGate(passes=gate_passes)
    gate_registry = GateExecutorRegistry()
    gate_registry.register("code", gate, config_fingerprint=_GATE_HASH)
    handlers = TaskHandlerRegistry()
    register_factory_handlers(
        handlers,
        project_root=project_root,
        db=db,
        executor_registry=executors,
        agent_registry=agents,
        gate_registry=gate_registry,
    )
    engine = WorkflowEngine(
        project_root,
        db,
        policy_engine=PolicyEngine(),
        handler_registry=handlers,
    )
    engine.register_workflow(workflow, tasks)
    return project_root, game_file, db, engine, workflow, tasks, executor, gate


def _approve_pending(engine: WorkflowEngine, db: Database, approval_id: str) -> None:
    approvals = ApprovalRepository(db)
    approval = approvals.get(approval_id)
    assert approval is not None
    workflow = WorkflowRepository(db).get(approval.workflow_id)
    task = TaskRepository(db).get(approval.task_id)
    assert workflow is not None and task is not None
    inputs = engine.approval_inputs(workflow, task)
    approved = ApprovalService.approve(
        approval,
        actor="integration-test-operator",
        comment="Accept exact generated operation inputs for this test fixture.",
        current_inputs=inputs,
    )
    approvals.save(approved)


def _add_unrelated_provider_decision(db: Database, *, rejected: bool) -> None:
    approval = ApprovalService.create_request(
        "workflow-e2e",
        "player-change",
        "factory_provider_call",
        "Unrelated candidate identity used to test approval selection.",
        operation_inputs={"unrelated_candidate_revision": "stale"},
    )
    if rejected:
        approval = ApprovalService.reject(
            approval,
            actor="integration-test-operator",
            comment="Reject this unrelated stale candidate only.",
        )
    else:
        approval = ApprovalService.approve(
            approval,
            actor="integration-test-operator",
            comment="Approve this unrelated stale candidate only.",
            current_inputs={"unrelated_candidate_revision": "stale"},
        )
    approval.requested_at = "2099-12-31T23:59:59+00:00"
    ApprovalRepository(db).save(approval)


@pytest.mark.parametrize("gate_passes", [True, False])
@pytest.mark.parametrize("orphan_proposal", [False, True])
def test_generic_game_write_requires_provider_gate_and_separate_human_acceptance(
    tmp_path: Path, gate_passes: bool, orphan_proposal: bool
) -> None:
    root, game_file, db, engine, workflow, tasks, executor, gate = _workflow_setup(
        tmp_path, gate_passes=gate_passes
    )
    if orphan_proposal:

        def seed_orphan(contract: Any) -> None:
            manager = ArtifactManager(root)
            repository = ArtifactRepository(db)
            orphan_bytes = b"orphan bytes from an earlier failed attempt"
            orphan_hash = hashlib.sha256(orphan_bytes).hexdigest()
            output_rel = (
                f".gamefactory/artifacts/factory/{workflow.id}/{contract.task_id}/"
                "orphan-failed-attempt/outputs/orphan"
            )
            output_path = root / Path(*output_rel.split("/"))
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(orphan_bytes)
            output_artifact = manager.register_file_artifact(
                workflow.id,
                contract.task_id,
                "factory_output:text/x-gdscript",
                "test-orphan-seed",
                output_rel,
            )
            repository.save(output_artifact)
            proposal_rel = (
                f".gamefactory/artifacts/factory/{workflow.id}/{contract.task_id}/"
                "orphan-failed-attempt/proposal.json"
            )
            orphan_proposal_artifact = manager.create_text_artifact(
                workflow.id,
                contract.task_id,
                "factory_proposal",
                "test-orphan-seed",
                proposal_rel,
                json.dumps(
                    {
                        "schema_version": "factory-provider-proposal-1.0.0",
                        "execution_id": "failed-earlier-attempt",
                        "request_fingerprint": "0" * 64,
                        "operation_hash": "0" * 64,
                        "files": [
                            {
                                "path": "scripts/player.gd",
                                "sha256": orphan_hash,
                                "expected_before_sha256": hashlib.sha256(_BASE).hexdigest(),
                                "artifact_id": output_artifact.id,
                            }
                        ],
                    },
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            repository.save(orphan_proposal_artifact)
            # Deliberately leave the orphan proposal unlinked to any completed
            # execution. It is older than the real proposal and must be ignored.

        executor.before_return = seed_orphan
    result = engine.run_workflow(workflow.id)
    approved_types: list[str] = []
    provider_approval_pending = ApprovalRepository(db).get(result.pending_approval_id or "")
    assert provider_approval_pending is not None
    assert provider_approval_pending.approval_type == "factory_provider_call"
    _add_unrelated_provider_decision(db, rejected=False)
    repeated = engine.run_workflow(workflow.id)
    assert repeated.pending_approval_id == provider_approval_pending.id
    assert executor.dispatches == 0
    result = repeated
    for _ in range(5):
        if not result.pending_approval_id:
            break
        pending = ApprovalRepository(db).get(result.pending_approval_id)
        assert pending is not None
        approved_types.append(pending.approval_type)
        _approve_pending(engine, db, pending.id)
        if pending.approval_type == "factory_provider_call":
            _add_unrelated_provider_decision(db, rejected=True)
        result = engine.run_workflow(workflow.id)

    assert executor.dispatches == 1, (
        f"provider did not dispatch; status={result.status}, pending={result.pending_approval_id}, "
        f"error={result.error_message}, approvals={approved_types}"
    )
    # A passing candidate-validation gate runs again against the final combined
    # candidate; a failed first gate correctly stops before that second pass.
    assert gate.calls == (2 if gate_passes else 1)
    assert "factory_provider_call" in approved_types
    assert "factory_candidate_validation" in approved_types
    if gate_passes:
        assert "factory_game_write" in approved_types
        assert result.status == WorkflowStatus.COMPLETED
        assert game_file.read_bytes() == _AFTER
        applications = [
            item
            for item in ArtifactRepository(db).list_by_workflow(workflow.id)
            if item.artifact_type == "factory_application"
        ]
        assert len(applications) == 1
        latest_gate = next(
            item
            for item in ArtifactRepository(db).list_by_workflow(workflow.id)
            if item.artifact_type == "factory_quality_report"
        )
        report = json.loads((root / latest_gate.relative_path).read_text(encoding="utf-8"))
        assert report["process_receipt_ids"]
        assert report["physical_evidence"]
        assert report["report"]["findings"][0]["message"].startswith("TEST FIXTURE ONLY")
    else:
        assert "factory_game_write" not in approved_types
        assert result.status != WorkflowStatus.COMPLETED
        assert game_file.read_bytes() == _BASE
        assert not [
            item
            for item in ArtifactRepository(db).list_by_workflow(workflow.id)
            if item.artifact_type == "factory_application"
        ]
    assert WorkflowRepository(db).get(workflow.id) is not None
    assert ExecutionRepository(db).get_latest_attempt(tasks[0].id) is not None
