"""Rendered Godot capture workflow: execute, technical validation, human review."""

from __future__ import annotations

import hashlib
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_capture_contracts import (
    REVIEW_CONTRACT,
    CaptureScenario,
    capture_fingerprint,
    load_capture_scenario,
)
from gamefactory.adapters.engines.godot_contracts import ObservedState, evaluate_state_assertions
from gamefactory.adapters.engines.godot_execution import GodotExecutor
from gamefactory.adapters.engines.godot_image import (
    FIXTURE_SCENARIOS,
    ImageValidationError,
    check_fixture_regions,
    decode_png,
)
from gamefactory.adapters.engines.godot_review_html import render_review_page
from gamefactory.adapters.engines.godot_staging import GodotStager, sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    QualityGateRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ValidationError
from gamefactory.core.domain.models import CostClass, Execution, Task, Workflow, generate_id
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.workflows.godot_verification import GodotVerificationHandlers
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)

HARNESS_VERSION = "0.3.0"


def _harness_bytes() -> bytes:
    return files("gamefactory").joinpath("resources/godot/capture_harness.gd").read_bytes()


def _source_context(project_root: Path, scenario_path: Path, executable: Path) -> dict[str, Any]:
    scenario = load_capture_scenario(scenario_path)
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
        "scenario_fingerprint": capture_fingerprint(scenario),
        "source_manifest_hash": source_hash,
        "executable_path": str(exe_path),
        "executable_sha256": sha256_file(exe_path),
        "harness_sha256": hashlib.sha256(harness).hexdigest(),
        "scene_sha256": sha256_file(scene_path),
    }


def create_godot_capture_workflow(
    project_id: str,
    project_root: Path | str,
    executable: Path | str,
    scenario_path: Path | str,
) -> tuple[Any, list[Task]]:
    from gamefactory.core.domain.models import Workflow, WorkflowStatus

    root = Path(project_root).resolve(strict=True)
    scenario_file = Path(scenario_path).resolve(strict=True)
    exe = Path(executable).resolve(strict=True)
    context = _source_context(root, scenario_file, exe)
    scenario: CaptureScenario = context["scenario"]
    workflow_id = generate_id("WF-CAPTURE")
    execute_id = f"{workflow_id}-EXECUTE"
    validate_id = f"{workflow_id}-VALIDATE"
    review_id = f"{workflow_id}-REVIEW"
    evidence_id = f"{workflow_id}-EVIDENCE"
    workflow = Workflow(
        id=workflow_id,
        project_id=project_id,
        name=f"Godot rendered capture: {scenario.scenario_id}",
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
        "viewport_width": scenario.viewport.width,
        "viewport_height": scenario.viewport.height,
        "renderer_profile": scenario.renderer_profile,
        "review_contract": REVIEW_CONTRACT,
    }
    tasks = [
        Task(
            id=execute_id,
            workflow_id=workflow_id,
            name="Stage, import, and render Godot captures",
            task_type="godot_capture_execute",
            cost_class=CostClass.LOCAL,
            parameters=parameters,
            timeout_seconds=scenario.timeout_seconds + scenario.import_timeout_seconds + 30.0,
        ),
        Task(
            id=validate_id,
            workflow_id=workflow_id,
            name="Validate state and decoded captures",
            task_type="godot_capture_validate",
            cost_class=CostClass.LOCAL,
            depends_on=[execute_id],
            parameters={"execute_task_id": execute_id},
        ),
        Task(
            id=review_id,
            workflow_id=workflow_id,
            name="Wait for human visual review",
            task_type="godot_visual_review",
            cost_class=CostClass.LOCAL,
            depends_on=[validate_id],
            parameters={"execute_task_id": execute_id, "validate_task_id": validate_id},
        ),
        Task(
            id=evidence_id,
            workflow_id=workflow_id,
            name="Record final quality evidence",
            task_type="record_evidence",
            cost_class=CostClass.LOCAL,
            depends_on=[review_id],
        ),
    ]
    return workflow, tasks


