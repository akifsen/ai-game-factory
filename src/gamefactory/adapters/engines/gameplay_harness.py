"""Real, attempt-scoped execution for the optional development Godot harness."""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform as host_platform
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_execution import _read_bounded_regular_file
from gamefactory.adapters.engines.godot_image import decode_png
from gamefactory.adapters.engines.godot_staging import GodotStager, _is_reparse, sha256_file
from gamefactory.core.domain.errors import ToolExecutionError, ValidationError
from gamefactory.core.domain.game_quality import GameplayObservation, PerformanceEvidence
from gamefactory.core.domain.gameplay_harness import (
    GAMEPLAY_HARNESS_OUTPUT_VERSION,
    GameplayHarnessRequest,
)
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner

HARNESS_VERSION = "1.0.0"
COLLECTION_METHOD = "godot-performance-monitor-latency-sampled"
_ENGINE_IDENTITY_RE = re.compile(
    r"^(?:Godot Engine )?(?P<version>[0-9]+\.[0-9]+(?:\.[0-9]+)?[^\r\n]*)$"
)
_MAX_OUTPUT = 8 * 1024 * 1024
_MAX_SCREENSHOTS = 16
_MAX_SCREENSHOT_BYTES = 4 * 1024 * 1024
_MAX_REQUEST_BYTES = 2_000_000
_MAX_TIMEOUT_SECONDS = 900.0


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _harness_bytes() -> bytes:
    return files("gamefactory").joinpath("resources/godot/gameplay_harness.gd").read_bytes()


def _write_exclusive(path: Path, raw: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def _reject_reparse_components(path: Path) -> None:
    absolute = path.absolute()
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current = current / component
        if _is_reparse(current):
            raise ValidationError("project and executable paths cannot traverse a link or junction")


def _read_contract_source(path: Path, expected_sha256: str) -> GameplayHarnessRequest:
    try:
        before = path.stat()
        if not before.st_size or before.st_size > _MAX_REQUEST_BYTES:
            raise ValidationError("gameplay request source is empty or exceeds its byte limit")
        with path.open("rb") as stream:
            raw = stream.read(_MAX_REQUEST_BYTES + 1)
            after = os.fstat(stream.fileno())
        if len(raw) > _MAX_REQUEST_BYTES or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        ) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValidationError("gameplay request source changed during bounded read")
        if hashlib.sha256(raw).hexdigest() != expected_sha256:
            raise ValidationError("gameplay request source changed after workflow registration")
        value = json.loads(raw)
    except ValidationError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValidationError("gameplay request source is unavailable or invalid") from exc
    return GameplayHarnessRequest.from_dict(value)


def _isolated_env(stage: Path, *, rendered: bool) -> dict[str, str]:
    keys = (
        {
            "DISPLAY",
            "WAYLAND_DISPLAY",
            "XAUTHORITY",
            "XDG_RUNTIME_DIR",
            "GDK_BACKEND",
            "QT_QPA_PLATFORM",
        }
        if rendered
        else set()
    )
    environment = {key: value for key, value in os.environ.items() if key in keys and value}
    app_data = stage / ".godot-appdata"
    local_app_data = stage / ".godot-local-appdata"
    app_data.mkdir(exist_ok=True)
    local_app_data.mkdir(exist_ok=True)
    environment.update({"APPDATA": str(app_data), "LOCALAPPDATA": str(local_app_data)})
    return environment


def _runtime_platform() -> str:
    value = host_platform.system().lower()
    return "windows" if value == "windows" else value


def _set_staged_viewport(project_file: Path, width: int, height: int) -> None:
    text = project_file.read_text(encoding="utf-8")
    pattern = re.compile(r"(?ms)^\[display\]\s*\n(.*?)(?=^\[|\Z)")
    match = pattern.search(text)
    settings = {
        "window/size/viewport_width": str(width),
        "window/size/viewport_height": str(height),
    }
    if match:
        block = match.group(1)
        for key, value in settings.items():
            block = re.sub(rf"(?m)^{re.escape(key)}\s*=.*$", "", block)
            block += f"{key}={value}\n"
        text = text[: match.start(1)] + block + text[match.end(1) :]
    else:
        text += "\n[display]\n" + "".join(f"{key}={value}\n" for key, value in settings.items())
    project_file.write_text(text, encoding="utf-8", newline="\n")


