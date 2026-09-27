"""Sequential DAG Workflow Engine for AI Game Factory.

Coordinates:
- DAG dependency resolution and topological scheduling
- Centralized TaskStateMachine and WorkflowStateMachine transitions
- Policy checks and human approval gates
- Atomic durable execution claims and leases
- Crash recovery and paid-operation reconciliation protection
- Independent artifact SHA-256 verification and quality gates
- Execution attempt history preservation
"""

from dataclasses import dataclass, field
from pathlib import Path

from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AuditLogRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    ProviderInvocationRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.core.approvals.approval_service import (
    ApprovalService,
    compute_operation_hash,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.dag import WorkflowDAG
from gamefactory.core.domain.errors import (
    ArtifactError,
    BudgetExceeded,
    LockError,
    ReconciliationRequired,
    ToolExecutionError,
    ValidationError,
)
from gamefactory.core.domain.models import (
    ApprovalRequest,
    ApprovalStatus,
    AuditEvent,
    CostClass,
    Evidence,
    Execution,
    ExecutionStatus,
    GateStatus,
    QualityGate,
    Task,
    TaskStatus,
    Workflow,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.domain.state_machine import (
    TaskStateMachine,
    WorkflowStateMachine,
)
from gamefactory.core.execution.locks import ExecutionLock
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.core.execution.redaction import redactor
from gamefactory.core.policies.policy_engine import (
    OperationType,
    PolicyEngine,
)
from gamefactory.workflows.builtin_tasks import BuiltinTaskActions
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerRegistry,
)
from gamefactory.workflows.ports import AssetGenerationProvider


@dataclass
class WorkflowExecutionResult:
    schema_version: int = field(default=1, kw_only=True)
    workflow_id: str
    status: WorkflowStatus
    completed_tasks: list[str]
    failed_tasks: list[str]
    blocked_tasks: list[str]
    pending_approval_id: str | None = None
    error_message: str | None = None

    def __post_init__(self) -> None:
        if self.error_message is not None:
            self.error_message = redactor.redact_text(self.error_message)
        if (
            self.schema_version != 1
            or not self.workflow_id
            or not isinstance(self.status, WorkflowStatus)
        ):
            raise ValueError("Invalid workflow result schema or lifecycle status")
        if any(
            not isinstance(ids, list) or any(not isinstance(item, str) for item in ids)
            for ids in (self.completed_tasks, self.failed_tasks, self.blocked_tasks)
        ):
            raise ValueError("Workflow result task lists must contain string IDs")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "workflow_id": self.workflow_id,
            "status": self.status.value,
            "completed_tasks": list(self.completed_tasks),
            "failed_tasks": list(self.failed_tasks),
            "blocked_tasks": list(self.blocked_tasks),
            "pending_approval_id": self.pending_approval_id,
            "error_message": self.error_message,
        }


