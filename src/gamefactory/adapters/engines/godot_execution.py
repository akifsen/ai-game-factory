"""Separate, bounded Godot import and harness processes for one execution attempt."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from gamefactory.adapters.engines.godot_capture_contracts import (
    CaptureScenario,
    capture_fingerprint,
    make_capture_request,
)
from gamefactory.adapters.engines.godot_contracts import (
    Scenario,
    make_runtime_request,
    scenario_fingerprint,
)
from gamefactory.adapters.engines.godot_staging import _is_reparse
from gamefactory.core.domain.errors import ToolExecutionError, ValidationError
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

_ENGINE_ERROR_PATTERNS = (
    re.compile(r"(?m)^SCRIPT ERROR:"),
    re.compile(r"(?m)^Parse Error:"),
    re.compile(r"(?m)^ERROR: Failed(?:\b|\s)"),
)


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _binary_read_flags() -> int:
    """Windows os.open is text mode unless O_BINARY is set, which stops at 0x1A."""
    return os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _display_driver() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform.startswith("linux"):
        return "x11"
    raise ValidationError(f"rendered capture is not supported on {sys.platform}")


_RENDER_LOG = re.compile(
    r"(?im)^.*(?:opengl|vulkan|d3d|compatibility|using device|dummy|headless).*$"
)


def _renderer_log(text: str) -> str:
    return "\n".join(_RENDER_LOG.findall(text)[:20])[:4000]


def _read_bounded_regular_file(path: Path, root: Path, limit: int) -> bytes:
    if _is_reparse(path):
        raise ValidationError("capture output cannot be a symlink or reparse point")
    try:
        path.resolve(strict=True).relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise ValidationError("capture output escapes its scratch directory") from exc
    if not path.is_file():
        raise ToolExecutionError("expected capture output is missing", details={"path": path.name})
    descriptor = os.open(path, _binary_read_flags())
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise ValidationError("capture output must be a regular file")
        chunks = bytearray()
        while len(chunks) <= limit:
            part = os.read(descriptor, min(64 * 1024, limit + 1 - len(chunks)))
            if not part:
                break
            chunks.extend(part)
        if len(chunks) > limit:
            raise ValidationError("capture output exceeds its byte limit")
        return bytes(chunks)
    finally:
        os.close(descriptor)


def _write_exclusive(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())


@dataclass
class GodotRun:
    observation: dict[str, Any] | None
    artifacts: dict[str, bytes]
    executable: str
    version: str
    source_hash: str
    harness_hash: str
    images: dict[str, bytes] | None = None
    requested_renderer: dict[str, str] | None = None
    process_renderer_log: str = ""


class GodotExecutor:
    def __init__(self, runner: ProcessRunner | Any | None = None) -> None:
        self.runner = runner or ProcessRunner(sanitize_output=True)

    def run(
        self,
        executable: Path | str,
        stage: Path,
        scratch: Path,
        scenario: Scenario,
        execution_id: str,
        scenario_hash: str,
        source_hash: str,
        harness_hash: str,
        import_timeout: float,
        runtime_timeout: float,
        durable_intent: Any | None = None,
    ) -> GodotRun:
        exe = Path(executable).resolve(strict=True)
        artifacts: dict[str, bytes] = {}
        artifacts["runtime-request.json"] = json.dumps(
            make_runtime_request(scenario, execution_id),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request_path = scratch / "runtime-request.json"
        observation_path = scratch / "runtime-observation.json"
        _write_exclusive(request_path, artifacts["runtime-request.json"])
        if observation_path.exists():
            raise ValidationError("Current attempt already contains a runtime observation")

        env = self._isolated_environment(scratch)
        if scenario_fingerprint(scenario) != scenario_hash:
            raise ValidationError(
                "Godot scenario changed after its approval fingerprint was computed"
            )
        version = self._run_phase(
            "version", [str(exe), "--version"], stage, scratch, 10.0, env, durable_intent
        )
        artifacts.update(version["artifacts"])
        if not version["success"] or not version["stdout"].strip():
            raise ToolExecutionError(
                "Godot version probe failed after approval",
                details=self._failure_details(version, "version"),
            )
        help_result = self._run_phase(
            "help", [str(exe), "--help"], stage, scratch, 10.0, env, durable_intent
        )
        artifacts.update(help_result["artifacts"])
        if not help_result["success"]:
            raise ToolExecutionError(
                "Godot CLI capability probe failed after approval",
                details=self._failure_details(help_result, "help"),
            )
        help_text = help_result["stdout"] + "\n" + help_result["stderr"]
        unsupported = [
            flag
            for flag in ("--headless", "--path", "--import", "--script")
            if flag not in help_text
        ]
        if unsupported:
            raise ValidationError(
                f"Installed Godot does not advertise required flags: {unsupported}"
            )
        version_string = version["stdout"].strip().splitlines()[0]
        imports = self._run_phase(
            "import",
            [str(exe), "--headless", "--path", str(stage), "--import"],
            stage,
            scratch,
            import_timeout,
            env,
            durable_intent,
        )
        artifacts.update(imports["artifacts"])
        if not imports["success"]:
            raise ToolExecutionError(
                "Godot project import failed",
                exit_code=imports["metadata"].get("exit_code"),
                stderr=imports["stderr"],
                details=self._failure_details(imports, "import"),
            )

        runtime_args = [
            str(exe),
            "--headless",
            "--path",
            str(stage),
            "--script",
            str(stage / ".factory-harness.gd"),
            "--",
            "--request",
            str(request_path),
            "--output",
            str(observation_path),
        ]
        runtime = self._run_phase(
            "runtime", runtime_args, stage, scratch, runtime_timeout, env, durable_intent
        )
        artifacts.update(runtime["artifacts"])
        if not runtime["success"]:
            raise ToolExecutionError(
                "Godot scenario process failed",
                exit_code=runtime["metadata"].get("exit_code"),
                stderr=runtime["stderr"],
                details=self._failure_details(runtime, "runtime"),
            )
        if _is_reparse(observation_path):
            raise ValidationError("Godot observation cannot be a symlink or reparse point")
        try:
            observation_path.resolve(strict=True).relative_to(scratch.resolve(strict=True))
        except (OSError, ValueError) as exc:
            raise ValidationError(
                "Godot observation path escapes the current execution scratch"
            ) from exc
        if not observation_path.is_file():
            raise ToolExecutionError(
                "Godot process exited successfully without a complete observation report",
                exit_code=runtime["metadata"].get("exit_code"),
                details={"godot_phase": "runtime"},
            )
        descriptor = os.open(observation_path, _binary_read_flags())
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValidationError("Godot observation must be a regular file")
            chunks = bytearray()
            while len(chunks) <= 1024 * 1024:
                part = os.read(descriptor, min(64 * 1024, 1024 * 1024 + 1 - len(chunks)))
                if not part:
                    break
                chunks.extend(part)
            if len(chunks) > 1024 * 1024:
                raise ValidationError("Godot observation exceeds the 1 MiB report limit")
            raw = bytes(chunks)
        finally:
            os.close(descriptor)
        artifacts["runtime-observation.json"] = raw
        try:
            report = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=self._unique_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(f"Godot observation is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(report, dict):
            raise ValidationError("Godot observation root must be a JSON object")
        return GodotRun(
            observation=report,
            artifacts=artifacts,
            executable=str(exe),
            version=version_string,
            source_hash=source_hash,
            harness_hash=harness_hash,
        )

    def run_capture(
        self,
        executable: Path | str,
        stage: Path,
        scratch: Path,
        scenario: CaptureScenario,
        execution_id: str,
        scenario_hash: str,
        source_hash: str,
        harness_hash: str,
        import_timeout: float,
        runtime_timeout: float,
        durable_intent: Any | None = None,
    ) -> GodotRun:
        """Import headless, then capture from a real Compatibility renderer."""
        exe = Path(executable).resolve(strict=True)
        if capture_fingerprint(scenario) != scenario_hash:
            raise ValidationError(
                "Godot capture scenario changed after its approval fingerprint was computed"
            )
        display = _display_driver()
        env = self._isolated_environment(scratch)
        if sys.platform.startswith("linux"):
            display_value = os.environ.get("DISPLAY")
            if not display_value:
                raise ValidationError(
                    "Linux rendered capture requires DISPLAY; headless fallback is not a capture"
                )
            env["DISPLAY"] = display_value
            authority = os.environ.get("XAUTHORITY")
            if authority:
                env["XAUTHORITY"] = authority
            software = os.environ.get("LIBGL_ALWAYS_SOFTWARE")
            if software:
                env["LIBGL_ALWAYS_SOFTWARE"] = software
        artifacts: dict[str, bytes] = {}
        version = self._run_phase(
            "version", [str(exe), "--version"], stage, scratch, 10.0, env, durable_intent
        )
        artifacts.update(version["artifacts"])
        if not version["success"] or not version["stdout"].strip():
            raise ToolExecutionError(
                "Godot version probe failed after approval",
                details=self._failure_details(version, "version"),
            )
        help_result = self._run_phase(
            "help", [str(exe), "--help"], stage, scratch, 10.0, env, durable_intent
        )
        artifacts.update(help_result["artifacts"])
        if not help_result["success"]:
            raise ToolExecutionError(
                "Godot CLI capability probe failed after approval",
                details=self._failure_details(help_result, "help"),
            )
        help_text = help_result["stdout"] + "\n" + help_result["stderr"]
        required_flags = (
            "--headless",
            "--path",
            "--import",
            "--script",
            "--display-driver",
            "--rendering-driver",
            "--rendering-method",
            "--windowed",
            "--resolution",
        )
        unsupported = [flag for flag in required_flags if flag not in help_text]
        if unsupported:
            raise ValidationError(
                f"Installed Godot does not advertise required flags: {unsupported}"
            )
        version_string = version["stdout"].strip().splitlines()[0]
        imports = self._run_phase(
            "import",
            [str(exe), "--headless", "--path", str(stage), "--import"],
            stage,
            scratch,
            import_timeout,
            env,
            durable_intent,
        )
        artifacts.update(imports["artifacts"])
        if not imports["success"]:
            raise ToolExecutionError(
                "Godot project import failed",
                exit_code=imports["metadata"].get("exit_code"),
                stderr=imports["stderr"],
                details=self._failure_details(imports, "import"),
            )
        capture_dir = scratch / "captures"
        capture_dir.mkdir(parents=True, exist_ok=False)
        request = make_capture_request(scenario, execution_id, str(capture_dir.resolve()))
        artifacts["runtime-request.json"] = json.dumps(
            request,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        request_path = scratch / "runtime-request.json"
        observation_path = scratch / "runtime-observation.json"
        _write_exclusive(request_path, artifacts["runtime-request.json"])
        requested = {
            "rendering_method": "gl_compatibility",
            "rendering_driver": "opengl3",
            "display_driver": display,
            "audio_driver": "Dummy",
            "window_mode": "windowed",
            "resolution": f"{scenario.viewport.width}x{scenario.viewport.height}",
            "software_rendering_requested": os.environ.get("LIBGL_ALWAYS_SOFTWARE", ""),
        }
        runtime_args = [
            str(exe),
            "--path",
            str(stage),
            "--display-driver",
            display,
            "--rendering-driver",
            "opengl3",
            "--rendering-method",
            "gl_compatibility",
            "--audio-driver",
            "Dummy",
            "--windowed",
            "--resolution",
            requested["resolution"],
            "--position",
            "40,40",
            "--script",
            str(stage / ".factory-harness.gd"),
            "--",
            "--request",
            str(request_path),
            "--output",
            str(observation_path),
            "--captures",
            str(capture_dir.resolve()),
        ]
        runtime = self._run_phase(
            "runtime", runtime_args, stage, scratch, runtime_timeout, env, durable_intent
        )
        artifacts.update(runtime["artifacts"])
        log = _renderer_log(runtime["stdout"] + "\n" + runtime["stderr"])
        if not runtime["success"]:
            raise ToolExecutionError(
                "Godot rendered capture process failed",
                exit_code=runtime["metadata"].get("exit_code"),
                stderr=runtime["stderr"],
                details=self._failure_details(runtime, "runtime"),
            )
        raw = _read_bounded_regular_file(observation_path, scratch, 1024 * 1024)
        artifacts["runtime-observation.json"] = raw
        try:
            report = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=self._unique_json_object,
                parse_constant=lambda value: (_ for _ in ()).throw(
                    ValueError(f"invalid JSON constant: {value}")
                ),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise ValidationError(f"Godot observation is not valid UTF-8 JSON: {exc}") from exc
        if not isinstance(report, dict):
            raise ValidationError("Godot observation root must be a JSON object")
        renderer = report.get("renderer")
        if not isinstance(renderer, dict) or renderer.get("rendering_method") != "gl_compatibility":
            raise ToolExecutionError(
                "Capture did not report a gl_compatibility renderer",
                details={"renderer": renderer, "process_log": log},
            )
        if (
            renderer.get("rendering_driver") == "dummy"
            or renderer.get("display_server") == "headless"
        ):
            raise ToolExecutionError(
                "Headless or dummy rendering is not a successful capture",
                details={"renderer": renderer, "process_log": log},
            )
        images: dict[str, bytes] = {}
        expected = [point.id + ".png" for point in scenario.captures]
        found = sorted(path.name for path in capture_dir.iterdir() if path.is_file())
        if found != sorted(expected):
            raise ToolExecutionError(
                "Capture process did not publish exactly the requested PNG files",
                details={"expected": expected, "found": found},
            )
        for name in expected:
            images[name] = _read_bounded_regular_file(
                capture_dir / name, capture_dir, 4 * 1024 * 1024
            )
            if not images[name].startswith(b"\x89PNG\r\n\x1a\n"):
                raise ToolExecutionError(f"capture file {name} is not a PNG")
        return GodotRun(
            observation=report,
            artifacts=artifacts,
            executable=str(exe),
            version=version_string,
            source_hash=source_hash,
            harness_hash=harness_hash,
            images=images,
            requested_renderer=requested,
            process_renderer_log=log,
        )

    @staticmethod
    def _isolated_environment(scratch: Path) -> dict[str, str]:
        home = scratch / "isolated-user"
        temp = scratch / "temp"
        appdata = home / "AppData" / "Roaming"
        local = home / "AppData" / "Local"
        for path in (home, temp, appdata, local):
            path.mkdir(parents=True, exist_ok=True)
        return {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "APPDATA": str(appdata),
            "LOCALAPPDATA": str(local),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "TMP": str(temp),
            "TEMP": str(temp),
            "TMPDIR": str(temp),
        }

    @staticmethod
    def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON field: {key}")
            result[key] = value
        return result

    @staticmethod
    def _failure_details(result: dict[str, Any], phase: str) -> dict[str, Any]:
        metadata = result.get("metadata", {})
        return {
            "godot_phase": phase,
            "metadata_artifact": f"{phase}-process.json",
            "process_state_uncertain": bool(metadata.get("pid"))
            and not bool(metadata.get("cleanup_completed", False)),
        }

    def _run_phase(
        self,
        phase: str,
        args: list[str],
        cwd: Path,
        scratch: Path,
        timeout: float,
        env: dict[str, str],
        durable_intent: Any | None = None,
    ) -> dict[str, Any]:
        intent_path = scratch / f"{phase}-intent.json"
        receipt_path = scratch / f"{phase}-terminal.json"
        intent = {
            "phase": phase,
            "argv": args,
            "cwd": str(cwd),
            "started_intent_at": _now(),
            "timeout_seconds": timeout,
        }
        _write_exclusive(intent_path, json.dumps(intent, ensure_ascii=False).encode("utf-8"))
        if durable_intent is not None:
            durable_intent(phase, intent_path.read_bytes())
        started = _now()
        try:
            result = self.runner.run(
                CommandRequest(args=args, cwd=cwd, timeout_seconds=timeout, env_overrides=env)
            )
            stdout = result.stdout
            stderr = result.stderr
            metadata = {
                **intent,
                "started_at": getattr(result, "started_at", None) or started,
                "completed_at": getattr(result, "completed_at", None) or _now(),
                "duration_seconds": getattr(result, "duration_seconds", 0.0),
                "pid": getattr(result, "pid", None),
                "exit_code": result.exit_code,
                "timed_out": getattr(result, "timed_out", False),
                "terminated": getattr(result, "terminated", False),
                "termination_status": getattr(result, "termination_status", "completed"),
                "cleanup_completed": getattr(result, "cleanup_completed", True),
                "stdout_truncated": getattr(result, "stdout_truncated", False),
                "stderr_truncated": getattr(result, "stderr_truncated", False),
            }
        except Exception as exc:
            details = getattr(exc, "details", {}) or {}
            stdout = str(details.get("stdout", ""))
            stderr = str(details.get("stderr", getattr(exc, "stderr", "") or ""))
            metadata = {
                **intent,
                **details,
                "started_at": details.get("started_at") or started,
                "completed_at": details.get("completed_at") or _now(),
                "exception": exc.__class__.__name__,
                "error": str(exc),
                "timed_out": exc.__class__.__name__ == "TimeoutError"
                or details.get("timed_out", False),
                "exit_code": details.get("exit_code"),
                "cleanup_completed": details.get("cleanup_completed", False),
            }
        outputs = {
            f"{phase}-stdout.log": stdout.encode("utf-8", errors="replace"),
            f"{phase}-stderr.log": stderr.encode("utf-8", errors="replace"),
            f"{phase}-process.json": json.dumps(
                metadata, sort_keys=True, ensure_ascii=False, default=str, allow_nan=False
            ).encode("utf-8"),
        }
        terminal = {
            "phase": phase,
            "completed_at": metadata.get("completed_at"),
            "exit_code": metadata.get("exit_code"),
            "pid": metadata.get("pid"),
            "timed_out": metadata.get("timed_out", False),
            "cleanup_completed": metadata.get("cleanup_completed", False),
        }
        terminal_bytes = json.dumps(terminal, sort_keys=True).encode("utf-8")
        try:
            for filename, content in outputs.items():
                _write_exclusive(scratch / filename, content)
            _write_exclusive(receipt_path, terminal_bytes)
            if durable_intent is not None:
                durable_intent(f"{phase}-terminal", terminal_bytes)
        except Exception as exc:
            # A launched process may have changed project state. If any part of
            # its terminal record cannot be durably persisted, keep the attempt
            # blocked even when local cleanup appears complete.
            if metadata.get("pid") is not None:
                exit_code = metadata.get("exit_code")
                raise ToolExecutionError(
                    f"Unable to persist terminal metadata for Godot {phase}; process state is uncertain",
                    exit_code=exit_code if isinstance(exit_code, int) else None,
                    stderr=stderr,
                    details={
                        "godot_phase": phase,
                        "process_state_uncertain": True,
                        "pid": metadata.get("pid"),
                        "cleanup_completed": metadata.get("cleanup_completed", False),
                        "metadata": metadata,
                    },
                ) from exc
            raise
        truncated = metadata.get("stdout_truncated", False) or metadata.get(
            "stderr_truncated", False
        )
        engine_error = any(
            pattern.search(f"{stdout}\n{stderr}") for pattern in _ENGINE_ERROR_PATTERNS
        )
        success = (
            not metadata.get("timed_out", False)
            and metadata.get("exit_code") == 0
            and not truncated
            and not engine_error
            and bool(metadata.get("cleanup_completed", True))
        )
        return {
            "success": success,
            "stdout": stdout,
            "stderr": stderr,
            "metadata": metadata,
            "artifacts": outputs,
        }
