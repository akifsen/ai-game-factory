"""Godot gameplay quality workflow: execute -> evaluate -> final evidence."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform as host_platform
import stat
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.gameplay_harness import (
    COLLECTION_METHOD,
    HARNESS_VERSION,
    _harness_bytes,
    execute_gameplay_harness,
)
from gamefactory.adapters.engines.godot_staging import GodotStager, _is_reparse, sha256_file
from gamefactory.adapters.persistence.repositories import (
    ArtifactRepository,
    EvidenceRepository,
    ExecutionRepository,
    QualityGateRepository,
)
from gamefactory.core.artifacts.artifact_manager import ArtifactManager
from gamefactory.core.domain.errors import ArtifactError, ToolExecutionError, ValidationError
from gamefactory.core.domain.game_quality import (
    GameplayObservation,
    PerformanceEvidence,
    QualityFinding,
    QualityReport,
)
from gamefactory.core.domain.gameplay_harness import GameplayHarnessRequest
from gamefactory.core.domain.models import (
    Artifact,
    CostClass,
    Evidence,
    Execution,
    ExecutionStatus,
    GateStatus,
    QualityGate,
    Task,
    Workflow,
    WorkflowStatus,
    generate_id,
    utc_now_iso,
)
from gamefactory.core.execution.path_guard import PathGuard
from gamefactory.core.execution.process_runner import ProcessRunner
from gamefactory.validators.game_quality import evaluate_gameplay, evaluate_performance
from gamefactory.workflows.handlers import (
    HandlerOperation,
    HandlerRecovery,
    TaskHandlerMetadata,
    TaskHandlerRegistry,
    TaskHandlerResult,
)

_EXECUTE = "gameplay_harness_execute"
_EVALUATE = "gameplay_quality_evaluate"
_PLACEHOLDER = "attempt-placeholder"
_MAX_REQUEST_BYTES = 2_000_000
_MAX_TIMEOUT_SECONDS = 900.0
_MAX_ARTIFACT_JSON_BYTES = 8 * 1024 * 1024
_MAX_SOURCE_MANIFEST_BYTES = 32 * 1024 * 1024
_MAX_SCREENSHOT_BYTES = 32 * 1024 * 1024


def _reject_reparse_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        if _is_reparse(current):
            raise ValidationError(
                "project, executable, and scenario cannot traverse a link or junction"
            )


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _read_request(path: Path) -> tuple[GameplayHarnessRequest, str]:
    _reject_reparse_components(path)
    try:
        before = path.stat()
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > _MAX_REQUEST_BYTES
        ):
            raise ValidationError("gameplay harness scenario source size or file type is invalid")
        descriptor = os.open(
            path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (
                before.st_dev,
                before.st_ino,
            ):
                raise ValidationError("gameplay harness scenario source changed before open")
            raw = stream.read(_MAX_REQUEST_BYTES + 1)
            after = os.fstat(stream.fileno())
        after_path = path.lstat()
        if (
            len(raw) > _MAX_REQUEST_BYTES
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (after_path.st_dev, after_path.st_ino)
        ):
            raise ValidationError("gameplay harness scenario source changed during bounded read")
        value = json.loads(raw)
    except ValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError(
            "gameplay harness scenario source is unavailable or invalid JSON"
        ) from exc
    return GameplayHarnessRequest.from_dict(value), hashlib.sha256(raw).hexdigest()


def _bounded_regular_bytes(path: Path, root: Path, max_bytes: int) -> bytes:
    lexical = Path(os.path.abspath(path))
    root = Path(os.path.abspath(root))
    _reject_reparse_components(lexical)
    try:
        lexical.relative_to(root)
    except ValueError as exc:
        raise ArtifactError("gameplay evidence path escapes the project root") from exc
    safe = PathGuard(root).resolve_safe_path(lexical.relative_to(root).as_posix())
    try:
        path_info = safe.lstat()
        if stat.S_ISLNK(path_info.st_mode) or getattr(path_info, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400
        ):
            raise ArtifactError("gameplay evidence cannot be a symlink or junction")
        descriptor = os.open(
            safe, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        with os.fdopen(descriptor, "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > max_bytes:
                raise ArtifactError("gameplay evidence is not a bounded regular file")
            if (before.st_dev, before.st_ino) != (path_info.st_dev, path_info.st_ino):
                raise ArtifactError("gameplay evidence changed before safe open")
            raw = stream.read(max_bytes + 1)
            after = os.fstat(stream.fileno())
        after_path = safe.lstat()
        if (
            len(raw) > max_bytes
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            or (after.st_dev, after.st_ino) != (after_path.st_dev, after_path.st_ino)
        ):
            raise ArtifactError("gameplay evidence changed during bounded read")
        return raw
    except OSError as exc:
        raise ArtifactError("gameplay evidence could not be read safely") from exc


def _platform_id() -> str:
    value = host_platform.system().lower()
    return "windows" if value == "windows" else value


def _select_completed_execution(executions: list[Execution], task_id: str) -> Execution:
    completed = [
        item
        for item in executions
        if item.task_id == task_id and item.status == ExecutionStatus.COMPLETED
    ]
    if not completed:
        raise ArtifactError("gameplay harness task has no completed execution attempt")
    return max(completed, key=lambda item: (item.attempt_number, item.started_at, item.id))


def _select_attempt_manifest(
    artifacts: list[Any], workflow_id: str, task_id: str, execution: Execution
) -> Any:
    """Select a manifest by the completed execution's exact attempt identity."""
    expected = (
        Path(".gamefactory")
        / "artifacts"
        / workflow_id
        / task_id
        / f"attempt-{execution.attempt_number}"
        / execution.id
        / "run-manifest.json"
    ).as_posix()
    candidates = [
        item
        for item in artifacts
        if item.artifact_type == "gameplay-run-manifest" and item.relative_path == expected
    ]
    if len(candidates) != 1:
        raise ArtifactError("selected completed execution must have exactly one run manifest")
    return candidates[0]


