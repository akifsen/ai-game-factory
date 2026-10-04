"""Reusable workflow builder and handlers for explicit native Godot project work."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path
from typing import Any, cast

from gamefactory.adapters.engines.godot_operations import execute_godot_operation
from gamefactory.adapters.engines.godot_staging import GodotStager, _is_reparse, sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    QualityGateRepository,
    TaskRepository,
)
from gamefactory.adapters.projects.discovery import discover_project
from gamefactory.adapters.projects.onboarding import create_godot_project
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    CostClass,
    Execution,
    Task,
    Workflow,
    WorkflowStatus,
    generate_id,
)
from gamefactory.core.domain.project_operations import validate_project_operation_request
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)

_OPERATIONS = {
    "discover": "project_discover",
    "create": "project_create",
    "editor": "godot_editor",
    "run": "godot_run",
    "export": "godot_export",
    "release": "release_manifest",
}


def _safe_root(value: Path | str) -> Path:
    original = Path(value)
    current = original.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Project path cannot contain a symbolic link or junction")
        current = current.parent
    root = original.resolve(strict=True)
    if not root.is_dir():
        raise ValidationError("project_root must be an existing directory")
    return root


def _operator_executable(value: Path | str) -> Path:
    original = Path(value)
    current = original.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Operator executable path cannot contain a link or junction")
        current = current.parent
    exe = original.resolve(strict=True)
    if not exe.is_file():
        raise ValidationError("Operator executable must be a regular file")
    return exe


def _read_receipt(path: Path) -> dict[str, Any]:
    if _is_reparse(path):
        raise ValidationError("Build receipt cannot be a link or junction")
    descriptor = os.open(
        path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError("Build receipt must be a regular file")
        chunks = bytearray()
        while len(chunks) <= 1024 * 1024:
            block = os.read(descriptor, min(65536, 1024 * 1024 + 1 - len(chunks)))
            if not block:
                break
            chunks.extend(block)
        if len(chunks) > 1024 * 1024:
            raise ValidationError("Build receipt exceeds the parse limit")
        raw = bytes(chunks)
        payload = json.loads(raw.decode("utf-8"))
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise ValidationError("Build receipt must be a JSON object")
    return payload


def _manifest(root: Path) -> dict[str, Any]:
    files, fingerprint = GodotStager(root, root / ".gamefactory" / "scratch").source_manifest()
    return {
        "source_manifest_sha256": fingerprint,
        "files": [{"path": f.relative_path, "sha256": f.sha256, "size": f.size} for f in files],
    }


def _release_payload(
    root: Path,
    attempt_id: str,
    executions: ExecutionRepository | None = None,
    gates: QualityGateRepository | None = None,
    artifacts: ArtifactRepository | None = None,
    artifact_manager: ArtifactManager | None = None,
) -> dict[str, Any]:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", attempt_id):
        raise ValidationError("build_attempt_id is invalid")
    attempt = root / ".gamefactory" / "attempts" / attempt_id
    if (
        _is_reparse(root / ".gamefactory")
        or _is_reparse(root / ".gamefactory" / "attempts")
        or _is_reparse(attempt)
    ):
        raise ValidationError("Build attempt cannot be a link or junction")
    receipt_path = attempt / "operation-result.json"
    if not receipt_path.is_file() or _is_reparse(receipt_path):
        raise ValidationError("Selected build attempt has no immutable result receipt")
    if receipt_path.stat().st_size > 1024 * 1024:
        raise ValidationError("Build receipt exceeds the parse limit")
    try:
        receipt = _read_receipt(receipt_path)
    except (OSError, ValueError) as exc:
        raise ValidationError("Selected build receipt is invalid") from exc
    process = receipt.get("process")
    output_manifest = receipt.get("output")
    if (
        receipt.get("operation") != "export"
        or receipt.get("attempt_id") != attempt_id
        or receipt.get("execution_id") != attempt_id
        or receipt.get("status") != "succeeded"
        or receipt.get("exit_code") != 0
        or receipt.get("timed_out")
        or not isinstance(process, dict)
        or not process.get("cleanup_completed")
        or not isinstance(output_manifest, dict)
        or not output_manifest.get("files")
    ):
        raise ValidationError("Selected attempt is not a successful export with output artifacts")
    execution_id = receipt.get("execution_id")
    if executions is not None and gates is not None:
        execution = executions.get(execution_id) if isinstance(execution_id, str) else None
        if (
            execution is None
            or execution.id != attempt_id
            or execution.status.value != "COMPLETED"
            or execution.task_id != receipt.get("task_id")
        ):
            raise ValidationError(
                "Selected export execution is not the completed attempt bound by its receipt"
            )
        export_task = TaskRepository(executions.db).get(execution.task_id)
        if (
            export_task is None
            or export_task.workflow_id != receipt.get("workflow_id")
            or export_task.task_type != "godot_export"
        ):
            raise ValidationError("Selected receipt is not bound to a registered Godot export task")
        if artifacts is None or artifact_manager is None:
            raise ValidationError("Cannot verify the durable export result artifact")
        evidence_repo = EvidenceRepository(executions.db)
        attempt_proofs = [
            proof
            for proof in evidence_repo.list_by_task(export_task.id)
            if proof.execution_id == execution.id and proof.evidence_type == "handler:godot_export"
        ]
        verified_result_ids: dict[str, tuple[str, int]] = {}
        for proof in attempt_proofs:
            gate_id = proof.raw_data.get("gate_id")
            handler_gate = next(
                (gate for gate in gates.list_by_task(export_task.id) if gate.id == gate_id), None
            )
            if (
                handler_gate is None
                or handler_gate.gate_type != "handler:godot_export"
                or handler_gate.status.value != "PASSED"
            ):
                continue
            verified_items = proof.raw_data.get("artifacts")
            if not isinstance(verified_items, list):
                continue
            for verified in verified_items:
                if (
                    isinstance(verified, dict)
                    and verified.get("status") == "VERIFIED"
                    and isinstance(verified.get("artifact_id"), str)
                    and isinstance(verified.get("content_hash"), str)
                    and isinstance(verified.get("file_size"), int)
                    and not isinstance(verified.get("file_size"), bool)
                ):
                    previous = verified_result_ids.get(verified["artifact_id"])
                    identity = (verified["content_hash"], verified["file_size"])
                    if previous is None or previous == identity:
                        verified_result_ids[verified["artifact_id"]] = identity
        durable_matches = []
        for artifact_id, proof_identity in verified_result_ids.items():
            artifact = artifacts.get(artifact_id)
            if (
                artifact is None
                or artifact.artifact_type != "godot_export-result"
                or artifact.workflow_id != export_task.workflow_id
                or artifact.task_id != export_task.id
            ):
                continue
            integrity = artifact_manager.verify_artifact_integrity(artifact)
            if (
                integrity.get("content_hash") != artifact.content_hash
                or proof_identity != (artifact.content_hash, artifact.file_size)
                or integrity.get("file_size") != artifact.file_size
            ):
                continue
            artifact_path = artifact_manager.path_guard.resolve_safe_path(artifact.relative_path)
            try:
                saved = _read_receipt(artifact_path)
            except (OSError, ValueError, ValidationError):
                continue
            if saved == receipt:
                durable_matches.append(artifact)
        if len(durable_matches) != 1:
            raise ValidationError(
                "Selected export attempt lacks unique execution-bound handler evidence for its exact durable receipt"
            )
    entries = []
    output_files = output_manifest["files"]
    if not isinstance(output_files, list) or len(output_files) > 10000:
        raise ValidationError("Build output manifest exceeds its entry limit")
    total_size = 0
    for item in output_files:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("path"), str)
            or len(item["path"]) > 1024
        ):
            raise ValidationError("Build output manifest entry is invalid")
        relative = Path(item["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValidationError("Build output path is not attempt-relative")
        original = attempt / relative
        current = original
        while current != current.parent:
            if _is_reparse(current):
                raise ValidationError("Export result path contains a link or junction")
            current = current.parent
        path = original.resolve(strict=True)
        try:
            path.relative_to(attempt.resolve(strict=True))
        except ValueError as exc:
            raise ValidationError("Export receipt path escapes its attempt") from exc
        if _is_reparse(path) or not path.is_file():
            raise ValidationError("Export artifact is not a regular file")
        actual_size = path.stat().st_size
        actual_hash = sha256_file(path)
        if actual_size <= 0:
            raise ValidationError("Export artifacts must be non-empty")
        total_size += actual_size
        if total_size > 4 * 1024 * 1024 * 1024:
            raise ValidationError("Build output manifest exceeds the byte limit")
        expected_hash = item.get("sha256")
        expected_size = item.get("size")
        if (
            not isinstance(expected_hash, str)
            or not isinstance(expected_size, int)
            or isinstance(expected_size, bool)
            or actual_hash != expected_hash
            or actual_size != expected_size
        ):
            raise ValidationError("Export artifact changed since build completion")
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "size": actual_size,
                "sha256": actual_hash,
            }
        )
    payload = {
        "schema_version": 1,
        "project_root": str(root),
        "build_attempt_id": attempt_id,
        "execution_id": execution_id,
        "source_manifest_sha256": receipt.get("source_manifest_sha256"),
        "executable_sha256": receipt.get("executable_sha256"),
        "preset": receipt.get("preset"),
        "exports": entries,
    }
    payload["manifest_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def build_project_operation_workflow(
    project_id: str,
    project_root: Path | str,
    operation: str,
    parameters: dict[str, Any] | None = None,
) -> tuple[Workflow, list[Task]]:
    """Build a deterministic one-operation DAG ending in the existing evidence gate."""
    if operation not in _OPERATIONS:
        raise ValidationError(f"Unsupported project operation: {operation}")
    validated = validate_project_operation_request(
        operation, {} if parameters is None else parameters
    )
    # The runtime validator accepts only JSON-compatible values from the
    # operation-specific contract; this mutable map is then augmented with
    # workflow-owned fingerprints and normalized paths.
    params = cast(dict[str, Any], validated)
    root = _safe_root(project_root)
    allowed = {
        "discover": {"include_git"},
        "create": {"destination", "name", "dimension"},
        "editor": {"executable", "timeout_seconds"},
        "run": {"executable", "scene", "timeout_seconds"},
        "export": {"executable", "preset", "output_name", "timeout_seconds"},
        "release": {"build_attempt_id"},
    }[operation]
    if set(params) - allowed:
        raise ValidationError("Unknown project operation parameter")
    if operation in {"editor", "run", "export"}:
        if not params.get("executable"):
            raise ValidationError("operator-selected executable is required")
        exe = _operator_executable(params["executable"])
        params.update(
            {
                "executable": str(exe),
                "executable_sha256": sha256_file(exe),
                "source_manifest": _manifest(root),
            }
        )
    if operation == "export":
        if not params.get("preset"):
            raise ValidationError("named export preset is required")
        params["preset"] = str(params["preset"])
    if operation == "run":
        params["scene"] = params.get("scene")
    timeout = params.get("timeout_seconds", 300)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or not 0 < timeout <= 900
    ):
        raise ValidationError("timeout_seconds must be finite and between 0 and 900")
    params["timeout_seconds"] = float(timeout)
    params["project_root"] = str(root)
    if operation == "release":
        if not params.get("build_attempt_id"):
            raise ValidationError("release requires a successful build_attempt_id")
        params["approved_manifest"] = _release_payload(root, str(params["build_attempt_id"]))
    if operation == "create":
        if not params.get("destination") or not params.get("name"):
            raise ValidationError("destination and name are required")
        destination = Path(params["destination"])
        if not destination.is_absolute():
            destination = root / destination
        try:
            destination.resolve(strict=False).relative_to(root)
        except ValueError as exc:
            raise ValidationError(
                "New project destination must remain inside the managed project root"
            ) from exc
        params["destination"] = str(destination)
        params.setdefault("dimension", "2d")
    if operation == "discover":
        params.setdefault("include_git", True)
    workflow_id = generate_id("WF-PROJECT")
    op_id, evidence_id = f"{workflow_id}-OP", f"{workflow_id}-EVIDENCE"
    workflow = Workflow(
        id=workflow_id,
        project_id=project_id,
        name=f"Project {operation}",
        status=WorkflowStatus.PENDING,
    )
    task = Task(
        id=op_id,
        workflow_id=workflow_id,
        name=f"Execute project {operation}",
        task_type=_OPERATIONS[operation],
        cost_class=CostClass.LOCAL,
        parameters=params,
        timeout_seconds=float(params["timeout_seconds"]),
    )
    evidence = Task(
        id=evidence_id,
        workflow_id=workflow_id,
        name="Record final quality evidence",
        task_type="record_evidence",
        depends_on=[op_id],
        cost_class=CostClass.LOCAL,
    )
    return workflow, [task, evidence]


class ProjectOperationHandlers:
    def __init__(
        self,
        root: Path | str,
        artifacts: ArtifactRepository,
        evidence: EvidenceRepository,
        gates: QualityGateRepository,
        executions: ExecutionRepository,
        artifact_manager: ArtifactManager,
        runner: ProcessRunner | Any | None = None,
    ) -> None:
        self.root = _safe_root(root)
        self.artifacts, self.evidence, self.gates, self.executions = (
            artifacts,
            evidence,
            gates,
            executions,
        )
        self.artifact_manager = artifact_manager
        self.runner = runner or ProcessRunner(sanitize_output=True)

    def refresh_parameters(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        p = dict(task.parameters)
        root = _safe_root(p["project_root"])
        if task.task_type == "release_manifest":
            current = _release_payload(
                root,
                p["build_attempt_id"],
                self.executions,
                self.gates,
                self.artifacts,
                self.artifact_manager,
            )
            if current != p["approved_manifest"]:
                raise ValidationError(
                    "Export manifest changed; create a new release workflow and approval"
                )
        elif task.task_type in {"godot_editor", "godot_run", "godot_export"}:
            if _manifest(root) != p["source_manifest"]:
                raise ValidationError(
                    "Project sources changed; create a new operation workflow and approval"
                )
            exe = _operator_executable(p["executable"])
            if sha256_file(exe) != p["executable_sha256"]:
                raise ValidationError("Godot executable changed after workflow definition")
        return p

    def approval_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        return {
            "project_root": task.parameters["project_root"],
            "manifest_sha256": task.parameters["approved_manifest"]["manifest_sha256"],
            "build_attempt_id": task.parameters["approved_manifest"]["build_attempt_id"],
            "exports": task.parameters["approved_manifest"]["exports"],
        }

    def _save(
        self,
        workflow: Workflow,
        task: Task,
        execution: Execution,
        kind: str,
        name: str,
        data: bytes,
    ) -> str:
        relative = Path(".gamefactory") / "artifacts" / workflow.id / task.id / execution.id / name
        target = self.artifact_manager.path_guard.ensure_safe_parent(relative.as_posix())
        if target.exists():
            raise ArtifactError("Refusing to overwrite project operation artifact")
        target.write_bytes(data)
        artifact = self.artifact_manager.register_file_artifact(
            workflow_id=workflow.id,
            task_id=task.id,
            artifact_type=kind,
            producer="ProjectOperationHandlers",
            relative_path=relative.as_posix(),
        )
        self.artifacts.save(artifact)
        return artifact.id

    def handle(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        p = task.parameters
        root = _safe_root(p["project_root"])
        if task.task_type == "project_discover":
            result = discover_project(
                root, include_git=bool(p.get("include_git", True)), runner=self.runner
            )
        elif task.task_type == "project_create":
            destination = Path(p["destination"]).resolve(strict=False)
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise ValidationError(
                    "New project destination escaped the managed project root"
                ) from exc
            result = create_godot_project(p["destination"], p["name"], p.get("dimension", "2d"))
        elif task.task_type in {"godot_editor", "godot_run", "godot_export"}:
            if _manifest(root) != p["source_manifest"]:
                raise ValidationError("Project sources changed after approval refresh")
            exe = _operator_executable(p["executable"])
            if sha256_file(exe) != p["executable_sha256"]:
                raise ValidationError("Godot executable changed after approval refresh")
            try:
                result = execute_godot_operation(
                    task.task_type.removeprefix("godot_"),
                    exe,
                    root,
                    attempt_id=execution.id,
                    workflow_id=workflow.id,
                    task_id=task.id,
                    expected_source_hash=p["source_manifest"]["source_manifest_sha256"],
                    expected_executable_sha256=p["executable_sha256"],
                    scene=p.get("scene"),
                    preset=p.get("preset"),
                    output_name=p.get("output_name"),
                    runner=self.runner,
                    timeout_seconds=task.timeout_seconds,
                )
            except Exception:
                receipt = (
                    root / ".gamefactory" / "attempts" / execution.id / "operation-result.json"
                )
                if receipt.is_file() and not _is_reparse(receipt):
                    self._save(
                        workflow,
                        task,
                        execution,
                        "godot-operation-failure",
                        "failure.json",
                        receipt.read_bytes(),
                    )
                raise
        elif task.task_type == "release_manifest":
            result = _release_payload(
                root,
                p["build_attempt_id"],
                self.executions,
                self.gates,
                self.artifacts,
                self.artifact_manager,
            )
            if result != p["approved_manifest"]:
                raise ValidationError("Export hashes changed after human release approval")
            result["release_status"] = "APPROVED_FOR_OPERATOR_RELEASE"
            result["published"] = False
        else:
            raise ValidationError("Unsupported registered project handler")
        payload = json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2).encode("utf-8")
        artifact_id = self._save(
            workflow, task, execution, task.task_type + "-result", "result.json", payload
        )
        return TaskHandlerResult(
            schema_version=1,
            summary=f"Project operation {task.task_type} completed",
            artifact_ids=[artifact_id],
        )


def register_project_operation_handlers(
    registry: TaskHandlerRegistry,
    root: Path | str,
    artifacts: ArtifactRepository,
    evidence: EvidenceRepository,
    gates: QualityGateRepository,
    executions: ExecutionRepository,
    artifact_manager: ArtifactManager,
    runner: ProcessRunner | Any | None = None,
) -> ProjectOperationHandlers:
    handlers = ProjectOperationHandlers(
        root, artifacts, evidence, gates, executions, artifact_manager, runner
    )
    for operation in (
        "project_discover",
        "project_create",
        "godot_editor",
        "godot_run",
        "godot_export",
    ):
        process = operation in {"godot_editor", "godot_run", "godot_export"}
        registry.register(
            operation,
            handlers.handle,
            TaskHandlerMetadata(
                operation=HandlerOperation.PROCESS_EXECUTION
                if process
                else HandlerOperation.REPOSITORY_WRITE
                if operation == "project_create"
                else HandlerOperation.LOCAL_READ,
                managed_write=process or operation == "project_create",
                recovery=HandlerRecovery.CONSERVATIVE_PROCESS
                if process
                else HandlerRecovery.SAFE_TO_RETRY,
                refresh_parameters=handlers.refresh_parameters if process else None,
            ),
        )
    registry.register(
        "release_manifest",
        handlers.handle,
        TaskHandlerMetadata(
            operation=HandlerOperation.REPOSITORY_WRITE,
            managed_write=True,
            mandatory_approval_type="HUMAN_RELEASE",
            refresh_parameters=handlers.refresh_parameters,
            approval_context=handlers.approval_context,
        ),
    )
    return handlers


def project_operation_schema() -> dict[str, Any]:
    from gamefactory.core.domain.project_operations import PROJECT_OPERATION_SCHEMA

    return dict(PROJECT_OPERATION_SCHEMA)