class WorkflowEngine:
    """Deterministic orchestrator for executing workflows."""

    def __init__(
        self,
        project_root: Path | str,
        db: Database,
        policy_engine: PolicyEngine | None = None,
        asset_provider: AssetGenerationProvider | None = None,
        process_runner: ProcessRunner | None = None,
        handler_registry: TaskHandlerRegistry | None = None,
    ) -> None:
        self.project_root = Path(project_root).resolve()
        self.db = db
        self.policy_engine = policy_engine or PolicyEngine()
        self.asset_provider = asset_provider
        self.handler_registry = handler_registry or TaskHandlerRegistry()
        self.process_runner = process_runner or ProcessRunner(sanitize_output=True)

        self.proj_repo = ProjectRepository(db)
        self.wf_repo = WorkflowRepository(db)
        self.task_repo = TaskRepository(db)
        self.exec_repo = ExecutionRepository(db)
        self.app_repo = ApprovalRepository(db)
        self.art_repo = ArtifactRepository(db)
        self.evi_repo = EvidenceRepository(db)
        self.gate_repo = QualityGateRepository(db)
        self.audit_repo = AuditLogRepository(db)
        self.invocation_repo = ProviderInvocationRepository(db)
        self.artifact_mgr = ArtifactManager(self.project_root)
        self.locks_dir = self.project_root / ".gamefactory" / "locks"
        self.builtin_actions = BuiltinTaskActions(
            self.project_root,
            self.artifact_mgr,
            self.art_repo,
            self.asset_provider,
            self.process_runner,
            lambda workflow_id, task_id, provider, fingerprint: self.invocation_repo.record(
                workflow_id,
                task_id,
                provider,
                fingerprint,
            ),
        )

    def register_workflow(self, workflow: Workflow, tasks: list[Task]) -> None:
        """Register a new workflow and its tasks, validating the DAG upfront."""
        WorkflowDAG(tasks)  # Validates dependencies and cycle freedom
        if any(task.workflow_id != workflow.id for task in tasks):
            raise ValidationError("Every task must belong to the registered workflow")
        self.wf_repo.save_with_tasks(
            workflow,
            tasks,
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Workflow",
                entity_id=workflow.id,
                action="REGISTERED",
                actor="System",
                details={"task_count": len(tasks)},
            ),
        )

    def run_workflow(self, workflow_id: str) -> WorkflowExecutionResult:
        """Execute or resume workflow sequentially to completion, approval pause, or failure."""
        wf = self.wf_repo.get(workflow_id)
        if not wf:
            raise ValidationError(f"Workflow '{workflow_id}' not found")

        tasks = self.task_repo.list_by_workflow(workflow_id)
        if not tasks:
            raise ValidationError(f"Workflow '{workflow_id}' has no tasks")

        dag = WorkflowDAG(tasks)
        lock = ExecutionLock(self.locks_dir, workflow_id)

        try:
            with lock.hold():
                return self._execute_dag(wf, dag)
        except LockError as exc:
            return WorkflowExecutionResult(
                workflow_id=workflow_id,
                status=wf.status,
                completed_tasks=[t.id for t in tasks if t.status == TaskStatus.COMPLETED],
                failed_tasks=[t.id for t in tasks if t.status == TaskStatus.FAILED],
                blocked_tasks=[t.id for t in tasks if t.status == TaskStatus.BLOCKED],
                error_message=str(exc),
            )

    def _execute_dag(self, wf: Workflow, dag: WorkflowDAG) -> WorkflowExecutionResult:
        """Internal execution loop held under execution lock."""
        # 1. Recover any tasks left in RUNNING status from a crashed previous process
        for t in self.task_repo.list_by_workflow(wf.id):
            if t.status == TaskStatus.RUNNING:
                latest_attempt = self.exec_repo.get_latest_attempt(t.id)
                if latest_attempt and latest_attempt.status in (
                    ExecutionStatus.RUNNING,
                    ExecutionStatus.UNCERTAIN,
                ):
                    handler_profile = self.handler_registry.metadata(t.task_type)
                    is_paid_or_metered = t.task_type == "paid_generation" or t.cost_class in (
                        CostClass.METERED,
                        CostClass.PAID,
                        CostClass.EXPENSIVE,
                    )
                    is_conservative_process = (
                        handler_profile is not None
                        and handler_profile.recovery == HandlerRecovery.CONSERVATIVE_PROCESS
                    )
                    recovery_known_terminal = False
                    recovery_callback = (
                        handler_profile.recovery_check if handler_profile is not None else None
                    )
                    if is_conservative_process and recovery_callback is not None:
                        try:
                            recovery_known_terminal = recovery_callback(wf, t, latest_attempt)
                        except Exception:
                            recovery_known_terminal = False
                    if is_paid_or_metered or is_conservative_process:
                        if (
                            is_conservative_process
                            and not is_paid_or_metered
                            and recovery_known_terminal
                        ):
                            latest_attempt.status = ExecutionStatus.FAILED
                            latest_attempt.error_message = "Orchestrator restarted after the owned process reached a durable terminal state; explicit retry required"
                            latest_attempt.retryable = True
                            latest_attempt.completed_at = utc_now_iso()
                            t.status = TaskStatus.FAILED
                            self._finalize_execution(
                                t,
                                latest_attempt,
                                AuditEvent(
                                    id=generate_id("AUDIT"),
                                    entity_type="Task",
                                    entity_id=t.id,
                                    action="RECOVERED_TERMINAL_PROCESS",
                                    actor="WorkflowEngine",
                                    details={"execution_id": latest_attempt.id, "retryable": True},
                                ),
                            )
                            continue
                        # Unknown child-process ownership is never treated as a safe retry.
                        latest_attempt.status = ExecutionStatus.UNCERTAIN
                        latest_attempt.error_message = "Process ownership or terminal state cannot be verified after orchestrator restart"
                        self.exec_repo.save(latest_attempt)
                        TaskStateMachine.validate_transition(t.id, t.status, TaskStatus.BLOCKED)
                        t.status = TaskStatus.BLOCKED
                        self.task_repo.update_status(t.id, t.status)
                        if wf.status != WorkflowStatus.BLOCKED:
                            WorkflowStateMachine.validate_transition(
                                wf.id, wf.status, WorkflowStatus.BLOCKED
                            )
                            wf.status = WorkflowStatus.BLOCKED
                            self.wf_repo.update_status(wf.id, wf.status)
                        raise ReconciliationRequired(
                            f"Crash recovery: Task '{t.id}' has uncertain owned execution attempt {latest_attempt.id}. Blind re-execution is forbidden; reconciliation required until process state is resolved.",
                            task_id=t.id,
                            execution_id=latest_attempt.id,
                        )
                    else:
                        latest_attempt.status = ExecutionStatus.FAILED
                        latest_attempt.error_message = (
                            "Process terminated unexpectedly during execution"
                        )
                        self.exec_repo.save(latest_attempt)
                        t.status = TaskStatus.PENDING
                        self.task_repo.update_status(t.id, t.status)

        # 2. Update workflow status to RUNNING if PENDING, BLOCKED, or FAILED (on retry)
        if wf.status in (WorkflowStatus.PENDING, WorkflowStatus.BLOCKED, WorkflowStatus.FAILED):
            WorkflowStateMachine.validate_transition(wf.id, wf.status, WorkflowStatus.RUNNING)
            wf.status = WorkflowStatus.RUNNING
            self.wf_repo.update_status(wf.id, wf.status)

        while True:
            # Refresh tasks from database
            current_tasks = {t.id: t for t in self.task_repo.list_by_workflow(wf.id)}
            completed_ids = {
                t.id for t in current_tasks.values() if t.status == TaskStatus.COMPLETED
            }
            failed_ids = {t.id for t in current_tasks.values() if t.status == TaskStatus.FAILED}
            blocked_ids = {t.id for t in current_tasks.values() if t.status == TaskStatus.BLOCKED}

            # If any task failed, fail workflow and exit
            if failed_ids:
                if wf.status != WorkflowStatus.FAILED:
                    WorkflowStateMachine.validate_transition(
                        wf.id, wf.status, WorkflowStatus.FAILED
                    )
                    wf.status = WorkflowStatus.FAILED
                    self.wf_repo.update_status(wf.id, wf.status)
                return WorkflowExecutionResult(
                    workflow_id=wf.id,
                    status=WorkflowStatus.FAILED,
                    completed_tasks=list(completed_ids),
                    failed_tasks=list(failed_ids),
                    blocked_tasks=list(blocked_ids),
                    error_message=f"Tasks failed: {list(failed_ids)}",
                )

            # If all tasks completed, mark workflow completed
            if len(completed_ids) == len(current_tasks):
                if wf.status != WorkflowStatus.COMPLETED:
                    WorkflowStateMachine.validate_transition(
                        wf.id, wf.status, WorkflowStatus.COMPLETED
                    )
                    wf.status = WorkflowStatus.COMPLETED
                    self.wf_repo.update_status(wf.id, wf.status)
                    self.audit_repo.append(
                        AuditEvent(
                            id=generate_id("AUDIT"),
                            entity_type="Workflow",
                            entity_id=wf.id,
                            action="COMPLETED",
                            actor="System",
                        )
                    )
                return WorkflowExecutionResult(
                    workflow_id=wf.id,
                    status=WorkflowStatus.COMPLETED,
                    completed_tasks=list(completed_ids),
                    failed_tasks=[],
                    blocked_tasks=[],
                )

            # Find next runnable task whose dependencies are satisfied
            in_progress = {t.id for t in current_tasks.values() if t.status == TaskStatus.RUNNING}
            ready_tasks = [
                t
                for t in dag.get_ready_tasks(completed_ids, in_progress)
                if t.status in (TaskStatus.PENDING, TaskStatus.BLOCKED)
            ]

            if not ready_tasks:
                # No tasks are ready to run right now. Check if any are BLOCKED
                if blocked_ids:
                    if wf.status != WorkflowStatus.BLOCKED:
                        WorkflowStateMachine.validate_transition(
                            wf.id, wf.status, WorkflowStatus.BLOCKED
                        )
                        wf.status = WorkflowStatus.BLOCKED
                        self.wf_repo.update_status(wf.id, wf.status)

                    # Find pending approval if any
                    pending_apps = self.app_repo.list_pending(wf.id)
                    pending_id = pending_apps[0].id if pending_apps else None

                    return WorkflowExecutionResult(
                        workflow_id=wf.id,
                        status=WorkflowStatus.BLOCKED,
                        completed_tasks=list(completed_ids),
                        failed_tasks=[],
                        blocked_tasks=list(blocked_ids),
                        pending_approval_id=pending_id,
                    )

                # Deadlock or unknown state
                break

            # Execute the first ready task sequentially
            task = ready_tasks[0]
            step_result = self._execute_task(wf, task)
            if step_result.status in (WorkflowStatus.BLOCKED, WorkflowStatus.FAILED):
                if (
                    step_result.status == WorkflowStatus.FAILED
                    and wf.status == WorkflowStatus.RUNNING
                ):
                    WorkflowStateMachine.validate_transition(
                        wf.id, wf.status, WorkflowStatus.FAILED
                    )
                    wf.status = WorkflowStatus.FAILED
                    self.wf_repo.update_status(wf.id, wf.status)
                return step_result

        return WorkflowExecutionResult(
            workflow_id=wf.id,
            status=wf.status,
            completed_tasks=list(completed_ids),
            failed_tasks=list(failed_ids),
            blocked_tasks=list(blocked_ids),
        )

    def _execute_task(self, wf: Workflow, task: Task) -> WorkflowExecutionResult:
        """Execute a single task with policy checks, approval gates, and crash protection."""
        if (
            task.task_type not in self.builtin_actions.task_types
            and self.handler_registry.get(task.task_type) is None
        ):
            raise ValidationError(f"Unknown task type '{task.task_type}'")
        # Artifact hashes are checked again at every resume/dispatch boundary.
        try:
            for artifact in self.art_repo.list_by_workflow(wf.id):
                self.artifact_mgr.verify_artifact_integrity(artifact)
        except ArtifactError as exc:
            if task.status != TaskStatus.BLOCKED:
                TaskStateMachine.validate_transition(task.id, task.status, TaskStatus.BLOCKED)
                task.status = TaskStatus.BLOCKED
                self.task_repo.update_status(task.id, task.status)
            if wf.status != WorkflowStatus.BLOCKED:
                WorkflowStateMachine.validate_transition(wf.id, wf.status, WorkflowStatus.BLOCKED)
                wf.status = WorkflowStatus.BLOCKED
                self.wf_repo.update_status(wf.id, wf.status)
            return WorkflowExecutionResult(
                wf.id, WorkflowStatus.BLOCKED, [], [], [task.id], error_message=str(exc)
            )
        # 1. Check for crash reconciliation requirement on paid tasks
        latest_attempt = self.exec_repo.get_latest_attempt(task.id)
        if latest_attempt and latest_attempt.status in (
            ExecutionStatus.RUNNING,
            ExecutionStatus.UNCERTAIN,
        ):
            handler_profile = self.handler_registry.metadata(task.task_type)
            if (
                task.task_type == "paid_generation"
                or task.cost_class
                in (
                    CostClass.METERED,
                    CostClass.PAID,
                    CostClass.EXPENSIVE,
                )
                or (
                    handler_profile is not None
                    and handler_profile.recovery == HandlerRecovery.CONSERVATIVE_PROCESS
                )
            ):
                # An execution was started but process died/interrupted before completion.
                # BLOCK for reconciliation per Requirement 38 & 74!
                if task.status != TaskStatus.BLOCKED:
                    TaskStateMachine.validate_transition(task.id, task.status, TaskStatus.BLOCKED)
                    task.status = TaskStatus.BLOCKED
                    self.task_repo.update_status(task.id, task.status)
                if wf.status != WorkflowStatus.BLOCKED:
                    WorkflowStateMachine.validate_transition(
                        wf.id, wf.status, WorkflowStatus.BLOCKED
                    )
                    wf.status = WorkflowStatus.BLOCKED
                    self.wf_repo.update_status(wf.id, wf.status)

                latest_attempt.status = ExecutionStatus.UNCERTAIN
                latest_attempt.error_message = (
                    "Process ownership or terminal state remains uncertain"
                )
                self.exec_repo.save(latest_attempt)
                raise ReconciliationRequired(
                    f"Crash recovery: Task '{task.id}' has uncertain owned execution attempt {latest_attempt.id}. Blind re-execution is forbidden; reconciliation required until process state is resolved.",
                    task_id=task.id,
                    execution_id=latest_attempt.id,
                )

        # 2. Determine effective operation class from both task intent and provider metadata.
        effective_cost_class = task.cost_class
        if task.task_type == "paid_generation" and self.asset_provider is not None:
            provider_class = self.asset_provider.cost_class
            if not isinstance(provider_class, CostClass):
                raise ValidationError("Provider declared an unsupported cost classification")
            rank = {
                CostClass.LOCAL: 0,
                CostClass.FREE_EXTERNAL: 1,
                CostClass.METERED: 2,
                CostClass.PAID: 3,
                CostClass.EXPENSIVE: 4,
            }
            effective_cost_class = max((task.cost_class, provider_class), key=rank.__getitem__)
        charged_classes = (CostClass.METERED, CostClass.PAID, CostClass.EXPENSIVE)
        if task.task_type == "paid_generation":
            op_type = (
                OperationType.PAID_OPERATION
                if effective_cost_class in charged_classes
                else OperationType.FREE_EXTERNAL
            )
        elif task.task_type == "generate_concept":
            op_type = OperationType.REPOSITORY_WRITE
        elif task.task_type == "controlled_command":
            op_type = OperationType.PROCESS_EXECUTION
        else:
            profile = self.handler_registry.metadata(task.task_type)
            op_type = (
                {
                    HandlerOperation.LOCAL_READ: OperationType.LOCAL_READ,
                    HandlerOperation.REPOSITORY_WRITE: OperationType.REPOSITORY_WRITE,
                    HandlerOperation.PROCESS_EXECUTION: OperationType.PROCESS_EXECUTION,
                    HandlerOperation.VISUAL_REVIEW: OperationType.VISUAL_REVIEW,
                }[profile.operation]
                if profile is not None
                else OperationType.LOCAL_READ
            )
        handler_profile = self.handler_registry.metadata(task.task_type)
        estimated_cost = float(task.parameters.get("cost", 0.0))

        # Check if an approval exists
        approvals = [a for a in self.app_repo.list_by_workflow(wf.id) if a.task_id == task.id]
        active_approval: ApprovalRequest | None = (
            max(approvals, key=lambda approval: (approval.requested_at, approval.id))
            if approvals
            else None
        )

        has_approved_decision = (
            active_approval is not None and active_approval.status == ApprovalStatus.APPROVED
        )
        approval_type = (
            "paid_generation"
            if op_type == OperationType.PAID_OPERATION
            else "process_execution"
            if op_type == OperationType.PROCESS_EXECUTION and handler_profile is not None
            else "visual_review"
            if op_type == OperationType.VISUAL_REVIEW
            else active_approval.approval_type
            if active_approval
            else "repository_write"
        )
        approval_inputs = self.approval_inputs(wf, task, effective_cost_class)
        current_op_hash = compute_operation_hash(task.id, approval_type, approval_inputs)
        has_approved_decision = bool(
            has_approved_decision
            and active_approval is not None
            and active_approval.operation_hash == current_op_hash
        )

        # Evaluate policy
        project_workflows = self.wf_repo.list_by_project(wf.project_id)
        current_spent = 0.0
        for project_workflow in project_workflows:
            for project_task in self.task_repo.list_by_workflow(project_workflow.id):
                current_spent += sum(
                    max(0.0, attempt.cost)
                    for attempt in self.exec_repo.list_by_task(project_task.id)
                )
        try:
            policy_res = self.policy_engine.evaluate(
                op_type=op_type,
                cost_class=effective_cost_class,
                estimated_cost=estimated_cost,
                current_spent=current_spent,
                has_approval=has_approved_decision,
                context={"managed_write": bool(handler_profile and handler_profile.managed_write)},
            )
        except BudgetExceeded as exc:
            return self._block_for_budget(wf, task, exc)

        if (
            policy_res.requires_approval
            and not has_approved_decision
            and not (
                active_approval
                and active_approval.status
                in (ApprovalStatus.REJECTED, ApprovalStatus.CHANGES_REQUESTED)
            )
        ):
            # Need to pause and request approval
            if not active_approval:
                artifacts = self.art_repo.list_by_workflow(wf.id)
                active_approval = ApprovalService.create_request(
                    workflow_id=wf.id,
                    task_id=task.id,
                    approval_type=policy_res.approval_type or "external_operation",
                    reason=policy_res.reason,
                    cost_class=effective_cost_class,
                    operation_inputs=approval_inputs,
                    artifact_ids=[artifact.id for artifact in artifacts],
                )
                self.app_repo.save(active_approval)
            elif (
                active_approval.status != ApprovalStatus.PENDING
                or active_approval.operation_hash != current_op_hash
            ):
                active_approval = ApprovalService.create_request(
                    workflow_id=wf.id,
                    task_id=task.id,
                    approval_type=policy_res.approval_type or "external_operation",
                    reason=policy_res.reason,
                    cost_class=effective_cost_class,
                    operation_inputs=approval_inputs,
                    artifact_ids=[
                        artifact.id for artifact in self.art_repo.list_by_workflow(wf.id)
                    ],
                )
                self.app_repo.save(active_approval)

            if task.status != TaskStatus.BLOCKED:
                TaskStateMachine.validate_transition(task.id, task.status, TaskStatus.BLOCKED)
                task.status = TaskStatus.BLOCKED
                self.task_repo.update_status(task.id, task.status)

            if wf.status != WorkflowStatus.BLOCKED:
                WorkflowStateMachine.validate_transition(wf.id, wf.status, WorkflowStatus.BLOCKED)
                wf.status = WorkflowStatus.BLOCKED
                self.wf_repo.update_status(wf.id, wf.status)

            self.audit_repo.append(
                AuditEvent(
                    id=generate_id("AUDIT"),
                    entity_type="Task",
                    entity_id=task.id,
                    action="BLOCKED_APPROVAL",
                    actor="PolicyEngine",
                    details={"approval_id": active_approval.id},
                )
            )

            return WorkflowExecutionResult(
                workflow_id=wf.id,
                status=WorkflowStatus.BLOCKED,
                completed_tasks=[],
                failed_tasks=[],
                blocked_tasks=[task.id],
                pending_approval_id=active_approval.id,
            )

        # If approval was rejected, fail task
        if active_approval and active_approval.status in (
            ApprovalStatus.REJECTED,
            ApprovalStatus.CHANGES_REQUESTED,
        ):
            TaskStateMachine.validate_transition(task.id, task.status, TaskStatus.FAILED)
            task.status = TaskStatus.FAILED
            self.task_repo.update_status(task.id, task.status)
            return WorkflowExecutionResult(
                workflow_id=wf.id,
                status=WorkflowStatus.FAILED,
                completed_tasks=[],
                failed_tasks=[task.id],
                blocked_tasks=[],
                error_message=f"Task '{task.id}' rejected by human operator: {active_approval.comment}",
            )

        # Atomic claim couples the state change and attempt insertion.
        attempt_num = (latest_attempt.attempt_number + 1) if latest_attempt else 1
        execution = Execution(
            id=generate_id("EXEC"),
            task_id=task.id,
            attempt_number=attempt_num,
            status=ExecutionStatus.RUNNING,
            cost=estimated_cost,
            estimated_cost=estimated_cost,
            cost_unit=str(task.parameters.get("cost_unit", "provider_units")),
            provider=self.asset_provider.name
            if task.task_type == "paid_generation" and self.asset_provider
            else None,
        )
        try:
            claimed = self.task_repo.claim_execution(
                task.id, execution, wf.project_id, self.policy_engine.rule.project_budget
            )
        except BudgetExceeded as exc:
            # Another workflow can reserve the remaining budget after evaluation.
            # The atomic claim rejects it; persist an actionable blocked lifecycle.
            return self._block_for_budget(wf, task, exc)
        if not claimed:
            return WorkflowExecutionResult(
                wf.id,
                WorkflowStatus.BLOCKED,
                [],
                [],
                [task.id],
                error_message="Task claim was lost to another runner",
            )
        task.status = TaskStatus.RUNNING

        # 5. Execute task action
        try:
            self._dispatch_task_action(wf, task, execution, current_op_hash)
            if (
                task.task_type != "record_evidence"
                and self.handler_registry.get(task.task_type) is None
            ):
                self._record_task_evidence(wf, task, execution)
            if task.task_type == "record_evidence":
                self._record_final_workflow_evidence(wf, task, execution)
                self._verify_task_gates(wf, task)
            execution.status = ExecutionStatus.COMPLETED
            execution.completed_at = utc_now_iso()
            task.status = TaskStatus.COMPLETED
            self._finalize_execution(
                task,
                execution,
                AuditEvent(
                    id=generate_id("AUDIT"),
                    entity_type="Task",
                    entity_id=task.id,
                    action="COMPLETED",
                    actor="WorkflowEngine",
                    details={"attempt": attempt_num},
                ),
            )

            return WorkflowExecutionResult(
                workflow_id=wf.id,
                status=WorkflowStatus.RUNNING,
                completed_tasks=[task.id],
                failed_tasks=[],
                blocked_tasks=[],
            )

        except Exception as exc:
            handler_profile = self.handler_registry.metadata(task.task_type)
            failure_details = getattr(exc, "details", {}) or {}
            uncertain = (
                task.task_type == "paid_generation" and effective_cost_class in charged_classes
            ) or (
                handler_profile is not None
                and handler_profile.recovery == HandlerRecovery.CONSERVATIVE_PROCESS
                and bool(failure_details.get("process_state_uncertain", False))
            )
            execution.status = ExecutionStatus.UNCERTAIN if uncertain else ExecutionStatus.FAILED
            execution.error_message = str(exc)
            execution.completed_at = utc_now_iso()
            if uncertain:
                details = getattr(exc, "details", {}) or {}
                execution.external_op_id = execution.external_op_id or details.get("external_op_id")
                if not execution.external_op_id and str(exc).startswith("CRASH_AFTER_SUBMISSION:"):
                    execution.external_op_id = str(exc).split(":", 1)[1]
                task.status = TaskStatus.BLOCKED
            else:
                task.status = TaskStatus.FAILED
                execution.retryable = isinstance(exc, ToolExecutionError)
            self._finalize_execution(
                task,
                execution,
                AuditEvent(
                    id=generate_id("AUDIT"),
                    entity_type="Task",
                    entity_id=task.id,
                    action="FAILED",
                    actor="WorkflowEngine",
                    details={"attempt": attempt_num, "error": str(exc)},
                ),
            )

            return WorkflowExecutionResult(
                workflow_id=wf.id,
                status=WorkflowStatus.BLOCKED if uncertain else WorkflowStatus.FAILED,
                completed_tasks=[],
                failed_tasks=[task.id],
                blocked_tasks=[task.id] if uncertain else [],
                error_message=(
                    f"Operation outcome is uncertain; reconciliation required: {exc}"
                    if uncertain
                    else str(exc)
                ),
            )

    def _block_for_budget(
        self, workflow: Workflow, task: Task, error: BudgetExceeded
    ) -> WorkflowExecutionResult:
        if task.status != TaskStatus.BLOCKED:
            TaskStateMachine.validate_transition(task.id, task.status, TaskStatus.BLOCKED)
            task.status = TaskStatus.BLOCKED
            self.task_repo.update_status(task.id, task.status)
        if workflow.status != WorkflowStatus.BLOCKED:
            WorkflowStateMachine.validate_transition(
                workflow.id, workflow.status, WorkflowStatus.BLOCKED
            )
            workflow.status = WorkflowStatus.BLOCKED
            self.wf_repo.update_status(workflow.id, workflow.status)
        return WorkflowExecutionResult(
            workflow.id, WorkflowStatus.BLOCKED, [], [], [task.id], error_message=str(error)
        )

    def _dispatch_task_action(
        self, wf: Workflow, task: Task, execution: Execution, operation_hash: str
    ) -> None:
        """Dispatch to a registered extension or a bounded built-in action."""
        extension = self.handler_registry.get(task.task_type)
        if extension is not None:
            try:
                handler_result = extension(wf, task, execution)
                if not hasattr(handler_result, "validate") or not hasattr(
                    handler_result, "schema_version"
                ):
                    raise ValidationError("Task handler returned an invalid result schema")
                handler_result.validate()
                if not handler_result.artifact_ids:
                    raise ValidationError("Task handler must return at least one artifact ID")
                verified = []
                for artifact_id in handler_result.artifact_ids:
                    artifact = self.art_repo.get(artifact_id)
                    if (
                        artifact is None
                        or artifact.workflow_id != wf.id
                        or artifact.task_id != task.id
                    ):
                        raise ArtifactError(
                            f"Handler artifact '{artifact_id}' is missing or outside its task scope"
                        )
                    verified.append(self.artifact_mgr.verify_artifact_integrity(artifact))
                gate = QualityGate(
                    id=generate_id("GATE"),
                    task_id=task.id,
                    gate_type=f"handler:{task.task_type}",
                    status=GateStatus.PASSED,
                    evaluated_at=utc_now_iso(),
                    reason="The engine independently verified handler artifact hashes",
                )
                self.gate_repo.save(gate)
                self.evi_repo.save(
                    Evidence(
                        id=generate_id("EVI"),
                        task_id=task.id,
                        execution_id=execution.id,
                        evidence_type=f"handler:{task.task_type}",
                        summary=handler_result.summary,
                        raw_data={
                            "schema_version": handler_result.schema_version,
                            "gate_id": gate.id,
                            "artifacts": verified,
                        },
                    )
                )
                execution.stdout = handler_result.summary
                return
            except (AttributeError, TypeError, ValueError) as exc:
                raise ValidationError(f"Task handler returned malformed result: {exc}") from exc
        if task.task_type not in self.builtin_actions.task_types:
            raise ValidationError(f"Unknown task type '{task.task_type}'")
        self.builtin_actions.execute(wf, task, execution, operation_hash)

    def _verify_task_gates(self, workflow: Workflow, task: Task) -> None:
        """Recheck evidence-bound hashes immediately before final task commit."""
        artifacts = self.art_repo.list_by_workflow(workflow.id)
        if not artifacts:
            raise ArtifactError("Mandatory artifact evidence is missing")
        for artifact in artifacts:
            self.artifact_mgr.verify_artifact_integrity(artifact)
        gates = self.gate_repo.list_by_task(task.id)
        evidence = self.evi_repo.list_by_task(task.id)
        if not any(g.status == GateStatus.PASSED for g in gates) or not evidence:
            raise ArtifactError("Mandatory quality gate and evidence must be recorded")

    def approval_inputs(
        self, workflow: Workflow, task: Task, cost_class: CostClass | None = None
    ) -> dict[str, object]:
        """Return the complete operation scope used at request and dispatch time."""
        profile = self.handler_registry.metadata(task.task_type)
        if profile is not None and profile.refresh_parameters is not None:
            refreshed = profile.refresh_parameters(workflow, task)
            if not isinstance(refreshed, dict):
                raise ValidationError("Task input refresh must return an object")
            # Ensure refreshed data is finite JSON before persisting it as a task snapshot.
            import json

            json.dumps(refreshed, allow_nan=False)
            if refreshed != task.parameters:
                self.task_repo.update_parameters(task.id, refreshed)
                task.parameters = refreshed
        effective_class = cost_class or task.cost_class
        if (
            cost_class is None
            and task.task_type == "paid_generation"
            and self.asset_provider is not None
        ):
            provider_class = self.asset_provider.cost_class
            if not isinstance(provider_class, CostClass):
                raise ValidationError("Provider declared an unsupported cost classification")
            rank = {
                CostClass.LOCAL: 0,
                CostClass.FREE_EXTERNAL: 1,
                CostClass.METERED: 2,
                CostClass.PAID: 3,
                CostClass.EXPENSIVE: 4,
            }
            effective_class = max((effective_class, provider_class), key=rank.__getitem__)
        artifacts = self.art_repo.list_by_workflow(workflow.id)
        scope: dict[str, object] = {
            "workflow_id": workflow.id,
            "task_type": task.task_type,
            "cost_class": effective_class.value,
            "provider": self.asset_provider.name
            if task.task_type == "paid_generation" and self.asset_provider
            else None,
            "artifacts": sorted((item.id, item.content_hash) for item in artifacts),
            "estimated_cost": float(task.parameters.get("cost", 0.0)),
        }
        if profile is not None and profile.approval_context is not None:
            scope["handler_context"] = profile.approval_context(workflow, task)
        return {
            "parameters": task.parameters,
            "scope": scope,
        }

    def _record_final_workflow_evidence(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> None:
        artifacts = self.art_repo.list_by_workflow(workflow.id)
        if not artifacts:
            raise ArtifactError("Cannot pass evidence gate without registered workflow artifacts")
        verified = [self.artifact_mgr.verify_artifact_integrity(item) for item in artifacts]
        gate = QualityGate(
            id=generate_id("GATE"),
            task_id=task.id,
            gate_type="workflow_artifact_integrity",
            status=GateStatus.PASSED,
            evaluated_at=utc_now_iso(),
            reason="The engine independently verified every registered workflow artifact",
        )
        self.gate_repo.save(gate)
        self.evi_repo.save(
            Evidence(
                id=generate_id("EVI"),
                task_id=task.id,
                execution_id=execution.id,
                evidence_type="validation_report",
                summary="All workflow artifacts passed SHA-256 verification",
                raw_data={"gate_id": gate.id, "artifacts": verified},
            )
        )

    def _record_task_evidence(self, workflow: Workflow, task: Task, execution: Execution) -> None:
        """Verify built-in evidence claims, then persist the engine-owned gate."""
        result = self.builtin_actions.evidence_for_task(workflow, task, execution)
        result.validate()
        verified = []
        for artifact_id in result.artifact_ids:
            artifact = self.art_repo.get(artifact_id)
            if artifact is None or artifact.workflow_id != workflow.id:
                raise ArtifactError(
                    f"Evidence artifact '{artifact_id}' is missing or outside this workflow"
                )
            verified.append(self.artifact_mgr.verify_artifact_integrity(artifact))
        gate = QualityGate(
            id=generate_id("GATE"),
            task_id=task.id,
            gate_type=f"result:{task.task_type}",
            status=GateStatus.PASSED,
            evaluated_at=utc_now_iso(),
            reason="Execution output passed its registered deterministic result validator",
        )
        self.gate_repo.save(gate)
        self.evi_repo.save(
            Evidence(
                id=generate_id("EVI"),
                task_id=task.id,
                execution_id=execution.id,
                evidence_type=f"execution:{task.task_type}",
                summary=result.summary,
                raw_data={
                    "schema_version": result.schema_version,
                    "gate_id": gate.id,
                    "artifacts": verified,
                    **result.details,
                },
            )
        )

    def _finalize_execution(self, task: Task, execution: Execution, event: AuditEvent) -> None:
        self.exec_repo.finalize_task(task, execution, event)

    def retry_task(self, workflow_id: str, task_id: str) -> WorkflowExecutionResult:
        """Retry a failed task by creating a new execution attempt and resuming execution."""
        task = self.task_repo.get(task_id)
        if not task:
            raise ValidationError(f"Task '{task_id}' not found")
        if task.status != TaskStatus.FAILED:
            raise ValidationError(
                f"Cannot retry task '{task_id}': status is {task.status.value}, expected FAILED",
                details={"task_id": task_id, "status": task.status.value},
            )
        if task.workflow_id != workflow_id:
            raise ValidationError(f"Task '{task_id}' does not belong to workflow '{workflow_id}'")

        wf = self.wf_repo.get(workflow_id)
        if not wf:
            raise ValidationError(f"Workflow '{workflow_id}' not found")

        attempts = self.exec_repo.list_by_task(task.id)
        if len(attempts) >= task.max_retries + 1:
            raise ValidationError(
                f"Task '{task_id}' exhausted its retry limit ({task.max_retries})"
            )
        if not attempts or not attempts[-1].retryable:
            raise ValidationError(f"Task '{task_id}' failure is not classified as safely retryable")

        # Allow transition from FAILED to PENDING for re-scheduling
        TaskStateMachine.validate_transition(task.id, task.status, TaskStatus.PENDING)
        task.status = TaskStatus.PENDING
        self.task_repo.update_status(task.id, task.status)

        self.audit_repo.append(
            AuditEvent(
                id=generate_id("AUDIT"),
                entity_type="Task",
                entity_id=task.id,
                action="RETRY_INITIATED",
                actor="Operator",
            )
        )

        return self.run_workflow(workflow_id)