def _performance_samples_match_raw(
    performance: PerformanceEvidence, raw_output: dict[str, Any]
) -> bool:
    """Bind persisted samples and collection window to the selected raw run."""
    return (
        performance.started_at == raw_output.get("started_at")
        and performance.ended_at == raw_output.get("ended_at")
        and performance.sample_interval_ms == raw_output.get("sample_interval_ms")
        and [sample.to_dict() for sample in performance.samples] == raw_output.get("samples")
    )


def _validate_source_manifest(manifest: Any, expected_sha256: str) -> None:
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "gameplay-source-manifest-1.0.0"
    ):
        raise ArtifactError("gameplay source manifest has an unsupported schema")
    files = manifest.get("files")
    if not isinstance(files, list) or len(files) > 10_000:
        raise ArtifactError("gameplay source manifest file list is invalid")
    digest = hashlib.sha256()
    last_path = ""
    for row in files:
        if not isinstance(row, dict) or set(row) != {"relative_path", "sha256", "size"}:
            raise ArtifactError("gameplay source manifest entry is malformed")
        relative = row["relative_path"]
        sha = row["sha256"]
        size = row["size"]
        if (
            not isinstance(relative, str)
            or not relative
            or relative <= last_path
            or "\\" in relative
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or not isinstance(sha, str)
            or len(sha) != 64
            or any(char not in "0123456789abcdef" for char in sha)
            or type(size) is not int
            or size < 0
        ):
            raise ArtifactError("gameplay source manifest entry failed validation")
        last_path = relative
        digest.update(f"{relative}\0{size}\0{sha}\n".encode())
    if manifest.get("source_sha256") != expected_sha256 or digest.hexdigest() != expected_sha256:
        raise ArtifactError(
            "gameplay source manifest content does not match the approved source hash"
        )