@dataclass(frozen=True)
class GameplayHarnessRun:
    execution_id: str
    attempt_number: int
    engine: str
    executable_sha256: str
    engine_version: str
    source_sha256: str
    source_manifest: tuple[dict[str, Any], ...]
    scenario_sha256: str
    contract_sha256: str
    harness_sha256: str
    observation: GameplayObservation
    performance: PerformanceEvidence | None
    screenshot_paths: tuple[Path, ...]
    screenshot_sha256: tuple[str, ...]
    receipt_paths: tuple[Path, ...]
    output_path: Path
    started_at: str
    ended_at: str

    @property
    def engine_identity_sha256(self) -> str:
        return hashlib.sha256(f"{self.engine}\0{self.executable_sha256}".encode()).hexdigest()


class GodotGameplayHarness:
    """Run only versioned harness requests against isolated Godot staging copies."""

    def __init__(self, process_runner: ProcessRunner | Any | None = None) -> None:
        self.runner = process_runner or ProcessRunner(sanitize_output=True)

    def execute(
        self,
        project_root: Path | str,
        executable: Path | str,
        scenario_path: Path | str,
        request: GameplayHarnessRequest | dict[str, Any],
        workflow_id: str,
        task_id: str,
        attempt_number: int,
        *,
        timeout_seconds: float = 120.0,
        expected_execution_id: str,
        expected_request_sha256: str,
        expected_source_sha256: str,
        expected_executable_sha256: str,
        expected_harness_sha256: str,
        durable_receipt: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> GameplayHarnessRun:
        contract = (
            request
            if isinstance(request, GameplayHarnessRequest)
            else GameplayHarnessRequest.from_dict(request)
        )
        if not expected_execution_id or contract.execution_id not in {
            expected_execution_id,
            "attempt-placeholder",
        }:
            raise ValidationError(
                "harness execution id does not bind the claimed workflow execution"
            )
        contract_data = contract.to_dict()
        contract_data["execution_id"] = expected_execution_id
        contract = GameplayHarnessRequest.from_dict(contract_data)
        root_input, exe_input, scenario_input = (
            Path(project_root),
            Path(executable),
            Path(scenario_path),
        )
        for original in (root_input, exe_input, scenario_input):
            _reject_reparse_components(original)
        root = root_input.resolve(strict=True)
        exe = exe_input.resolve(strict=True)
        scenario_file = scenario_input.resolve(strict=True)
        try:
            scenario_file.relative_to(root)
        except ValueError as exc:
            raise ValidationError("scenario contract source must be inside the project") from exc
        file_contract = _read_contract_source(scenario_file, expected_request_sha256)
        if (
            file_contract.execution_id not in {expected_execution_id, "attempt-placeholder"}
            or file_contract.contract_sha256 != contract.contract_sha256
            or file_contract.scenario_sha256 != contract.scenario_sha256
        ):
            raise ValidationError(
                "bounded scenario source does not match the approved request contract"
            )
        if not exe.is_file() or not scenario_file.is_file() or attempt_number < 1:
            raise ValidationError("executable, scenario source, or attempt number is invalid")
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or not 1.0 <= timeout_seconds <= _MAX_TIMEOUT_SECONDS
        ):
            raise ValidationError(
                "harness phase timeout must be finite and between 1 and 900 seconds"
            )
        budget = contract.performance_budget
        if budget is not None and COLLECTION_METHOD not in budget.collection_methods:
            raise ValidationError(
                f"performance budget does not allow collection method {COLLECTION_METHOD!r}"
            )
        if budget is not None and budget.platform != _runtime_platform():
            raise ValidationError(
                f"performance budget platform {budget.platform!r} does not match this runtime ({_runtime_platform()!r})"
            )
        harness = _harness_bytes()
        harness_hash = hashlib.sha256(harness).hexdigest()
        executable_hash = sha256_file(exe)
        stager = GodotStager(root, root / ".gamefactory" / "scratch")
        _, source_hash = stager.source_manifest(scenario_file)
        if source_hash != expected_source_sha256:
            raise ValidationError("Godot source snapshot changed after workflow registration")
        if executable_hash != expected_executable_sha256:
            raise ValidationError("Godot executable changed after workflow registration")
        if harness_hash != expected_harness_sha256:
            raise ValidationError("Packaged gameplay harness changed after workflow registration")
        stage, manifest, source_hash = stager.create_stage(
            workflow_id, contract.execution_id, scenario_file, expected_source_sha256, harness
        )
        scratch = stage.parent
        if contract.viewport is not None:
            _set_staged_viewport(stage / "project.godot", *contract.viewport)
        request_path = scratch / "harness-request.json"
        output_path = scratch / "harness-output.json"
        if output_path.exists():
            raise ValidationError("harness output already exists for this attempt")

        receipt_paths: list[Path] = []

        def run_phase(phase: str, args: list[str], *, rendered: bool = False) -> CommandResult:
            index = len(receipt_paths)
            intent = {
                "schema_version": "gameplay-process-receipt-1.0.0",
                "workflow_id": workflow_id,
                "task_id": task_id,
                "execution_id": contract.execution_id,
                "attempt_number": attempt_number,
                "phase": f"{phase}-intent",
                "scenario_sha256": contract.scenario_sha256,
                "contract_sha256": contract.contract_sha256,
                "source_sha256": source_hash,
                "executable_sha256": executable_hash,
                "harness_sha256": harness_hash,
                "engine": "godot",
                "created_at": _now(),
                "process": {"args": args, "pid": None},
            }
            intent_path = scratch / f"{index:02d}-{phase}-intent.json"
            _write_exclusive(intent_path, json.dumps(intent, sort_keys=True, indent=2).encode())
            receipt_paths.append(intent_path)
            if durable_receipt:
                durable_receipt(phase + "-intent", intent)
            try:
                if sha256_file(exe) != expected_executable_sha256:
                    raise ValidationError("Godot executable changed before process launch")
                result = self.runner.run(
                    CommandRequest(
                        args=args,
                        cwd=stage,
                        timeout_seconds=timeout_seconds,
                        env_overrides=_isolated_env(stage, rendered=rendered),
                        minimal_env=True,
                        structured_json_output=True,
                    )
                )
                process = result.to_dict()
                process["stdout"] = "[OMITTED]"
                process["stderr"] = "[OMITTED]"
                terminal = {
                    **intent,
                    "phase": f"{phase}-terminal",
                    "completed_at": _now(),
                    "process": process,
                }
                terminal_path = scratch / f"{index:02d}-{phase}-terminal.json"
                _write_exclusive(
                    terminal_path, json.dumps(terminal, sort_keys=True, indent=2).encode()
                )
                receipt_paths.append(terminal_path)
                if durable_receipt:
                    durable_receipt(phase + "-terminal", terminal)
                return result
            except Exception as exc:
                details = getattr(exc, "details", {})
                allowed_details = {
                    "pid",
                    "exit_code",
                    "timed_out",
                    "cleanup_completed",
                    "cleanup_status",
                    "termination_status",
                    "started_at",
                    "completed_at",
                    "duration",
                    "duration_seconds",
                    "capture_completed",
                    "stdout_truncated",
                    "stderr_truncated",
                    "command",
                    "executable",
                }
                process = (
                    {key: value for key, value in details.items() if key in allowed_details}
                    if isinstance(details, dict)
                    else {}
                )
                process["error_type"] = type(exc).__name__
                terminal = {
                    **intent,
                    "phase": f"{phase}-terminal",
                    "completed_at": _now(),
                    "process": process,
                }
                terminal_path = scratch / f"{index:02d}-{phase}-terminal.json"
                _write_exclusive(
                    terminal_path, json.dumps(terminal, sort_keys=True, indent=2).encode()
                )
                receipt_paths.append(terminal_path)
                if durable_receipt:
                    durable_receipt(phase + "-terminal", terminal)
                raise

        started = _now()
        version_result = run_phase("version", [str(exe), "--version"])
        version_text = (
            (version_result.protocol_stdout or version_result.stdout).strip().splitlines()
        )
        if version_result.exit_code != 0 or not version_text:
            raise ToolExecutionError("Godot version probe did not return an engine identity")
        match = _ENGINE_IDENTITY_RE.fullmatch(version_text[0])
        if not match:
            raise ValidationError("Godot version output has an unsupported identity")
        engine_version = match.group("version").strip()
        build_suffix = engine_version.rsplit(".", 1)
        if len(build_suffix) == 2 and len(build_suffix[1]) == 9:
            try:
                int(build_suffix[1], 16)
            except ValueError:
                pass
            else:
                engine_version = build_suffix[0]
        if not engine_version.startswith("4."):
            raise ValidationError("gameplay harness currently supports Godot 4.x only")
        request_value = contract.to_dict()
        request_value.update(
            {
                "contract_sha256": contract.contract_sha256,
                "source_sha256": source_hash,
                "harness_sha256": harness_hash,
                "executable_sha256": executable_hash,
                "engine_identity_sha256": hashlib.sha256(
                    f"Godot {engine_version}\0{executable_hash}".encode()
                ).hexdigest(),
            }
        )
        _write_exclusive(
            request_path,
            json.dumps(
                request_value,
                sort_keys=True,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8"),
        )
        import_result = run_phase(
            "import", [str(exe), "--headless", "--path", str(stage), "--import"]
        )
        if import_result.exit_code != 0:
            raise ToolExecutionError(
                "Godot gameplay project import failed", exit_code=import_result.exit_code
            )
        has_capture = any(action.action == "capture_screenshot" for action in contract.actions)
        requires_renderer = has_capture or "draw_calls" in contract.metrics
        if requires_renderer and sys.platform not in {"win32", "linux"}:
            raise ValidationError(
                "rendered gameplay harness runs are supported on Windows and Linux only"
            )
        if (
            requires_renderer
            and sys.platform == "linux"
            and not any(os.environ.get(key) for key in ("DISPLAY", "WAYLAND_DISPLAY"))
        ):
            raise ValidationError(
                "rendered gameplay harness run requires an active Linux display server"
            )
        runtime_args = [str(exe)]
        if not requires_renderer:
            runtime_args.append("--headless")
        # Performance budgets require actual renderer pacing. Physics ticks remain
        # fixed inside the harness, while Godot's wall-clock frame pacing is real.
        execution_mode = (
            "wall_clock_render_fixed_physics" if budget is not None else "fixed_render_and_physics"
        )
        runtime_args += ["--path", str(stage)]
        if budget is None:
            runtime_args += ["--fixed-fps", str(contract.fixed_tick_hz)]
        runtime_args += [
            "--script",
            str(stage / ".factory-harness.gd"),
            "--",
            "--request",
            str(request_path),
            "--output",
            str(output_path),
        ]
        runtime_result = run_phase("runtime", runtime_args, rendered=requires_renderer)
        ended = _now()
        if runtime_result.exit_code != 0:
            raise ToolExecutionError(
                "Godot gameplay harness process failed", exit_code=runtime_result.exit_code
            )
        output_raw = _read_bounded_regular_file(output_path, scratch, _MAX_OUTPUT)
        try:
            output = json.loads(output_raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationError("Godot gameplay output is not valid JSON") from exc
        required_output = {
            "schema_version",
            "execution_id",
            "seed",
            "fixed_tick_hz",
            "final_tick",
            "scenario_sha256",
            "contract_sha256",
            "source_sha256",
            "harness_sha256",
            "executable_sha256",
            "engine_identity_sha256",
            "state",
            "state_tick",
            "engine_version",
            "started_at",
            "ended_at",
            "sample_interval_ms",
            "samples",
            "screenshots",
            "exit_status",
            "state_observed_at",
            "execution_mode",
        }
        if (
            not isinstance(output, dict)
            or set(output) != required_output
            or output.get("schema_version") != GAMEPLAY_HARNESS_OUTPUT_VERSION
        ):
            raise ValidationError("Godot gameplay output is missing the supported version envelope")
        identity_hash = hashlib.sha256(
            f"Godot {engine_version}\0{executable_hash}".encode()
        ).hexdigest()
        expected = {
            "execution_id": contract.execution_id,
            "seed": contract.seed,
            "fixed_tick_hz": contract.fixed_tick_hz,
            "scenario_sha256": contract.scenario_sha256,
            "contract_sha256": contract.contract_sha256,
            "source_sha256": source_hash,
            "harness_sha256": harness_hash,
            "executable_sha256": executable_hash,
            "engine_identity_sha256": identity_hash,
        }
        for key, value in expected.items():
            if output.get(key) != value:
                raise ValidationError(
                    f"Godot gameplay output {key} does not match the approved request"
                )
        if type(output.get("final_tick")) is not int or output["final_tick"] > contract.max_ticks:
            raise ValidationError(
                "Godot gameplay output tick count is missing or exceeds the bound"
            )
        expected_state_tick = next(
            action.tick for action in contract.actions if action.action == "dump_state"
        )
        if output.get("state_tick") != expected_state_tick:
            raise ValidationError("Godot state output was not captured at the selected fixed tick")
        if output.get("exit_status") != 0:
            raise ValidationError("Godot gameplay harness reported a failed execution")
        if output.get("engine_version") != engine_version:
            raise ValidationError("Godot output engine identity changed during execution")
        if output.get("execution_mode") != execution_mode:
            raise ValidationError(
                "Godot execution mode does not match the requested quality evidence"
            )
        observation_data: dict[str, Any] = {
            "schema_version": "gameplay-observation-1.0.0",
            "scenario_sha256": contract.scenario_sha256,
            "engine": f"Godot {engine_version}",
            "observed_at": output.get("state_observed_at"),
            "state": output.get("state"),
            "evidence_refs": [],
        }
        observation = GameplayObservation.from_dict(observation_data)
        if set(observation.state) != {binding.field for binding in contract.property_bindings}:
            raise ValidationError("Godot state output is missing or adding bound properties")

        screenshots: list[Path] = []
        screenshot_hashes: list[str] = []
        reported = output.get("screenshots")
        if not isinstance(reported, list) or len(reported) > _MAX_SCREENSHOTS:
            raise ValidationError("Godot screenshot output is not a bounded list")
        requested_captures = sum(
            action.action == "capture_screenshot" for action in contract.actions
        )
        if len(reported) != requested_captures:
            raise ValidationError("Godot screenshot output count does not match the request")
        for name in reported:
            if not isinstance(name, str) or Path(name).name != name or not name.endswith(".png"):
                raise ValidationError("screenshot output path is invalid")
            path = scratch / name
            raw = _read_bounded_regular_file(path, scratch, _MAX_SCREENSHOT_BYTES)
            width, height = contract.viewport or (0, 0)
            decoded = decode_png(raw, width, height)
            image = decoded.image.convert("L")
            histogram = image.histogram()
            if (
                len([count for count in histogram if count > 0]) < 2
                or max(histogram, default=0) >= sum(histogram) - 1
            ):
                raise ValidationError("viewport screenshot appears blank or uniform")
            screenshots.append(path)
            screenshot_hashes.append(hashlib.sha256(raw).hexdigest())

        performance = None
        sample_rows = output.get("samples")
        if not isinstance(sample_rows, list) or len(sample_rows) > 9:
            raise ValidationError("Godot metric output must be a bounded array")
        if sample_rows:
            if budget is None:
                raise ValidationError(
                    "Godot emitted metrics without a versioned performance budget"
                )
            performance = PerformanceEvidence.from_dict(
                {
                    "schema_version": "performance-evidence-1.0.0",
                    "platform": _runtime_platform(),
                    "budget_sha256": budget.sha256,
                    "engine": f"Godot {engine_version}",
                    "collection_method": COLLECTION_METHOD,
                    "started_at": output.get("started_at"),
                    "ended_at": output.get("ended_at"),
                    "sample_interval_ms": output.get("sample_interval_ms"),
                    "scenario_sha256": contract.scenario_sha256,
                    "samples": sample_rows,
                    "evidence_refs": [],
                }
            )
            if {sample.metric for sample in performance.samples} - set(contract.metrics):
                raise ValidationError("Godot returned an unrequested metric")

        return GameplayHarnessRun(
            contract.execution_id,
            attempt_number,
            f"Godot {engine_version}",
            executable_hash,
            engine_version,
            source_hash,
            tuple(item.__dict__ for item in manifest),
            contract.scenario_sha256,
            contract.contract_sha256,
            harness_hash,
            observation,
            performance,
            tuple(screenshots),
            tuple(screenshot_hashes),
            tuple(receipt_paths),
            output_path,
            started,
            ended,
        )


def execute_gameplay_harness(*args: Any, **kwargs: Any) -> GameplayHarnessRun:
    """Public adapter entry point for workflow/integration workers."""
    return GodotGameplayHarness(kwargs.pop("process_runner", None)).execute(*args, **kwargs)