class GodotCaptureHandlers:
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
        self._journals = GodotVerificationHandlers(
            self.root,
            artifact_manager,
            artifacts,
            executions,
            evidence,
            gates,
            self.runner,
        )

    def refresh_parameters(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        parameters = dict(task.parameters)
        context = _source_context(
            self.root,
            Path(str(parameters["scenario_file"])).resolve(strict=True),
            Path(str(parameters["executable_path"])).resolve(strict=True),
        )
        parameters.update(
            {
                "scenario_snapshot": context["scenario_snapshot"],
                "scenario_fingerprint": context["scenario_fingerprint"],
                "source_manifest_hash": context["source_manifest_hash"],
                "executable_path": context["executable_path"],
                "executable_sha256": context["executable_sha256"],
                "harness_sha256": context["harness_sha256"],
                "scene_sha256": context["scene_sha256"],
                "viewport_width": context["scenario"].viewport.width,
                "viewport_height": context["scenario"].viewport.height,
                "renderer_profile": context["scenario"].renderer_profile,
                "review_contract": REVIEW_CONTRACT,
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
                "viewport_width",
                "viewport_height",
                "renderer_profile",
                "review_contract",
            )
        }

    def review_refresh(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        parameters = dict(task.parameters)
        parameters["review_fingerprint"] = self._review_fingerprint(workflow, task)
        return parameters

    def review_context(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        del workflow
        return {"review_fingerprint": task.parameters.get("review_fingerprint")}

    def recovery_check(self, workflow: Workflow, task: Task, execution: Execution) -> bool:
        return self._journals.recovery_check(workflow, task, execution)

    def execute(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        parameters = task.parameters
        scenario = load_capture_scenario(parameters["scenario_file"])
        if scenario.model_dump(mode="json") != parameters["scenario_snapshot"]:
            raise ValidationError("Scenario changed after its input snapshot was refreshed")
        if capture_fingerprint(scenario) != parameters["scenario_fingerprint"]:
            raise ValidationError("Scenario fingerprint changed before execution")
        harness = _harness_bytes()
        harness_hash = hashlib.sha256(harness).hexdigest()
        if harness_hash != parameters["harness_sha256"]:
            raise ValidationError("Packaged capture harness changed after approval")
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
            durable_ids.append(
                self._save_text(
                    workflow.id,
                    task.id,
                    execution.id,
                    "godot-source-manifest",
                    "source-manifest.json",
                    json.dumps(
                        {
                            "schema_version": "0.3.0",
                            "workflow_id": workflow.id,
                            "task_id": task.id,
                            "execution_id": execution.id,
                            "source_manifest_sha256": manifest_hash,
                            "files": [item.__dict__ for item in manifest],
                        },
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

            durable_ids.append(
                self._save_text(
                    workflow.id,
                    task.id,
                    execution.id,
                    "godot-scenario-snapshot",
                    "scenario-snapshot.json",
                    json.dumps(parameters["scenario_snapshot"], ensure_ascii=False, sort_keys=True),
                )
            )
            run = self.executor.run_capture(
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
            for filename, content in sorted(run.artifacts.items()):
                text = content.decode("utf-8")
                if filename.endswith("-process.json"):
                    payload = json.loads(text)
                    payload.update(
                        {
                            "workflow_id": workflow.id,
                            "task_id": task.id,
                            "execution_id": execution.id,
                            "attempt_number": execution.attempt_number,
                            "harness_version": HARNESS_VERSION,
                        }
                    )
                    text = json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2)
                durable_ids.append(
                    self._save_text(
                        workflow.id,
                        task.id,
                        execution.id,
                        "godot-" + filename.removesuffix(".log").removesuffix(".json"),
                        filename,
                        text,
                    )
                )
            assert run.images is not None
            for filename, content in sorted(run.images.items()):
                durable_ids.append(
                    self._save_bytes(
                        workflow.id,
                        task.id,
                        execution.id,
                        "godot-capture-png",
                        filename,
                        content,
                    )
                )
            durable_ids.append(
                self._save_text(
                    workflow.id,
                    task.id,
                    execution.id,
                    "godot-render-context",
                    "render-context.json",
                    json.dumps(
                        {
                            "requested": run.requested_renderer,
                            "engine_api": (run.observation or {}).get("renderer"),
                            "process_log": run.process_renderer_log,
                            "process_log_source": "process_log",
                            "engine_source": "engine_api",
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                        indent=2,
                    ),
                )
            )
        except Exception as exc:
            if not durable_ids:
                durable_ids.append(
                    self._save_text(
                        workflow.id,
                        task.id,
                        execution.id,
                        "godot-execution-error",
                        "execution-error.json",
                        json.dumps({"execution_id": execution.id, "error": str(exc)}),
                    )
                )
            raise
        return TaskHandlerResult(
            schema_version=1,
            summary=f"Godot {run.version} rendered {len(run.images)} captures",
            artifact_ids=durable_ids,
        )

    def validate(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        execute_task_id = str(task.parameters["execute_task_id"])
        parent = self.executions.get_latest_attempt(execute_task_id)
        if parent is None or parent.status.value != "COMPLETED":
            raise ValidationError("No completed capture attempt is available to validate")
        records = [
            item
            for item in self.artifacts.list_by_task(execute_task_id)
            if Path(item.relative_path).parts[-2] == parent.id
        ]
        scenario_record = _one(records, "godot-scenario-snapshot")
        observation_record = _one(records, "godot-runtime-observation")
        for record in (scenario_record, observation_record):
            self.artifact_manager.verify_artifact_integrity(record)
        scenario = CaptureScenario.model_validate_json(
            self.artifact_manager.path_guard.resolve_safe_path(
                scenario_record.relative_path
            ).read_bytes()
        )
        observation = json.loads(
            self.artifact_manager.path_guard.resolve_safe_path(
                observation_record.relative_path
            ).read_text(encoding="utf-8")
        )
        state_error = None
        states: dict[int, ObservedState] = {}
        try:
            states = _bind_states(observation, scenario, parent.id)
            state_report = evaluate_state_assertions(scenario.assertions, states)
        except (ValueError, ValidationError, TypeError) as exc:
            state_report = {"status": "FAIL", "findings": [], "error": str(exc)}
            state_error = str(exc)
        image_findings: list[dict[str, object]] = []
        decoded: list[dict[str, Any]] = []
        pngs = [item for item in records if item.artifact_type == "godot-capture-png"]
        by_name = {Path(item.relative_path).name: item for item in pngs}
        technical_images = True
        for point in scenario.captures:
            record = by_name.get(point.id + ".png")
            if record is None:
                technical_images = False
                image_findings.append({"id": point.id, "status": "FAIL", "detail": "missing PNG"})
                continue
            self.artifact_manager.verify_artifact_integrity(record)
            raw = self.artifact_manager.path_guard.resolve_safe_path(
                record.relative_path
            ).read_bytes()
            try:
                image = decode_png(raw, scenario.viewport.width, scenario.viewport.height)
            except ImageValidationError as exc:
                technical_images = False
                image_findings.append({"id": point.id, "status": "FAIL", "detail": str(exc)})
                continue
            if hashlib.sha256(raw).hexdigest() != record.content_hash:
                technical_images = False
                image_findings.append({"id": point.id, "status": "FAIL", "detail": "hash mismatch"})
                continue
            state = states.get(point.tick)
            region_rows: list[dict[str, object]] = []
            if state is not None and scenario.scenario_id in FIXTURE_SCENARIOS:
                region_rows = check_fixture_regions(image.image, state.model_dump())
                image_findings.extend(
                    {**row, "id": f"{point.id}:{row['id']}"} for row in region_rows
                )
            decoded.append(
                {
                    "id": point.id,
                    "tick": point.tick,
                    "sha256": image.sha256,
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "bytes": image.size,
                    "state": state.model_dump() if state else None,
                    "src": f"images/{point.id}.png",
                    "raw": raw,
                }
            )
        regions_pass = all(item.get("status") == "PASS" for item in image_findings)
        state_pass = state_report.get("status") == "PASS" and state_error is None
        technical = (
            state_pass
            and technical_images
            and regions_pass
            and len(decoded) == len(scenario.captures)
        )
        report = {
            "schema_version": "0.3.0",
            "status": "PASS" if technical else "FAIL",
            "state": state_report,
            "images": [
                {key: value for key, value in item.items() if key != "raw"} for item in decoded
            ],
            "region_checks": image_findings,
            "execution_id": parent.id,
            "scenario_fingerprint": capture_fingerprint(scenario),
        }
        report_id = self._save_text(
            workflow.id,
            task.id,
            execution.id,
            "godot-capture-validation",
            "validation-report.json",
            json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2),
        )
        html_report = {
            "title": f"Capture review {scenario.scenario_id}",
            "project_id": workflow.project_id,
            "scenario_id": scenario.scenario_id,
            "technical_status": "PASS" if technical else "FAIL",
            "human_status": "PENDING",
            "environment": json.dumps(
                {
                    "viewport": [scenario.viewport.width, scenario.viewport.height],
                    "renderer_profile": scenario.renderer_profile,
                    "scenario_fingerprint": capture_fingerprint(scenario),
                },
                sort_keys=True,
            ),
            "images": report["images"],
            "checks": image_findings,
            "commands": (
                f"gamefactory --project {self.root} approvals --workflow {workflow.id}\n"
                f'gamefactory --project {self.root} approve APPROVAL_ID --comment "Reviewed"\n'
                f"gamefactory --project {self.root} resume {workflow.id}\n"
                f'gamefactory --project {self.root} reject APPROVAL_ID --comment "Changes needed"'
            ),
        }
        page = render_review_page(html_report)
        html_id = self._save_text(
            workflow.id,
            task.id,
            execution.id,
            "godot-review-html",
            "review/index.html",
            page,
        )
        image_ids = []
        for item in decoded:
            image_ids.append(
                self._save_bytes(
                    workflow.id,
                    task.id,
                    execution.id,
                    "godot-review-png",
                    f"review/images/{item['id']}.png",
                    item["raw"],
                )
            )
        if not technical:
            raise ValidationError(
                "Capture technical validation failed; visual approval was not opened: "
                + json.dumps(
                    {
                        "state": state_report.get("status"),
                        "state_error": state_error,
                        "images": image_findings,
                        "decoded": len(decoded),
                        "expected": len(scenario.captures),
                    },
                    default=str,
                )[:4000]
            )
        return TaskHandlerResult(
            schema_version=1,
            summary="Capture state and images passed independent checks; human review is still required",
            artifact_ids=[report_id, html_id, *image_ids],
        )

    def review(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        current = self._review_fingerprint(workflow, task)
        expected = task.parameters.get("review_fingerprint")
        if current != expected:
            raise ValidationError("Review evidence changed after the visual approval fingerprint")
        receipt = {
            "schema_version": "0.3.0",
            "decision": "APPROVED",
            "review_contract": REVIEW_CONTRACT,
            "review_fingerprint": current,
            "note": "This receipt records the approval already checked by the workflow engine. It is not itself part of that fingerprint.",
        }
        receipt_id = self._save_text(
            workflow.id,
            task.id,
            execution.id,
            "godot-review-receipt",
            "review-receipt.json",
            json.dumps(receipt, ensure_ascii=False, sort_keys=True, indent=2),
        )
        return TaskHandlerResult(
            schema_version=1,
            summary="Visual review evidence still matches the approved fingerprint",
            artifact_ids=[receipt_id],
        )

    def _review_fingerprint(self, workflow: Workflow, task: Task) -> dict[str, Any]:
        execute_task_id = str(task.parameters["execute_task_id"])
        validate_task_id = str(task.parameters["validate_task_id"])
        parent = self.executions.get_latest_attempt(execute_task_id)
        validate = self.executions.get_latest_attempt(validate_task_id)
        if parent is None or validate is None:
            raise ValidationError("Capture review is missing an execution attempt")
        captures = []
        for artifact in self.artifacts.list_by_task(execute_task_id):
            if (
                artifact.artifact_type == "godot-capture-png"
                and Path(artifact.relative_path).parts[-2] == parent.id
            ):
                self.artifact_manager.verify_artifact_integrity(artifact)
                captures.append(
                    {
                        "name": Path(artifact.relative_path).name,
                        "sha256": artifact.content_hash,
                    }
                )
        html = _one(
            [
                item
                for item in self.artifacts.list_by_task(validate_task_id)
                if item.artifact_type == "godot-review-html"
                and Path(item.relative_path).parts[-3] == validate.id
            ],
            "godot-review-html",
        )
        self.artifact_manager.verify_artifact_integrity(html)
        return {
            "review_contract": REVIEW_CONTRACT,
            "execute_execution_id": parent.id,
            "validate_execution_id": validate.id,
            "captures": sorted(captures, key=lambda item: str(item["name"])),
            "report_sha256": html.content_hash,
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
            "GodotCaptureHandler",
            relative.as_posix(),
            content,
        )
        self.artifacts.save(artifact)
        return artifact.id

    def _save_bytes(
        self,
        workflow_id: str,
        task_id: str,
        execution_id: str,
        artifact_type: str,
        filename: str,
        content: bytes,
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
        safe = self.artifact_manager.path_guard.ensure_safe_parent(relative.as_posix())
        with safe.open("xb") as stream:
            stream.write(content)
        artifact = self.artifact_manager.register_file_artifact(
            workflow_id,
            task_id,
            artifact_type,
            "GodotCaptureHandler",
            relative.as_posix(),
        )
        self.artifacts.save(artifact)
        return artifact.id


def _one(records: list[Any], artifact_type: str) -> Any:
    matches = [item for item in records if item.artifact_type == artifact_type]
    if len(matches) != 1:
        raise ArtifactError(f"expected exactly one {artifact_type} artifact for this attempt")
    return matches[0]


def _bind_states(
    observation: dict[str, Any], scenario: CaptureScenario, execution_id: str
) -> dict[int, ObservedState]:
    if observation.get("schema_version") != "0.3.0":
        raise ValueError("observation schema is not 0.3.0")
    if observation.get("execution_id") != execution_id:
        raise ValueError("observation execution_id does not match the current attempt")
    if observation.get("scenario_sha256") != capture_fingerprint(scenario):
        raise ValueError("observation scenario fingerprint does not match")
    if observation.get("completed_tick") != scenario.max_tick:
        raise ValueError("observation completed_tick does not match max_tick")
    snapshots = observation.get("snapshots")
    captures = observation.get("captures")
    if not isinstance(snapshots, list) or not isinstance(captures, list):
        raise ValueError("observation snapshots and captures must be arrays")
    if [item.get("tick") for item in snapshots] != list(scenario.snapshots):
        raise ValueError("observation ticks do not match the scenario")
    if [item.get("id") for item in captures] != [point.id for point in scenario.captures]:
        raise ValueError("observation capture ids do not match the scenario")
    if [item.get("tick") for item in captures] != list(scenario.snapshots):
        raise ValueError("observation capture ticks do not match the scenario")
    states: dict[int, ObservedState] = {}
    for item in snapshots:
        if not isinstance(item, dict):
            raise ValueError("snapshot must be an object")
        states[int(item["tick"])] = ObservedState.model_validate(item["state"])
    for item in captures:
        if not isinstance(item, dict):
            raise ValueError("capture must be an object")
        state = states[int(item["tick"])]
        if item.get("state") != state.model_dump():
            raise ValueError("capture state does not match the checkpoint snapshot")
    return states


def register_godot_capture_handlers(
    registry: TaskHandlerRegistry,
    project_root: Path | str,
    artifact_manager: ArtifactManager,
    artifacts: ArtifactRepository,
    executions: ExecutionRepository,
    evidence: EvidenceRepository,
    gates: QualityGateRepository,
    process_runner: ProcessRunner | Any | None = None,
) -> GodotCaptureHandlers:
    handlers = GodotCaptureHandlers(
        Path(project_root),
        artifact_manager,
        artifacts,
        executions,
        evidence,
        gates,
        process_runner,
    )
    registry.register(
        "godot_capture_execute",
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
    registry.register("godot_capture_validate", handlers.validate)
    registry.register(
        "godot_visual_review",
        handlers.review,
        TaskHandlerMetadata(
            operation=HandlerOperation.VISUAL_REVIEW,
            managed_write=True,
            approval_context=handlers.review_context,
            refresh_parameters=handlers.review_refresh,
        ),
    )
    return handlers