def create_gameplay_quality_workflow(
    project_id: str,
    project_root: Path | str,
    executable: Path | str,
    scenario_path: Path | str,
    *,
    timeout_seconds: float = 120.0,
) -> tuple[Workflow, list[Task]]:
    """Create an attempt-bound workflow from a versioned JSON harness request.

    The request file is a project source file. It is independently snapshotted
    because GodotStager intentionally excludes the scenario file from its source
    manifest; every other project byte is still bound by that source manifest.
    """
    root_input, exe_input, scenario_input = (
        Path(project_root),
        Path(executable),
        Path(scenario_path),
    )
    for original in (root_input, exe_input, scenario_input):
        _reject_reparse_components(original)
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 1.0 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS
    ):
        raise ValidationError("workflow phase timeout must be finite and between 1 and 900 seconds")
    root = root_input.resolve(strict=True)
    exe = exe_input.resolve(strict=True)
    scenario_file = scenario_input.resolve(strict=True)
    try:
        scenario_file.relative_to(root)
    except ValueError as exc:
        raise ValidationError("gameplay scenario request must be inside the Godot project") from exc
    if not exe.is_file() or not (root / "project.godot").is_file():
        raise ValidationError("Godot project and executable are required")
    request, request_hash = _read_request(scenario_file)
    if request.execution_id != _PLACEHOLDER:
        raise ValidationError(f"registered harness request execution_id must be {_PLACEHOLDER!r}")
    _, source_hash = GodotStager(root, root / ".gamefactory" / "scratch").source_manifest(
        scenario_file
    )
    harness_hash = hashlib.sha256(_harness_bytes()).hexdigest()
    executable_hash = sha256_file(exe)
    workflow_id = generate_id("WF-GAMEPLAY")
    execute_id, evaluate_id, evidence_id = (
        f"{workflow_id}-{suffix}" for suffix in ("EXECUTE", "EVALUATE", "EVIDENCE")
    )
    workflow = Workflow(
        id=workflow_id,
        project_id=project_id,
        name=f"Godot gameplay quality: {request.scenario.scenario_id}",
        status=WorkflowStatus.PENDING,
    )
    parameters = {
        "scenario_path": str(scenario_file),
        "request_snapshot": request.to_dict(),
        "request_file_sha256": request_hash,
        "source_manifest_sha256": source_hash,
        "executable_path": str(exe),
        "executable_sha256": executable_hash,
        "harness_sha256": harness_hash,
        "harness_version": HARNESS_VERSION,
        "timeout_seconds": float(timeout_seconds),
    }
    tasks = [
        Task(
            id=execute_id,
            workflow_id=workflow_id,
            name="Stage and execute bounded Godot gameplay scenario",
            task_type=_EXECUTE,
            cost_class=CostClass.LOCAL,
            parameters=parameters,
            timeout_seconds=timeout_seconds * 3 + 30.0,
        ),
        Task(
            id=evaluate_id,
            workflow_id=workflow_id,
            name="Evaluate gameplay assertions and performance budget",
            task_type=_EVALUATE,
            cost_class=CostClass.LOCAL,
            depends_on=[execute_id],
            parameters={
                "execute_task_id": execute_id,
                "request_snapshot": request.to_dict(),
                "request_file_sha256": request_hash,
                "source_manifest_sha256": source_hash,
                "executable_sha256": executable_hash,
                "harness_sha256": harness_hash,
            },
        ),
        Task(
            id=evidence_id,
            workflow_id=workflow_id,
            name="Record final quality evidence",
            task_type="record_evidence",
            cost_class=CostClass.LOCAL,
            depends_on=[evaluate_id],
        ),
    ]
    return workflow, tasks


