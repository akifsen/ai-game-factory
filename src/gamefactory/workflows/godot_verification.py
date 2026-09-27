"""Godot verification task handlers, immutable approval inputs, and workflow builder."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_contracts import (
    Scenario,
    evaluate_assertions,
    load_scenario,
    scenario_fingerprint,
    validate_observation,
)
from gamefactory.adapters.engines.godot_execution import GodotExecutor
from gamefactory.adapters.engines.godot_staging import GodotStager, sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    QualityGateRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import (
    CostClass,
    Evidence,
    Execution,
    ExecutionStatus,
    GateStatus,
    QualityGate,
    Task,
    Workflow,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)

HARNESS_VERSION = "0.2.0"


def _harness_bytes() -> bytes:
    return files("gamefactory").joinpath("resources/godot/harness.gd").read_bytes()


def _source_context(project_root: Path, scenario_path: Path, executable: Path) -> dict[str, Any]:
    scenario = load_scenario(scenario_path)
    try:
        scene_path = PathGuard(project_root).resolve_safe_path(scenario.scene[6:])
    except Exception as exc:
        raise ValidationError(f"Scenario scene path is invalid: {exc}") from exc
    if not scene_path.is_file():
        raise ValidationError(f"Scenario scene does not exist: {scenario.scene}")
    _, source_hash = GodotStager(
        project_root, project_root / ".gamefactory" / "scratch"
    ).source_manifest(scenario_path)
    exe_path = executable.resolve(strict=True)
    harness = _harness_bytes()
    return {
        "scenario": scenario,
        "scenario_snapshot": scenario.model_dump(mode="json"),
        "scenario_fingerprint": scenario_fingerprint(scenario),
        "source_manifest_hash": source_hash,
        "executable_path": str(exe_path),
        "executable_sha256": sha256_file(exe_path),
        "harness_sha256": hashlib.sha256(harness).hexdigest(),
        "scene_sha256": sha256_file(scene_path),
    }


def create_godot_verification_workflow(
    project_id: str,
    project_root: Path | str,
    executable: Path | str,
    scenario_path: Path | str,
) -> tuple[Any, list[Task]]:
    """Build a small execute → independent-validate → existing final-evidence DAG."""
    from gamefactory.core.domain.models import Workflow, WorkflowStatus, generate_id

    root = Path(project_root).resolve(strict=True)
    scenario_file = Path(scenario_path).resolve(strict=True)
    exe = Path(executable).resolve(strict=True)
    context = _source_context(root, scenario_file, exe)
    workflow_id = generate_id("WF-GODOT")
    execute_id = f"{workflow_id}-EXECUTE"
    validate_id = f"{workflow_id}-VALIDATE"
    evidence_id = f"{workflow_id}-EVIDENCE"
    workflow = Workflow(
        id=workflow_id,
        project_id=project_id,
        name=f"Godot headless verification: {context['scenario'].scenario_id}",
        status=WorkflowStatus.PENDING,
    )
    parameters = {
        "scenario_file": str(scenario_file),
        "scenario_snapshot": context["scenario_snapshot"],
        "scenario_fingerprint": context["scenario_fingerprint"],
        "source_manifest_hash": context["source_manifest_hash"],
        "executable_path": context["executable_path"],
        "executable_sha256": context["executable_sha256"],
        "harness_sha256": context["harness_sha256"],
        "scene_sha256": context["scene_sha256"],
    }
    tasks = [
        Task(
            id=execute_id,
            workflow_id=workflow_id,
            name="Stage, import, and execute Godot scenario",
            task_type="godot_execute",
            cost_class=CostClass.LOCAL,
            parameters=parameters,
            timeout_seconds=context["scenario"].timeout_seconds
            + context["scenario"].import_timeout_seconds
            + 30.0,
        ),
        Task(
            id=validate_id,
            workflow_id=workflow_id,
            name="Independently validate Godot observations",
            task_type="godot_validate",
            cost_class=CostClass.LOCAL,
            depends_on=[execute_id],
            parameters={"execute_task_id": execute_id},
        ),
        Task(
            id=evidence_id,
            workflow_id=workflow_id,
            name="Record final quality evidence",
            task_type="record_evidence",
            cost_class=CostClass.LOCAL,
            depends_on=[validate_id],
        ),
    ]
    return workflow, tasks


class GodotVerificationHandlers:
    def __init__(
        self,
        project_root: Path,
        artifact_manager: ArtifactManager,
        artifacts: ArtifactRepository,
        executions: ExecutionRepository,
        evidence: EvidenceRepository,
        gates: QualityGateRepository,
        process_runner: ProcessRunner | Any | None = None,
    ) -> None:
        self.root = project_root.resolve()
        self.artifact_manager = artifact_manager
        self.artifacts = artifacts
        self.executions = executions
        self.evidence = evidence
        self.gates = gates
        self.runner = process_runner or ProcessRunner(sanitize_output=True)
        self.executor = GodotExecutor(self.runner)

    def refresh_parameters(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        parameters = dict(task.parameters)
        scenario_path = Path(str(parameters["scenario_file"])).resolve(strict=True)
        exe = Path(str(parameters["executable_path"])).resolve(strict=True)
        context = _source_context(self.root, scenario_path, exe)
        parameters.update(
            {
                "scenario_snapshot": context["scenario_snapshot"],
                "scenario_fingerprint": context["scenario_fingerprint"],
                "source_manifest_hash": context["source_manifest_hash"],
                "executable_path": context["executable_path"],
                "executable_sha256": context["executable_sha256"],
                "harness_sha256": context["harness_sha256"],
                "scene_sha256": context["scene_sha256"],
            }
        )
        return parameters

    def approval_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        return {
            key: task.parameters[key]
            for key in (
                "scenario_file",
                "scenario_fingerprint",
                "scenario_snapshot",
                "source_manifest_hash",
                "executable_path",
                "executable_sha256",
                "harness_sha256",
                "scene_sha256",
            )
        }

    def recovery_check(self, workflow: Workflow, task: Task, execution: Execution) -> bool:
        """Trust only hash-verified, correctly bound terminal receipts for this attempt."""
        records = [
            artifact
            for artifact in self.artifacts.list_by_task(task.id)
            if artifact.artifact_type == "godot-process-journal"
            and len(Path(artifact.relative_path).parts) >= 2
            and Path(artifact.relative_path).parts[-2] == execution.id
        ]
        if not records:
            return True  # The pre-launch intent boundary was never reached.
        intents: set[str] = set()
        terminals: set[str] = set()
        for artifact in records:
            try:
                self.artifact_manager.verify_artifact_integrity(artifact)
                path = self.artifact_manager.path_guard.resolve_safe_path(artifact.relative_path)
                payload = json.loads(path.read_text(encoding="utf-8"))
                if (
                    payload.get("workflow_id") != workflow.id
                    or payload.get("task_id") != task.id
                    or payload.get("execution_id") != execution.id
                    or payload.get("attempt_number") != execution.attempt_number
                ):
                    return False
                phase = payload.get("phase")
                if not isinstance(phase, str):
                    return False
                if phase.endswith("-terminal"):
                    process = payload.get("process")
                    if not isinstance(process, dict):
                        return False
                    # A ProcessRunner error before spawn has no child PID, so a
                    # terminal receipt is conclusive even though cleanup was not
                    # applicable. Once a PID exists, require confirmed cleanup.
                    if process.get("pid") is not None and not process.get(
                        "cleanup_completed", False
                    ):
                        return False
                    terminals.add(phase.removesuffix("-terminal"))
                else:
                    intents.add(phase)
            except (OSError, ValueError, TypeError, ArtifactError):
                return False
        return bool(intents) and intents == terminals

    def execute(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        parameters = task.parameters
        scenario = load_scenario(parameters["scenario_file"])
        if scenario.model_dump(mode="json") != parameters["scenario_snapshot"]:
            raise ValidationError("Scenario changed after its input snapshot was refreshed")
        if scenario_fingerprint(scenario) != parameters["scenario_fingerprint"]:
            raise ValidationError("Scenario fingerprint changed before execution")
        harness = _harness_bytes()
        harness_hash = hashlib.sha256(harness).hexdigest()
        if harness_hash != parameters["harness_sha256"]:
            raise ValidationError("Packaged Godot harness changed after approval")
        executable_path = Path(parameters["executable_path"]).resolve(strict=True)
        if sha256_file(executable_path) != parameters["executable_sha256"]:
            raise ValidationError("Godot executable changed after approval")
        scratch = self.root / ".gamefactory" / "scratch"
        stager = GodotStager(self.root, scratch)
        stage: Path | None = None
        durable_ids: list[str] = []
        try:
            stage, manifest, manifest_hash = stager.create_stage(
                workflow.id,
                execution.id,
                parameters["scenario_file"],
                parameters["source_manifest_hash"],
                harness,
            )
            manifest_json = {
                "schema_version": "0.2.0",
                "workflow_id": workflow.id,
                "task_id": task.id,
                "execution_id": execution.id,
                "source_manifest_sha256": manifest_hash,
                "files": [item.__dict__ for item in manifest],
            }
            durable_ids.append(
                self._save_text(
                    workflow.id,
                    task.id,
                    execution.id,
                    "godot-source-manifest",
                    "source-manifest.json",
                    json.dumps(manifest_json, ensure_ascii=False, sort_keys=True, indent=2),
                )
            )
            durable_ids.append(
                self._save_text(
                    workflow.id,
                    task.id,
                    execution.id,
                    "godot-scenario-snapshot",
                    "scenario-snapshot.json",
                    json.dumps(
                        parameters["scenario_snapshot"],
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ),
                )
            )

            def persist_intent(phase: str, content: bytes) -> None:
                journal = json.loads(content.decode("utf-8"))
                envelope = {
                    "workflow_id": workflow.id,
                    "task_id": task.id,
                    "execution_id": execution.id,
                    "attempt_number": execution.attempt_number,
                    "scenario_fingerprint": parameters["scenario_fingerprint"],
                    "source_manifest_sha256": manifest_hash,
                    "executable_path": str(executable_path),
                    "executable_sha256": parameters["executable_sha256"],
                    "harness_sha256": harness_hash,
                    "harness_version": HARNESS_VERSION,
                    "phase": phase,
                    "process": journal,
                }
                durable_ids.append(
                    self._save_text(
                        workflow.id,
                        task.id,
                        execution.id,
                        "godot-process-journal",
                        f"{phase}.json",
                        json.dumps(envelope, ensure_ascii=False, sort_keys=True, indent=2),
                    )
                )

            run = self.executor.run(
                parameters["executable_path"],
                stage,
                stage.parent,
                scenario,
                execution.id,
                parameters["scenario_fingerprint"],
                manifest_hash,
                harness_hash,
                scenario.import_timeout_seconds,
                scenario.timeout_seconds,
                durable_intent=persist_intent,
            )
            durable_ids.extend(
                self._save_run_artifacts(
                    workflow.id,
                    task.id,
                    execution.id,
                    execution.attempt_number,
                    run.artifacts,
                    parameters,
                )
            )
            durable_ids.append(
                self._save_text(
                    workflow.id,
                    task.id,
                    execution.id,
                    "godot-execution-summary",
                    "execution-summary.json",
                    json.dumps(
                        {
                            "execution_id": execution.id,
                            "executable": run.executable,
                            "version": run.version,
                            "source_manifest_sha256": run.source_hash,
                            "harness_sha256": run.harness_hash,
                            "harness_version": HARNESS_VERSION,
                            "scenario_fingerprint": parameters["scenario_fingerprint"],
                            "observation_sha256": hashlib.sha256(
                                run.artifacts["runtime-observation.json"]
                            ).hexdigest(),
                        },
                        sort_keys=True,
                        indent=2,
                    ),
                )
            )
        except Exception as exc:
            if stage is not None:
                try:
                    durable_ids.extend(
                        self._save_scratch_artifacts(
                            workflow.id,
                            task.id,
                            execution.id,
                            execution.attempt_number,
                            stage.parent,
                            parameters,
                        )
                    )
                except Exception as artifact_error:
                    details = getattr(exc, "details", None)
                    if isinstance(details, dict) and details.get("process_state_uncertain"):
                        details["failure_artifact_persistence_error"] = str(artifact_error)
                        raise exc from artifact_error
                    raise
            if not durable_ids:
                durable_ids.append(
                    self._save_text(
                        workflow.id,
                        task.id,
                        execution.id,
                        "godot-execution-error",
                        "execution-error.json",
                        json.dumps(
                            {"execution_id": execution.id, "error": str(exc)}, ensure_ascii=False
                        ),
                    )
                )
            raise
        return TaskHandlerResult(
            schema_version=1,
            summary=f"Godot {run.version} import/runtime exited successfully for {scenario.scenario_id}",
            artifact_ids=durable_ids,
        )

    def validate(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        execute_task_id = str(task.parameters["execute_task_id"])
        parent_execution = self.executions.get_latest_attempt(execute_task_id)
        if parent_execution is None or parent_execution.status != ExecutionStatus.COMPLETED:
            raise ValidationError("No completed Godot execution attempt is available to validate")
        records = self.artifacts.list_by_task(execute_task_id)
        current_records = [
            item
            for item in records
            if len(Path(item.relative_path).parts) >= 2
            and Path(item.relative_path).parts[-2] == parent_execution.id
        ]
        scenario_record = next(
            (item for item in current_records if item.artifact_type == "godot-scenario-snapshot"),
            None,
        )
        observation_record = next(
            (item for item in current_records if item.artifact_type == "godot-runtime-observation"),
            None,
        )
        if scenario_record is None or observation_record is None:
            raise ArtifactError(
                "Godot scenario snapshot or current observation artifact is missing"
            )
        scenario_path = self.artifact_manager.path_guard.resolve_safe_path(
            scenario_record.relative_path
        )
        observation_path = self.artifact_manager.path_guard.resolve_safe_path(
            observation_record.relative_path
        )
        self.artifact_manager.verify_artifact_integrity(scenario_record)
        self.artifact_manager.verify_artifact_integrity(observation_record)
        scenario = Scenario.model_validate_json(scenario_path.read_bytes())
        observation_data = json.loads(observation_path.read_text(encoding="utf-8"))
        findings: dict[str, Any]
        try:
            observation = validate_observation(observation_data, scenario, parent_execution.id)
            findings = evaluate_assertions(scenario, observation)
        except Exception as exc:
            findings = {
                "schema_version": "0.2.0",
                "status": "FAIL",
                "findings": [],
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        report_id = self._save_text(
            workflow.id,
            task.id,
            execution.id,
            "godot-validation-report",
            "validation-report.json",
            json.dumps(findings, ensure_ascii=False, sort_keys=True, indent=2),
        )
        passed = findings.get("status") == "PASS"
        gate = QualityGate(
            id=generate_id("GATE"),
            task_id=task.id,
            gate_type="godot-independent-assertions",
            status=GateStatus.PASSED if passed else GateStatus.FAILED,
            evaluated_at=utc_now_iso(),
            reason="Python evaluated observed state against the stored scenario oracle"
            if passed
            else "Godot observations failed independent schema or assertion validation",
        )
        self.gates.save(gate)
        self.evidence.save(
            Evidence(
                id=generate_id("EVI"),
                task_id=task.id,
                execution_id=execution.id,
                evidence_type="godot-independent-validation",
                summary="Godot observation checked by the Python-owned scenario oracle",
                raw_data={"gate_id": gate.id, "findings": findings, "artifact_ids": [report_id]},
            )
        )
        if not passed:
            raise ValidationError(
                "Godot independent assertions failed; inspect validation-report.json"
            )
        findings_list = findings.get("findings", [])
        finding_count = len(findings_list) if isinstance(findings_list, list) else 0
        return TaskHandlerResult(
            schema_version=1,
            summary=f"{finding_count} independent Godot assertions passed",
            artifact_ids=[report_id],
        )

    def _save_run_artifacts(
        self,
        workflow_id: str,
        task_id: str,
        execution_id: str,
        attempt_number: int,
        outputs: dict[str, bytes],
        input_snapshot: dict[str, Any],
    ) -> list[str]:
        ids: list[str] = []
        for filename, content in sorted(outputs.items()):
            artifact_type = "godot-" + filename.removesuffix(".log").removesuffix(".json")
            if filename.endswith("-process.json"):
                process_data = json.loads(content.decode("utf-8"))
                process_data.update(
                    {
                        "workflow_id": workflow_id,
                        "task_id": task_id,
                        "execution_id": execution_id,
                        "attempt_number": attempt_number,
                        "scenario_fingerprint": input_snapshot["scenario_fingerprint"],
                        "source_manifest_sha256": input_snapshot["source_manifest_hash"],
                        "executable_sha256": input_snapshot["executable_sha256"],
                        "harness_sha256": input_snapshot["harness_sha256"],
                        "harness_version": HARNESS_VERSION,
                    }
                )
                content = json.dumps(
                    process_data, ensure_ascii=False, sort_keys=True, indent=2
                ).encode("utf-8")
            ids.append(
                self._save_text(
                    workflow_id,
                    task_id,
                    execution_id,
                    artifact_type,
                    filename,
                    content.decode("utf-8", errors="replace"),
                )
            )
        return ids

    def _save_scratch_artifacts(
        self,
        workflow_id: str,
        task_id: str,
        execution_id: str,
        attempt_number: int,
        scratch: Path,
        input_snapshot: dict[str, Any],
    ) -> list[str]:
        ids: list[str] = []
        for path in sorted(scratch.iterdir()):
            if not path.is_file() or path.is_symlink():
                continue
            if path.name.endswith(("-intent.json", "-terminal.json")):
                continue
            if path.name == "runtime-request.json":
                if path.stat().st_size > 1024 * 1024:
                    continue
                ids.append(
                    self._save_text(
                        workflow_id,
                        task_id,
                        execution_id,
                        "godot-" + path.stem,
                        path.name,
                        path.read_text(encoding="utf-8", errors="replace"),
                    )
                )
                continue
            if path.name.endswith((".log", "-process.json", "runtime-observation.json")):
                if path.stat().st_size > 1024 * 1024:
                    continue
                content = path.read_text(encoding="utf-8", errors="replace")
                if path.name.endswith("-process.json"):
                    try:
                        process_data = json.loads(content)
                    except (json.JSONDecodeError, TypeError):
                        process_data = {"malformed_metadata": content}
                    process_data.update(
                        self._process_context(
                            workflow_id,
                            task_id,
                            execution_id,
                            attempt_number,
                            input_snapshot,
                        )
                    )
                    content = json.dumps(process_data, ensure_ascii=False, sort_keys=True, indent=2)
                ids.append(
                    self._save_text(
                        workflow_id,
                        task_id,
                        execution_id,
                        "godot-" + path.stem,
                        path.name,
                        content,
                    )
                )
        return ids

    @staticmethod
    def _process_context(
        workflow_id: str,
        task_id: str,
        execution_id: str,
        attempt_number: int,
        input_snapshot: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "workflow_id": workflow_id,
            "task_id": task_id,
            "execution_id": execution_id,
            "attempt_number": attempt_number,
            "scenario_fingerprint": input_snapshot["scenario_fingerprint"],
            "source_manifest_sha256": input_snapshot["source_manifest_hash"],
            "executable_sha256": input_snapshot["executable_sha256"],
            "harness_sha256": input_snapshot["harness_sha256"],
            "harness_version": HARNESS_VERSION,
        }

    def _save_text(
        self,
        workflow_id: str,
        task_id: str,
        execution_id: str,
        artifact_type: str,
        filename: str,
        content: str,
    ) -> str:
        relative = (
            Path(".gamefactory")
            / "artifacts"
            / "godot"
            / workflow_id
            / task_id
            / execution_id
            / filename
        )
        artifact = self.artifact_manager.create_text_artifact(
            workflow_id,
            task_id,
            artifact_type,
            "GodotVerificationHandler",
            relative.as_posix(),
            content,
        )
        self.artifacts.save(artifact)
        return artifact.id


def register_godot_handlers(
    registry: TaskHandlerRegistry,
    project_root: Path | str,
    artifact_manager: ArtifactManager,
    artifacts: ArtifactRepository,
    executions: ExecutionRepository,
    evidence: EvidenceRepository,
    gates: QualityGateRepository,
    process_runner: ProcessRunner | Any | None = None,
) -> GodotVerificationHandlers:
    handlers = GodotVerificationHandlers(
        Path(project_root), artifact_manager, artifacts, executions, evidence, gates, process_runner
    )
    registry.register(
        "godot_execute",
        handlers.execute,
        TaskHandlerMetadata(
            operation=HandlerOperation.PROCESS_EXECUTION,
            managed_write=True,
            recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
            approval_context=handlers.approval_context,
            refresh_parameters=handlers.refresh_parameters,
            recovery_check=handlers.recovery_check,
        ),
    )
    registry.register("godot_validate", handlers.validate)
    return handlers
