"""Build and execute bounded generic game-production workflows through registered providers."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Protocol, cast

from gamefactory.adapters.engines.godot_staging import (
    _SECRET_FILES,
    _SECRET_NAMES,
    _SECRET_SUFFIXES,
    _SKIP_DIRS,
    GodotStager,
    SourceFile,
    _is_reparse,
)
from gamefactory.adapters.persistence.database import Database
from gamefactory.adapters.persistence.generic_operations import (
    DatabaseAuthorizationVerifier,
    GenericOperationRepository,
)
from gamefactory.adapters.persistence.repositories import (
    ApprovalRepository,
    ArtifactRepository,
    AuditLogRepository,
    CostLedgerRepository,
    EvidenceRepository,
    ExecutionRepository,
    ProjectRepository,
    QualityGateRepository,
    TaskRepository,
    WorkflowRepository,
)
from gamefactory.agents.context import BoundedContext, ContextBuilder, ContextItem, ContextSelection
from gamefactory.agents.director import Director
from gamefactory.agents.registry import AgentRegistry
from gamefactory.core.approvals.approval_service import compute_operation_hash
from gamefactory.core.approvals.operation_scope import build_operation_inputs
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.agent_contracts import (
    AgentKind,
    AgentOutcome,
    AgentTaskContract,
    SourceReference,
)
from gamefactory.core.domain.errors import ArtifactError, ReconciliationRequired, ValidationError
from gamefactory.core.domain.factory_workflow import FactoryWorkflowManifest, WorkflowTaskSpec
from gamefactory.core.domain.game_quality import (
    QualityReport,
)
from gamefactory.core.domain.game_quality import (
    VisualReview as GameVisualReview,
)
from gamefactory.core.domain.models import (
    ApprovalStatus,
    Artifact,
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
    generate_id,
    utc_now_iso,
)
from gamefactory.core.domain.provider_execution import (
    AuthorizationVerifier,
    ProviderAuthorization,
    ProviderExecutionStatus,
    ProviderRun,
)
from gamefactory.core.execution.locks import ExecutionLock
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.validators.game_quality import (
    evaluate_gameplay,
    evaluate_performance,
    record_visual_review,
)
from gamefactory.workflows.accounting import CostAccounting
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SECRET_PARTS = {
    ".git",
    ".gamefactory",
    ".aws",
    ".ssh",
    ".codex",
    ".agents",
    ".azure",
    ".gnupg",
    ".hg",
    ".svn",
}
_DEVICE = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$", re.I)


def _artifact_matches_completed_attempt(
    payload: dict[str, Any],
    artifact_id: str,
    artifact_sha256: str,
    execution_id: str,
    recovery_proof: dict[str, Any] | None,
) -> bool:
    if payload.get("execution_id") == execution_id:
        return True
    if not isinstance(recovery_proof, dict):
        return False
    return bool(
        artifact_id in recovery_proof.get("artifact_ids", [])
        and recovery_proof.get("request_fingerprint") == payload.get("request_fingerprint")
        and recovery_proof.get("artifact_hashes", {}).get(artifact_id) == artifact_sha256
    )


class CapabilityExecutor(Protocol):
    def execute(
        self,
        contract: AgentTaskContract,
        context: Any,
        workspace: Path,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun | dict[str, Any]: ...


class QueryableCapabilityExecutor(CapabilityExecutor, Protocol):
    def query(
        self,
        external_id: str,
        request_fingerprint: str,
        authorization: ProviderAuthorization,
        authorization_verifier: AuthorizationVerifier,
    ) -> ProviderRun | dict[str, Any]: ...


class ExecutorRegistry:
    """Explicit local capability executor registry; no implicit remote marketplace."""

    def __init__(self) -> None:
        self._executors: dict[str, tuple[CapabilityExecutor, frozenset[str], bool, str]] = {}

    def register(
        self,
        executor_id: str,
        executor: CapabilityExecutor,
        *,
        capabilities: set[str] | frozenset[str],
        config_fingerprint: str,
    ) -> None:
        if (
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", executor_id)
            or executor_id in self._executors
        ):
            raise ValidationError("executor id is invalid or already registered")
        if not capabilities or any(
            not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.:-]{0,127}", c) for c in capabilities
        ):
            raise ValidationError("registered executor capabilities are invalid")
        digest = config_fingerprint
        if not _SHA256.fullmatch(digest):
            raise ValidationError("executor config_fingerprint must be lowercase SHA-256")
        adapter_fingerprint = getattr(executor, "config_fingerprint", None)
        if adapter_fingerprint != digest:
            raise ValidationError(
                "registered executor fingerprint does not match adapter configuration"
            )
        configured_attr = getattr(executor, "is_configured", False)
        configured = bool(configured_attr() if callable(configured_attr) else configured_attr)
        self._executors[executor_id] = (executor, frozenset(capabilities), configured, digest)

    def get(self, executor_id: str) -> tuple[CapabilityExecutor, frozenset[str], bool, str] | None:
        return self._executors.get(executor_id)


class GateExecutorRegistry:
    """Explicit registry for trusted validators and runtime/gameplay harnesses."""

    def __init__(self) -> None:
        self._runners: dict[str, Any] = {}

    def register(self, gate: str, runner: Any, *, config_fingerprint: str) -> None:
        if gate not in {"code", "gameplay", "performance", "visual"} or gate in self._runners:
            raise ValidationError("gate id is invalid or already registered")
        if not callable(getattr(runner, "evaluate", None)):
            raise ValidationError("gate runner must be configured and implement evaluate")
        digest = config_fingerprint
        if not _SHA256.fullmatch(digest):
            raise ValidationError("gate config_fingerprint must be lowercase SHA-256")
        if getattr(runner, "config_fingerprint", None) != digest:
            raise ValidationError(
                "registered gate fingerprint does not match trusted runner configuration"
            )
        self._runners[gate] = (runner, digest)

    def get(self, gate: str) -> tuple[Any, str] | None:
        return self._runners.get(gate)


def _safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValidationError("output path must be a non-empty trimmed string")
    path = value
    parts = path.split("/")
    secret_name = re.compile(
        r"(^|[._-])(secrets?|credentials?|tokens?|passwords?|private(?:[_-]?keys?)?)([._-]|$)", re.I
    )
    secret_file = parts[-1].lower() in {
        ".env",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "authorized_keys",
        "id_rsa",
        "id_ed25519",
    }
    secret_suffix = Path(parts[-1]).suffix.lower() in {
        ".pem",
        ".key",
        ".p12",
        ".pfx",
        ".jks",
        ".keystore",
    }
    invalid_windows = any(
        ":" in part
        or any(char in '<>"|?*' for char in part)
        or part.endswith((".", " "))
        or _DEVICE.fullmatch(part)
        or any(ord(char) < 32 for char in part)
        for part in parts
    )
    if (
        "\\" in value
        or path.startswith("/")
        or re.match(r"^[A-Za-z]:", path)
        or any(p in {"", ".", ".."} for p in parts)
        or invalid_windows
        or any(
            p.lower() in _SECRET_PARTS
            or p.lower() in {".godot", ".verify-pytest", ".verify-venv", ".verification"}
            for p in parts
        )
        or secret_name.search(parts[-1])
        or secret_file
        or secret_suffix
        or parts[-1].lower().startswith(".env")
    ):
        raise ValidationError("output path is absolute, traversing, reserved, or secret-bearing")
    if len(path) > 1024:
        raise ValidationError("output path is too long")
    return path


def _path_component(identity: str) -> str:
    """Portable opaque component; user-controlled IDs never become path syntax."""
    readable = re.sub(r"[^A-Za-z0-9_-]+", "_", identity).strip("._-")[:32] or "item"
    suffix = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    return f"{readable}-{suffix}"


def _artifact_dir(workflow_id: str, task_id: str, execution_id: str) -> str:
    return f".gamefactory/artifacts/factory/{_path_component(workflow_id)}/{_path_component(task_id)}/{_path_component(execution_id)}"


def _reject_reparse_components(root: Path, relative_path: str) -> None:
    if _is_reparse(root):
        raise ValidationError("Managed project root cannot be a symlink or junction")
    current = root
    for part in Path(relative_path).parts:
        current = current / part
        if _is_reparse(current):
            raise ValidationError(
                "Managed paths cannot traverse symlinks or junctions",
                details={"path": relative_path},
            )


def _manifest(
    value: FactoryWorkflowManifest | dict[str, Any] | str | bytes,
) -> FactoryWorkflowManifest:
    if isinstance(value, FactoryWorkflowManifest):
        return FactoryWorkflowManifest.model_validate_json(value.model_dump_json())
    if isinstance(value, bytes):
        return FactoryWorkflowManifest.model_validate_json(value)
    if isinstance(value, str):
        return FactoryWorkflowManifest.model_validate_json(value)
    return FactoryWorkflowManifest.model_validate_json(json.dumps(value, allow_nan=False))


def _spec(value: WorkflowTaskSpec | dict[str, Any] | str) -> WorkflowTaskSpec:
    if isinstance(value, WorkflowTaskSpec):
        return WorkflowTaskSpec.model_validate_json(value.model_dump_json())
    return WorkflowTaskSpec.model_validate_json(
        value if isinstance(value, str) else json.dumps(value, allow_nan=False)
    )


def _task_spec(task: Task) -> WorkflowTaskSpec:
    value = task.parameters.get("factory_spec")
    if not isinstance(value, (WorkflowTaskSpec, dict, str)):
        raise ValidationError("Workflow task is missing its strict factory specification")
    return _spec(value)


def build_workflow(
    manifest: FactoryWorkflowManifest | dict[str, Any] | str | bytes,
) -> tuple[Workflow, list[Task]]:
    """Build core Workflow/Task records from the operator-authored, versioned manifest."""
    manifest = _manifest(manifest)
    tasks: list[Task] = []
    gates_by_task: dict[str, list[str]] = {}
    decisions_by_task: dict[str, list[str]] = {}
    specs_by_id = {spec.task_id: spec for spec in manifest.tasks}
    all_game_writers = [item.task_id for item in manifest.tasks if item.game_write]
    graph = {spec.task_id: spec.dependencies for spec in manifest.tasks}

    def ancestors(task_id: str) -> set[str]:
        found: set[str] = set()
        stack = list(graph[task_id])
        while stack:
            node = stack.pop()
            if node not in found:
                found.add(node)
                stack.extend(graph[node])
        return found

    for spec in manifest.tasks:
        params = {
            "factory_spec": spec.model_dump(mode="json"),
            "manifest_sha256": manifest.sha256,
            "cost": spec.cost.max_amount,
            "cost_unit": f"{spec.cost.currency}:{spec.cost.unit}",
        }
        dependencies = list(spec.dependencies)
        for source in spec.inputs:
            if source.source_gate_scope == "combined_candidate":
                capture_id = f"{spec.task_id}:combined-capture:{source.source_gate}"
                dependencies.append(capture_id)
                if not any(task.id == capture_id for task in tasks):
                    tasks.append(
                        Task(
                            id=capture_id,
                            workflow_id=manifest.workflow_id,
                            name=f"Combined candidate {source.source_gate} capture for {spec.name}",
                            task_type="factory_quality_gate",
                            cost_class=CostClass.LOCAL,
                            depends_on=all_game_writers,
                            parameters={
                                "gate": source.source_gate,
                                "source_task_id": spec.task_id,
                                "candidate_writer_tasks": all_game_writers,
                                "factory_spec": spec.model_dump(mode="json"),
                                "manifest_sha256": manifest.sha256,
                                "combined_capture": True,
                            },
                            max_retries=0,
                            timeout_seconds=3600,
                        )
                    )
        for source_id in {item.source_task_id for item in spec.inputs if item.source_task_id}:
            source_spec = specs_by_id[source_id]
            dependencies.extend(f"{source_id}:gate:{gate}" for gate in source_spec.required_gates)
            if "visual" in source_spec.required_gates:
                dependencies.append(f"{source_id}:visual-decision")
        tasks.append(
            Task(
                id=spec.task_id,
                workflow_id=manifest.workflow_id,
                name=spec.name,
                task_type="factory_provider_task",
                cost_class=CostClass(spec.cost.cost_class),
                depends_on=list(dict.fromkeys(dependencies)),
                parameters=params,
                max_retries=spec.max_retries,
                timeout_seconds=spec.timeout_seconds,
            )
        )
        gates_by_task[spec.task_id] = []
        decisions_by_task[spec.task_id] = []
        candidate_writers = [
            item.task_id
            for item in manifest.tasks
            if item.game_write
            and (item.task_id == spec.task_id or item.task_id in ancestors(spec.task_id))
        ]
        for gate in spec.required_gates:
            gate_id = f"{spec.task_id}:gate:{gate}"
            gates_by_task[spec.task_id].append(gate_id)
            tasks.append(
                Task(
                    id=gate_id,
                    workflow_id=manifest.workflow_id,
                    name=f"{gate.title()} quality gate for {spec.name}",
                    task_type="factory_quality_gate",
                    cost_class=CostClass.LOCAL,
                    depends_on=[spec.task_id],
                    parameters={
                        "gate": gate,
                        "source_task_id": spec.task_id,
                        "candidate_writer_tasks": candidate_writers,
                        "factory_spec": spec.model_dump(mode="json"),
                        "manifest_sha256": manifest.sha256,
                    },
                    max_retries=0,
                    timeout_seconds=3600,
                )
            )
            if gate == "visual":
                decision_id = f"{spec.task_id}:visual-decision"
                decisions_by_task[spec.task_id].append(decision_id)
                tasks.append(
                    Task(
                        id=decision_id,
                        workflow_id=manifest.workflow_id,
                        name=f"Human visual decision for {spec.name}",
                        task_type="factory_visual_decision",
                        cost_class=CostClass.LOCAL,
                        depends_on=[gate_id],
                        parameters={
                            "gate_task_id": gate_id,
                            "source_task_id": spec.task_id,
                            "manifest_sha256": manifest.sha256,
                        },
                        max_retries=0,
                        timeout_seconds=60,
                    )
                )
    if manifest.require_human_game_acceptance and any(spec.game_write for spec in manifest.tasks):
        writers = [spec.task_id for spec in manifest.tasks if spec.game_write]
        # Final gates run again over the complete candidate after every proposal
        # and intermediate dependency gate has completed. This avoids approving
        # a set of individually-tested fragments as if they were one product.
        final_gate_ids: list[str] = []
        final_decision_ids: list[str] = []
        intermediate_ids = [item for values in gates_by_task.values() for item in values]
        intermediate_ids += [item for values in decisions_by_task.values() for item in values]
        final_dependencies = list(dict.fromkeys(writers + intermediate_ids))
        final_gate_specs = [
            spec
            for spec in manifest.tasks
            if spec.game_write or (spec.family == "vision" and "visual" in spec.required_gates)
        ]
        for spec in final_gate_specs:
            writer = spec.task_id
            for gate in spec.required_gates:
                gate_id = f"{manifest.workflow_id}:final:{writer}:gate:{gate}"
                final_gate_ids.append(gate_id)
                tasks.append(
                    Task(
                        id=gate_id,
                        workflow_id=manifest.workflow_id,
                        name=f"Final candidate {gate} gate for {spec.name}",
                        task_type="factory_quality_gate",
                        cost_class=CostClass.LOCAL,
                        depends_on=final_dependencies,
                        parameters={
                            "gate": gate,
                            "source_task_id": writer,
                            "candidate_writer_tasks": writers,
                            "factory_spec": spec.model_dump(mode="json"),
                            "manifest_sha256": manifest.sha256,
                            "final_candidate": True,
                        },
                        max_retries=0,
                        timeout_seconds=3600,
                    )
                )
                if gate == "visual":
                    decision_id = f"{gate_id}:human-decision"
                    final_decision_ids.append(decision_id)
                    tasks.append(
                        Task(
                            id=decision_id,
                            workflow_id=manifest.workflow_id,
                            name=f"Human final visual decision for {spec.name}",
                            task_type="factory_visual_decision",
                            cost_class=CostClass.LOCAL,
                            depends_on=[gate_id],
                            parameters={
                                "gate_task_id": gate_id,
                                "source_task_id": writer,
                                "manifest_sha256": manifest.sha256,
                            },
                            max_retries=0,
                            timeout_seconds=60,
                        )
                    )
        required = final_gate_ids + final_decision_ids
        if not final_gate_ids:
            raise ValidationError(
                "game file application requires at least one successfully evaluated quality gate"
            )
        # One narrowly controlled retry is reserved for the audited committed-
        # journal continuation after a process crash; ordinary handler failures
        # remain non-retryable unless explicitly classified safe by the engine.
        tasks.append(
            Task(
                id=f"{manifest.workflow_id}:accept",
                workflow_id=manifest.workflow_id,
                name="Human accepted game-file application",
                task_type="factory_acceptance",
                cost_class=CostClass.LOCAL,
                depends_on=required,
                parameters={
                    "writer_tasks": writers,
                    "required_gate_tasks": final_gate_ids,
                    "required_decision_tasks": final_decision_ids,
                    "manifest_sha256": manifest.sha256,
                },
                max_retries=1,
                timeout_seconds=300,
            )
        )
    wf = Workflow(id=manifest.workflow_id, project_id=manifest.project_id, name=manifest.name)
    from gamefactory.core.domain.dag import WorkflowDAG

    WorkflowDAG(tasks)
    return wf, tasks


def preflight_manifest(
    manifest: FactoryWorkflowManifest | dict[str, Any],
    executors: ExecutorRegistry,
    agents: AgentRegistry,
    gates: GateExecutorRegistry | None = None,
) -> dict[str, Any]:
    """Return explicit configured/unavailable routing without attempting provider calls."""
    manifest = _manifest(manifest)
    rows = []
    for spec in manifest.tasks:
        entry = executors.get(spec.executor_id)
        if entry is None:
            rows.append(
                {
                    "task_id": spec.task_id,
                    "status": "BLOCKED",
                    "reason": f"Executor {spec.executor_id!r} is not registered",
                }
            )
            continue
        _, available, configured, executor_hash = entry
        missing = set(spec.capabilities) - set(available)
        if not configured or missing:
            rows.append(
                {
                    "task_id": spec.task_id,
                    "status": "BLOCKED",
                    "reason": "Executor is not configured"
                    if not configured
                    else f"Capabilities unavailable: {sorted(missing)}",
                }
            )
            continue
        # Director is authoritative for definitions, capability and cost fit.
        registered_agent = agents.get(spec.agent_id)
        if registered_agent is None or not registered_agent.enabled:
            rows.append(
                {
                    "task_id": spec.task_id,
                    "status": "BLOCKED",
                    "reason": f"Agent {spec.agent_id!r} is not registered and enabled",
                }
            )
            continue
        contract = _build_agent_contract(spec, manifest.project_id, ())
        try:
            eligible = agents.route(contract, available_capabilities=available)
        except ValidationError as exc:
            rows.append({"task_id": spec.task_id, "status": "BLOCKED", "reason": str(exc)})
        else:
            if registered_agent not in eligible:
                rows.append(
                    {
                        "task_id": spec.task_id,
                        "status": "BLOCKED",
                        "reason": "Selected agent is not eligible for the configured executor",
                    }
                )
            else:
                missing_gates = [
                    gate for gate in spec.required_gates if gates is None or gates.get(gate) is None
                ]
                if missing_gates:
                    rows.append(
                        {
                            "task_id": spec.task_id,
                            "status": "NOT_VERIFIED",
                            "reason": f"Gate executors unavailable: {missing_gates}",
                            "executor_sha256": executor_hash,
                        }
                    )
                else:
                    rows.append(
                        {
                            "task_id": spec.task_id,
                            "status": "READY",
                            "executor_id": spec.executor_id,
                            "executor_sha256": executor_hash,
                        }
                    )
    summary = (
        "READY"
        if all(row["status"] == "READY" for row in rows)
        else "BLOCKED"
        if any(row["status"] == "BLOCKED" for row in rows)
        else "NOT_VERIFIED"
    )
    return {
        "schema_version": "factory-workflow-preflight-1.0.0",
        "manifest_sha256": manifest.sha256,
        "tasks": rows,
        "status": summary,
    }


def _build_agent_contract(
    spec: WorkflowTaskSpec, project_id: str, sources: tuple[SourceReference, ...]
) -> AgentTaskContract:
    read_only_vision = spec.kind == AgentKind.VISION
    if read_only_vision and (spec.output_scopes or spec.expected_artifacts):
        raise ValidationError("Vision provider tasks are read-only and cannot declare output files")
    contract = {
        "schema_version": "1.0.0",
        "task_id": spec.task_id,
        "selected_agent_id": spec.agent_id,
        "task_type": spec.family,
        "kind": spec.kind.value,
        "objective": spec.objective,
        "project_id": project_id,
        "prompt_template": {
            "template_id": spec.prompt_template_id,
            "version": spec.prompt_template_version,
        },
        "sources": [source.model_dump(mode="json") for source in sources],
        "required_capabilities": [
            {"name": capability, "required": True} for capability in spec.capabilities
        ],
        "allowed_tools": list(spec.allowed_tools),
        "forbidden_tools": list(spec.forbidden_tools),
        "tool_constraints": spec.tool_constraints.model_dump(mode="json"),
        "cost_constraints": spec.cost.model_dump(mode="json"),
        "dependencies": list(spec.dependencies),
        "acceptance_criteria": list(spec.acceptance_criteria),
        "required_evidence_types": [],
        "context_max_bytes": spec.max_context_bytes,
        "max_output_bytes": 0 if read_only_vision else spec.max_output_bytes,
        "max_output_files": 0 if read_only_vision else spec.max_output_files,
        "allowed_output_paths": list(spec.output_scopes),
    }
    return AgentTaskContract.model_validate_json(
        json.dumps(contract, ensure_ascii=False, allow_nan=False)
    )


def register_factory_handlers(
    registry: TaskHandlerRegistry,
    *,
    project_root: Path | str,
    db: Database,
    executor_registry: ExecutorRegistry,
    agent_registry: AgentRegistry,
    gate_registry: GateExecutorRegistry,
) -> None:
    """Register provider proposals, candidate-bound gates, and human decisions.

    Gate adapters implement ``evaluate(candidate_root, candidate_sha256, *, gate,
    task_spec, parameters)`` and return versioned input for the deterministic
    evaluator. They are trusted local integrations and never receive a generic
    command string from an agent.
    """
    root_input = Path(project_root)
    if _is_reparse(root_input):
        raise ValidationError("Project root cannot be a symlink or junction")
    root = root_input.resolve(strict=True)
    guard = PathGuard(root)
    artifact_mgr = ArtifactManager(root)
    artifact_repo = ArtifactRepository(db)
    evidence_repo = EvidenceRepository(db)
    execution_repo = ExecutionRepository(db)
    gate_repo = QualityGateRepository(db)
    approval_repo = ApprovalRepository(db)
    audit_repo = AuditLogRepository(db)
    cost_repo = CostLedgerRepository(db)
    task_repo = TaskRepository(db)
    op_repo = GenericOperationRepository(db)
    accounting = CostAccounting(cost_repo, audit_repo)
    verifier = DatabaseAuthorizationVerifier(op_repo)

    def workflow_tasks(workflow_id: str) -> dict[str, Task]:
        return {item.id: item for item in task_repo.list_by_workflow(workflow_id)}

    def latest_completed_execution_id(task_id: str) -> str | None:
        attempt = execution_repo.get_latest_attempt(task_id)
        return (
            attempt.id
            if attempt is not None and attempt.status == ExecutionStatus.COMPLETED
            else None
        )

    def workflow_id_for(task_id: str) -> str:
        task = task_repo.get(task_id)
        if task is None:
            raise ValidationError("Task disappeared while resolving attempt-bound artifacts")
        return task.workflow_id

    def latest_attempt_artifact(task_id: str, artifact_type: str) -> Any | None:
        execution_id = latest_completed_execution_id(task_id)
        if execution_id is None:
            return None
        for artifact in reversed(artifact_repo.list_by_workflow(workflow_id_for(task_id))):
            if artifact.task_id != task_id or artifact.artifact_type != artifact_type:
                continue
            artifact_mgr.verify_artifact_integrity(artifact)
            _reject_reparse_components(root, artifact.relative_path)
            try:
                payload = json.loads(
                    guard.resolve_safe_path(artifact.relative_path).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            recovery_proof = next(
                (
                    item
                    for item in reversed(evidence_repo.list_by_task(task_id))
                    if item.execution_id == execution_id
                    and item.evidence_type == "factory_provider_recovery"
                ),
                None,
            )
            proof_details = recovery_proof.raw_data if recovery_proof is not None else {}
            if _artifact_matches_completed_attempt(
                payload,
                artifact.id,
                artifact.content_hash,
                execution_id,
                proof_details if isinstance(proof_details, dict) else None,
            ):
                return artifact
        return None

    def source_context(
        workflow: Workflow,
        spec: WorkflowTaskSpec,
        *,
        include_gate_evidence: bool = True,
    ) -> BoundedContext:
        bounded_contexts = ContextBuilder(root, max_total_bytes=spec.max_context_bytes)
        direct = [
            ContextSelection(path=item.path, purpose=item.purpose)
            for item in spec.inputs
            if item.path is not None and item.source_task_id is None
        ]
        selected = list(bounded_contexts.build(direct).items) if direct else []
        for item in spec.inputs:
            if item.source_task_id is None:
                continue
            if item.source_gate is not None:
                if not include_gate_evidence:
                    continue
                gate_task_id = (
                    f"{item.source_task_id}:gate:{item.source_gate}"
                    if item.source_gate_scope == "predecessor"
                    else f"{spec.task_id}:combined-capture:{item.source_gate}"
                )
                report_art = next(
                    (
                        art
                        for art in artifact_repo.list_by_workflow(workflow.id)
                        if art.task_id == gate_task_id
                        and art.artifact_type == "factory_quality_report"
                    ),
                    None,
                )
                if report_art is None:
                    raise ValidationError(
                        f"Named gate evidence from {gate_task_id} is not available"
                    )
                artifact_mgr.verify_artifact_integrity(report_art)
                report_payload = json.loads(
                    guard.resolve_safe_path(report_art.relative_path).read_text(encoding="utf-8")
                )
                source_task = workflow_tasks(workflow.id).get(spec.task_id)
                expected_manifest = (
                    source_task.parameters.get("manifest_sha256")
                    if source_task is not None
                    else None
                )
                if (
                    report_payload.get("schema_version") != "factory-quality-gate-1.0.0"
                    or report_payload.get("manifest_sha256") != expected_manifest
                    or report_payload.get("gate_task_id") != gate_task_id
                    or report_payload.get("gate") != item.source_gate
                    or not isinstance(report_payload.get("candidate_sha256"), str)
                    or len(report_payload["candidate_sha256"]) != 64
                ):
                    raise ValidationError(
                        "Selected evidence is not bound to the declared completed gate and candidate"
                    )
                matches = [
                    entry
                    for entry in report_payload.get("physical_evidence", [])
                    if entry.get("name") == item.evidence_name
                ]
                if len(matches) != 1:
                    raise ValidationError(
                        "Gate evidence selector must identify exactly one named physical evidence file"
                    )
                evidence_art = artifact_repo.get(matches[0].get("artifact_id", ""))
                if evidence_art is None or evidence_art.content_hash != matches[0].get("sha256"):
                    raise ArtifactError(
                        "Gate evidence artifact identity/hash does not match its report"
                    )
                if "screenshot" not in str(matches[0].get("purpose", "")).lower() or not str(
                    matches[0].get("media_type", "")
                ).startswith("image/"):
                    raise ValidationError(
                        "Visual selectors may consume only registered screenshot image evidence"
                    )
                artifact_mgr.verify_artifact_integrity(evidence_art)
                evidence_path = guard.resolve_safe_path(evidence_art.relative_path)
                if evidence_path.stat().st_size > 64 * 1024 * 1024:
                    raise ValidationError("Selected gate evidence exceeds the bounded context size")
                content = evidence_path.read_bytes()
                gate_token = hashlib.sha256(gate_task_id.encode("utf-8")).hexdigest()[:16]
                bound_purpose = f"{item.purpose[:180]} [gate={gate_token}; candidate_sha256={report_payload['candidate_sha256']}; report_sha256={report_art.content_hash}]"
                selected.append(
                    ContextItem(
                        SourceReference(
                            path=item.path or "",
                            sha256=hashlib.sha256(content).hexdigest(),
                            size_bytes=len(content),
                            purpose=bound_purpose,
                        ),
                        content,
                    )
                )
                continue
            prior = latest_attempt_artifact(item.source_task_id, "factory_proposal")
            if prior is None:
                raise ValidationError(
                    f"Generated input from {item.source_task_id} has no immutable proposal artifact"
                )
            artifact_mgr.verify_artifact_integrity(prior)
            _reject_reparse_components(root, prior.relative_path)
            payload = json.loads(
                guard.resolve_safe_path(prior.relative_path).read_text(encoding="utf-8")
            )
            row = next(
                (
                    entry
                    for entry in payload.get("files", [])
                    if entry.get("path") == item.artifact_path
                ),
                None,
            )
            if row is None or not isinstance(row.get("artifact_id"), str):
                raise ValidationError(
                    f"Generated input {item.artifact_path!r} is not present in the predecessor output envelope"
                )
            output_art = artifact_repo.get(row["artifact_id"])
            if (
                output_art is None
                or output_art.task_id != item.source_task_id
                or output_art.content_hash != row.get("sha256")
            ):
                raise ArtifactError(
                    "Generated input artifact identity/hash does not match its provider proposal"
                )
            artifact_mgr.verify_artifact_integrity(output_art)
            _reject_reparse_components(root, output_art.relative_path)
            output_path = guard.resolve_safe_path(output_art.relative_path)
            if output_path.stat().st_size > 64 * 1024 * 1024:
                raise ValidationError("Generated artifact input exceeds the per-file context limit")
            content = output_path.read_bytes()
            source = SourceReference(
                path=item.artifact_path or "",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                purpose=item.purpose,
            )
            selected.append(ContextItem(source, content))
        paths = [item.source.path for item in selected]
        if len(paths) != len(set(paths)):
            raise ValidationError("Direct and generated workflow inputs must have unique paths")
        total = sum(len(item.content) for item in selected)
        if total > spec.max_context_bytes:
            raise ValidationError("Combined direct and generated context exceeds max_context_bytes")
        if spec.game_write:
            selected_sources = {
                item.source.path: item.source
                for item in selected
                if any(inp.path == item.source.path for inp in spec.inputs)
            }
            for output_rel in spec.output_scopes:
                _safe_relative(output_rel)
                _reject_reparse_components(root, output_rel)
                target = guard.resolve_safe_path(output_rel)
                if target.exists():
                    selected_source = selected_sources.get(output_rel)
                    if selected_source is None:
                        raise ValidationError(
                            f"Existing game-write target {output_rel!r} must be explicitly selected as approved context"
                        )
                    if (
                        not target.is_file()
                        or target.stat().st_size != selected_source.size_bytes
                        or target.stat().st_size > spec.max_context_bytes
                    ):
                        raise ValidationError(
                            f"Existing game-write target {output_rel!r} is not a selected regular source"
                        )
                    # The context builder read and hashed this exact path before
                    # approval construction; compare again to detect intervening edits.
                    current = hashlib.sha256(
                        _bounded_file_bytes(target, spec.max_context_bytes)
                    ).hexdigest()
                    if current != selected_source.sha256:
                        raise ValidationError(
                            f"Existing game-write target {output_rel!r} changed since context selection"
                        )
        return BoundedContext(tuple(selected), total)

    def approval_context(workflow: Workflow, task: Task) -> dict[str, Any]:
        spec = _spec(task.parameters["factory_spec"])
        context = source_context(workflow, spec)
        executor_entry = executor_registry.get(spec.executor_id)
        config_hash = executor_entry[3] if executor_entry else None
        return {
            "manifest_sha256": task.parameters.get("manifest_sha256"),
            "factory_spec": spec.model_dump(mode="json"),
            "executor_config_sha256": config_hash,
            "input_sources": [item.source.model_dump(mode="json") for item in context.items],
        }

    def operation_hash_for(
        workflow: Workflow, task: Task, approval_type: str, artifacts: list[Any] | None = None
    ) -> str:
        artifacts = artifact_repo.list_by_workflow(workflow.id) if artifacts is None else artifacts
        profile_context = approval_context(workflow, task)
        inputs = build_operation_inputs(
            workflow, task, artifacts, task.cost_class, None, profile_context
        )
        return compute_operation_hash(task.id, approval_type, inputs)

    def approved_provider(
        workflow: Workflow, task: Task, spec: WorkflowTaskSpec
    ) -> tuple[Any, ProviderAuthorization, str]:
        entries = sorted(
            (
                item
                for item in approval_repo.list_by_workflow(workflow.id)
                if item.task_id == task.id and item.approval_type == "factory_provider_call"
            ),
            key=lambda item: (item.requested_at, item.id),
            reverse=True,
        )
        artifacts_by_id = {item.id: item for item in artifact_repo.list_by_workflow(workflow.id)}
        approval: Any | None = None
        approved_artifacts: list[Any] = []
        for candidate in entries:
            candidate_artifacts = [
                artifacts_by_id[artifact_id]
                for artifact_id in candidate.artifact_ids
                if artifact_id in artifacts_by_id
            ]
            if len(candidate_artifacts) != len(candidate.artifact_ids):
                continue
            digest = operation_hash_for(
                workflow, task, "factory_provider_call", candidate_artifacts
            )
            if digest == candidate.operation_hash:
                approval = candidate
                approved_artifacts = candidate_artifacts
                break
        if approval is None or approval.status != ApprovalStatus.APPROVED:
            raise ValidationError("No current input-bound provider approval exists")
        digest = operation_hash_for(workflow, task, "factory_provider_call", approved_artifacts)
        if digest != approval.operation_hash:
            raise ValidationError(
                "Provider approval inputs changed after the human decision "
                f"(approved={approval.operation_hash}; current={digest}; "
                f"approval_type={approval.approval_type})"
            )
        executor_entry = executor_registry.get(spec.executor_id)
        if executor_entry is None:
            raise ValidationError("Executor is not registered")
        executor, capabilities, configured, config_hash = executor_entry
        if (
            not configured
            or not bool(getattr(executor, "is_configured", False))
            or not set(spec.capabilities).issubset(capabilities)
        ):
            raise ValidationError("Executor is unavailable or lacks required capabilities")
        if getattr(executor, "config_fingerprint", None) != config_hash:
            raise ValidationError("Executor configuration changed after registration")
        request_inputs = {
            "approval_operation_hash": digest,
            "executor_id": spec.executor_id,
            "executor_config_sha256": config_hash,
            "task_id": task.id,
            "manifest_sha256": task.parameters.get("manifest_sha256"),
            "factory_spec": spec.model_dump(mode="json"),
            "input_sources": approval_context(workflow, task)["input_sources"],
        }
        fingerprint = hashlib.sha256(
            json.dumps(
                request_inputs,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode()
        ).hexdigest()
        authorization = ProviderAuthorization(
            approval_id=approval.id,
            operation_hash=digest,
            request_fingerprint=fingerprint,
            max_cost=float(spec.cost.max_amount),
            currency=spec.cost.currency,
            unit=spec.cost.unit,
            cost_class=spec.cost.cost_class,
        )
        return executor_entry, authorization, fingerprint

    def save_text(
        workflow: Workflow,
        task: Task,
        execution: Execution,
        artifact_type: str,
        rel: str,
        payload: dict[str, Any],
        provider: str,
    ) -> Any:
        body = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
        )
        _reject_reparse_components(root, rel)
        artifact = artifact_mgr.create_text_artifact(
            workflow.id, task.id, artifact_type, provider, rel, body
        )
        _reject_reparse_components(root, rel)
        artifact_repo.save(artifact)
        return artifact

    def produce(workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        spec = _task_spec(task)
        if task.task_type != "factory_provider_task" or workflow.project_id != task.parameters.get(
            "project_id", workflow.project_id
        ):
            raise ValidationError("Factory provider task identity is invalid")
        executor_entry, authorization, fingerprint = approved_provider(workflow, task, spec)
        executor, executor_caps, configured, config_hash = executor_entry
        context = source_context(workflow, spec)
        contract = _build_agent_contract(spec, workflow.project_id, context.sources)
        eligible = agent_registry.route(contract, available_capabilities=executor_caps)
        selected = next(
            (candidate for candidate in eligible if candidate.definition.agent_id == spec.agent_id),
            None,
        )
        if selected is None:
            raise ValidationError(
                "The manifest-selected agent is not eligible for this executor/contract"
            )
        op_repo.ensure_project_cost_unit(
            workflow.project_id, f"{spec.cost.currency}:{spec.cost.unit}"
        )
        row, first_submission = op_repo.claim_intent(
            workflow.id,
            task.id,
            spec.executor_id,
            fingerprint,
            float(spec.cost.max_amount),
            spec.cost.currency,
            spec.cost.unit,
            float(spec.cost.max_amount),
            execution.id,
            authorization.approval_id,
            authorization.operation_hash,
        )
        if row["status"] == "COMPLETED":
            result_evidence = next(
                (
                    item
                    for item in reversed(evidence_repo.list_by_task(task.id))
                    if item.evidence_type == "factory_provider_result"
                    and item.execution_id == row.get("execution_id")
                ),
                None,
            )
            evidence_details = result_evidence.raw_data if result_evidence is not None else {}
            recovered_ids = (
                evidence_details.get("artifact_ids") if isinstance(evidence_details, dict) else None
            )
            if (
                not isinstance(recovered_ids, list)
                or not recovered_ids
                or evidence_details.get("request_fingerprint") != fingerprint
            ):
                raise ReconciliationRequired(
                    "Provider call completed but its complete result envelope is missing; no repeat was made",
                    task.id,
                    execution.id,
                    {"process_state_uncertain": True, "external_op_id": row.get("external_id")},
                )
            recovered = []
            for artifact_id in recovered_ids:
                artifact = artifact_repo.get(artifact_id)
                if (
                    artifact is None
                    or artifact.task_id != task.id
                    or (
                        artifact.artifact_type not in {"factory_proposal", "factory_visual_review"}
                        and not artifact.artifact_type.startswith("factory_output:")
                    )
                ):
                    raise ReconciliationRequired(
                        "Provider result references an invalid or incomplete artifact; no repeat was made",
                        task.id,
                        execution.id,
                        {"process_state_uncertain": True, "external_op_id": row.get("external_id")},
                    )
                artifact_mgr.verify_artifact_integrity(artifact)
                recovered.append(artifact)
            by_id = {item.id: item for item in recovered}
            referenced_output_ids: set[str] = set()
            for envelope in (
                item
                for item in recovered
                if item.artifact_type in {"factory_proposal", "factory_visual_review"}
            ):
                envelope_payload = json.loads(
                    guard.resolve_safe_path(envelope.relative_path).read_text(encoding="utf-8")
                )
                if envelope_payload.get("request_fingerprint") != fingerprint:
                    raise ReconciliationRequired(
                        "Recovered provider envelope has a different request fingerprint",
                        task.id,
                        execution.id,
                        {"process_state_uncertain": True, "external_op_id": row.get("external_id")},
                    )
                for output in envelope_payload.get("files", []):
                    output_artifact = by_id.get(output.get("artifact_id"))
                    if (
                        output_artifact is None
                        or not output_artifact.artifact_type.startswith("factory_output:")
                        or output_artifact.content_hash != output.get("sha256")
                    ):
                        raise ReconciliationRequired(
                            "Recovered provider envelope references missing or changed output bytes",
                            task.id,
                            execution.id,
                            {
                                "process_state_uncertain": True,
                                "external_op_id": row.get("external_id"),
                            },
                        )
                    referenced_output_ids.add(output_artifact.id)
            if referenced_output_ids != {
                item.id for item in recovered if item.artifact_type.startswith("factory_output:")
            }:
                raise ReconciliationRequired(
                    "Recovered result envelope does not account for its complete output artifact set",
                    task.id,
                    execution.id,
                    {"process_state_uncertain": True, "external_op_id": row.get("external_id")},
                )
            if (
                len(recovered) != len(recovered_ids)
                or not any(item.artifact_type == "factory_proposal" for item in recovered)
                and not any(item.artifact_type == "factory_visual_review" for item in recovered)
            ):
                raise ReconciliationRequired(
                    "Provider completion has no recognized immutable output envelope",
                    task.id,
                    execution.id,
                    {"process_state_uncertain": True, "external_op_id": row.get("external_id")},
                )
            evidence_repo.save(
                Evidence(
                    generate_id("EVI"),
                    task.id,
                    execution.id,
                    "factory_provider_recovery",
                    "Reused the complete immutable provider result after query/recovery without resubmission.",
                    {
                        "source_execution_id": row.get("execution_id"),
                        "request_fingerprint": fingerprint,
                        "artifact_ids": recovered_ids,
                        "artifact_hashes": {item.id: item.content_hash for item in recovered},
                    },
                )
            )
            return TaskHandlerResult(
                1,
                "Recovered the complete immutable provider result without resubmission.",
                recovered_ids,
            )
        if row["status"] in {"FAILED", "RECONCILED"}:
            raise ReconciliationRequired(
                "Prior provider intent is terminal; create a new approved workflow revision",
                task.id,
                execution.id,
                {"process_state_uncertain": True, "external_op_id": row.get("external_id")},
            )
        work = Path(tempfile.mkdtemp(prefix="gamefactory-provider-"))
        try:
            # Provider workspace is deliberately empty; approved source bytes
            # are supplied only through the bounded context object.
            if first_submission:
                raw_run = executor.execute(
                    contract, context, work, fingerprint, authorization, verifier
                )
            else:
                external_id = row.get("external_id")
                query = getattr(executor, "query", None)
                if not external_id or not callable(query):
                    raise ReconciliationRequired(
                        "An earlier call may have been submitted; query-only recovery is unavailable",
                        task.id,
                        execution.id,
                        {"process_state_uncertain": True},
                    )
                raw_run = query(external_id, fingerprint, authorization, verifier)
            run = ProviderRun.from_value(
                raw_run.to_dict() if isinstance(raw_run, ProviderRun) else raw_run
            )
            if run.actual_cost is not None and (run.cost_currency is None or run.cost_unit is None):
                raise ValidationError("A reported actual cost requires its currency and unit")
            # Persist provider identity and any known incurred charge before
            # validating response schemas or file scopes. Invalid output can
            # still be billable and must remain visible to reconciliation.
            same_cost_unit = (
                run.cost_currency == spec.cost.currency and run.cost_unit == spec.cost.unit
            )
            initial_status = (
                "UNCERTAIN"
                if run.status != ProviderExecutionStatus.FAILED
                or (run.actual_cost is not None and not same_cost_unit)
                else "FAILED"
            )
            persisted_actual = (
                run.actual_cost if run.actual_cost is not None and same_cost_unit else None
            )
            op_repo.update_intent(
                task.id, fingerprint, initial_status, run.external_id, persisted_actual
            )
            if run.actual_cost is not None and same_cost_unit:
                accounting.settle_terminal_success(
                    task.id,
                    run.actual_cost,
                    execution_id=execution.id,
                    cost_unit=f"{run.cost_currency}:{run.cost_unit}",
                )
            if run.status != ProviderExecutionStatus.COMPLETED:
                new_status = (
                    "UNCERTAIN" if run.status == ProviderExecutionStatus.UNCERTAIN else "FAILED"
                )
                if run.actual_cost is not None and not same_cost_unit:
                    assert run.cost_currency is not None and run.cost_unit is not None
                    op_repo.update_intent(task.id, fingerprint, "UNCERTAIN", run.external_id, None)
                    op_repo.record_unreconciled_charge(
                        task.id,
                        fingerprint,
                        actual_cost=run.actual_cost,
                        currency=run.cost_currency,
                        unit=run.cost_unit,
                        external_id=run.external_id,
                    )
                    evidence_repo.save(
                        Evidence(
                            generate_id("EVI"),
                            task.id,
                            execution.id,
                            "factory_provider_unreconciled_charge",
                            "Provider reported an actual charge in an incompatible unit; no implicit conversion or ledger settlement was performed.",
                            {
                                "request_fingerprint": fingerprint,
                                "actual_cost": run.actual_cost,
                                "currency": run.cost_currency,
                                "unit": run.cost_unit,
                                "external_id": run.external_id,
                                "status": "UNRECONCILED_NO_CONVERSION",
                            },
                        )
                    )
                    raise ReconciliationRequired(
                        "Provider result charge uses a different currency/unit; explicit reconciliation is required",
                        task.id,
                        execution.id,
                        {
                            "process_state_uncertain": True,
                            "external_op_id": run.external_id,
                            "reported_actual_cost": run.actual_cost,
                            "reported_currency": run.cost_currency,
                            "reported_unit": run.cost_unit,
                        },
                    )
                op_repo.update_intent(
                    task.id, fingerprint, new_status, run.external_id, run.actual_cost
                )
                if run.actual_cost is not None:
                    accounting.settle_terminal_success(
                        task.id,
                        run.actual_cost,
                        execution_id=execution.id,
                        cost_unit=f"{run.cost_currency}:{run.cost_unit}",
                    )
                if new_status == "UNCERTAIN" or run.actual_cost is None:
                    raise ReconciliationRequired(
                        "Provider result is uncertain or has unknown cost; reservation is retained",
                        task.id,
                        execution.id,
                        {"process_state_uncertain": True, "external_op_id": run.external_id},
                    )
                raise ValidationError(f"Provider returned terminal {run.status} status")
            if run.actual_cost is not None and (
                run.cost_currency != spec.cost.currency or run.cost_unit != spec.cost.unit
            ):
                assert run.cost_currency is not None and run.cost_unit is not None
                op_repo.update_intent(task.id, fingerprint, "UNCERTAIN", run.external_id, None)
                op_repo.record_unreconciled_charge(
                    task.id,
                    fingerprint,
                    actual_cost=run.actual_cost,
                    currency=run.cost_currency,
                    unit=run.cost_unit,
                    external_id=run.external_id,
                )
                evidence_repo.save(
                    Evidence(
                        generate_id("EVI"),
                        task.id,
                        execution.id,
                        "factory_provider_unreconciled_charge",
                        "Provider reported an actual charge in an incompatible unit; no implicit conversion or ledger settlement was performed.",
                        {
                            "request_fingerprint": fingerprint,
                            "actual_cost": run.actual_cost,
                            "currency": run.cost_currency,
                            "unit": run.cost_unit,
                            "external_id": run.external_id,
                            "status": "UNRECONCILED_NO_CONVERSION",
                        },
                    )
                )
                raise ReconciliationRequired(
                    "Actual provider charge uses a different currency/unit; preserve it for explicit reconciliation without conversion",
                    task.id,
                    execution.id,
                    {
                        "process_state_uncertain": True,
                        "external_op_id": run.external_id,
                        "reported_actual_cost": run.actual_cost,
                        "reported_currency": run.cost_currency,
                        "reported_unit": run.cost_unit,
                    },
                )
            if run.actual_cost is not None and run.actual_cost > spec.cost.max_amount:
                op_repo.update_intent(
                    task.id, fingerprint, "UNCERTAIN", run.external_id, run.actual_cost
                )
                accounting.settle_terminal_success(
                    task.id,
                    run.actual_cost,
                    execution_id=execution.id,
                    cost_unit=f"{run.cost_currency}:{run.cost_unit}",
                )
                execution.cost = float(run.actual_cost)
                execution.cost_unit = f"{run.cost_currency}:{run.cost_unit}"
                raise ReconciliationRequired(
                    "Provider output incurred a charge above its approved ceiling; actual charge recorded, no further dispatch or acceptance is allowed",
                    task.id,
                    execution.id,
                    {
                        "process_state_uncertain": True,
                        "external_op_id": run.external_id,
                        "reported_actual_cost": run.actual_cost,
                        "approved_maximum": spec.cost.max_amount,
                    },
                )
            expected_sources = {source.path: source.sha256 for source in context.sources}
            artifact_ids: list[str] = []
            rel_base = _artifact_dir(workflow.id, task.id, execution.id)
            if run.proposal is not None:
                if run.proposal.task_id != spec.task_id or run.proposal.agent_id != spec.agent_id:
                    raise ValidationError(
                        "Provider proposal task/agent identity does not match the selected contract"
                    )
                validated = Director().validate_proposal(
                    contract,
                    run.proposal,
                    agent_registry,
                    available_capabilities=executor_caps,
                    completed_dependencies=set(spec.dependencies),
                )
                if (
                    validated.outcome != AgentOutcome.PROPOSED
                    or validated.agent_id != spec.agent_id
                ):
                    raise ValidationError("Provider proposal is not an eligible PROPOSED result")
                declared_paths = {path: path for path in spec.output_scopes}
                files_by_path = {output.path: output for output in run.files}
                if (
                    len(files_by_path) > spec.max_output_files
                    or sum(len(output.bytes()) for output in run.files) > spec.max_output_bytes
                ):
                    raise ValidationError("Provider exceeded declared output file/byte bounds")
                if set(files_by_path) != {item.path for item in run.proposal.proposed_files}:
                    raise ValidationError(
                        "Proposal files and provider staged-file envelope do not match"
                    )
                if spec.expected_artifacts and set(files_by_path) != set(spec.expected_artifacts):
                    raise ValidationError(
                        "Provider outputs do not exactly satisfy expected_artifacts"
                    )
                context_hashes = {source.path: source.sha256 for source in context.sources}
                for proposed in run.proposal.proposed_files:
                    output = files_by_path[proposed.path]
                    if proposed.operation == "DELETE":
                        raise ValidationError("Provider DELETE proposals are forbidden")
                    if (
                        proposed.output_sha256 != output.sha256
                        or proposed.before_sha256 != output.expected_before_sha256
                    ):
                        raise ValidationError(
                            "Proposal before/output hashes differ from staged output envelope"
                        )
                    if (
                        proposed.path not in declared_paths
                        or _safe_relative(proposed.path) != proposed.path
                    ):
                        raise ValidationError(
                            f"Output {proposed.path!r} is outside exact declared output scopes"
                        )
                    target = guard.resolve_safe_path(proposed.path)
                    _reject_reparse_components(root, proposed.path)
                    if target.exists():
                        if (
                            proposed.operation != "UPDATE"
                            or proposed.before_sha256 is None
                            or context_hashes.get(proposed.path) != proposed.before_sha256
                        ):
                            raise ValidationError(
                                f"Update to existing target {proposed.path!r} lacks an exact approved source hash"
                            )
                    elif proposed.operation != "CREATE" or proposed.before_sha256 is not None:
                        raise ValidationError(
                            f"New target {proposed.path!r} must be an explicit CREATE with no before hash"
                        )
                output_records = []
                for index, output in enumerate(run.files):
                    rel = f"{rel_base}/outputs/{index:03d}-{output.sha256[:16]}"
                    _reject_reparse_components(root, rel)
                    safe = guard.ensure_safe_parent(rel)
                    with safe.open("xb") as stream:
                        stream.write(output.bytes())
                    _reject_reparse_components(root, rel)
                    artifact = artifact_mgr.register_file_artifact(
                        workflow.id,
                        task.id,
                        f"factory_output:{output.media_type}",
                        spec.executor_id,
                        rel,
                    )
                    artifact_repo.save(artifact)
                    artifact_ids.append(artifact.id)
                    output_records.append(
                        {
                            "path": output.path,
                            "sha256": output.sha256,
                            "media_type": output.media_type,
                            "expected_before_sha256": output.expected_before_sha256,
                            "artifact_id": artifact.id,
                        }
                    )
                proposal_payload = {
                    "schema_version": "factory-provider-proposal-1.0.0",
                    "execution_id": execution.id,
                    "request_fingerprint": fingerprint,
                    "operation_hash": authorization.operation_hash,
                    "executor_config_sha256": config_hash,
                    "candidate_source_hashes": expected_sources,
                    "contract": contract.model_dump(mode="json"),
                    "proposal": run.proposal.model_dump(mode="json"),
                    "provider_metadata": run.metadata,
                    "files": output_records,
                }
                proposal_art = save_text(
                    workflow,
                    task,
                    execution,
                    "factory_proposal",
                    f"{rel_base}/proposal.json",
                    proposal_payload,
                    spec.executor_id,
                )
                artifact_ids.append(proposal_art.id)
                summary = run.proposal.summary
            else:
                review = run.visual_review
                if (
                    review is None
                    or review.task_id != spec.task_id
                    or review.provider_id != spec.executor_id
                ):
                    raise ValidationError("Visual review task/provider identity mismatch")
                if run.files:
                    raise ValidationError("Visual review runs may not stage game files")
                if any(
                    source.path not in expected_sources
                    or expected_sources[source.path] != source.sha256
                    for source in review.reviewed_sources
                ):
                    raise ValidationError(
                        "Visual review cites a source outside or changed from the approved context"
                    )
                if len(review.reviewed_sources) != len(context.sources):
                    raise ValidationError(
                        "Visual review must cite every selected screenshot/reference/art-bible source"
                    )
                screenshot = next(
                    (
                        source
                        for source in context.sources
                        if "screenshot" in source.purpose.lower()
                    ),
                    None,
                )
                reference_hashes = [
                    source.sha256
                    for source in context.sources
                    if "reference" in source.purpose.lower()
                ]
                art_bible = next(
                    (
                        source
                        for source in context.sources
                        if "art bible" in source.purpose.lower()
                        or "art_bible" in source.purpose.lower()
                    ),
                    None,
                )
                if screenshot is None or not reference_hashes or art_bible is None:
                    raise ValidationError(
                        "Visual review inputs must explicitly include screenshot, reference, and art-bible sources"
                    )
                severity = "info"
                summary = (
                    "Unscored provider opinion; conclusion="
                    + review.conclusion
                    + ". "
                    + " ".join(review.findings)
                )[:2048]
                canonical_review = GameVisualReview.from_dict(
                    {
                        "schema_version": "visual-review-1.0.0",
                        "screenshot_sha256": screenshot.sha256,
                        "reference_sha256": reference_hashes,
                        "art_bible_sha256": art_bible.sha256,
                        "reviewer": f"provider:{review.provider_id}",
                        "findings": [
                            {
                                "dimension": "provider_visual_opinion",
                                "severity": severity,
                                "confidence": 0.0,
                                "summary": summary,
                                "evidence_refs": [
                                    f"source:{source.path}:{source.sha256}"
                                    for source in review.reviewed_sources
                                ],
                            }
                        ],
                    }
                )
                canonical_report = record_visual_review(canonical_review)
                review_payload = {
                    "schema_version": "factory-provider-visual-review-1.0.0",
                    "execution_id": execution.id,
                    "request_fingerprint": fingerprint,
                    "executor_config_sha256": config_hash,
                    "provider_review": review.model_dump(mode="json"),
                    "provider_finding_mapping": {
                        "kind": "unscored-advisory-string",
                        "confidence": 0.0,
                        "conclusion_is_not_a_gate": True,
                        "human_decision_required": True,
                    },
                    "canonical_advisory_report": canonical_report.to_dict(),
                    "screenshot_source": {"path": screenshot.path, "sha256": screenshot.sha256},
                    "reference_sha256": reference_hashes,
                    "art_bible_sha256": art_bible.sha256,
                    "source_purposes": {source.path: source.purpose for source in context.sources},
                }
                artifact = save_text(
                    workflow,
                    task,
                    execution,
                    "factory_visual_review",
                    f"{rel_base}/visual-review.json",
                    review_payload,
                    spec.executor_id,
                )
                artifact_ids.append(artifact.id)
                summary = "Provider visual findings are advisory only; explicit human review is still required."
            op_repo.update_intent(
                task.id, fingerprint, "COMPLETED", run.external_id, run.actual_cost
            )
            if run.actual_cost is not None and execution.cost > 0:
                accounting.settle_terminal_success(
                    task.id,
                    run.actual_cost,
                    execution_id=execution.id,
                    cost_unit=f"{run.cost_currency}:{run.cost_unit}",
                )
            execution.cost = float(
                run.actual_cost if run.actual_cost is not None else execution.estimated_cost
            )
            execution.cost_unit = f"{spec.cost.currency}:{spec.cost.unit}"
            execution.provider = spec.executor_id
            execution.external_op_id = run.external_id
            evidence_repo.save(
                Evidence(
                    generate_id("EVI"),
                    task.id,
                    execution.id,
                    "factory_provider_result",
                    summary,
                    {
                        "artifact_ids": artifact_ids,
                        "request_fingerprint": fingerprint,
                        "approval_id": authorization.approval_id,
                        "operation_hash": authorization.operation_hash,
                        "actual_cost": run.actual_cost,
                        "reservation_retained_for_unknown_cost": run.actual_cost is None,
                    },
                )
            )
            return TaskHandlerResult(1, summary, artifact_ids)
        except ReconciliationRequired:
            raise
        except Exception as exc:
            # After durable SUBMITTING intent, parse/scope/artifact failures may
            # still represent a paid call. Preserve the reserve for operator review.
            op_repo.update_intent(task.id, fingerprint, "UNCERTAIN", row.get("external_id"), None)
            raise ReconciliationRequired(
                f"Provider output could not be safely retained: {exc}",
                task.id,
                execution.id,
                {"process_state_uncertain": True, "external_op_id": row.get("external_id")},
            ) from exc
        finally:
            shutil.rmtree(work, ignore_errors=True)

    def candidate_snapshot(
        workflow: Workflow, writer_ids: list[str]
    ) -> tuple[Path, str, list[dict[str, Any]]]:
        temp = Path(tempfile.mkdtemp(prefix="gamefactory-candidate-"))
        try:
            # Apply the same bounded Godot staging exclusions while additionally
            # skipping the task's explicitly protected .verify-pytest directory
            # before it is opened or hashed.
            exclusions = _SKIP_DIRS | {".verify-pytest"}
            source_files: list[SourceFile] = []
            total_source_bytes = 0

            def walk_error(error: OSError) -> None:
                raise ValidationError(f"Cannot enumerate candidate source: {error}") from error

            for directory, child_dirs, filenames in os.walk(
                root, topdown=True, followlinks=False, onerror=walk_error
            ):
                current = Path(directory)
                keep: list[str] = []
                for dirname in sorted(child_dirs):
                    child = current / dirname
                    if dirname.lower() in exclusions:
                        continue
                    if _is_reparse(child):
                        raise ValidationError("Candidate project contains a symlink or junction")
                    keep.append(dirname)
                child_dirs[:] = keep
                for filename in sorted(filenames):
                    source = current / filename
                    if _is_reparse(source):
                        raise ValidationError("Candidate project contains a linked file")
                    if filename.lower() == "override.cfg" and source.parent == root:
                        continue
                    if (
                        filename.lower() == ".env"
                        or filename.lower().startswith(".env.")
                        or filename.lower() in _SECRET_FILES
                        or filename.lower().startswith(("id_rsa.", "id_ed25519."))
                        or _SECRET_NAMES.search(filename)
                        or source.suffix.lower() in _SECRET_SUFFIXES
                    ):
                        continue
                    info = source.stat()
                    if not source.is_file():
                        raise ValidationError("Candidate sources must be regular files")
                    total_source_bytes += info.st_size
                    if len(source_files) >= 10_000 or total_source_bytes > 512 * 1024 * 1024:
                        raise ValidationError("Candidate snapshot exceeds trusted staging bounds")
                    relative = source.relative_to(root).as_posix()
                    destination = temp / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    consumed = 0
                    descriptor = os.open(
                        source,
                        os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0),
                    )
                    with (
                        os.fdopen(descriptor, "rb") as incoming,
                        destination.open("xb") as outgoing,
                    ):
                        opened = os.fstat(incoming.fileno())
                        after_open = source.lstat()
                        if (opened.st_dev, opened.st_ino) != (
                            after_open.st_dev,
                            after_open.st_ino,
                        ) or opened.st_size != info.st_size:
                            raise ValidationError(
                                f"Candidate source changed while opening: {relative}"
                            )
                        while chunk := incoming.read(min(1024 * 1024, info.st_size + 1 - consumed)):
                            consumed += len(chunk)
                            if consumed > info.st_size:
                                raise ValidationError(
                                    f"Candidate source grew while reading: {relative}"
                                )
                            digest.update(chunk)
                            outgoing.write(chunk)
                        outgoing.flush()
                        os.fsync(outgoing.fileno())
                    if consumed != info.st_size:
                        raise ValidationError(f"Candidate source changed while copying: {relative}")
                    source_files.append(SourceFile(relative, digest.hexdigest(), consumed))
            source_files.sort(key=lambda item: item.relative_path)
            file_hashes: dict[str, str] = {
                entry.relative_path: str(entry.sha256) for entry in source_files
            }
            written: list[dict[str, Any]] = []
            for writer_id in writer_ids:
                # Candidate bytes must come from the same completed execution
                # lineage used by approval and acceptance. An orphan proposal
                # from a failed earlier attempt is never eligible for staging.
                proposal_art = latest_attempt_artifact(writer_id, "factory_proposal")
                if proposal_art is None:
                    raise ArtifactError(f"Candidate writer {writer_id} has no proposal artifact")
                artifact_mgr.verify_artifact_integrity(proposal_art)
                payload = json.loads(
                    guard.resolve_safe_path(proposal_art.relative_path).read_text(encoding="utf-8")
                )
                for record in payload.get("files", []):
                    rel = _safe_relative(record.get("path"))
                    output_art = artifact_repo.get(record.get("artifact_id", ""))
                    if (
                        output_art is None
                        or output_art.task_id != writer_id
                        or output_art.content_hash != record.get("sha256")
                    ):
                        raise ArtifactError(
                            "Candidate output artifact binding is missing or mismatched"
                        )
                    artifact_mgr.verify_artifact_integrity(output_art)
                    content_path = guard.resolve_safe_path(output_art.relative_path)
                    _reject_reparse_components(root, output_art.relative_path)
                    if content_path.stat().st_size > 64 * 1024 * 1024:
                        raise ValidationError(
                            "Staged output artifact exceeds the bounded file limit"
                        )
                    content = content_path.read_bytes()
                    current_hash = file_hashes.get(rel)
                    expected_before = record.get("expected_before_sha256")
                    if expected_before is None:
                        if current_hash is not None:
                            raise ValidationError(f"Candidate CREATE target {rel!r} already exists")
                    elif current_hash != expected_before:
                        raise ValidationError(f"Candidate UPDATE base hash mismatch for {rel!r}")
                    destination = temp / rel
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(content)
                    output_digest = hashlib.sha256(content).hexdigest()
                    file_hashes[rel] = output_digest
                    written.append(
                        {
                            "task_id": writer_id,
                            "path": rel,
                            "sha256": output_digest,
                            "expected_before_sha256": expected_before,
                            "artifact_id": output_art.id,
                        }
                    )
            candidate_files, candidate_hash = GodotStager(
                temp, temp.parent / "scratch"
            ).source_manifest()
            return temp, candidate_hash, written
        except Exception:
            shutil.rmtree(temp, ignore_errors=True)
            raise

    def evaluate_gate(workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        gate_name = task.parameters.get("gate")
        if gate_name not in {"code", "gameplay", "performance", "visual"}:
            raise ValidationError("Unknown quality gate")
        source_id = task.parameters.get("source_task_id")
        writer_ids = task.parameters.get("candidate_writer_tasks", [])
        if not isinstance(writer_ids, list) or any(
            not isinstance(item, str) for item in writer_ids
        ):
            raise ValidationError("Gate candidate writers are malformed")
        registered = gate_registry.get(gate_name)
        if registered is None:
            raise ValidationError(f"Required {gate_name} validator/harness is not configured")
        runner, runner_hash = registered
        if getattr(runner, "config_fingerprint", None) != runner_hash:
            raise ValidationError("Gate runner configuration changed after preflight/registration")
        temp, candidate_hash, written = candidate_snapshot(workflow, writer_ids)
        try:
            receipts: list[str] = []

            def record_process_intent(kind: str, details: dict[str, Any]) -> str:
                allowed_keys = {
                    "candidate_sha256",
                    "runner_config_sha256",
                    "engine_version",
                    "scenario_sha256",
                    "request_sha256",
                    "input_sha256",
                    "purpose",
                    "timeout_seconds",
                    "profile",
                }
                if (
                    not isinstance(kind, str)
                    or not kind.strip()
                    or len(kind) > 96
                    or not isinstance(details, dict)
                    or set(details) - allowed_keys
                ):
                    raise ValidationError("Gate process receipt fields are invalid")
                if (
                    details.get("candidate_sha256") != candidate_hash
                    or details.get("runner_config_sha256") != runner_hash
                ):
                    raise ValidationError(
                        "Gate process receipt is not bound to the approved candidate/runner"
                    )
                try:
                    encoded = json.dumps(
                        details, allow_nan=False, sort_keys=True, separators=(",", ":")
                    )
                except (TypeError, ValueError) as exc:
                    raise ValidationError("Gate process receipt must be finite JSON") from exc
                if len(encoded.encode("utf-8")) > 16_384:
                    raise ValidationError("Gate process receipt exceeds the durable receipt limit")
                receipt_id = generate_id("EVI")
                evidence_repo.save(
                    Evidence(
                        receipt_id,
                        task.id,
                        execution.id,
                        "factory_gate_process_intent",
                        f"Durable process intent for {gate_name} gate.",
                        {
                            "schema_version": "factory-gate-process-intent-1.0.0",
                            "gate": gate_name,
                            "candidate_sha256": candidate_hash,
                            "runner_config_sha256": runner_hash,
                            "kind": kind,
                            "intent": details,
                        },
                    )
                )
                receipts.append(receipt_id)
                return receipt_id

            gate_parameters = {
                **task.parameters,
                "execution_id": execution.id,
                "attempt_number": execution.attempt_number,
                "runner_config_sha256": runner_hash,
                "record_process_intent": record_process_intent,
            }
            visual_sources: list[dict[str, Any]] = []
            visual_source_files: list[dict[str, Any]] = []
            visual_capture_provenance: list[dict[str, Any]] = []
            provider_visual_reviews: list[dict[str, Any]] = []
            if gate_name == "visual":
                task_map = workflow_tasks(workflow.id)
                source_spec_ids = set(writer_ids)
                if isinstance(source_id, str):
                    source_spec_ids.add(source_id)
                for source_spec_id in source_spec_ids:
                    source_task = task_map.get(source_spec_id)
                    if source_task is None:
                        continue
                    source_spec = _task_spec(source_task)
                    context = source_context(
                        workflow,
                        source_spec,
                        include_gate_evidence=not task.parameters.get("combined_capture"),
                    )
                    for item in context.items:
                        source_ref = item.source.model_dump(mode="json")
                        visual_sources.append(source_ref)
                        if (
                            "screenshot" in item.source.purpose.lower()
                            or "reference" in item.source.purpose.lower()
                            or "art bible" in item.source.purpose.lower()
                            or "art_bible" in item.source.purpose.lower()
                        ):
                            visual_source_files.append(
                                {
                                    "path": item.source.path,
                                    "sha256": item.source.sha256,
                                    "purpose": item.source.purpose,
                                    "content_base64": base64.b64encode(item.content).decode(
                                        "ascii"
                                    ),
                                }
                            )
                    if not task.parameters.get("combined_capture"):
                        for selector in source_spec.inputs:
                            if selector.source_gate is None:
                                continue
                            capture_id = (
                                f"{selector.source_task_id}:gate:{selector.source_gate}"
                                if selector.source_gate_scope == "predecessor"
                                else f"{source_spec.task_id}:combined-capture:{selector.source_gate}"
                            )
                            capture_art = latest_attempt_artifact(
                                capture_id, "factory_quality_report"
                            )
                            if capture_art is None:
                                raise ValidationError(
                                    "Visual review capture report is not from a completed attempt"
                                )
                            capture_payload = json.loads(
                                guard.resolve_safe_path(capture_art.relative_path).read_text(
                                    encoding="utf-8"
                                )
                            )
                            if capture_payload.get("candidate_sha256") != candidate_hash:
                                raise ValidationError(
                                    "Reviewed screenshot was captured from a different candidate snapshot"
                                )
                            selected_evidence = [
                                entry
                                for entry in capture_payload.get("physical_evidence", [])
                                if entry.get("name") == selector.evidence_name
                            ]
                            if len(selected_evidence) != 1:
                                raise ValidationError(
                                    "Selected screenshot is not unique in its immutable capture report"
                                )
                            visual_capture_provenance.append(
                                {
                                    "gate_task_id": capture_id,
                                    "report_artifact_id": capture_art.id,
                                    "report_sha256": capture_art.content_hash,
                                    "candidate_sha256": capture_payload.get("candidate_sha256"),
                                    "evidence_name": selector.evidence_name,
                                    "evidence_artifact_id": selected_evidence[0].get("artifact_id"),
                                    "evidence_sha256": selected_evidence[0].get("sha256"),
                                }
                            )
                seen_sources: set[tuple[str, str]] = set()
                unique_sources: list[dict[str, Any]] = []
                for visual_source in visual_sources:
                    identity = (visual_source["path"], visual_source["sha256"])
                    if identity not in seen_sources:
                        seen_sources.add(identity)
                        unique_sources.append(visual_source)
                visual_sources = unique_sources
                review_task_ids = set(writer_ids)
                if not task.parameters.get("combined_capture") and isinstance(source_id, str):
                    review_task_ids.add(source_id)
                for review_task_id in review_task_ids:
                    artifact = latest_attempt_artifact(review_task_id, "factory_visual_review")
                    if artifact is None:
                        continue
                    artifact_mgr.verify_artifact_integrity(artifact)
                    _reject_reparse_components(root, artifact.relative_path)
                    visual_path = guard.resolve_safe_path(artifact.relative_path)
                    if visual_path.stat().st_size > 256 * 1024:
                        raise ValidationError(
                            "Stored provider visual review exceeds its bounded envelope size"
                        )
                    provider_visual_reviews.append(
                        {
                            "artifact_id": artifact.id,
                            "sha256": artifact.content_hash,
                            "payload": json.loads(visual_path.read_text(encoding="utf-8")),
                        }
                    )
                gate_parameters["visual_source_hashes"] = visual_sources
                gate_parameters["visual_source_files"] = visual_source_files
                gate_parameters["visual_capture_provenance"] = visual_capture_provenance
                gate_parameters["provider_visual_reviews"] = provider_visual_reviews
            raw = runner.evaluate(
                temp,
                candidate_hash,
                gate=gate_name,
                task_spec=task.parameters.get("factory_spec"),
                parameters=gate_parameters,
            )
            if not isinstance(raw, dict) or raw.get("candidate_sha256") != candidate_hash:
                raise ValidationError(
                    "Registered validator did not bind its evidence to the exact candidate snapshot"
                )
            if (
                gate_name in {"code", "gameplay", "performance"}
                or task.parameters.get("combined_capture")
            ) and not receipts:
                raise ValidationError(
                    "Trusted gate runner did not persist a pre-dispatch process intent receipt"
                )
            returned_receipts = raw.get("process_receipt_ids", receipts)
            if returned_receipts != receipts:
                raise ValidationError(
                    "Gate runner process receipt references differ from durable pre-dispatch receipts"
                )
            evidence_files = raw.get("evidence_files")
            if (
                not isinstance(evidence_files, list)
                or not evidence_files
                or len(evidence_files) > 32
            ):
                raise ValidationError("Gate runner must return bounded physical evidence files")
            evidence_names = [item.get("name") for item in evidence_files if isinstance(item, dict)]
            if len(evidence_names) != len(evidence_files) or len(evidence_names) != len(
                set(evidence_names)
            ):
                raise ValidationError(
                    "Gate evidence filenames must be unique for unambiguous manifest selectors"
                )
            physical_refs: list[str] = []
            physical_records: list[dict[str, Any]] = []
            total_evidence_bytes = 0
            for index, evidence_file in enumerate(evidence_files):
                if not isinstance(evidence_file, dict) or set(evidence_file) != {
                    "name",
                    "sha256",
                    "content_base64",
                    "media_type",
                    "purpose",
                }:
                    raise ValidationError("Gate evidence file envelope fields are invalid")
                name = _safe_relative(evidence_file["name"])
                if not _SHA256.fullmatch(evidence_file["sha256"]):
                    raise ValidationError("Gate evidence file sha256 is invalid")
                try:
                    content = base64.b64decode(evidence_file["content_base64"], validate=True)
                except (ValueError, TypeError) as exc:
                    raise ValidationError("Gate evidence file content is not valid base64") from exc
                if (
                    len(content) > 64 * 1024 * 1024
                    or hashlib.sha256(content).hexdigest() != evidence_file["sha256"]
                ):
                    raise ValidationError("Gate evidence file hash or per-file size limit failed")
                total_evidence_bytes += len(content)
                if total_evidence_bytes > 80 * 1024 * 1024:
                    raise ValidationError("Gate evidence files exceed aggregate limit")
                evidence_rel = f"{_artifact_dir(workflow.id, task.id, execution.id)}/evidence/{index:03d}-{evidence_file['sha256'][:16]}"
                _reject_reparse_components(root, evidence_rel)
                safe = guard.ensure_safe_parent(evidence_rel)
                with safe.open("xb") as stream:
                    stream.write(content)
                artifact = artifact_mgr.register_file_artifact(
                    workflow.id,
                    task.id,
                    f"factory_gate_evidence:{evidence_file['media_type']}",
                    f"FactoryGate:{gate_name}",
                    evidence_rel,
                )
                artifact_repo.save(artifact)
                physical_refs.append(f"artifact:{artifact.id}:{artifact.content_hash}")
                physical_records.append(
                    {
                        "name": name,
                        "artifact_id": artifact.id,
                        "sha256": artifact.content_hash,
                        "media_type": evidence_file["media_type"],
                        "purpose": evidence_file["purpose"],
                    }
                )
            if gate_name == "gameplay":
                scenario_data = raw.get("scenario")
                observation_value = raw.get("observation")
                if not isinstance(scenario_data, dict) or not isinstance(observation_value, dict):
                    raise ValidationError(
                        "Gameplay runner must return scenario and observation objects"
                    )
                observation_data = dict(observation_value)
                observation_data["evidence_refs"] = physical_refs
                report = evaluate_gameplay(scenario_data, observation_data)
            elif gate_name == "performance":
                budget_data = raw.get("budget")
                evidence_value = raw.get("evidence")
                if not isinstance(budget_data, dict) or not isinstance(evidence_value, dict):
                    raise ValidationError(
                        "Performance runner must return budget and evidence objects"
                    )
                perf_data = dict(evidence_value)
                perf_data["evidence_refs"] = physical_refs
                report = evaluate_performance(budget_data, perf_data)
            elif gate_name == "visual":
                review_data = raw.get("review")
                review = GameVisualReview.from_dict(review_data)
                screenshot_files = [
                    item for item in physical_records if item["purpose"].lower() == "screenshot"
                ]
                reference_sources = [
                    item for item in visual_sources if "reference" in item["purpose"].lower()
                ]
                art_bible_sources = [
                    item
                    for item in visual_sources
                    if "art bible" in item["purpose"].lower()
                    or "art_bible" in item["purpose"].lower()
                ]
                if (
                    len(screenshot_files) != 1
                    or not reference_sources
                    or len(art_bible_sources) != 1
                ):
                    raise ValidationError(
                        "Visual gate requires one verified screenshot plus selected reference and art-bible sources"
                    )
                if (
                    review.screenshot_sha256 != screenshot_files[0]["sha256"]
                    or review.reference_sha256
                    != tuple(dict.fromkeys(item["sha256"] for item in reference_sources))
                    or review.art_bible_sha256 != art_bible_sources[0]["sha256"]
                ):
                    raise ValidationError(
                        "Visual report screenshot/reference/art-bible hashes do not match physical and selected inputs"
                    )
                review_dict = review.to_dict()
                for finding in review_dict["findings"]:
                    finding["evidence_refs"] = physical_refs
                report = record_visual_review(GameVisualReview.from_dict(review_dict))
            else:
                report_data = raw.get("report")
                report_data = dict(report_data or {})
                for finding in report_data.get("findings", []):
                    finding["evidence_refs"] = physical_refs
                report = QualityReport.from_dict(report_data)
                if (
                    not report.findings
                    or report.visual_advisories
                    or any(item.artifact_sha256 != candidate_hash for item in report.findings)
                ):
                    raise ValidationError(
                        "Code validator findings must be non-empty and bound to candidate_sha256"
                    )
            result_payload = {
                "schema_version": "factory-quality-gate-1.0.0",
                "execution_id": execution.id,
                "manifest_sha256": task.parameters.get("manifest_sha256"),
                "gate_task_id": task.id,
                "gate": gate_name,
                "candidate_sha256": candidate_hash,
                "runner_config_sha256": runner_hash,
                "writer_tasks": writer_ids,
                "written_outputs": written,
                "physical_evidence": physical_records,
                "process_receipt_ids": receipts,
                "report": report.to_dict(),
                "scenario_sha256": raw.get("scenario_sha256")
                or (raw.get("scenario") or {}).get("scenario_sha256"),
                "budget_sha256": raw.get("budget_sha256")
                or (raw.get("budget") or {}).get("sha256"),
            }
            rel = f"{_artifact_dir(workflow.id, task.id, execution.id)}/quality-{gate_name}.json"
            artifact = save_text(
                workflow,
                task,
                execution,
                "factory_quality_report",
                rel,
                result_payload,
                f"FactoryGate:{gate_name}",
            )
            status = (
                GateStatus.PENDING
                if gate_name == "visual"
                else GateStatus.FAILED
                if report.status == "FAIL"
                else GateStatus.PASSED
            )
            gate_repo.save(
                QualityGate(
                    generate_id("GATE"),
                    task.id,
                    f"factory:{gate_name}",
                    status,
                    utc_now_iso(),
                    "Visual findings are advisory pending explicit human decision."
                    if gate_name == "visual"
                    else f"Independent {gate_name} evaluator returned {report.status} for candidate {candidate_hash}.",
                )
            )
            evidence_repo.save(
                Evidence(
                    generate_id("EVI"),
                    task.id,
                    execution.id,
                    f"factory_quality:{gate_name}",
                    f"{gate_name} evaluator report: {report.status}",
                    {
                        "artifact_id": artifact.id,
                        "candidate_sha256": candidate_hash,
                        "runner_config_sha256": runner_hash,
                        "report": report.to_dict(),
                    },
                )
            )
            if gate_name != "visual" and report.status == "FAIL":
                raise ValidationError(
                    f"Required {gate_name} gate failed; candidate cannot be accepted"
                )
            return TaskHandlerResult(
                1,
                f"{gate_name} evidence recorded for candidate {candidate_hash} with status {report.status}.",
                [artifact.id],
            )
        finally:
            shutil.rmtree(temp, ignore_errors=True)

    def visual_decision(workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        gate_task_id = task.parameters.get("gate_task_id")
        if not isinstance(gate_task_id, str):
            raise ValidationError("Visual decision is missing its gate task identity")
        report_art = latest_attempt_artifact(gate_task_id, "factory_quality_report")
        if report_art is None:
            raise ArtifactError("Advisory visual report is missing")
        artifact_mgr.verify_artifact_integrity(report_art)
        payload = json.loads(
            guard.resolve_safe_path(report_art.relative_path).read_text(encoding="utf-8")
        )
        if payload.get("gate") != "visual" or not payload.get("candidate_sha256"):
            raise ValidationError("Visual decision is not bound to an advisory report/candidate")
        decision_art = save_text(
            workflow,
            task,
            execution,
            "factory_visual_decision",
            f"{_artifact_dir(workflow.id, task.id, execution.id)}/visual-decision.json",
            {
                "schema_version": "factory-visual-decision-1.0.0",
                "execution_id": execution.id,
                "report_artifact_id": report_art.id,
                "report_sha256": report_art.content_hash,
                "candidate_sha256": payload["candidate_sha256"],
            },
            "FactoryVisualDecision",
        )
        gate_repo.save(
            QualityGate(
                generate_id("GATE"),
                task.id,
                "factory:visual_human_decision",
                GateStatus.PASSED,
                utc_now_iso(),
                "An explicit, hash-bound human visual decision was approved.",
            )
        )
        evidence_repo.save(
            Evidence(
                generate_id("EVI"),
                task.id,
                execution.id,
                "factory_visual_human_decision",
                "Human approved the exact visual advisory report and candidate snapshot.",
                {
                    "decision_artifact_id": decision_art.id,
                    "report_artifact_id": report_art.id,
                    "report_sha256": report_art.content_hash,
                    "candidate_sha256": payload["candidate_sha256"],
                    "approval_id": next(
                        (
                            a.id
                            for a in approval_repo.list_by_workflow(workflow.id)
                            if a.task_id == task.id and a.status == ApprovalStatus.APPROVED
                        ),
                        None,
                    ),
                },
            )
        )
        return TaskHandlerResult(
            1,
            "Explicit human visual review recorded; provider opinion itself did not approve the candidate.",
            [decision_art.id],
        )

    def visual_decision_context(workflow: Workflow, task: Task) -> dict[str, Any]:
        gate_task_id = task.parameters.get("gate_task_id")
        if not isinstance(gate_task_id, str):
            raise ValidationError("Visual decision is missing its gate task identity")
        report_art = latest_attempt_artifact(gate_task_id, "factory_quality_report")
        if report_art is None:
            raise ArtifactError("Advisory visual report is missing before human approval")
        artifact_mgr.verify_artifact_integrity(report_art)
        payload = json.loads(
            guard.resolve_safe_path(report_art.relative_path).read_text(encoding="utf-8")
        )
        return {
            "manifest_sha256": task.parameters.get("manifest_sha256"),
            "gate_task_id": gate_task_id,
            "report_artifact_id": report_art.id,
            "report_sha256": report_art.content_hash,
            "candidate_sha256": payload.get("candidate_sha256"),
        }

    def gate_approval_context(workflow: Workflow, task: Task) -> dict[str, Any]:
        gate = task.parameters.get("gate")
        registered = gate_registry.get(gate) if isinstance(gate, str) else None
        if registered is None:
            raise ValidationError(f"Required {gate!r} gate runner is unavailable")
        runner, registered_hash = registered
        if getattr(runner, "config_fingerprint", None) != registered_hash:
            raise ValidationError("Gate runner configuration changed before process approval")
        writers = task.parameters.get("candidate_writer_tasks", [])
        temp, candidate_hash, writes = candidate_snapshot(workflow, writers)
        shutil.rmtree(temp, ignore_errors=True)
        return {
            "manifest_sha256": task.parameters.get("manifest_sha256"),
            "gate": gate,
            "runner_config_sha256": registered_hash,
            "candidate_sha256": candidate_hash,
            "candidate_outputs": writes,
            "gate_parameters": task.parameters,
        }

    def ready_apply_journal(
        workflow: Workflow, task: Task, current_execution_id: str | None = None
    ) -> tuple[str, dict[str, Any]] | None:
        journal_dir = root / ".gamefactory" / "operations" / "factory-apply"
        if not journal_dir.is_dir() or _is_reparse(journal_dir):
            return None
        # Journal names are opaque implementation details and existing durable
        # journals may predate the current task-id filename encoding. Validate
        # every bounded recent candidate by its strict payload/database binding.
        for candidate in sorted(journal_dir.glob("*.json"))[-64:]:
            relative = candidate.relative_to(root).as_posix()
            try:
                journal_path, payload, _ = _read_factory_journal(
                    root, _factory_journal_relative(relative)
                )
            except (OSError, ValidationError):
                continue
            if (
                payload["status"] != "FINALIZATION_READY"
                or payload["workflow_id"] != workflow.id
                or payload["task_id"] != task.id
                or payload["candidate_sha256"] is None
            ):
                continue
            try:
                _, _, _ = _journal_database_binding(
                    root, payload, db, allow_current_execution_id=current_execution_id
                )
            except ValidationError:
                continue
            states, conflicts = _journal_file_states(root, payload)
            if conflicts or any(
                item["current_sha256"] != item["expected_after_sha256"] for item in states
            ):
                continue
            return relative, payload
        return None

    def acceptance_context(workflow: Workflow, task: Task) -> dict[str, Any]:
        recovered = ready_apply_journal(workflow, task)
        if recovered is not None:
            _, payload = recovered
            approved_context = payload.get("approved_context")
            if not isinstance(approved_context, dict):
                raise ValidationError("Committed journal is missing its approved operation context")
            return approved_context
        writer_ids = task.parameters.get("writer_tasks", [])
        refs = []
        for writer_id in writer_ids:
            artifact = latest_attempt_artifact(writer_id, "factory_proposal")
            if artifact is None:
                raise ValidationError(f"Writer {writer_id} has no latest completed proposal")
            refs.append((artifact.id, artifact.content_hash))
        for gate_id in task.parameters.get("required_gate_tasks", []):
            artifact = latest_attempt_artifact(gate_id, "factory_quality_report")
            if artifact is None:
                raise ValidationError(f"Required gate {gate_id} has no latest completed report")
            refs.append((artifact.id, artifact.content_hash))
        for decision_id in task.parameters.get("required_decision_tasks", []):
            artifact = latest_attempt_artifact(decision_id, "factory_visual_decision")
            if artifact is None:
                raise ValidationError(
                    f"Visual decision {decision_id} has no latest completed artifact"
                )
            refs.append((artifact.id, artifact.content_hash))
        temp, candidate_hash, _ = candidate_snapshot(workflow, writer_ids)
        shutil.rmtree(temp, ignore_errors=True)
        return {
            "manifest_sha256": task.parameters.get("manifest_sha256"),
            "writer_tasks": writer_ids,
            "required_gate_tasks": task.parameters.get("required_gate_tasks"),
            "required_decision_tasks": task.parameters.get("required_decision_tasks"),
            "candidate_sha256": candidate_hash,
            "candidate_evidence": sorted(refs),
        }

    def accept(workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        recovered = ready_apply_journal(workflow, task, execution.id)
        if recovered is not None:
            relative, payload = recovered
            artifact_id = payload.get("finalization_artifact_id")
            artifact = artifact_repo.get(artifact_id) if isinstance(artifact_id, str) else None
            if (
                artifact is None
                or artifact.artifact_type != "factory_application"
                or artifact.workflow_id != workflow.id
                or artifact.task_id != task.id
            ):
                raise ValidationError(
                    "Committed journal is missing its immutable application artifact"
                )
            artifact_mgr.verify_artifact_integrity(artifact)
            application = json.loads(
                guard.resolve_safe_path(artifact.relative_path).read_text(encoding="utf-8")
            )
            if (
                application.get("candidate_sha256") != payload["candidate_sha256"]
                or application.get("journal") != relative
            ):
                raise ValidationError(
                    "Recovered application artifact does not match its committed journal"
                )
            return TaskHandlerResult(
                1,
                "Resumed a verified committed game-file application without reapplying outputs.",
                [artifact.id],
            )
        writer_ids = task.parameters.get("writer_tasks")
        gate_task_ids = task.parameters.get("required_gate_tasks")
        decision_task_ids = task.parameters.get("required_decision_tasks", [])
        if (
            not isinstance(writer_ids, list)
            or not writer_ids
            or not isinstance(gate_task_ids, list)
            or not gate_task_ids
            or not isinstance(decision_task_ids, list)
        ):
            raise ValidationError("Acceptance task is missing writer or required gate identities")
        # Every declared gate must have a current report; only deterministic PASS
        # and separately human-approved visual decision artifacts can satisfy it.
        reports: dict[str, dict[str, Any]] = {}
        candidate_hashes: set[str] = set()
        for gate_task_id in gate_task_ids:
            report_art = latest_attempt_artifact(gate_task_id, "factory_quality_report")
            if report_art is None:
                raise ValidationError(f"Required gate {gate_task_id} has no report")
            artifact_mgr.verify_artifact_integrity(report_art)
            payload = json.loads(
                guard.resolve_safe_path(report_art.relative_path).read_text(encoding="utf-8")
            )
            payload["_report_artifact_id"] = report_art.id
            payload["_report_sha256"] = report_art.content_hash
            if payload.get("manifest_sha256") != task.parameters.get("manifest_sha256"):
                raise ValidationError("Gate report manifest identity does not match acceptance")
            if payload.get("gate") != "visual":
                if payload.get("report", {}).get("status") != "PASS":
                    raise ValidationError(
                        f"Required deterministic gate {gate_task_id} did not PASS"
                    )
            else:
                if not any(
                    (decision := workflow_tasks(workflow.id).get(decision_id)) is not None
                    and decision.parameters.get("gate_task_id") == gate_task_id
                    for decision_id in decision_task_ids
                ):
                    raise ValidationError(
                        "Advisory visual findings require their separately declared human decision task"
                    )
            candidate_hashes.add(payload["candidate_sha256"])
            reports[gate_task_id] = payload
        if len(candidate_hashes) != 1:
            raise ValidationError("Required gates evaluated different candidate snapshots")
        # Snapshot fresh baseline again and stage all outputs with CAS checks.
        for decision_id in decision_task_ids:
            decision = workflow_tasks(workflow.id).get(decision_id)
            if decision is None or not any(
                g.task_id == decision_id
                and g.gate_type == "factory:visual_human_decision"
                and g.status == GateStatus.PASSED
                for g in gate_repo.list_by_task(decision_id)
            ):
                raise ValidationError("Required human visual decision has not passed")
            decision_art = latest_attempt_artifact(decision_id, "factory_visual_decision")
            if decision_art is None:
                raise ArtifactError("Human visual decision artifact is missing")
            artifact_mgr.verify_artifact_integrity(decision_art)
            decision_payload = json.loads(
                guard.resolve_safe_path(decision_art.relative_path).read_text(encoding="utf-8")
            )
            linked_gate_id = decision.parameters.get("gate_task_id")
            if not isinstance(linked_gate_id, str):
                raise ValidationError("Visual decision is missing its gate task identity")
            linked = reports.get(linked_gate_id)
            linked_art = latest_attempt_artifact(linked_gate_id, "factory_quality_report")
            if (
                linked is None
                or linked_art is None
                or decision_payload.get("report_sha256") != linked_art.content_hash
                or decision_payload.get("candidate_sha256") != linked.get("candidate_sha256")
            ):
                raise ValidationError(
                    "Human visual decision is bound to a different report or candidate"
                )
        temp, candidate_hash, writes = candidate_snapshot(workflow, writer_ids)
        shutil.rmtree(temp, ignore_errors=True)
        if len(candidate_hashes) != 1 or candidate_hash != next(iter(candidate_hashes)):
            raise ValidationError(
                "Candidate changed after quality gates; new gates and approval are required"
            )
        destinations: list[tuple[Path, bytes, str, str | None]] = []
        for record in writes:
            artifact = artifact_repo.get(record["artifact_id"])
            if artifact is None:
                raise ArtifactError("Accepted output artifact disappeared")
            content = _bounded_file_bytes(
                guard.resolve_safe_path(artifact.relative_path), 64 * 1024 * 1024
            )
            target = guard.resolve_safe_path(record["path"])
            original = record["expected_before_sha256"]
            if target.exists():
                if (
                    original is None
                    or hashlib.sha256(_bounded_file_bytes(target, 512 * 1024 * 1024)).hexdigest()
                    != original
                ):
                    raise ValidationError(
                        f"Game file {record['path']!r} changed since proposal approval"
                    )
            elif original is not None:
                raise ValidationError(f"Expected original game file {record['path']!r} disappeared")
            destinations.append((target, content, record["path"], original))
        journal_dir = root / ".gamefactory" / "operations" / "factory-apply"
        _reject_reparse_components(root, ".gamefactory/operations/factory-apply")
        journal_dir.mkdir(parents=True, exist_ok=True)
        _reject_reparse_components(root, ".gamefactory/operations/factory-apply")
        journal = journal_dir / f"{_path_component(task.id)}-{_path_component(execution.id)}.json"
        backups: list[tuple[Path, Path | None, Path | None, str]] = []
        backup_root = (
            journal_dir / f"{_path_component(task.id)}-{_path_component(execution.id)}.backups"
        )
        _reject_reparse_components(root, backup_root.relative_to(root).as_posix())
        backup_root.mkdir(parents=True, exist_ok=True)
        _reject_reparse_components(root, backup_root.relative_to(root).as_posix())
        accepted_context = acceptance_context(workflow, task)
        current_approval_inputs = build_operation_inputs(
            workflow,
            task,
            artifact_repo.list_by_workflow(workflow.id),
            task.cost_class,
            None,
            accepted_context,
        )
        current_operation_hash = compute_operation_hash(
            task.id, "factory_game_write", current_approval_inputs
        )
        accepted_approval = next(
            (
                item
                for item in approval_repo.list_by_workflow(workflow.id)
                if item.task_id == task.id
                and item.approval_type == "factory_game_write"
                and item.status == ApprovalStatus.APPROVED
                and item.operation_hash == current_operation_hash
            ),
            None,
        )
        if accepted_approval is None:
            raise ValidationError(
                "Game-file promotion requires the current persisted human acceptance approval"
            )
        file_records = []
        for index, (target, content, rel, expected) in enumerate(destinations):
            old_bytes = _bounded_file_bytes(target, 512 * 1024 * 1024) if target.exists() else None
            backup_path = backup_root / f"{index:04d}.backup" if old_bytes is not None else None
            capture_path = backup_root / f"{index:04d}.capture" if old_bytes is not None else None
            if backup_path is not None:
                if old_bytes is None:
                    raise ValidationError("Original game-file bytes disappeared before backup")
                fd = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                with os.fdopen(fd, "wb") as stream:
                    stream.write(old_bytes)
                    stream.flush()
                    os.fsync(stream.fileno())
            backups.append((target, backup_path, capture_path, hashlib.sha256(content).hexdigest()))
            write = next(item for item in writes if item["path"] == rel)
            file_records.append(
                {
                    "path": rel,
                    "before_sha256": expected,
                    "after_sha256": hashlib.sha256(content).hexdigest(),
                    "artifact_id": write["artifact_id"],
                    "backup_path": backup_path.relative_to(root).as_posix()
                    if backup_path
                    else None,
                    "backup_sha256": hashlib.sha256(old_bytes).hexdigest()
                    if old_bytes is not None
                    else None,
                    "capture_path": capture_path.relative_to(root).as_posix()
                    if capture_path
                    else None,
                    "was_created": old_bytes is None,
                }
            )
        journal_payload = {
            "schema_version": "factory-apply-journal-1.0.0",
            "workflow_id": workflow.id,
            "task_id": task.id,
            "execution_id": execution.id,
            "approval_id": accepted_approval.id,
            "operation_hash": accepted_approval.operation_hash,
            "approved_context": accepted_context,
            "candidate_sha256": candidate_hash,
            "writer_tasks": writer_ids,
            "required_gate_tasks": gate_task_ids,
            "required_decision_tasks": decision_task_ids,
            "gate_reports": {
                gate_id: {
                    "report_artifact_id": reports[gate_id]["_report_artifact_id"],
                    "report_sha256": reports[gate_id]["_report_sha256"],
                    "candidate_sha256": reports[gate_id].get("candidate_sha256"),
                }
                for gate_id in reports
            },
            "status": "PREPARED",
            "files": file_records,
        }
        _write_json_atomic(journal, journal_payload)
        applied: list[Path] = []
        captured: set[Path] = set()
        promotion_conflicts: list[str] = []
        try:
            for index, (target, content, rel, expected) in enumerate(destinations):
                target.parent.mkdir(parents=True, exist_ok=True)
                _reject_reparse_components(root, rel)
                backup_path = backups[index][1]
                capture_path = backups[index][2]
                if backup_path is not None:
                    # Atomically capture whatever is actually at the destination.
                    # A stale pre-check can never overwrite a concurrent edit.
                    assert capture_path is not None
                    _reserve_recovery_slot(capture_path)
                    os.replace(target, capture_path)
                    captured.add(target)
                    captured_hash = _hash_regular_bounded(
                        root,
                        capture_path.relative_to(root).as_posix(),
                        capture_path,
                        512 * 1024 * 1024,
                    )
                    if captured_hash != expected:
                        if not target.exists():
                            try:
                                os.link(capture_path, target)
                                capture_path.unlink()
                                captured.discard(target)
                                promotion_conflicts.append(
                                    f"{rel}: destination changed during acceptance; concurrent bytes restored, immutable baseline retained"
                                )
                            except FileExistsError:
                                promotion_conflicts.append(
                                    f"{rel}: destination changed during acceptance; concurrent bytes preserved in capture"
                                )
                        raise ValidationError(
                            f"Game file {rel!r} changed during acceptance; immutable baseline and displaced bytes preserved"
                        )
                    capture_path.unlink()
                elif target.exists():
                    raise ValidationError(
                        f"Game file {rel!r} was concurrently created during acceptance"
                    )
                fd, name = tempfile.mkstemp(prefix=".factory-", dir=target.parent)
                try:
                    with os.fdopen(fd, "wb") as stream:
                        stream.write(content)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.link(name, target)
                    os.unlink(name)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
                applied.append(target)
                captured.discard(target)
            journal_payload["status"] = "COMMITTED"
            _write_json_atomic(journal, journal_payload)
        except Exception:
            rollback_errors = list(promotion_conflicts)
            for target, backup_path, capture_path, after_hash in reversed(backups):
                if target not in applied and target not in captured:
                    continue
                try:
                    if capture_path is not None and capture_path.exists():
                        capture_hash = _hash_regular_bounded(
                            root,
                            capture_path.relative_to(root).as_posix(),
                            capture_path,
                            512 * 1024 * 1024,
                        )
                        empty_hash = hashlib.sha256(b"").hexdigest()
                        if capture_hash not in {
                            hashlib.sha256(backup_path.read_bytes()).hexdigest()
                            if backup_path is not None
                            else None,
                            empty_hash,
                        }:
                            if target.exists():
                                raise OSError(
                                    "displaced concurrent edit and current target both preserved for recovery"
                                )
                            os.link(capture_path, target)
                            if (
                                _hash_regular_bounded(
                                    root,
                                    target.relative_to(root).as_posix(),
                                    target,
                                    512 * 1024 * 1024,
                                )
                                != capture_hash
                            ):
                                raise OSError(
                                    "displaced concurrent edit could not be restored exactly"
                                )
                            capture_path.unlink()
                            raise OSError(
                                "displaced concurrent edit restored; immutable original backup preserved"
                            )
                        capture_path.unlink()
                    if target.exists():
                        quarantine = (
                            backup_root
                            / f"rollback-{_path_component(target.relative_to(root).as_posix())}"
                        )
                        _reserve_recovery_slot(quarantine)
                        os.replace(target, quarantine)
                        quarantined_hash = _hash_regular_bounded(
                            root,
                            quarantine.relative_to(root).as_posix(),
                            quarantine,
                            512 * 1024 * 1024,
                        )
                        if quarantined_hash == after_hash:
                            quarantine.unlink()
                        elif backup_path is not None and quarantined_hash == _hash_regular_bounded(
                            root,
                            backup_path.relative_to(root).as_posix(),
                            backup_path,
                            512 * 1024 * 1024,
                        ):
                            if not target.exists():
                                os.link(quarantine, target)
                            quarantine.unlink()
                        else:
                            if not target.exists():
                                try:
                                    os.link(quarantine, target)
                                    quarantine.unlink()
                                except FileExistsError:
                                    pass
                            raise OSError(
                                "target changed after promotion; unexpected bytes and backup preserved"
                            )
                    if backup_path is not None and backup_path.exists():
                        try:
                            os.link(backup_path, target)
                        except FileExistsError as exc:
                            if _hash_regular_bounded(
                                root, target.relative_to(root).as_posix(), target, 512 * 1024 * 1024
                            ) != _hash_regular_bounded(
                                root,
                                backup_path.relative_to(root).as_posix(),
                                backup_path,
                                512 * 1024 * 1024,
                            ):
                                raise OSError(
                                    "concurrent target preserved; original backup retained for recovery"
                                ) from exc
                except OSError as rollback_error:
                    rollback_errors.append(
                        f"{target.relative_to(root).as_posix()}: {rollback_error}"
                    )
            journal_payload["status"] = "ROLLBACK_REQUIRED" if rollback_errors else "ROLLED_BACK"
            journal_payload["rollback_errors"] = rollback_errors
            _write_json_atomic(journal, journal_payload)
            raise
        result_path = f"{_artifact_dir(workflow.id, task.id, execution.id)}/application.json"
        result = save_text(
            workflow,
            task,
            execution,
            "factory_application",
            result_path,
            {
                "schema_version": "factory-application-1.0.0",
                "candidate_sha256": candidate_hash,
                "journal": journal.relative_to(root).as_posix(),
                "files": [
                    {"path": rel, "sha256": hashlib.sha256(content).hexdigest()}
                    for _, content, rel, _ in destinations
                ],
                "accepted_writer_tasks": writer_ids,
                "gate_reports": reports,
            },
            "FactoryAcceptance",
        )
        audit_repo.append(
            AuditEvent(
                generate_id("AUDIT"),
                "Workflow",
                workflow.id,
                "FACTORY_GAME_FILES_APPLIED",
                "WorkflowEngine",
                details={
                    "task_id": task.id,
                    "candidate_sha256": candidate_hash,
                    "journal": journal.relative_to(root).as_posix(),
                    "file_count": len(destinations),
                },
            )
        )
        return TaskHandlerResult(
            1,
            "Hash-bound candidate passed its declared gates and was applied with a recovery journal.",
            [result.id],
        )

    registry.register(
        "factory_provider_task",
        produce,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            managed_write=True,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
            mandatory_approval_type="factory_provider_call",
            safe_paid_recovery=True,
            approval_context=approval_context,
            recovery_check=lambda workflow, task, attempt: op_repo.queryable(
                task.id, executor_registry
            ),
        ),
    )
    registry.register(
        "factory_quality_gate",
        evaluate_gate,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            managed_write=True,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
            mandatory_approval_type="factory_candidate_validation",
            approval_context=gate_approval_context,
        ),
    )
    registry.register(
        "factory_visual_decision",
        visual_decision,
        TaskHandlerMetadata(
            operation=HandlerOperation.VISUAL_REVIEW,
            mandatory_approval_type="factory_visual_human_decision",
            approval_context=visual_decision_context,
        ),
    )
    registry.register(
        "factory_acceptance",
        accept,
        TaskHandlerMetadata(
            operation=HandlerOperation.REPOSITORY_WRITE,
            managed_write=True,
            mandatory_approval_type="factory_game_write",
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
            safe_paid_recovery=True,
            recovery_check=lambda workflow, task, _attempt: (
                ready_apply_journal(workflow, task) is not None
            ),
            approval_context=acceptance_context,
            changes_requested_blocks=lambda _task: True,
        ),
    )


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".journal-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _reserve_recovery_slot(path: Path) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)


@contextmanager
def _journal_file_lock(path: Path) -> Iterator[None]:
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if os.name == "nt":
            import msvcrt

            if os.fstat(fd).st_size == 0:
                os.write(fd, b"0")
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl_api = cast(Any, fcntl)

            fcntl_api.flock(fd, fcntl_api.LOCK_EX)
        yield
    finally:
        if os.name == "nt":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            try:
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl_api = cast(Any, fcntl)

            fcntl_api.flock(fd, fcntl_api.LOCK_UN)
        os.close(fd)


@contextmanager
def _factory_apply_recovery_locks(
    root: Path, workflow_id: str, journal_lock_path: Path
) -> Iterator[None]:
    """Acquire the normal workflow lock before the journal-specific lock."""
    with ExecutionLock(root / ".gamefactory" / "locks", workflow_id).hold():
        with _journal_file_lock(journal_lock_path):
            yield


def _factory_journal_relative(value: str) -> str:
    """Validate the private journal namespace without treating it as a game path."""
    if not isinstance(value, str) or len(value) > 512 or "\\" in value or value.startswith("/"):
        raise ValidationError("Apply journal path is invalid")
    parts = value.split("/")
    if (
        len(parts) != 4
        or parts[:3] != [".gamefactory", "operations", "factory-apply"]
        or not re.fullmatch(r"[A-Za-z0-9_-]{1,192}\.json", parts[3])
    ):
        raise ValidationError(
            "Journal must be directly inside .gamefactory/operations/factory-apply"
        )
    if any(part in {"", ".", ".."} or any(ord(ch) < 32 for ch in part) for part in parts):
        raise ValidationError("Apply journal path is not normalized")
    return "/".join(parts)


def _bounded_file_bytes(path: Path, maximum: int) -> bytes:
    """Read a stable, regular file without trusting a prior pathname size check."""
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        with os.fdopen(fd, "rb") as stream:
            path_before = path.lstat()
            before = os.fstat(stream.fileno())
            if (
                not stat.S_ISREG(path_before.st_mode)
                or not stat.S_ISREG(before.st_mode)
                or (path_before.st_dev, path_before.st_ino) != (before.st_dev, before.st_ino)
                or before.st_size < 0
                or before.st_size > maximum
            ):
                raise ValidationError(
                    "Managed evidence file is not regular or exceeds its size bound"
                )
            data = stream.read(maximum + 1)
            after = os.fstat(stream.fileno())
            path_after = path.lstat()
            if (
                len(data) > maximum
                or len(data) != before.st_size
                or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
                != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
                or (path_after.st_dev, path_after.st_ino) != (after.st_dev, after.st_ino)
            ):
                raise ValidationError("Managed evidence file changed while it was being read")
            return data
    except OSError as exc:
        raise ValidationError("Managed evidence file is unavailable") from exc


def _validated_project_root(value: Path | str) -> Path:
    original = Path(value).absolute()
    for component in reversed((original, *original.parents)):
        if _is_reparse(component):
            raise ValidationError("Project root path cannot traverse a symlink or junction")
    return original.resolve(strict=True)


def _read_factory_journal(root: Path, relative: str) -> tuple[Path, dict[str, Any], str]:
    path = root / Path(*relative.split("/"))
    _reject_reparse_components(root, relative)
    raw = _bounded_file_bytes(path, 1_000_000)
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("Apply journal is not valid JSON") from exc
    required = {
        "schema_version",
        "workflow_id",
        "task_id",
        "execution_id",
        "approval_id",
        "operation_hash",
        "approved_context",
        "candidate_sha256",
        "writer_tasks",
        "required_gate_tasks",
        "required_decision_tasks",
        "gate_reports",
        "status",
        "files",
    }
    allowed = required | {
        "rollback_errors",
        "recovery_conflicts",
        "recovered_at",
        "recovery_actor",
        "recovery_comment",
        "finalization_artifact_id",
        "finalized_at",
    }
    if not isinstance(payload, dict) or set(payload) - allowed or required - set(payload):
        raise ValidationError("Apply journal fields do not match the strict versioned schema")
    if payload["schema_version"] != "factory-apply-journal-1.0.0" or payload["status"] not in {
        "PREPARED",
        "COMMITTED",
        "ROLLBACK_REQUIRED",
        "ROLLED_BACK",
        "FINALIZATION_READY",
    }:
        raise ValidationError("Unsupported apply journal schema or state")
    for key in ("workflow_id", "task_id", "execution_id", "approval_id"):
        if not isinstance(payload[key], str) or not payload[key] or len(payload[key]) > 128:
            raise ValidationError(f"Apply journal {key} is invalid")
    if not isinstance(payload["candidate_sha256"], str) or not _SHA256.fullmatch(
        payload["candidate_sha256"]
    ):
        raise ValidationError("Apply journal candidate hash is invalid")
    if not isinstance(payload["operation_hash"], str) or not _SHA256.fullmatch(
        payload["operation_hash"]
    ):
        raise ValidationError("Apply journal approval hash is invalid")
    if not isinstance(payload["files"], list) or not 1 <= len(payload["files"]) <= 256:
        raise ValidationError("Apply journal file list is invalid")
    for key in ("writer_tasks", "required_gate_tasks", "required_decision_tasks"):
        values = payload[key]
        if (
            not isinstance(values, list)
            or len(values) > 256
            or any(not isinstance(item, str) or not item or len(item) > 128 for item in values)
            or len(values) != len(set(values))
        ):
            raise ValidationError(f"Apply journal {key} list is malformed")
    if not payload["writer_tasks"] or not payload["required_gate_tasks"]:
        raise ValidationError("Apply journal is missing required writer/gate identities")
    seen: set[str] = set()
    for entry in payload["files"]:
        if not isinstance(entry, dict) or set(entry) != {
            "path",
            "before_sha256",
            "after_sha256",
            "artifact_id",
            "backup_path",
            "backup_sha256",
            "capture_path",
            "was_created",
        }:
            raise ValidationError("Apply journal file entry is malformed")
        game_rel = _safe_relative(entry["path"])
        if game_rel.casefold() in seen:
            raise ValidationError("Apply journal has duplicate or case-aliased paths")
        seen.add(game_rel.casefold())
        if (
            not isinstance(entry["after_sha256"], str)
            or not _SHA256.fullmatch(entry["after_sha256"])
            or (
                entry["before_sha256"] is not None
                and (
                    not isinstance(entry["before_sha256"], str)
                    or not _SHA256.fullmatch(entry["before_sha256"])
                )
            )
        ):
            raise ValidationError("Apply journal file hashes are malformed")
        if not isinstance(entry["was_created"], bool) or entry["was_created"] != (
            entry["before_sha256"] is None
        ):
            raise ValidationError("Apply journal baseline declaration is inconsistent")
        if entry["was_created"]:
            if (
                entry["backup_path"] is not None
                or entry["backup_sha256"] is not None
                or entry["capture_path"] is not None
            ):
                raise ValidationError("Created-file journal entry cannot declare a backup")
        else:
            expected_backup = f".gamefactory/operations/factory-apply/{path.stem}.backups/{len(seen) - 1:04d}.backup"
            expected_capture = expected_backup[:-7] + ".capture"
            if (
                entry["backup_path"] != expected_backup
                or entry["capture_path"] != expected_capture
                or not isinstance(entry["backup_sha256"], str)
                or not _SHA256.fullmatch(entry["backup_sha256"])
            ):
                raise ValidationError(
                    "Apply journal backup is outside its exclusively owned sidecar"
                )
        if not isinstance(entry["artifact_id"], str) or not entry["artifact_id"]:
            raise ValidationError("Apply journal output artifact identity is missing")
    if not isinstance(payload["gate_reports"], dict) or set(payload["gate_reports"]) != set(
        payload["required_gate_tasks"]
    ):
        raise ValidationError("Apply journal gate report set is incomplete")
    context = payload["approved_context"]
    if not isinstance(context, dict) or set(context) != {
        "manifest_sha256",
        "writer_tasks",
        "required_gate_tasks",
        "required_decision_tasks",
        "candidate_sha256",
        "candidate_evidence",
    }:
        raise ValidationError("Apply journal original approval context is malformed")
    for gate_id, report in payload["gate_reports"].items():
        if (
            not isinstance(gate_id, str)
            or not isinstance(report, dict)
            or set(report) != {"report_artifact_id", "report_sha256", "candidate_sha256"}
        ):
            raise ValidationError("Apply journal gate report reference is malformed")
        if (
            not isinstance(report["report_artifact_id"], str)
            or not _SHA256.fullmatch(report["report_sha256"])
            or report["candidate_sha256"] != payload["candidate_sha256"]
        ):
            raise ValidationError("Apply journal gate report binding is malformed")
    return path, payload, hashlib.sha256(raw).hexdigest()


def _journal_database_binding(
    root: Path,
    payload: dict[str, Any],
    db: Database,
    *,
    allow_current_execution_id: str | None = None,
) -> tuple[Any, Any, Any]:
    task = TaskRepository(db).get(payload["task_id"])
    execution = ExecutionRepository(db).get(payload["execution_id"])
    workflow = WorkflowRepository(db).get(payload["workflow_id"])
    approval = ApprovalRepository(db).get(payload["approval_id"])
    if task is None or execution is None or workflow is None or approval is None:
        raise ValidationError("Journal DB identity is incomplete")
    project = ProjectRepository(db).get(workflow.project_id)
    if project is None or _validated_project_root(project.root_path) != root.resolve(strict=True):
        raise ValidationError("Journal project identity/root does not match the persisted project")
    if (
        task.workflow_id != workflow.id
        or execution.task_id != task.id
        or approval.workflow_id != workflow.id
        or approval.task_id != task.id
    ):
        raise ValidationError("Journal DB identities do not agree")
    if (
        approval.approval_type != "factory_game_write"
        or approval.status != ApprovalStatus.APPROVED
        or approval.operation_hash != payload["operation_hash"]
    ):
        raise ValidationError("Journal is not bound to its original approved game-write operation")
    if task.task_type != "factory_acceptance" or not task.parameters.get("manifest_sha256"):
        raise ValidationError("Journal task/candidate binding is invalid")
    if execution.status == ExecutionStatus.RUNNING:
        raise ValidationError(
            "A running acceptance cannot be recovered while its execution is active"
        )
    if task.status == TaskStatus.RUNNING:
        latest = ExecutionRepository(db).get_latest_attempt(task.id)
        if (
            allow_current_execution_id is None
            or latest is None
            or latest.id != allow_current_execution_id
            or latest.id == execution.id
        ):
            raise ValidationError(
                "A running acceptance cannot be recovered outside its own committed-journal retry"
            )
    if (
        task.parameters.get("writer_tasks") != payload["writer_tasks"]
        or task.parameters.get("required_gate_tasks") != payload["required_gate_tasks"]
        or task.parameters.get("required_decision_tasks", []) != payload["required_decision_tasks"]
    ):
        raise ValidationError(
            "Journal writer/gate/decision set differs from the persisted acceptance task"
        )
    context = payload["approved_context"]
    if (
        context.get("manifest_sha256") != task.parameters.get("manifest_sha256")
        or context.get("writer_tasks") != payload["writer_tasks"]
        or context.get("required_gate_tasks") != payload["required_gate_tasks"]
        or context.get("required_decision_tasks") != payload["required_decision_tasks"]
        or context.get("candidate_sha256") != payload["candidate_sha256"]
    ):
        raise ValidationError(
            "Journal approval context differs from persisted task/candidate identity"
        )
    artifacts = ArtifactRepository(db)
    approval_artifacts = []
    for artifact_id in approval.artifact_ids:
        artifact = artifacts.get(artifact_id)
        if artifact is None or artifact.workflow_id != workflow.id:
            raise ValidationError("Original approval artifact set is incomplete")
        approval_artifacts.append(artifact)
    operation_inputs = build_operation_inputs(
        workflow, task, approval_artifacts, task.cost_class, None, context
    )
    if (
        compute_operation_hash(task.id, approval.approval_type, operation_inputs)
        != approval.operation_hash
    ):
        raise ValidationError(
            "Journal original acceptance context does not reproduce the approved operation hash"
        )
    candidate_evidence = context.get("candidate_evidence")
    if not isinstance(candidate_evidence, list) or len(candidate_evidence) > 1024:
        raise ValidationError("Approved candidate evidence list is malformed")
    approved: dict[str, Any] = {}
    for reference in candidate_evidence:
        if (
            not isinstance(reference, list)
            or len(reference) != 2
            or not isinstance(reference[0], str)
            or not isinstance(reference[1], str)
            or not _SHA256.fullmatch(reference[1])
            or reference[0] in approved
        ):
            raise ValidationError("Approved candidate evidence reference is malformed")
        artifact = artifacts.get(reference[0])
        if (
            artifact is None
            or artifact.id not in approval.artifact_ids
            or artifact.workflow_id != workflow.id
            or artifact.content_hash != reference[1]
        ):
            raise ValidationError(
                "Approved candidate evidence no longer matches its immutable artifact"
            )
        approved[artifact.id] = artifact
    proposal_artifacts = [
        item
        for item in approved.values()
        if item.artifact_type == "factory_proposal" and item.task_id in payload["writer_tasks"]
    ]
    gate_artifacts = [
        item for item in approved.values() if item.artifact_type == "factory_quality_report"
    ]
    decision_artifacts = [
        item for item in approved.values() if item.artifact_type == "factory_visual_decision"
    ]
    if {item.task_id for item in proposal_artifacts} != set(payload["writer_tasks"]) or len(
        proposal_artifacts
    ) != len(payload["writer_tasks"]):
        raise ValidationError(
            "Approval does not bind exactly one accepted proposal per game writer"
        )
    if {item.task_id for item in gate_artifacts} != set(payload["required_gate_tasks"]) or len(
        gate_artifacts
    ) != len(payload["required_gate_tasks"]):
        raise ValidationError("Approval does not bind the exact declared quality gate reports")
    if {item.task_id for item in decision_artifacts} != set(
        payload["required_decision_tasks"]
    ) or len(decision_artifacts) != len(payload["required_decision_tasks"]):
        raise ValidationError("Approval does not bind the exact required human visual decisions")
    expected_files: dict[str, dict[str, Any]] = {}
    for proposal_artifact in proposal_artifacts:
        _reject_reparse_components(root, proposal_artifact.relative_path)
        proposal_path = root / Path(*proposal_artifact.relative_path.split("/"))
        proposal_raw = _bounded_file_bytes(proposal_path, 4 * 1024 * 1024)
        if hashlib.sha256(proposal_raw).hexdigest() != proposal_artifact.content_hash:
            raise ValidationError("Approved proposal artifact is missing or changed")
        proposal = json.loads(proposal_raw.decode("utf-8"))
        if proposal.get("schema_version") != "factory-provider-proposal-1.0.0":
            raise ValidationError("Approved proposal envelope has an unsupported schema")
        latest_provider_execution = ExecutionRepository(db).get_latest_attempt(
            proposal_artifact.task_id
        )
        proposal_execution_id = proposal.get("execution_id")
        if (
            latest_provider_execution is None
            or latest_provider_execution.status != ExecutionStatus.COMPLETED
        ):
            raise ValidationError(
                "Approved proposal does not belong to a completed provider attempt"
            )
        if proposal_execution_id != latest_provider_execution.id:
            recovery = next(
                (
                    item
                    for item in reversed(
                        EvidenceRepository(db).list_by_task(proposal_artifact.task_id)
                    )
                    if item.execution_id == latest_provider_execution.id
                    and item.evidence_type == "factory_provider_recovery"
                ),
                None,
            )
            recovery_data = recovery.raw_data if recovery is not None else {}
            if (
                not isinstance(recovery_data, dict)
                or proposal_artifact.id not in recovery_data.get("artifact_ids", [])
                or recovery_data.get("artifact_hashes", {}).get(proposal_artifact.id)
                != proposal_artifact.content_hash
                or recovery_data.get("request_fingerprint") != proposal.get("request_fingerprint")
            ):
                raise ValidationError(
                    "Approved proposal is not proven by the latest completed recovery attempt"
                )
        for output in proposal.get("files", []):
            if not isinstance(output, dict) or set(output) != {
                "path",
                "sha256",
                "media_type",
                "expected_before_sha256",
                "artifact_id",
            }:
                raise ValidationError("Approved proposal output record is malformed")
            normalized = _safe_relative(output["path"])
            if normalized in expected_files:
                raise ValidationError("Approved proposals contain duplicate output paths")
            output_artifact = artifacts.get(output["artifact_id"])
            if (
                output_artifact is None
                or output_artifact.task_id != proposal_artifact.task_id
                or output_artifact.workflow_id != workflow.id
                or output_artifact.content_hash != output["sha256"]
                or not output_artifact.artifact_type.startswith("factory_output:")
            ):
                raise ValidationError(
                    "Approved proposal output is not bound to its immutable staged artifact"
                )
            expected_files[normalized] = {
                "path": normalized,
                "after_sha256": output["sha256"],
                "before_sha256": output["expected_before_sha256"],
                "artifact_id": output["artifact_id"],
            }
    journal_files = {
        item["path"]: {
            "path": item["path"],
            "after_sha256": item["after_sha256"],
            "before_sha256": item["before_sha256"],
            "artifact_id": item["artifact_id"],
        }
        for item in payload["files"]
    }
    if expected_files != journal_files:
        raise ValidationError(
            "Journal game paths, baselines, hashes, or output artifacts differ from approved proposals"
        )
    for report_artifact in gate_artifacts:
        _reject_reparse_components(root, report_artifact.relative_path)
        report_path = root / Path(*report_artifact.relative_path.split("/"))
        report_raw = _bounded_file_bytes(report_path, 4 * 1024 * 1024)
        if hashlib.sha256(report_raw).hexdigest() != report_artifact.content_hash:
            raise ValidationError("Approved gate report is missing or changed")
        report_payload = json.loads(report_raw.decode("utf-8"))
        gate_id = report_artifact.task_id
        journal_ref = payload["gate_reports"].get(gate_id)
        report_execution_id = report_payload.get("execution_id")
        report_execution = (
            ExecutionRepository(db).get(report_execution_id)
            if isinstance(report_execution_id, str)
            else None
        )
        latest_gate_execution = ExecutionRepository(db).get_latest_attempt(gate_id)
        if (
            journal_ref is None
            or journal_ref["report_artifact_id"] != report_artifact.id
            or journal_ref["report_sha256"] != report_artifact.content_hash
            or report_execution is None
            or report_execution.status != ExecutionStatus.COMPLETED
            or latest_gate_execution is None
            or latest_gate_execution.id != report_execution_id
            or report_payload.get("gate_task_id") != gate_id
            or report_payload.get("manifest_sha256") != task.parameters["manifest_sha256"]
            or report_payload.get("candidate_sha256") != payload["candidate_sha256"]
        ):
            raise ValidationError(
                "Approved gate report does not match its journal candidate/manifest"
            )
        if (
            report_payload.get("gate") != "visual"
            and report_payload.get("report", {}).get("status") != "PASS"
        ):
            raise ValidationError(
                "A declared deterministic gate was not PASS in the approved report"
            )
        if report_payload.get("gate") != "visual" and not any(
            gate.status == GateStatus.PASSED
            and gate.gate_type == f"factory:{report_payload.get('gate')}"
            for gate in QualityGateRepository(db).list_by_task(gate_id)
        ):
            raise ValidationError(
                "Declared deterministic gate has no persisted passing quality decision"
            )
    for decision_artifact in decision_artifacts:
        _reject_reparse_components(root, decision_artifact.relative_path)
        decision_path = root / Path(*decision_artifact.relative_path.split("/"))
        decision_raw = _bounded_file_bytes(decision_path, 1 * 1024 * 1024)
        if hashlib.sha256(decision_raw).hexdigest() != decision_artifact.content_hash:
            raise ValidationError("Approved human visual decision artifact is missing or changed")
        decision = json.loads(decision_raw.decode("utf-8"))
        decision_task = TaskRepository(db).get(decision_artifact.task_id)
        gate_task_id = (
            decision_task.parameters.get("gate_task_id") if decision_task is not None else None
        )
        linked = payload["gate_reports"].get(gate_task_id)
        decision_execution_id = decision.get("execution_id")
        latest_decision_execution = ExecutionRepository(db).get_latest_attempt(
            decision_artifact.task_id
        )
        if (
            decision.get("candidate_sha256") != payload["candidate_sha256"]
            or linked is None
            or decision.get("report_sha256") != linked["report_sha256"]
        ):
            raise ValidationError(
                "Human visual decision is not bound to the journal's exact gate report"
            )
        if (
            latest_decision_execution is None
            or latest_decision_execution.id != decision_execution_id
            or latest_decision_execution.status != ExecutionStatus.COMPLETED
        ):
            raise ValidationError("Human visual decision is not from the latest completed attempt")
        if not any(
            item.task_id == decision_artifact.task_id
            and item.approval_type == "factory_visual_human_decision"
            and item.status == ApprovalStatus.APPROVED
            for item in ApprovalRepository(db).list_by_workflow(workflow.id)
        ):
            raise ValidationError("Visual advisory has no persisted human approval")
    for entry in payload["files"]:
        artifact = artifacts.get(entry["artifact_id"])
        if (
            artifact is None
            or artifact.workflow_id != workflow.id
            or artifact.task_id not in payload["writer_tasks"]
            or not artifact.artifact_type.startswith("factory_output:")
            or artifact.content_hash != entry["after_sha256"]
        ):
            raise ValidationError(
                "Journal file is not bound to its accepted immutable output artifact"
            )
    for gate_id, report in payload["gate_reports"].items():
        artifact = artifacts.get(report["report_artifact_id"])
        if (
            artifact is None
            or artifact.task_id != gate_id
            or artifact.artifact_type != "factory_quality_report"
            or artifact.content_hash != report["report_sha256"]
        ):
            raise ValidationError("Journal quality gate report artifact binding is invalid")
    expected_evidence_ids = {
        item.id for item in proposal_artifacts + gate_artifacts + decision_artifacts
    }
    if set(approved) != expected_evidence_ids:
        raise ValidationError(
            "Journal approval context includes missing or unexpected candidate artifacts"
        )
    return task, execution, approval


def _journal_file_states(
    root: Path, payload: dict[str, Any]
) -> tuple[list[dict[str, Any]], list[str]]:
    states: list[dict[str, Any]] = []
    conflicts: list[str] = []
    for entry in payload["files"]:
        rel = entry["path"]
        target = root / Path(*rel.split("/"))
        backup = root / Path(*entry["backup_path"].split("/")) if entry["backup_path"] else None
        capture = root / Path(*entry["capture_path"].split("/")) if entry["capture_path"] else None
        try:
            current = (
                _hash_regular_bounded(root, rel, target, 512 * 1024 * 1024)
                if target.exists()
                else None
            )
            backup_hash = (
                _hash_regular_bounded(root, entry["backup_path"], backup, 512 * 1024 * 1024)
                if backup and backup.exists()
                else None
            )
            capture_hash = (
                _hash_regular_bounded(root, entry["capture_path"], capture, 512 * 1024 * 1024)
                if capture and capture.exists()
                else None
            )
        except ValidationError:
            current = "UNSAFE_PATH_OR_FILE"
            backup_hash = "UNSAFE_PATH_OR_FILE"
            capture_hash = "UNSAFE_PATH_OR_FILE"
            conflicts.append(rel)
        if backup is not None and backup_hash not in {None, entry["backup_sha256"]}:
            conflicts.append(rel)
        empty_hash = hashlib.sha256(b"").hexdigest()
        if (
            backup is not None
            and backup_hash is None
            and current != entry["before_sha256"]
            and capture_hash != entry["before_sha256"]
            and payload["status"] in {"PREPARED", "ROLLBACK_REQUIRED"}
        ):
            conflicts.append(rel)
        if capture is not None and capture_hash is not None:
            if payload["status"] in {"COMMITTED", "FINALIZATION_READY"}:
                conflicts.append(rel)
            elif current is not None and capture_hash not in {entry["before_sha256"], empty_hash}:
                conflicts.append(rel)
            elif capture_hash == empty_hash and current != entry["before_sha256"]:
                conflicts.append(rel)
        acceptable = {entry["after_sha256"]}
        if entry["before_sha256"] is not None:
            acceptable.add(entry["before_sha256"])
        if current is not None and current not in acceptable:
            conflicts.append(rel)
        if (
            payload["status"] in {"COMMITTED", "FINALIZATION_READY"}
            and current != entry["after_sha256"]
        ):
            conflicts.append(rel)
        states.append(
            {
                "path": rel,
                "current_sha256": current,
                "expected_before_sha256": entry["before_sha256"],
                "expected_after_sha256": entry["after_sha256"],
                "backup_sha256": backup_hash,
                "capture_sha256": capture_hash,
            }
        )
    return states, sorted(set(conflicts))


def _make_committed_apply_retry_eligible(
    payload: dict[str, Any], db: Database, actor: str, comment: str
) -> bool:
    """Expose one audited retry only for the exact committed acceptance attempt."""
    tasks = TaskRepository(db)
    executions = ExecutionRepository(db)
    task = tasks.get(payload["task_id"])
    latest = executions.get_latest_attempt(payload["task_id"])
    if task is None or latest is None or latest.id != payload["execution_id"]:
        raise ValidationError("Committed journal is not the latest acceptance attempt")
    attempts = executions.list_by_task(task.id)
    if len(attempts) >= task.max_retries + 1:
        raise ValidationError("Committed journal continuation exceeds the acceptance retry bound")
    if (
        task.status == TaskStatus.FAILED
        and latest.status == ExecutionStatus.FAILED
        and latest.retryable
    ):
        return True
    if task.status == TaskStatus.COMPLETED:
        return False
    event = AuditEvent(
        generate_id("AUDIT"),
        "Task",
        task.id,
        "FACTORY_COMMITTED_APPLY_RETRY_ELIGIBLE",
        actor,
        details={
            "workflow_id": payload["workflow_id"],
            "execution_id": latest.id,
            "journal_candidate_sha256": payload["candidate_sha256"],
            "comment": comment,
        },
    )
    if not executions.make_factory_committed_retry_eligible(task.id, latest.id, event):
        raise ValidationError(
            "Committed journal is not eligible for a safe bounded engine retry from the current attempt"
        )
    return True


def _hash_regular_bounded(root: Path, relative: str, path: Path, maximum: int) -> str:
    _reject_reparse_components(root, relative)
    return hashlib.sha256(_bounded_file_bytes(path, maximum)).hexdigest()


def inspect_factory_apply_journal(
    project_root: Path | str, journal_relative_path: str, *, db: Database
) -> dict[str, Any]:
    """Read-only preview of a DB-bound factory apply recovery operation."""
    root = _validated_project_root(project_root)
    relative = _factory_journal_relative(journal_relative_path)
    journal_path, payload, journal_sha = _read_factory_journal(root, relative)
    task, execution, _ = _journal_database_binding(root, payload, db)
    states, conflicts = _journal_file_states(root, payload)
    if conflicts:
        action = "BLOCKED"
    elif payload["status"] in {"PREPARED", "ROLLBACK_REQUIRED"}:
        action = "ROLLBACK"
    elif payload["status"] == "COMMITTED":
        action = "FINALIZE_COMMITTED"
    else:
        action = "NONE"
    return {
        "journal": relative,
        "journal_sha256": journal_sha,
        "status": payload["status"],
        "recovery_action": action,
        "workflow_id": payload["workflow_id"],
        "task_id": payload["task_id"],
        "execution_id": payload["execution_id"],
        "task_status": task.status.value,
        "execution_status": execution.status.value,
        "files": states,
        "conflicts": conflicts,
    }


def recover_factory_apply_journal(
    project_root: Path | str,
    journal_relative_path: str,
    *,
    db: Database,
    expected_journal_sha256: str,
    actor: str,
    comment: str,
) -> dict[str, Any]:
    """CAS-guard and audit journal rollback/finalization; normal engine retry owns completion."""
    if not isinstance(expected_journal_sha256, str) or not _SHA256.fullmatch(
        expected_journal_sha256
    ):
        raise ValidationError("Expected journal SHA-256 is required")
    if (
        not isinstance(actor, str)
        or not actor.strip()
        or len(actor.strip()) > 128
        or not isinstance(comment, str)
        or not comment.strip()
        or len(comment.strip()) > 2000
    ):
        raise ValidationError("Recovery actor and bounded audit comment are required")
    root = _validated_project_root(project_root)
    relative = _factory_journal_relative(journal_relative_path)
    journal_path = root / Path(*relative.split("/"))
    lock_path = journal_path.with_suffix(".lock")
    _reject_reparse_components(root, lock_path.relative_to(root).as_posix())
    preflight = inspect_factory_apply_journal(root, relative, db=db)
    _reject_reparse_components(root, ".gamefactory/locks")
    with _factory_apply_recovery_locks(root, preflight["workflow_id"], lock_path):
        preview = inspect_factory_apply_journal(root, relative, db=db)
        if preview["journal_sha256"] != expected_journal_sha256:
            raise ValidationError("Apply journal changed after preview; inspect it again")
        if preview["recovery_action"] == "BLOCKED":
            raise ValidationError("Unexpected live or backup bytes prevent safe recovery")
        _, payload, _ = _read_factory_journal(root, relative)
        task, execution, approval = _journal_database_binding(root, payload, db)
        if payload["status"] in {"ROLLED_BACK", "FINALIZATION_READY"}:
            retry_ready = (
                _make_committed_apply_retry_eligible(payload, db, actor.strip(), comment.strip())
                if payload["status"] == "FINALIZATION_READY"
                else False
            )
            return {
                **preview,
                "resume_required": True,
                "retry_ready": retry_ready,
                "approval_id": approval.id,
                "next_step": f"gamefactory retry {payload['workflow_id']} {payload['task_id']}"
                if retry_ready
                else "normal workflow retry after restoring baseline",
            }
        if payload["status"] == "COMMITTED":
            if (
                hashlib.sha256(_bounded_file_bytes(journal_path, 1_000_000)).hexdigest()
                != expected_journal_sha256
            ):
                raise ValidationError(
                    "Apply journal changed during committed recovery; inspect again"
                )
            # Rebuild only the immutable application record from exact journal
            # bindings. The normal engine attempt still owns task completion.
            file_rows = []
            for entry in payload["files"]:
                artifact = ArtifactRepository(db).get(entry["artifact_id"])
                if artifact is None:
                    raise ValidationError("Committed output artifact is unavailable")
                _reject_reparse_components(root, artifact.relative_path)
                artifact_path = root / Path(*artifact.relative_path.split("/"))
                content = _bounded_file_bytes(artifact_path, 64 * 1024 * 1024)
                if (
                    hashlib.sha256(content).hexdigest() != entry["after_sha256"]
                    or _hash_regular_bounded(
                        root,
                        entry["path"],
                        root / Path(*entry["path"].split("/")),
                        512 * 1024 * 1024,
                    )
                    != entry["after_sha256"]
                ):
                    raise ValidationError(
                        "Committed game output no longer matches its approved bytes"
                    )
                file_rows.append({"path": entry["path"], "sha256": entry["after_sha256"]})
            report_rows: dict[str, dict[str, Any]] = {}
            for gate_id, gate in payload["gate_reports"].items():
                report_artifact = ArtifactRepository(db).get(gate["report_artifact_id"])
                if report_artifact is None:
                    raise ValidationError("Committed gate report artifact is unavailable")
                report_path = root / Path(*report_artifact.relative_path.split("/"))
                _reject_reparse_components(root, report_artifact.relative_path)
                report_raw = _bounded_file_bytes(report_path, 4 * 1024 * 1024)
                if hashlib.sha256(report_raw).hexdigest() != gate["report_sha256"]:
                    raise ValidationError("Committed gate report has changed")
                report_payload = json.loads(report_raw.decode("utf-8"))
                report_payload["_report_artifact_id"] = report_artifact.id
                report_payload["_report_sha256"] = report_artifact.content_hash
                report_rows[gate_id] = report_payload
            application = {
                "schema_version": "factory-application-1.0.0",
                "candidate_sha256": payload["candidate_sha256"],
                "journal": relative,
                "files": file_rows,
                "accepted_writer_tasks": payload["writer_tasks"],
                "gate_reports": report_rows,
            }
            encoded = json.dumps(
                application,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
            artifact_relative = f"{_artifact_dir(payload['workflow_id'], payload['task_id'], payload['execution_id'])}/application.json"
            artifact_path = root / Path(*artifact_relative.split("/"))
            artifact_path.parent.mkdir(parents=True, exist_ok=True)
            content_hash = hashlib.sha256(encoded).hexdigest()
            existing = next(
                (
                    item
                    for item in ArtifactRepository(db).list_by_task(payload["task_id"])
                    if item.artifact_type == "factory_application"
                    and item.relative_path == artifact_relative
                ),
                None,
            )
            if existing is not None:
                _reject_reparse_components(root, artifact_relative)
                if (
                    existing.content_hash != content_hash
                    or hashlib.sha256(
                        _bounded_file_bytes(artifact_path, 4 * 1024 * 1024)
                    ).hexdigest()
                    != content_hash
                ):
                    raise ValidationError(
                        "Existing application artifact conflicts with committed journal"
                    )
                application_artifact_id = existing.id
            else:
                if artifact_path.exists():
                    _reject_reparse_components(root, artifact_relative)
                    if (
                        hashlib.sha256(
                            _bounded_file_bytes(artifact_path, 4 * 1024 * 1024)
                        ).hexdigest()
                        != content_hash
                    ):
                        raise ValidationError(
                            "Unexpected bytes occupy the application artifact path"
                        )
                else:
                    fd, temp_name = tempfile.mkstemp(
                        prefix=".application-", dir=artifact_path.parent
                    )
                    try:
                        with os.fdopen(fd, "wb") as stream:
                            stream.write(encoded)
                            stream.flush()
                            os.fsync(stream.fileno())
                        os.link(temp_name, artifact_path)
                    finally:
                        if os.path.exists(temp_name):
                            os.unlink(temp_name)
                application_artifact_id = generate_id("ART")
                ArtifactRepository(db).save(
                    Artifact(
                        application_artifact_id,
                        payload["workflow_id"],
                        payload["task_id"],
                        "factory_application",
                        "FactoryApplyRecovery",
                        artifact_relative,
                        content_hash,
                        len(encoded),
                        "VALIDATED",
                    )
                )
            payload["status"] = "FINALIZATION_READY"
            payload["recovery_actor"] = actor.strip()
            payload["recovery_comment"] = comment.strip()
            payload["finalized_at"] = utc_now_iso()
            payload["finalization_artifact_id"] = application_artifact_id
            if (
                hashlib.sha256(_bounded_file_bytes(journal_path, 1_000_000)).hexdigest()
                != expected_journal_sha256
            ):
                raise ValidationError(
                    "Apply journal changed before finalization marker; inspect again"
                )
            _write_json_atomic(journal_path, payload)
            EvidenceRepository(db).save(
                Evidence(
                    generate_id("EVI"),
                    payload["task_id"],
                    payload["execution_id"],
                    "factory_application_recovery",
                    "Committed files were verified and the application artifact was finalized for normal engine resume.",
                    {
                        "journal": relative,
                        "candidate_sha256": payload["candidate_sha256"],
                        "artifact_id": application_artifact_id,
                        "artifact_sha256": content_hash,
                        "actor": actor.strip(),
                        "comment": comment.strip(),
                    },
                )
            )
            AuditLogRepository(db).append(
                AuditEvent(
                    generate_id("AUDIT"),
                    "Workflow",
                    payload["workflow_id"],
                    "FACTORY_APPLY_RECOVERY_FINALIZE_READY",
                    actor.strip(),
                    details={
                        "task_id": payload["task_id"],
                        "execution_id": payload["execution_id"],
                        "journal": relative,
                        "candidate_sha256": payload["candidate_sha256"],
                        "artifact_id": application_artifact_id,
                        "comment": comment.strip(),
                    },
                )
            )
            retry_ready = _make_committed_apply_retry_eligible(
                payload, db, actor.strip(), comment.strip()
            )
            return {
                **preview,
                "status": "FINALIZATION_READY",
                "recovery_action": "NONE",
                "resume_required": True,
                "retry_ready": retry_ready,
                "next_step": f"gamefactory retry {payload['workflow_id']} {payload['task_id']}",
                "approval_id": approval.id,
                "application_artifact_id": application_artifact_id,
            }
        conflicts: list[str] = []
        if (
            hashlib.sha256(_bounded_file_bytes(journal_path, 1_000_000)).hexdigest()
            != expected_journal_sha256
        ):
            raise ValidationError("Apply journal changed before rollback; inspect again")
        backup_root_rel = f".gamefactory/operations/factory-apply/{Path(relative).stem}.backups"
        backup_root = root / Path(*backup_root_rel.split("/"))
        for index, entry in reversed(list(enumerate(payload["files"]))):
            rel = entry["path"]
            target = root / Path(*rel.split("/"))
            quarantine: Path | None = None
            target.parent.mkdir(parents=True, exist_ok=True)
            backup = root / Path(*entry["backup_path"].split("/")) if entry["backup_path"] else None
            capture = (
                root / Path(*entry["capture_path"].split("/")) if entry["capture_path"] else None
            )
            current = (
                _hash_regular_bounded(root, rel, target, 512 * 1024 * 1024)
                if target.is_file()
                else None
            )
            if entry["was_created"]:
                if current is None:
                    continue
                if current != entry["after_sha256"]:
                    conflicts.append(rel)
                    continue
                quarantine = backup_root / f"recovery-{index:04d}-{generate_id('Q')}.candidate"
                backup_root.mkdir(parents=True, exist_ok=True)
                try:
                    _reserve_recovery_slot(quarantine)
                    os.replace(target, quarantine)
                    captured_hash = _hash_regular_bounded(
                        root, quarantine.relative_to(root).as_posix(), quarantine, 512 * 1024 * 1024
                    )
                    if captured_hash == entry["after_sha256"]:
                        quarantine.unlink()
                    else:
                        if not target.exists():
                            try:
                                os.link(quarantine, target)
                                quarantine.unlink()
                            except FileExistsError:
                                pass
                        conflicts.append(rel)
                except (FileExistsError, OSError):
                    conflicts.append(rel)
                continue
            if capture is not None and capture.exists():
                capture_hash = _hash_regular_bounded(
                    root, entry["capture_path"], capture, 512 * 1024 * 1024
                )
                empty_hash = hashlib.sha256(b"").hexdigest()
                if capture_hash == empty_hash and current == entry["before_sha256"]:
                    capture.unlink()
                elif capture_hash == entry["before_sha256"]:
                    if current is None:
                        try:
                            os.link(capture, target)
                        except FileExistsError:
                            conflicts.append(rel)
                            continue
                    capture.unlink()
                    current = entry["before_sha256"]
                elif current is None:
                    # An editor's bytes were atomically displaced just before a
                    # crash. Restore them exclusively and preserve the separate
                    # approved baseline backup for later operator resolution.
                    try:
                        os.link(capture, target)
                        restored_hash = _hash_regular_bounded(root, rel, target, 512 * 1024 * 1024)
                        if restored_hash == capture_hash:
                            capture.unlink()
                    except FileExistsError:
                        pass
                    conflicts.append(rel)
                    continue
                else:
                    conflicts.append(rel)
                    continue
            assert backup is not None
            if current == entry["before_sha256"]:
                continue
            if (
                not backup.is_file()
                or _hash_regular_bounded(root, entry["backup_path"], backup, 512 * 1024 * 1024)
                != entry["backup_sha256"]
            ):
                conflicts.append(rel)
                continue
            if current not in {None, entry["after_sha256"]}:
                conflicts.append(rel)
                continue
            if current == entry["after_sha256"]:
                quarantine = backup_root / f"recovery-{index:04d}-{generate_id('Q')}.candidate"
                backup_root.mkdir(parents=True, exist_ok=True)
                try:
                    _reserve_recovery_slot(quarantine)
                    os.replace(target, quarantine)
                    if (
                        _hash_regular_bounded(
                            root,
                            quarantine.relative_to(root).as_posix(),
                            quarantine,
                            512 * 1024 * 1024,
                        )
                        != entry["after_sha256"]
                    ):
                        if not target.exists():
                            try:
                                os.link(quarantine, target)
                                quarantine.unlink()
                            except FileExistsError:
                                pass
                        conflicts.append(rel)
                        continue
                except (FileExistsError, OSError):
                    conflicts.append(rel)
                    continue
            try:
                os.link(backup, target)
            except FileExistsError:
                if (
                    not target.is_file()
                    or _hash_regular_bounded(root, rel, target, 512 * 1024 * 1024)
                    != entry["before_sha256"]
                ):
                    conflicts.append(rel)
                    continue
            if (
                target.is_file()
                and _hash_regular_bounded(root, rel, target, 512 * 1024 * 1024)
                == entry["before_sha256"]
            ):
                if quarantine is not None and quarantine.exists():
                    quarantine.unlink()
            else:
                conflicts.append(rel)
        payload["status"] = "ROLLBACK_REQUIRED" if conflicts else "ROLLED_BACK"
        payload["recovery_conflicts"] = conflicts
        payload["recovered_at"] = utc_now_iso()
        payload["recovery_actor"] = actor.strip()
        payload["recovery_comment"] = comment.strip()
        if (
            hashlib.sha256(_bounded_file_bytes(journal_path, 1_000_000)).hexdigest()
            != expected_journal_sha256
        ):
            raise ValidationError("Apply journal changed before rollback marker; inspect again")
        _write_json_atomic(journal_path, payload)
        AuditLogRepository(db).append(
            AuditEvent(
                generate_id("AUDIT"),
                "Workflow",
                payload["workflow_id"],
                "FACTORY_APPLY_RECOVERY_ROLLED_BACK"
                if not conflicts
                else "FACTORY_APPLY_RECOVERY_BLOCKED",
                actor.strip(),
                details={
                    "task_id": payload["task_id"],
                    "execution_id": payload["execution_id"],
                    "journal": relative,
                    "conflicts": conflicts,
                    "comment": comment.strip(),
                },
            )
        )
        return {
            **preview,
            "status": payload["status"],
            "recovery_action": "BLOCKED" if conflicts else "NONE",
            "conflicts": conflicts,
            "resume_required": not bool(conflicts),
            "approval_id": approval.id,
        }