class GameplayQualityHandlers:
    """Persistence-aware task handlers used by existing WorkflowEngine workers."""

    def __init__(
        self,
        project_root: Path | str,
        artifact_manager: ArtifactManager,
        artifacts: ArtifactRepository,
        executions: ExecutionRepository,
        evidence: EvidenceRepository,
        gates: QualityGateRepository,
        process_runner: ProcessRunner | Any | None = None,
    ) -> None:
        root_input = Path(project_root).expanduser()
        _reject_reparse_components(root_input)
        self.root = root_input.resolve(strict=True)
        if not self.root.is_dir():
            raise ValidationError("gameplay quality project root must be a directory")
        self.manager, self.artifacts, self.executions = artifact_manager, artifacts, executions
        self.evidence, self.gates = evidence, gates
        self.process_runner = process_runner or ProcessRunner(sanitize_output=True)
        self.path_guard = PathGuard(self.root)

    def register(self, registry: TaskHandlerRegistry) -> None:
        registry.register(
            _EXECUTE,
            self.execute,
            TaskHandlerMetadata(
                operation=HandlerOperation.PROCESS_EXECUTION,
                managed_write=True,
                recovery=HandlerRecovery.CONSERVATIVE_PROCESS,
                recovery_check=self.recovery_check,
            ),
        )
        registry.register(
            _EVALUATE,
            self.evaluate,
            TaskHandlerMetadata(
                operation=HandlerOperation.REPOSITORY_WRITE,
                managed_write=True,
            ),
        )

    def _save_bytes(
        self, workflow_id: str, task_id: str, artifact_type: str, relative_path: str, raw: bytes
    ) -> Any:
        path = self.path_guard.ensure_safe_parent(relative_path)
        with path.open("xb") as stream:
            stream.write(raw)
            stream.flush()
        artifact = self.manager.register_file_artifact(
            workflow_id, task_id, artifact_type, "GodotGameplayHarness", relative_path
        )
        self.artifacts.save(artifact)
        return artifact

    def _save_json(
        self, workflow_id: str, task_id: str, artifact_type: str, relative_path: str, value: Any
    ) -> Any:
        return self._save_bytes(
            workflow_id, task_id, artifact_type, relative_path, _json_bytes(value) + b"\n"
        )

    def recovery_check(self, workflow: Workflow, task: Task, execution: Execution) -> bool:
        records = [
            item
            for item in self.artifacts.list_by_task(task.id)
            if item.artifact_type == "gameplay-process-receipt"
            and Path(item.relative_path).parent.name == execution.id
        ]
        if not records:
            return True
        phases: dict[str, set[str]] = {}
        for artifact in records:
            try:
                self.manager.verify_artifact_integrity(artifact)
                path = self.path_guard.resolve_safe_path(artifact.relative_path)
                receipt = json.loads(path.read_text(encoding="utf-8"))
                if any(
                    receipt.get(key) != expected
                    for key, expected in (
                        ("workflow_id", workflow.id),
                        ("task_id", task.id),
                        ("execution_id", execution.id),
                        ("attempt_number", execution.attempt_number),
                    )
                ):
                    return False
                phase = receipt.get("phase")
                if not isinstance(phase, str) or not phase.endswith(("-intent", "-terminal")):
                    return False
                base, kind = phase.rsplit("-", 1)
                phases.setdefault(base, set()).add(kind)
                if kind == "terminal":
                    process = receipt.get("process")
                    if not isinstance(process, dict):
                        return False
                    if (
                        process.get("pid") is not None
                        and process.get("cleanup_completed") is not True
                    ):
                        return False
            except (OSError, ValueError, TypeError, ArtifactError):
                return False
        return bool(phases) and all(parts == {"intent", "terminal"} for parts in phases.values())

    def _load_json_artifact(
        self, artifact: Any, *, max_bytes: int = _MAX_ARTIFACT_JSON_BYTES
    ) -> Any:
        self.manager.verify_artifact_integrity(artifact)
        raw = _bounded_regular_bytes(self.root / artifact.relative_path, self.root, max_bytes)
        if hashlib.sha256(raw).hexdigest() != artifact.content_hash:
            raise ArtifactError("gameplay evidence changed after artifact integrity verification")
        try:
            return json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ArtifactError("gameplay evidence artifact is not valid bounded JSON") from exc

    def execute(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        params = task.parameters
        scenario_file = Path(params["scenario_path"])
        template, request_hash = _read_request(scenario_file)
        if (
            request_hash != params["request_file_sha256"]
            or template.to_dict() != params["request_snapshot"]
        ):
            raise ValidationError("Gameplay request changed after workflow registration")
        if sha256_file(Path(params["executable_path"])) != params["executable_sha256"]:
            raise ValidationError("Godot executable changed after workflow registration")
        request_data = template.to_dict()
        request_data["execution_id"] = execution.id
        request = GameplayHarnessRequest.from_dict(request_data)
        receipt_counter = 0
        artifact_ids: list[str] = []

        def persist_receipt(phase: str, receipt: dict[str, Any]) -> None:
            nonlocal receipt_counter
            relative = f".gamefactory/artifacts/{workflow.id}/{task.id}/attempt-{execution.attempt_number}/{execution.id}/receipt-{receipt_counter:03d}.json"
            receipt_counter += 1
            artifact = self._save_json(
                workflow.id, task.id, "gameplay-process-receipt", relative, receipt
            )
            artifact_ids.append(artifact.id)

        run = execute_gameplay_harness(
            self.root,
            params["executable_path"],
            scenario_file,
            request,
            workflow.id,
            task.id,
            execution.attempt_number,
            process_runner=self.process_runner,
            timeout_seconds=float(params["timeout_seconds"]),
            expected_execution_id=execution.id,
            expected_request_sha256=params["request_file_sha256"],
            expected_source_sha256=params["source_manifest_sha256"],
            expected_executable_sha256=params["executable_sha256"],
            expected_harness_sha256=params["harness_sha256"],
            durable_receipt=persist_receipt,
        )
        attempt_rel = f".gamefactory/artifacts/{workflow.id}/{task.id}/attempt-{execution.attempt_number}/{execution.id}"
        harness_output_path = f"{attempt_rel}/harness-output.json"
        raw_output_bytes = _bounded_regular_bytes(
            run.output_path, self.root, _MAX_ARTIFACT_JSON_BYTES
        )
        raw_output = json.loads(raw_output_bytes)
        harness_output_artifact = self._save_bytes(
            workflow.id,
            task.id,
            "gameplay-harness-output",
            harness_output_path,
            raw_output_bytes,
        )
        artifact_ids.append(harness_output_artifact.id)
        source_manifest_path = f"{attempt_rel}/source-manifest.json"
        source_manifest_artifact = self._save_json(
            workflow.id,
            task.id,
            "gameplay-source-manifest",
            source_manifest_path,
            {
                "schema_version": "gameplay-source-manifest-1.0.0",
                "source_sha256": run.source_sha256,
                "files": list(run.source_manifest),
            },
        )
        artifact_ids.append(source_manifest_artifact.id)
        screenshot_refs: list[str] = []
        screenshot_artifact_ids: list[str] = []
        for index, (path, expected_hash) in enumerate(
            zip(run.screenshot_paths, run.screenshot_sha256, strict=True)
        ):
            raw = _bounded_regular_bytes(path, self.root, _MAX_SCREENSHOT_BYTES)
            if hashlib.sha256(raw).hexdigest() != expected_hash:
                raise ArtifactError("Screenshot changed after independent PNG validation")
            relative = f"{attempt_rel}/screenshot-{index:03d}.png"
            artifact = self._save_bytes(
                workflow.id, task.id, "gameplay-visual-capture", relative, raw
            )
            screenshot_refs.append(relative)
            screenshot_artifact_ids.append(artifact.id)
            artifact_ids.append(artifact.id)

        observation = GameplayObservation(
            run.observation.scenario_sha256,
            run.observation.engine,
            run.observation.observed_at,
            run.observation.state,
            (
                *screenshot_refs,
                harness_output_path,
                source_manifest_path,
                f"{attempt_rel}/run-manifest.json",
            ),
        )
        obs_artifact = self._save_json(
            workflow.id,
            task.id,
            "gameplay-observation",
            f"{attempt_rel}/gameplay-observation.json",
            observation.to_dict(),
        )
        artifact_ids.append(obs_artifact.id)
        performance_artifact_id: str | None = None
        if run.performance is not None:
            performance = PerformanceEvidence(
                run.performance.platform,
                run.performance.budget_sha256,
                run.performance.engine,
                run.performance.collection_method,
                run.performance.started_at,
                run.performance.ended_at,
                run.performance.sample_interval_ms,
                run.performance.scenario_sha256,
                run.performance.samples,
                observation.evidence_refs,
            )
            perf_artifact = self._save_json(
                workflow.id,
                task.id,
                "performance-evidence",
                f"{attempt_rel}/performance-evidence.json",
                performance.to_dict(),
            )
            performance_artifact_id = perf_artifact.id
            artifact_ids.append(perf_artifact.id)
        manifest = {
            "schema_version": "gameplay-run-manifest-1.0.0",
            "workflow_id": workflow.id,
            "task_id": task.id,
            "execution_id": execution.id,
            "attempt_number": execution.attempt_number,
            "seed": request.seed,
            "fixed_tick_hz": request.fixed_tick_hz,
            "scenario_sha256": run.scenario_sha256,
            "contract_sha256": run.contract_sha256,
            "source_sha256": run.source_sha256,
            "harness_sha256": run.harness_sha256,
            "executable_sha256": run.executable_sha256,
            "engine": run.engine,
            "engine_identity_sha256": run.engine_identity_sha256,
            "harness_version": HARNESS_VERSION,
            "state_tick": None,
            "request_file_sha256": params["request_file_sha256"],
            "harness_output_artifact_id": harness_output_artifact.id,
            "source_manifest_artifact_id": source_manifest_artifact.id,
            "source_manifest_path": source_manifest_path,
            "observation_artifact_id": obs_artifact.id,
            "performance_artifact_id": performance_artifact_id,
            "screenshot_refs": screenshot_refs,
            "screenshot_artifact_ids": screenshot_artifact_ids,
            "platform": run.performance.platform if run.performance is not None else _platform_id(),
            "receipt_artifact_ids": list(artifact_ids[:receipt_counter]),
            "started_at": run.started_at,
            "ended_at": run.ended_at,
        }
        manifest["state_tick"] = raw_output.get("state_tick")
        output_art = self._save_json(
            workflow.id,
            task.id,
            "gameplay-run-manifest",
            f"{attempt_rel}/run-manifest.json",
            manifest,
        )
        artifact_ids.append(output_art.id)
        if not artifact_ids:
            raise ArtifactError("gameplay execution produced no durable artifacts")
        return TaskHandlerResult(
            1,
            f"Godot gameplay scenario executed with {len(screenshot_refs)} rendered captures",
            artifact_ids,
        )

    def evaluate(self, workflow: Workflow, task: Task, execution: Execution) -> TaskHandlerResult:
        if execution.task_id != task.id:
            raise ArtifactError(
                "quality evaluation execution does not belong to its evaluator task"
            )
        params = task.parameters
        source_task_id = params["execute_task_id"]
        source_artifacts = self.artifacts.list_by_task(source_task_id)
        selected_execution = _select_completed_execution(
            self.executions.list_by_task(source_task_id), source_task_id
        )
        manifest_artifact = _select_attempt_manifest(
            source_artifacts, workflow.id, source_task_id, selected_execution
        )
        attempt_root = (
            Path(".gamefactory")
            / "artifacts"
            / workflow.id
            / source_task_id
            / f"attempt-{selected_execution.attempt_number}"
            / selected_execution.id
        ).as_posix()
        manifest = self._load_json_artifact(manifest_artifact)
        artifacts_by_id = {artifact.id: artifact for artifact in source_artifacts}

        def bound_artifact(
            field: str, artifact_type: str, filename: str, *, optional: bool = False
        ) -> Artifact | None:
            artifact_id = manifest.get(field)
            if artifact_id is None and optional:
                return None
            if not isinstance(artifact_id, str) or artifact_id not in artifacts_by_id:
                raise ArtifactError(f"run manifest has no registered {field}")
            artifact = artifacts_by_id[artifact_id]
            if (
                artifact.artifact_type != artifact_type
                or artifact.relative_path != f"{attempt_root}/{filename}"
            ):
                raise ArtifactError(f"run manifest {field} points to an unexpected artifact")
            return artifact

        raw_output_artifact = bound_artifact(
            "harness_output_artifact_id", "gameplay-harness-output", "harness-output.json"
        )
        observation_artifact = bound_artifact(
            "observation_artifact_id", "gameplay-observation", "gameplay-observation.json"
        )
        source_manifest_artifact = bound_artifact(
            "source_manifest_artifact_id", "gameplay-source-manifest", "source-manifest.json"
        )
        performance_artifact = bound_artifact(
            "performance_artifact_id",
            "performance-evidence",
            "performance-evidence.json",
            optional=True,
        )
        if (
            raw_output_artifact is None
            or observation_artifact is None
            or source_manifest_artifact is None
        ):
            raise ArtifactError("run manifest is missing required evidence artifacts")
        observation = GameplayObservation.from_dict(self._load_json_artifact(observation_artifact))
        raw_output = self._load_json_artifact(raw_output_artifact)
        source_manifest = self._load_json_artifact(
            source_manifest_artifact, max_bytes=_MAX_SOURCE_MANIFEST_BYTES
        )
        request = GameplayHarnessRequest.from_dict(params["request_snapshot"])
        if (
            manifest.get("schema_version") != "gameplay-run-manifest-1.0.0"
            or manifest.get("execution_id") != selected_execution.id
            or manifest.get("attempt_number") != selected_execution.attempt_number
            or manifest.get("task_id") != source_task_id
            or manifest.get("workflow_id") != workflow.id
        ):
            raise ValidationError(
                "gameplay manifest is bound to a different workflow execution attempt"
            )
        expected_binding = {
            "workflow_id": workflow.id,
            "task_id": source_task_id,
            "execution_id": selected_execution.id,
            "attempt_number": selected_execution.attempt_number,
            "scenario_sha256": request.scenario_sha256,
            "contract_sha256": request.contract_sha256,
            "source_sha256": params["source_manifest_sha256"],
            "executable_sha256": params["executable_sha256"],
            "harness_sha256": params["harness_sha256"],
            "request_file_sha256": params["request_file_sha256"],
            "harness_output_artifact_id": raw_output_artifact.id,
            "source_manifest_artifact_id": source_manifest_artifact.id,
            "source_manifest_path": source_manifest_artifact.relative_path,
        }
        if any(manifest.get(key) != value for key, value in expected_binding.items()):
            raise ValidationError(
                "gameplay manifest does not bind this workflow's deterministic contract"
            )
        if (
            raw_output.get("execution_id") != selected_execution.id
            or raw_output.get("seed") != manifest.get("seed")
            or raw_output.get("scenario_sha256") != request.scenario_sha256
            or raw_output.get("contract_sha256") != request.contract_sha256
            or raw_output.get("source_sha256") != params["source_manifest_sha256"]
            or raw_output.get("harness_sha256") != params["harness_sha256"]
            or raw_output.get("executable_sha256") != params["executable_sha256"]
            or raw_output.get("engine_identity_sha256") != manifest.get("engine_identity_sha256")
            or raw_output.get("execution_mode")
            != (
                "wall_clock_render_fixed_physics"
                if request.performance_budget is not None
                else "fixed_render_and_physics"
            )
            or raw_output.get("state") != observation.state
            or raw_output.get("state_observed_at") != observation.observed_at
            or f"Godot {raw_output.get('engine_version')}" != observation.engine
        ):
            raise ValidationError(
                "raw Godot output provenance or scalar state differs from persisted evidence"
            )
        if selected_execution.status != ExecutionStatus.COMPLETED:
            raise ArtifactError("gameplay output does not bind a completed execution attempt")
        expected_state_tick = next(
            action.tick for action in request.actions if action.action == "dump_state"
        )
        if (
            observation.scenario_sha256 != request.scenario_sha256
            or set(observation.state) - set(request.scenario.fields)
            or manifest.get("state_tick") != expected_state_tick
            or raw_output.get("state_tick") != expected_state_tick
            or raw_output.get("fixed_tick_hz") != request.fixed_tick_hz
        ):
            raise ValidationError("gameplay observation does not match the scenario field contract")
        screenshot_refs = manifest.get("screenshot_refs")
        screenshot_ids = manifest.get("screenshot_artifact_ids")
        if (
            not isinstance(screenshot_refs, list)
            or not isinstance(screenshot_ids, list)
            or len(screenshot_refs) != len(screenshot_ids)
        ):
            raise ValidationError("run manifest screenshot artifact references are malformed")
        selected_screenshots = [artifacts_by_id.get(item) for item in screenshot_ids]
        if any(item is None for item in selected_screenshots):
            raise ArtifactError("run manifest references an unregistered screenshot artifact")
        registered_screenshots: list[Artifact] = []
        for item in selected_screenshots:
            if item is None:
                raise ArtifactError("run manifest references an unregistered screenshot artifact")
            registered_screenshots.append(item)
        screenshot_artifacts = registered_screenshots
        if (
            any(item.artifact_type != "gameplay-visual-capture" for item in screenshot_artifacts)
            or [item.relative_path for item in screenshot_artifacts] != screenshot_refs
            or screenshot_refs
            != [
                f"{attempt_root}/screenshot-{index:03d}.png"
                for index in range(len(screenshot_refs))
            ]
        ):
            raise ArtifactError(
                "run manifest screenshot ids do not identify its exact capture paths"
            )
        expected_refs = (
            *screenshot_refs,
            raw_output_artifact.relative_path,
            source_manifest_artifact.relative_path,
            manifest_artifact.relative_path,
        )
        if observation.evidence_refs != expected_refs:
            raise ValidationError(
                "gameplay observation references do not match registered captures"
            )
        for linked in screenshot_artifacts:
            self.manager.verify_artifact_integrity(linked)
            capture = _bounded_regular_bytes(
                self.root / linked.relative_path, self.root, _MAX_SCREENSHOT_BYTES
            )
            if hashlib.sha256(capture).hexdigest() != linked.content_hash:
                raise ArtifactError(
                    "gameplay screenshot changed after artifact integrity verification"
                )
        _validate_source_manifest(source_manifest, params["source_manifest_sha256"])
        reports = [evaluate_gameplay(request.scenario, observation)]
        budget = request.performance_budget
        sample_rows = raw_output.get("samples")
        if not isinstance(sample_rows, list) or len(sample_rows) > 9:
            raise ValidationError("raw harness output metric samples are missing or over the limit")
        if budget is not None:
            if performance_artifact is None:
                if sample_rows:
                    raise ArtifactError(
                        "raw harness metrics exist but the selected manifest has no performance artifact"
                    )
                findings = tuple(
                    QualityFinding(
                        metric.metric,
                        "FAIL",
                        None,
                        metric.to_dict(),
                        manifest_artifact.content_hash,
                        (manifest_artifact.relative_path,),
                        "Required performance sample was unavailable; no value was fabricated.",
                    )
                    for metric in budget.metrics
                )
                reports.append(QualityReport(f"performance:{budget.platform}", findings))
            else:
                performance = PerformanceEvidence.from_dict(
                    self._load_json_artifact(performance_artifact)
                )
                if not sample_rows:
                    raise ValidationError(
                        "persisted performance evidence has no matching raw harness samples"
                    )
                raw_performance = PerformanceEvidence.from_dict(
                    {
                        "schema_version": "performance-evidence-1.0.0",
                        "platform": manifest.get("platform"),
                        "budget_sha256": budget.sha256,
                        "engine": f"Godot {raw_output.get('engine_version')}",
                        "collection_method": COLLECTION_METHOD,
                        "started_at": raw_output.get("started_at"),
                        "ended_at": raw_output.get("ended_at"),
                        "sample_interval_ms": raw_output.get("sample_interval_ms"),
                        "scenario_sha256": request.scenario_sha256,
                        "samples": sample_rows,
                        "evidence_refs": observation.evidence_refs,
                    }
                )
                if (
                    manifest.get("platform") != _platform_id()
                    or raw_performance.to_dict() != performance.to_dict()
                    or not _performance_samples_match_raw(performance, raw_output)
                    or performance.budget_sha256 != budget.sha256
                ):
                    raise ValidationError(
                        "persisted performance evidence differs from raw samples, window, platform, or budget"
                    )
                reports.append(evaluate_performance(budget, performance))
        elif performance_artifact is not None or sample_rows:
            raise ValidationError(
                "performance samples were produced for a task without a performance budget"
            )
        findings = tuple(f for report in reports for f in report.findings)
        report = QualityReport(f"gameplay-quality:{request.scenario.scenario_id}", findings)
        report_path = (
            Path(".gamefactory")
            / "artifacts"
            / workflow.id
            / task.id
            / f"evaluation-attempt-{execution.attempt_number}"
            / execution.id
            / "quality-report.json"
        ).as_posix()
        report_artifact = self._save_json(
            workflow.id, task.id, "gameplay-quality-report", report_path, report.to_dict()
        )
        self.evidence.save(
            Evidence(
                id=generate_id("EVI"),
                task_id=task.id,
                execution_id=execution.id,
                evidence_type="gameplay-quality-report",
                summary=f"Gameplay deterministic quality evaluation: {report.status}",
                raw_data={
                    "artifact_id": report_artifact.id,
                    "report": report.to_dict(),
                    "source_execution_id": selected_execution.id,
                    "source_attempt_number": selected_execution.attempt_number,
                    "evaluator_execution_id": execution.id,
                    "run_manifest_artifact_id": manifest_artifact.id,
                },
            )
        )
        gate = QualityGate(
            id=generate_id("GATE"),
            task_id=task.id,
            gate_type="gameplay-quality",
            status=GateStatus.PASSED if report.status == "PASS" else GateStatus.FAILED,
            evaluated_at=utc_now_iso(),
            reason=f"Deterministic gameplay/performance report status: {report.status}",
        )
        self.gates.save(gate)
        if report.status != "PASS":
            raise ToolExecutionError(
                f"Gameplay quality requirements failed; report retained at {report_path}"
            )
        return TaskHandlerResult(
            1, "Gameplay assertions and configured performance budgets passed", [report_artifact.id]
        )


def register_gameplay_quality_handlers(
    registry: TaskHandlerRegistry,
    project_root: Path | str,
    artifact_manager: ArtifactManager,
    artifacts: ArtifactRepository,
    executions: ExecutionRepository,
    evidence: EvidenceRepository,
    gates: QualityGateRepository,
    process_runner: ProcessRunner | Any | None = None,
) -> GameplayQualityHandlers:
    """Register public execute/evaluate handlers for a WorkflowEngine integration."""
    handlers = GameplayQualityHandlers(
        project_root, artifact_manager, artifacts, executions, evidence, gates, process_runner
    )
    handlers.register(registry)
    return handlers
