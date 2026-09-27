"""Explicit implementations of the small deterministic V0.1 task actions.

The engine owns state, policy, claims, finalization, and verification. This class
only performs the bounded action selected for a claimed task.
"""

import math
import socket
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from gamefactory.adapters.persistence.repositories import ArtifactRepository
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ToolExecutionError, ValidationError
from gamefactory.core.domain.models import Execution, Task, Workflow, utc_now_iso
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner
from gamefactory.workflows.ports import (
    AssetGenerationProvider,
    GenerationRequest,
    GenerationResponse,
)


@dataclass(frozen=True)
class BuiltinEvidenceResult:
    schema_version: int
    summary: str
    artifact_ids: list[str] = field(default_factory=list)
    details: dict[str, object] = field(default_factory=dict)

    def validate(self) -> None:
        if self.schema_version != 1 or not self.summary.strip():
            raise ValueError("Invalid built-in evidence result schema")
        if any(not isinstance(item, str) or not item for item in self.artifact_ids):
            raise ValueError("Evidence artifact IDs must be non-empty strings")


class BuiltinTaskActions:
    """Bounded registry for built-in task action implementations."""

    def __init__(
        self,
        project_root: Path,
        artifacts: ArtifactManager,
        artifact_repo: ArtifactRepository,
        provider: AssetGenerationProvider | None,
        process_runner: ProcessRunner,
        record_invocation: Callable[[str, str, str, str], None],
    ) -> None:
        self.project_root = project_root
        self.artifacts = artifacts
        self.artifact_repo = artifact_repo
        self.provider = provider
        self.process_runner = process_runner
        self.path_guard = PathGuard(project_root)
        self.record_invocation = record_invocation
        self._handlers: dict[str, Callable[..., None]] = {
            "inspect_project": self._inspect_project,
            "generate_concept": self._generate_concept,
            "validate_artifact": self._validate_artifact,
            "paid_generation": self._paid_generation,
            "controlled_command": self._controlled_command,
            "record_evidence": self._record_evidence_request,
            "simulated_failure": self._simulated_failure,
        }

    @property
    def task_types(self) -> frozenset[str]:
        return frozenset(self._handlers)

    def execute(
        self,
        workflow: Workflow,
        task: Task,
        execution: Execution,
        operation_hash: str | None = None,
    ) -> None:
        handler = self._handlers.get(task.task_type)
        if handler is None:
            raise ValidationError(f"Unknown task type '{task.task_type}'")
        if task.task_type == "paid_generation":
            self._paid_generation(workflow, task, execution, operation_hash or "")
        else:
            handler(workflow, task, execution)
        execution.completed_at = utc_now_iso()

    def evidence_for_task(
        self, workflow: Workflow, task: Task, execution: Execution
    ) -> BuiltinEvidenceResult:
        """Return bounded evidence claims for independent engine verification."""
        if task.task_type in ("inspect_project", "generate_concept", "paid_generation"):
            artifacts = self.artifact_repo.list_by_task(task.id)
            if not artifacts:
                raise ArtifactError(f"Task '{task.id}' produced no registered artifact")
            return BuiltinEvidenceResult(
                1, "Task output artifact registered", [a.id for a in artifacts]
            )
        if task.task_type == "validate_artifact":
            artifacts = [
                a
                for a in self.artifact_repo.list_by_workflow(workflow.id)
                if a.artifact_type == "concept_spec"
            ]
            if not artifacts:
                raise ArtifactError("No artifacts were independently available for validation")
            return BuiltinEvidenceResult(
                1,
                "Concept specification artifacts selected for validation",
                [a.id for a in artifacts],
            )
        if task.task_type == "controlled_command":
            if execution.exit_code != 0:
                raise ToolExecutionError("A successful process exit code is required for evidence")
            return BuiltinEvidenceResult(
                1,
                "Controlled process exited successfully",
                details={"exit_code": execution.exit_code, "stdout": execution.stdout or ""},
            )
        if task.task_type == "simulated_failure":
            return BuiltinEvidenceResult(
                1,
                "Deterministic retry fixture completed",
                details={"result": execution.stdout or ""},
            )
        raise ValidationError(f"No evidence validator registered for task type '{task.task_type}'")

    def _inspect_project(self, workflow: Workflow, task: Task, execution: Execution) -> None:
        rel = f".gamefactory/artifacts/{workflow.id}/{task.id}/attempt-{execution.attempt_number}/inspection.md"
        artifact = self.artifacts.create_text_artifact(
            workflow.id,
            task.id,
            "inspection_report",
            "DemoProjectInspector",
            rel,
            f"# Inspection Report\nProject Root: {self.project_root}\nTime: {utc_now_iso()}",
        )
        self.artifact_repo.save(artifact)
        execution.stdout = "Inspected project structure successfully"

    def _generate_concept(self, workflow: Workflow, task: Task, execution: Execution) -> None:
        name = task.parameters.get("concept_name", "Mech Unit")
        rel = f".gamefactory/artifacts/{workflow.id}/{task.id}/attempt-{execution.attempt_number}/concept.md"
        artifact = self.artifacts.create_text_artifact(
            workflow.id,
            task.id,
            "concept_spec",
            "DemoConceptSpecGenerator",
            rel,
            f"# Concept Specification\nName: {name}\nPoly Budget: {task.parameters.get('poly_budget', 10000)}",
        )
        self.artifact_repo.save(artifact)
        execution.stdout = f"Generated concept specification for {name}"

    def _validate_artifact(self, workflow: Workflow, task: Task, execution: Execution) -> None:
        artifacts = [
            a
            for a in self.artifact_repo.list_by_workflow(workflow.id)
            if a.artifact_type == "concept_spec"
        ]
        if not artifacts:
            raise ArtifactError("No concept specification artifact found to validate")
        execution.stdout = (
            f"Found {len(artifacts)} concept specification artifact(s) for engine validation"
        )

    def _paid_generation(
        self, workflow: Workflow, task: Task, execution: Execution, op_hash: str
    ) -> None:
        if self.provider is None:
            raise ValidationError("No paid asset provider is configured")
        prompt = str(task.parameters.get("prompt", "Asset Prompt"))
        output_rel = (
            f".gamefactory/artifacts/{workflow.id}/{task.id}/attempt-{execution.attempt_number}.glb"
        )
        output_abs = self.path_guard.ensure_safe_parent(output_rel)
        request = GenerationRequest(
            prompt=prompt,
            target_format="glb",
            parameters={**task.parameters, "output_path": str(output_abs)},
            operation_hash=op_hash,
        )
        # Durable intent immediately before any provider implementation is called.
        self.record_invocation(workflow.id, task.id, self.provider.name, op_hash)
        response = self.provider.generate(request)
        self._validate_provider_response(response)
        execution.external_op_id = response.external_op_id
        execution.cost = response.cost
        execution.cost_unit = response.cost_unit
        execution.provider = self.provider.name
        assert response.output_path is not None
        actual_path = Path(response.output_path).resolve()
        try:
            relative = actual_path.relative_to(self.project_root).as_posix()
        except ValueError as exc:
            raise ArtifactError("Provider output is outside project root") from exc
        if actual_path != output_abs.resolve():
            raise ArtifactError("Provider output path does not match the reserved attempt path")
        if not actual_path.is_file() or actual_path.stat().st_size == 0:
            raise ArtifactError("Provider output is missing or empty")
        artifact = self.artifacts.register_file_artifact(
            workflow.id,
            task.id,
            "model_3d",
            self.provider.name,
            relative,
        )
        self.artifact_repo.save(artifact)
        execution.stdout = f"Generated asset with operation ID {response.external_op_id}"

    @staticmethod
    def _validate_provider_response(response: GenerationResponse) -> None:
        if not isinstance(response, GenerationResponse):
            raise ValidationError("Provider returned a result with an unsupported schema")
        if (
            not isinstance(response.external_op_id, str)
            or not response.external_op_id.strip()
            or response.status != "SUCCESS"
            or not isinstance(response.output_path, str)
            or not response.output_path.strip()
            or not isinstance(response.cost, (int, float))
            or not math.isfinite(response.cost)
            or response.cost < 0
            or not isinstance(response.cost_unit, str)
            or not response.cost_unit.strip()
            or not isinstance(response.details, dict)
        ):
            raise ValidationError(
                "Provider response is malformed or not a confirmed successful result"
            )

    def _controlled_command(self, workflow: Workflow, task: Task, execution: Execution) -> None:
        request = CommandRequest(
            args=[sys.executable, "-c", "print('Controlled execution completed successfully')"],
            cwd=self.project_root,
            timeout_seconds=task.timeout_seconds,
        )
        result = self.process_runner.run(request)
        execution.exit_code = result.exit_code
        execution.stdout = result.stdout
        execution.stderr = result.stderr
        execution.pid = getattr(result, "pid", None)
        execution.host = socket.gethostname()
        if result.exit_code != 0:
            raise ToolExecutionError(
                f"Command failed with exit code {result.exit_code}: {result.stderr}"
            )

    @staticmethod
    def _record_evidence_request(workflow: Workflow, task: Task, execution: Execution) -> None:
        execution.stdout = "Requested independent verification of registered workflow artifacts"

    @staticmethod
    def _simulated_failure(workflow: Workflow, task: Task, execution: Execution) -> None:
        fail_attempts = int(task.parameters.get("fail_attempts", 1))
        if execution.attempt_number <= fail_attempts:
            raise ToolExecutionError(
                f"Attempt {execution.attempt_number} failed: {task.parameters.get('error', 'Simulated failure')}"
            )
        execution.stdout = f"Attempt {execution.attempt_number} succeeded after retry"
