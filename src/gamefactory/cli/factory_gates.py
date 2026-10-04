"""Trusted deterministic gate runners used by generic Factory workflows.

Providers propose changes only. These runners independently import or execute a
hash-bound candidate with the locally configured Godot binary. They never accept
provider-supplied gate results.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import stat
import struct
import time
from pathlib import Path
from typing import Any, Literal, cast

from gamefactory.adapters.engines.gameplay_harness import (
    COLLECTION_METHOD,
    HARNESS_VERSION,
    execute_gameplay_harness,
)
from gamefactory.adapters.engines.godot_image import decode_png
from gamefactory.adapters.engines.godot_staging import (
    GodotStager,
    _is_reparse,
    sha256_file,
)
from gamefactory.config.loader import ConfigLoader
from gamefactory.core.domain.agent_contracts import VisualReview as AgentVisualReview
from gamefactory.core.domain.errors import ValidationError
from gamefactory.core.domain.factory_workflow import FactoryWorkflowManifest, WorkflowTaskSpec
from gamefactory.core.domain.game_quality import (
    GameplayScenario,
    QualityFinding,
    QualityReport,
    VisualFinding,
)
from gamefactory.core.domain.game_quality import (
    VisualReview as QualityVisualReview,
)
from gamefactory.core.domain.gameplay_harness import GameplayHarnessRequest
from gamefactory.core.domain.models import generate_id
from gamefactory.core.execution.path_guard import PathGuard, assert_managed_directory
from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner

_CODE_GATE_VERSION = "godot-import-gate-1.1.0"
_MAX_SCENARIO_BYTES = 2_000_000
_MAX_TIMEOUT = 900.0


def _canonical_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


def _safe_file(
    root: Path, relative: Any, *, label: str, max_bytes: int = _MAX_SCENARIO_BYTES
) -> tuple[Path, bytes]:
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or "\\" in relative
        or any(part in {"", ".", ".."} for part in relative.split("/"))
    ):
        raise ValidationError(f"{label} must be a normalized project-relative path")
    guard = PathGuard(root)
    original = root / relative
    current = original.absolute()
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError(f"{label} cannot traverse a symbolic link or junction")
        current = current.parent
    path = guard.resolve_safe_path(relative)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
        try:
            info = os.fstat(descriptor)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or info.st_size <= 0
                or info.st_size > max_bytes
            ):
                raise ValidationError(f"{label} is missing or exceeds its bounded file size")
            raw_buffer = bytearray()
            while len(raw_buffer) <= max_bytes:
                block = os.read(descriptor, min(64_000, max_bytes + 1 - len(raw_buffer)))
                if not block:
                    break
                raw_buffer.extend(block)
            if len(raw_buffer) > max_bytes or len(raw_buffer) != info.st_size:
                raise ValidationError(f"{label} changed during its bounded read")
            raw = bytes(raw_buffer)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise ValidationError(f"{label} is unavailable") from exc
    return path, raw


def _task_spec(value: Any) -> dict[str, Any]:
    if isinstance(value, WorkflowTaskSpec):
        return value.model_dump(mode="json")
    if isinstance(value, dict):
        return value
    raise ValidationError("Gate task specification is missing")


def _evidence_file(
    name: str, raw: bytes, purpose: str, media_type: str = "application/json"
) -> dict[str, str]:
    if len(raw) > 16_000_000:
        raise ValidationError("Gate evidence file exceeds the 16 MiB limit")
    return {
        "name": name,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "content_base64": base64.b64encode(raw).decode("ascii"),
        "media_type": media_type,
        "purpose": purpose,
    }


def _bounded_result(result: Any) -> dict[str, Any]:
    value = cast(dict[str, Any], result.to_dict())
    value["stdout"] = value.get("stdout", "")[:4096]
    value["stderr"] = value.get("stderr", "")[:4096]
    value["args"] = value.get("args", [])[:32]
    return value


def _intent(
    parameters: Any,
    gate: str,
    candidate_sha256: str,
    config_sha256: str,
    phase: str,
    receipt: dict[str, Any],
) -> str:
    if not isinstance(parameters, dict) or not callable(parameters.get("record_process_intent")):
        raise ValidationError("Gate process requires the durable process-intent recorder")
    execution_id = parameters.get("execution_id")
    if not isinstance(execution_id, str) or not execution_id:
        raise ValidationError("Gate process requires its execution and attempt identity")
    allowed: dict[str, Any] = {
        key: receipt[key]
        for key in (
            "engine_version",
            "scenario_sha256",
            "request_sha256",
            "input_sha256",
            "timeout_seconds",
            "profile",
        )
        if key in receipt
    }
    registered_fingerprint = parameters.get("runner_config_sha256", config_sha256)
    if not isinstance(registered_fingerprint, str) or not re.fullmatch(
        r"[0-9a-f]{64}", registered_fingerprint
    ):
        raise ValidationError(
            "Gate process receipt is missing the registered dispatcher fingerprint"
        )
    allowed.update(
        {
            "candidate_sha256": candidate_sha256,
            "runner_config_sha256": registered_fingerprint,
            "purpose": f"{gate}:{phase}:execution={execution_id}:child={config_sha256}",
        }
    )
    result = parameters["record_process_intent"](f"factory-gate-{gate}-{phase}", allowed)
    if not isinstance(result, str) or not result:
        raise ValidationError("Durable process-intent recorder returned no receipt id")
    return result


def _gate_parameters(spec: dict[str, Any], passed: Any) -> dict[str, Any]:
    direct = spec.get("parameters", {})
    if not isinstance(direct, dict):
        raise ValidationError("Task parameters must be an object")
    if isinstance(passed, dict) and isinstance(passed.get("factory_spec"), dict):
        nested = passed["factory_spec"].get("parameters", {})
        if isinstance(nested, dict):
            return {**nested, **passed}
    return {**direct, **(passed if isinstance(passed, dict) else {})}


def _godot_executable(project_root: Path, parameters: dict[str, Any]) -> Path:
    explicit = parameters.get("godot_executable")
    if explicit is None:
        config = ConfigLoader.load_config(project_root)
        explicit = config.engine.executable_path
    if not isinstance(explicit, str) or not explicit:
        raise ValidationError("Gate requires an explicitly configured Godot executable")
    original = Path(explicit).expanduser().absolute()
    current = original
    while current != current.parent:
        if _is_reparse(current):
            raise ValidationError("Godot executable path cannot contain links or junctions")
        current = current.parent
    executable = original.resolve(strict=True)
    if not executable.is_file():
        raise ValidationError("Configured Godot executable is not a regular file")
    return executable


def _harness_digest() -> str:
    from importlib.resources import files

    data = files("gamefactory").joinpath("resources/godot/gameplay_harness.gd").read_bytes()
    return hashlib.sha256(data).hexdigest()


def _has_project_diagnostic(log: str) -> bool:
    benign_environment_lines = {"ERROR: Failed to read the root certificate store."}
    for line in log.splitlines():
        normalized = re.sub(r"\x1b\[[0-9;]*m", "", line).strip()
        if normalized in benign_environment_lines:
            continue
        if re.search(r"(?i)(?:SCRIPT ERROR|PARSE ERROR|ERROR:|failed to load script)", normalized):
            return True
    return False


class GodotImportGateRunner:
    """Headless import/parser gate on a fresh, bounded staged candidate."""

    def __init__(
        self, project_root: Path, executable: Path, *, timeout_seconds: float = 300.0
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        self.executable = executable
        self.timeout_seconds = _timeout(timeout_seconds)
        self.executable_sha256 = sha256_file(executable)
        self.harness_sha256 = _harness_digest()
        self._fingerprint = _canonical_hash(
            {
                "gate": "code",
                "version": _CODE_GATE_VERSION,
                "project_root": str(self.project_root),
                "executable": str(executable),
                "executable_sha256": self.executable_sha256,
                "harness_sha256": self.harness_sha256,
                "timeout_seconds": self.timeout_seconds,
            }
        )

    @property
    def config_fingerprint(self) -> str:
        if (
            sha256_file(self.executable) != self.executable_sha256
            or _harness_digest() != self.harness_sha256
        ):
            raise ValidationError(
                "Code gate executable or harness changed after runner registration"
            )
        return self._fingerprint

    def evaluate(
        self,
        candidate_workspace: Path | str,
        candidate_sha256: str,
        *,
        gate: str,
        task_spec: Any,
        parameters: Any,
    ) -> dict[str, Any]:
        if gate != "code":
            raise ValidationError("Godot import runner only handles the code gate")
        if self.config_fingerprint != self._fingerprint:
            raise ValidationError("Code gate fingerprint changed after registration")
        root = Path(candidate_workspace).resolve(strict=True)
        if not re.fullmatch(r"[0-9a-f]{64}", candidate_sha256):
            raise ValidationError("candidate hash must be lowercase SHA-256")
        scratch = root / ".gamefactory" / "scratch"
        marker_dir = scratch / "code-gate-marker"
        assert_managed_directory(root, ".gamefactory")
        marker_dir.mkdir(parents=True, exist_ok=True)
        marker = marker_dir / "source-marker.json"
        marker.touch(exist_ok=True)
        stager = GodotStager(root, scratch)
        files, observed = stager.source_manifest()
        if observed != candidate_sha256:
            raise ValidationError("candidate source changed before the code gate")
        workflow_id, execution_id = generate_id("WF-CODEGATE"), generate_id("EX-CODEGATE")
        from importlib.resources import files as resource_files

        harness = (
            resource_files("gamefactory")
            .joinpath("resources/godot/gameplay_harness.gd")
            .read_bytes()
        )
        stage, _, staged_hash = stager.create_stage(
            workflow_id, execution_id, marker, candidate_sha256, harness
        )
        if staged_hash != candidate_sha256:
            raise ValidationError("staged code candidate hash changed")
        app_data = stage / ".godot-appdata"
        local_app_data = stage / ".godot-local-appdata"
        app_data.mkdir()
        local_app_data.mkdir()
        godot_env = {"APPDATA": str(app_data), "LOCALAPPDATA": str(local_app_data)}
        params = _gate_parameters({}, parameters)
        timeout = _timeout(params.get("timeout_seconds", self.timeout_seconds))
        expected_exe_hash: Any = params.get("godot_executable_sha256", self.executable_sha256)
        if (
            expected_exe_hash != self.executable_sha256
            or sha256_file(self.executable) != self.executable_sha256
        ):
            raise ValidationError("Godot executable changed after gate registration")
        intent_execution_id = params.get("execution_id")
        if not isinstance(intent_execution_id, str) or not intent_execution_id:
            raise ValidationError("Code gate requires its actual execution identity")
        intent_id = _intent(
            parameters,
            "code",
            candidate_sha256,
            self.config_fingerprint,
            "godot-import",
            {
                "executable_sha256": sha256_file(self.executable),
                "staged_source_sha256": staged_hash,
                "args": [
                    "--headless",
                    "--editor",
                    "--path",
                    "<staged-candidate>",
                    "--quit",
                    "--import",
                ],
                "timeout_seconds": timeout,
                "profile": "godot-headless-import",
                "user_data_profile": "isolated-staged",
            },
        )
        process_started = time.monotonic()
        result = ProcessRunner(sanitize_output=True).run(
            CommandRequest(
                args=[
                    str(self.executable),
                    "--headless",
                    "--editor",
                    "--path",
                    str(stage),
                    "--quit",
                    "--import",
                ],
                cwd=stage,
                timeout_seconds=timeout,
                env_overrides=godot_env,
                minimal_env=True,
            )
        )
        log = (result.stdout + "\n" + result.stderr)[:1_000_000]
        log_error = _has_project_diagnostic(log)
        script_results: list[dict[str, Any]] = []
        script_paths = [
            item.relative_path for item in files if item.relative_path.lower().endswith(".gd")
        ]
        if len(script_paths) > 256:
            raise ValidationError("Code gate supports at most 256 GDScript files per candidate")
        parser_failed = False
        for relative in script_paths:
            remaining = timeout - (time.monotonic() - process_started)
            if remaining <= 0:
                parser_failed = True
                script_results.append({"path": relative, "status": "budget_exhausted"})
                break
            script_path = stage / relative
            script_hash = sha256_file(script_path)
            parse_receipt = _intent(
                parameters,
                "code",
                candidate_sha256,
                self.config_fingerprint,
                "gdscript-parse",
                {
                    "input_sha256": script_hash,
                    "timeout_seconds": min(remaining, 30.0),
                    "profile": f"--check-only --script {relative}",
                },
            )
            parse_result = ProcessRunner(sanitize_output=True).run(
                CommandRequest(
                    args=[
                        str(self.executable),
                        "--headless",
                        "--path",
                        str(stage),
                        "--check-only",
                        "--script",
                        str(script_path),
                    ],
                    cwd=stage,
                    timeout_seconds=min(remaining, 30.0),
                    env_overrides=godot_env,
                    minimal_env=True,
                )
            )
            parse_log = (parse_result.stdout + "\n" + parse_result.stderr)[:1_000_000]
            parse_log_error = _has_project_diagnostic(parse_log)
            ok = (
                parse_result.exit_code == 0
                and not parse_result.timed_out
                and parse_result.cleanup_completed
                and not parse_log_error
            )
            parser_failed = parser_failed or not ok
            script_results.append(
                {
                    "path": relative,
                    "sha256": script_hash,
                    "status": "PASS" if ok else "FAIL",
                    "process_receipt_id": parse_receipt,
                    "result": _bounded_result(parse_result),
                    "log_excerpt": parse_log[:2048],
                    "log_error": parse_log_error,
                }
            )
        intent_ids = [
            intent_id,
            *[
                item["process_receipt_id"]
                for item in script_results
                if "process_receipt_id" in item
            ],
        ]
        passed = (
            result.exit_code == 0
            and not result.timed_out
            and result.cleanup_completed
            and not log_error
            and not parser_failed
        )
        finding = QualityFinding(
            "godot_import",
            "PASS" if passed else "FAIL",
            {
                "exit_code": result.exit_code,
                "timed_out": result.timed_out,
                "cleanup_completed": result.cleanup_completed,
            },
            {"exit_code": 0, "timed_out": False, "cleanup_completed": True},
            candidate_sha256,
            tuple(f"gate-process-intent:{item}" for item in intent_ids),
            "Godot imported the staged candidate successfully."
            if passed
            else "Godot import failed, timed out, or process cleanup was incomplete.",
        )
        report = QualityReport("code:godot-import", (finding,))
        evidence = json.dumps(
            {
                "execution_id": intent_execution_id,
                "process_receipt_ids": intent_ids,
                "result": _bounded_result(result),
                "log_excerpt": log[:8192],
                "log_error": log_error,
                "script_results": script_results,
                "source_files": len(files),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "candidate_sha256": candidate_sha256,
            "report": report.to_dict(),
            "evidence_refs": list(finding.evidence_refs),
            "evidence_files": [
                _evidence_file(
                    "quality/code-gate.json",
                    evidence,
                    "Godot import output and per-script parser results",
                )
            ],
            "source_files": len(files),
            "execution_id": execution_id,
            "process_receipt_ids": intent_ids,
        }


def _timeout(value: Any) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 1 <= value <= _MAX_TIMEOUT
    ):
        raise ValidationError("gate timeout must be finite and between 1 and 900 seconds")
    return float(value)


class GodotGameplayGateRunner:
    """Execute real fixed-physics actions and optional measured metrics on a candidate."""

    def __init__(
        self,
        project_root: Path,
        executable: Path,
        request_path: str,
        scenario_path: str,
        *,
        timeout_seconds: float = 300.0,
        require_performance: bool = False,
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        self.executable = executable
        self.request_path = request_path
        self.scenario_path = scenario_path
        self.timeout_seconds = _timeout(timeout_seconds)
        self.require_performance = require_performance
        _, request_raw = _safe_file(
            self.project_root, request_path, label="Gameplay harness request"
        )
        _, scenario_raw = _safe_file(self.project_root, scenario_path, label="Gameplay scenario")
        self.request_sha256 = hashlib.sha256(request_raw).hexdigest()
        self.scenario = GameplayScenario.from_dict(json.loads(scenario_raw))
        self.request = GameplayHarnessRequest.from_dict(json.loads(request_raw))
        if self.request.scenario.scenario_sha256 != self.scenario.scenario_sha256:
            raise ValidationError("Harness request does not bind the selected gameplay scenario")
        if require_performance and self.request.performance_budget is None:
            raise ValidationError("Performance gate requires a versioned performance budget")
        if self.request.execution_id != "attempt-placeholder":
            raise ValidationError(
                "Gate harness request must use execution_id 'attempt-placeholder'"
            )
        self.executable_sha256 = sha256_file(executable)
        self.harness_sha256 = _harness_digest()
        self._fingerprint = _canonical_hash(
            {
                "gate": "performance" if require_performance else "gameplay",
                "harness_version": HARNESS_VERSION,
                "project_root": str(self.project_root),
                "executable": str(executable),
                "executable_sha256": self.executable_sha256,
                "harness_sha256": self.harness_sha256,
                "scenario_sha256": self.scenario.scenario_sha256,
                "request_sha256": self.request_sha256,
                "budget_sha256": self.request.performance_budget.sha256
                if self.request.performance_budget
                else None,
                "collection_method": COLLECTION_METHOD,
                "timeout_seconds": self.timeout_seconds,
            }
        )

    @property
    def config_fingerprint(self) -> str:
        if (
            sha256_file(self.executable) != self.executable_sha256
            or _harness_digest() != self.harness_sha256
        ):
            raise ValidationError(
                "Gameplay gate executable or harness changed after runner registration"
            )
        _, request_raw = _safe_file(
            self.project_root, self.request_path, label="Gameplay harness request"
        )
        _, scenario_raw = _safe_file(
            self.project_root, self.scenario_path, label="Gameplay scenario"
        )
        if (
            hashlib.sha256(request_raw).hexdigest() != self.request_sha256
            or GameplayScenario.from_dict(json.loads(scenario_raw)).scenario_sha256
            != self.scenario.scenario_sha256
        ):
            raise ValidationError(
                "Gameplay gate scenario or request changed after runner registration"
            )
        return self._fingerprint

    def evaluate(
        self,
        candidate_workspace: Path | str,
        candidate_sha256: str,
        *,
        gate: str,
        task_spec: Any,
        parameters: Any,
    ) -> dict[str, Any]:
        spec = _task_spec(task_spec)
        if gate != ("performance" if self.require_performance else "gameplay"):
            raise ValidationError("Gameplay runner gate type does not match its registration")
        if self.config_fingerprint != self._fingerprint:
            raise ValidationError("Gameplay gate fingerprint changed after registration")
        if gate != ("performance" if self.require_performance else "gameplay"):
            raise ValidationError("Gameplay runner gate type does not match its registration")
        params = _gate_parameters(spec, parameters)
        candidate = Path(candidate_workspace).resolve(strict=True)
        stager = GodotStager(candidate, candidate / ".gamefactory" / "scratch")
        _, full_hash = stager.source_manifest()
        if full_hash != candidate_sha256:
            raise ValidationError("candidate source changed before gameplay quality gate")
        request_path, request_raw = _safe_file(
            candidate, self.request_path, label="Gameplay harness request"
        )
        scenario_path, scenario_raw = _safe_file(
            candidate, self.scenario_path, label="Gameplay scenario"
        )
        if hashlib.sha256(request_raw).hexdigest() != self.request_sha256:
            raise ValidationError("Gameplay harness request changed after gate registration")
        scenario = GameplayScenario.from_dict(json.loads(scenario_raw))
        if scenario.scenario_sha256 != self.scenario.scenario_sha256:
            raise ValidationError("Gameplay scenario changed after gate registration")
        _, harness_source_hash = stager.source_manifest(request_path)
        execution_id = params.get("execution_id")
        if not isinstance(execution_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", execution_id
        ):
            raise ValidationError("Gate execution_id is required for process receipt correlation")
        receipts: list[str] = []

        def durable_receipt(phase: str, receipt: dict[str, Any]) -> None:
            receipts.append(
                _intent(parameters, gate, candidate_sha256, self.config_fingerprint, phase, receipt)
            )

        run = execute_gameplay_harness(
            candidate,
            self.executable,
            request_path,
            self.request,
            generate_id("WF-QGATE"),
            execution_id,
            int(params.get("attempt_number", 1)),
            timeout_seconds=self.timeout_seconds,
            expected_execution_id=execution_id,
            expected_request_sha256=self.request_sha256,
            expected_source_sha256=harness_source_hash,
            expected_executable_sha256=self.executable_sha256,
            expected_harness_sha256=self.harness_sha256,
            durable_receipt=durable_receipt,
        )
        observation_data = run.observation.to_dict()
        observation_data["evidence_refs"] = [f"gate-process-intent:{item}" for item in receipts]
        performance_data = run.performance.to_dict() if run.performance is not None else None
        if performance_data is not None:
            performance_data["evidence_refs"] = observation_data["evidence_refs"]
        result = {
            "candidate_sha256": candidate_sha256,
            "scenario": scenario.to_dict(),
            "observation": observation_data,
            "evidence_refs": observation_data["evidence_refs"],
            "screenshots": [
                {"path": path.name, "sha256": digest}
                for path, digest in zip(run.screenshot_paths, run.screenshot_sha256, strict=True)
            ],
            "process_receipt_ids": receipts,
            "execution": {
                "execution_id": execution_id,
                "harness_execution_id": run.execution_id,
                "engine": run.engine,
                "engine_identity_sha256": run.engine_identity_sha256,
                "source_sha256": run.source_sha256,
                "harness_sha256": run.harness_sha256,
                "executable_sha256": run.executable_sha256,
                "started_at": run.started_at,
                "ended_at": run.ended_at,
            },
        }
        if self.require_performance:
            if performance_data is None:
                raise ValidationError("Performance harness did not collect any documented samples")
            budget = self.request.performance_budget
            if budget is None:
                raise ValidationError("Performance gate has no configured evidence budget")
            result["budget"] = budget.to_dict()
            result["evidence"] = performance_data
        output_raw = run.output_path.read_bytes()
        files = [
            _evidence_file(
                "quality/gameplay-harness-output.json", output_raw, "Raw bounded harness output"
            )
        ]
        for index, (path, digest) in enumerate(
            zip(run.screenshot_paths, run.screenshot_sha256, strict=True)
        ):
            image_bytes = path.read_bytes()
            if hashlib.sha256(image_bytes).hexdigest() != digest:
                raise ValidationError("Gameplay screenshot changed before artifact capture")
            files.append(
                _evidence_file(
                    f"quality/screenshot-{index + 1}.png",
                    image_bytes,
                    "Physical Godot viewport screenshot",
                    "image/png",
                )
            )
        receipts_raw = json.dumps(
            {
                "execution_id": execution_id,
                "process_receipt_ids": receipts,
                "harness_execution_id": run.execution_id,
                "engine_identity_sha256": run.engine_identity_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        files.append(
            _evidence_file(
                "quality/gameplay-process-receipts.json",
                receipts_raw,
                "Durable Godot process intent references",
            )
        )
        result["evidence_files"] = files
        return result


class ProviderVisualGateRunner:
    """Validate a persisted advisory provider review against physical image bytes."""

    def __init__(
        self,
        project_root: Path,
        screenshot_path: str,
        art_bible_path: str,
        reference_paths: tuple[str, ...],
        provider_fingerprint: str,
    ) -> None:
        self.project_root = project_root.resolve(strict=True)
        self.screenshot_path = screenshot_path
        self.art_bible_path = art_bible_path
        self.reference_paths = reference_paths
        screenshot, screenshot_raw = _safe_file(
            self.project_root, screenshot_path, label="Visual screenshot", max_bytes=16_000_000
        )
        art_bible, art_raw = _safe_file(
            self.project_root, art_bible_path, label="Visual art bible", max_bytes=2_000_000
        )
        self.screenshot_sha256 = hashlib.sha256(screenshot_raw).hexdigest()
        self.art_bible_sha256 = hashlib.sha256(art_raw).hexdigest()
        refs: dict[str, str] = {}
        for relative in reference_paths:
            path, raw = _safe_file(
                self.project_root, relative, label="Visual reference", max_bytes=16_000_000
            )
            refs[relative] = hashlib.sha256(raw).hexdigest()
        self.reference_hashes = tuple(sorted(refs.values()))
        self.reference_hashes_by_path = refs
        self.provider_fingerprint = provider_fingerprint
        self._fingerprint = _canonical_hash(
            {
                "gate": "visual",
                "screenshot": [screenshot_path, self.screenshot_sha256],
                "art_bible": [art_bible_path, self.art_bible_sha256],
                "references": refs,
                "provider_config_sha256": provider_fingerprint,
            }
        )

    @property
    def config_fingerprint(self) -> str:
        _, shot = _safe_file(
            self.project_root, self.screenshot_path, label="Visual screenshot", max_bytes=16_000_000
        )
        _, art = _safe_file(
            self.project_root, self.art_bible_path, label="Visual art bible", max_bytes=2_000_000
        )
        if (
            hashlib.sha256(shot).hexdigest() != self.screenshot_sha256
            or hashlib.sha256(art).hexdigest() != self.art_bible_sha256
        ):
            raise ValidationError(
                "Visual screenshot or art bible changed after runner registration"
            )
        for relative, expected in self.reference_hashes_by_path.items():
            _, content = _safe_file(
                self.project_root, relative, label="Visual reference", max_bytes=16_000_000
            )
            if hashlib.sha256(content).hexdigest() != expected:
                raise ValidationError("Visual reference changed after runner registration")
        return self._fingerprint

    def evaluate(
        self,
        candidate_workspace: Path | str,
        candidate_sha256: str,
        *,
        gate: str,
        task_spec: Any,
        parameters: Any,
    ) -> dict[str, Any]:
        if gate != "visual":
            raise ValidationError("Visual runner only handles the visual gate")
        if self.config_fingerprint != self._fingerprint:
            raise ValidationError("Visual gate fingerprint changed after registration")
        candidate = Path(candidate_workspace).resolve(strict=True)
        stager = GodotStager(candidate, candidate / ".gamefactory" / "scratch")
        _, observed = stager.source_manifest()
        if observed != candidate_sha256:
            raise ValidationError("candidate changed before visual review validation")
        shot, shot_raw = _safe_file(
            candidate, self.screenshot_path, label="Visual screenshot", max_bytes=16_000_000
        )
        art_bible, art_raw = _safe_file(
            candidate, self.art_bible_path, label="Visual art bible", max_bytes=2_000_000
        )
        screenshot_sha = hashlib.sha256(shot_raw).hexdigest()
        art_sha = hashlib.sha256(art_raw).hexdigest()
        if screenshot_sha != self.screenshot_sha256 or art_sha != self.art_bible_sha256:
            raise ValidationError("Visual screenshot or art bible changed after gate registration")
        expected_sources = {self.screenshot_path: screenshot_sha, self.art_bible_path: art_sha}
        for relative in self.reference_paths:
            path, raw = _safe_file(
                candidate, relative, label="Visual reference", max_bytes=16_000_000
            )
            expected_sources[relative] = hashlib.sha256(raw).hexdigest()
        if isinstance(parameters, dict):
            reviews = parameters.get("provider_visual_reviews")
            selected = parameters.get("visual_source_hashes")
        else:
            reviews, selected = None, None
        if not isinstance(reviews, list) or not reviews:
            raise ValidationError("Verified provider visual-review envelopes are missing")
        if isinstance(selected, dict):
            selected_items = selected.get("sources", selected)
        else:
            selected_items = selected
        if isinstance(selected_items, list):
            selection_map = {
                item.get("path"): item.get("sha256")
                for item in selected_items
                if isinstance(item, dict)
            }
        elif isinstance(selected_items, dict):
            selection_map = selected_items
        else:
            selection_map = {}
        if selection_map != expected_sources:
            raise ValidationError(
                "Selected visual source hashes do not match the candidate snapshot"
            )
        task = _task_spec(task_spec)
        canonical_reviews: list[QualityVisualReview] = []
        for envelope in reviews:
            if not isinstance(envelope, dict):
                continue
            review_payload = (
                envelope.get("payload") if isinstance(envelope.get("payload"), dict) else envelope
            )
            if not isinstance(review_payload, dict):
                continue
            raw_review = review_payload.get(
                "provider_review", review_payload.get("visual_review", review_payload.get("review"))
            )
            try:
                advisory = AgentVisualReview.model_validate_json(
                    json.dumps(raw_review, allow_nan=False)
                )
            except (TypeError, ValueError):
                continue
            if advisory.task_id != task.get("task_id") or advisory.provider_id != task.get(
                "agent_id"
            ):
                continue
            observed_sources = {source.path: source.sha256 for source in advisory.reviewed_sources}
            if observed_sources != expected_sources:
                continue
            provider_config_sha = review_payload.get(
                "executor_config_sha256", envelope.get("provider_config_sha256")
            )
            if provider_config_sha != self.provider_fingerprint:
                continue
            severity: Literal["info", "warning", "concern"] = (
                "info"
                if advisory.conclusion == "LIKELY_MATCH"
                else "concern"
                if advisory.conclusion == "POSSIBLE_ISSUE"
                else "warning"
            )
            details: tuple[str, ...] = advisory.findings or (advisory.conclusion,)
            findings = tuple(
                VisualFinding(
                    "provider_advisory",
                    severity,
                    0.0,
                    summary,
                    (f"screenshot-sha256:{screenshot_sha}",),
                )
                for summary in details[:128]
            )
            canonical = QualityVisualReview.from_dict(
                {
                    "schema_version": "visual-review-1.0.0",
                    "screenshot_sha256": screenshot_sha,
                    "reference_sha256": list(self.reference_hashes),
                    "art_bible_sha256": art_sha,
                    "reviewer": advisory.provider_id,
                    "findings": [finding.to_dict() for finding in findings],
                }
            )
            canonical_reviews.append(canonical)
        if len(canonical_reviews) != 1:
            raise ValidationError(
                "Exactly one provider review must bind the approved screenshot and references"
            )
        review = canonical_reviews[0]
        if len(shot_raw) < 24:
            raise ValidationError("Visual screenshot PNG header is truncated")
        width, height = struct.unpack(">II", shot_raw[16:24])
        decoded = decode_png(shot_raw, width, height)
        if (
            width <= 1
            or height <= 1
            or decoded.image.getbbox() is None
            or all(
                low == high
                for low, high in cast(
                    tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
                    decoded.image.convert("RGB").getextrema(),
                )
            )
        ):
            raise ValidationError("Visual gate screenshot is blank or has invalid dimensions")
        refs = [
            f"screenshot-sha256:{screenshot_sha}",
            f"art-bible-sha256:{art_sha}",
            *[f"visual-reference-sha256:{item}" for item in self.reference_hashes],
        ]
        return {
            "candidate_sha256": candidate_sha256,
            "review": review.to_dict(),
            "screenshot_path": str(shot.relative_to(candidate).as_posix()),
            "screenshot_sha256": screenshot_sha,
            "evidence_refs": refs,
            "evidence_files": [
                _evidence_file("quality/visual-screenshot.png", shot_raw, "screenshot", "image/png")
            ],
            "process_receipt_ids": [],
        }


class CandidateVisualGateRunner:
    """Capture real candidate pixels, then validate an advisory review against that capture."""

    def __init__(
        self,
        root: Path,
        executable: Path,
        request_path: str,
        scenario_path: str,
        art_bible_path: str,
        reference_paths: tuple[str, ...],
        provider_fingerprint: str,
        *,
        timeout_seconds: float = 300.0,
    ) -> None:
        self.root = root.resolve(strict=True)
        self.executable = executable
        self.request_path = request_path
        self.scenario_path = scenario_path
        self.art_bible_path = art_bible_path
        self.reference_paths = reference_paths
        self.timeout_seconds = _timeout(timeout_seconds)
        self.executable_sha256 = sha256_file(executable)
        self.harness_sha256 = _harness_digest()
        _, request_raw = _safe_file(self.root, request_path, label="Visual capture harness request")
        _, scenario_raw = _safe_file(self.root, scenario_path, label="Visual capture scenario")
        self.request_sha256 = hashlib.sha256(request_raw).hexdigest()
        self.request = GameplayHarnessRequest.from_dict(json.loads(request_raw))
        self.scenario = GameplayScenario.from_dict(json.loads(scenario_raw))
        if (
            self.request.scenario.scenario_sha256 != self.scenario.scenario_sha256
            or sum(item.action == "capture_screenshot" for item in self.request.actions) != 1
        ):
            raise ValidationError(
                "Visual capture request must bind the selected scenario and capture exactly one screenshot"
            )
        _, art_raw = _safe_file(
            self.root, art_bible_path, label="Visual art bible", max_bytes=2_000_000
        )
        self.art_bible_sha256 = hashlib.sha256(art_raw).hexdigest()
        self.reference_hashes: dict[str, str] = {}
        for path in reference_paths:
            _, raw = _safe_file(self.root, path, label="Visual reference", max_bytes=16_000_000)
            self.reference_hashes[path] = hashlib.sha256(raw).hexdigest()
        if not self.reference_hashes:
            raise ValidationError("Visual review requires at least one explicit reference image")
        self.provider_fingerprint = provider_fingerprint
        self._fingerprint = _canonical_hash(
            {
                "gate": "visual",
                "mode": "candidate-capture-and-advisory-review-1.0.0",
                "root": str(self.root),
                "executable": str(executable),
                "executable_sha256": self.executable_sha256,
                "harness_sha256": self.harness_sha256,
                "request_path": request_path,
                "request_sha256": self.request_sha256,
                "scenario_sha256": self.scenario.scenario_sha256,
                "art_bible_sha256": self.art_bible_sha256,
                "reference_hashes": self.reference_hashes,
                "provider_fingerprint": provider_fingerprint,
                "timeout_seconds": self.timeout_seconds,
            }
        )

    @property
    def config_fingerprint(self) -> str:
        if (
            sha256_file(self.executable) != self.executable_sha256
            or _harness_digest() != self.harness_sha256
        ):
            raise ValidationError("Visual gate executable or harness changed after registration")
        _, request = _safe_file(
            self.root, self.request_path, label="Visual capture harness request"
        )
        _, scenario = _safe_file(self.root, self.scenario_path, label="Visual capture scenario")
        if (
            hashlib.sha256(request).hexdigest() != self.request_sha256
            or GameplayScenario.from_dict(json.loads(scenario)).scenario_sha256
            != self.scenario.scenario_sha256
        ):
            raise ValidationError("Visual gate request or scenario changed after registration")
        _, art = _safe_file(
            self.root, self.art_bible_path, label="Visual art bible", max_bytes=2_000_000
        )
        if hashlib.sha256(art).hexdigest() != self.art_bible_sha256:
            raise ValidationError("Visual gate art bible changed after registration")
        for path, expected in self.reference_hashes.items():
            _, raw = _safe_file(self.root, path, label="Visual reference", max_bytes=16_000_000)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValidationError("Visual gate reference changed after registration")
        return self._fingerprint

    def evaluate(
        self,
        candidate_workspace: Path | str,
        candidate_sha256: str,
        *,
        gate: str,
        task_spec: Any,
        parameters: Any,
    ) -> dict[str, Any]:
        if gate != "visual" or not isinstance(parameters, dict):
            raise ValidationError("Candidate visual gate parameters are invalid")
        if self.config_fingerprint != self._fingerprint:
            raise ValidationError("Candidate visual gate fingerprint changed after registration")
        candidate = Path(candidate_workspace).resolve(strict=True)
        stager = GodotStager(candidate, candidate / ".gamefactory" / "scratch")
        _, observed = stager.source_manifest()
        if observed != candidate_sha256:
            raise ValidationError("Candidate changed before visual gate evaluation")
        if parameters.get("combined_capture") is True:
            return self._capture(candidate, candidate_sha256, parameters)
        return self._review(candidate_sha256, task_spec, parameters)

    def _capture(
        self, candidate: Path, candidate_sha256: str, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        execution_id = parameters.get("execution_id")
        if not isinstance(execution_id, str) or not execution_id:
            raise ValidationError("Candidate screenshot capture requires an execution id")
        _, request = _safe_file(candidate, self.request_path, label="Visual capture request")
        _, scenario_raw = _safe_file(candidate, self.scenario_path, label="Visual capture scenario")
        if (
            hashlib.sha256(request).hexdigest() != self.request_sha256
            or GameplayScenario.from_dict(json.loads(scenario_raw)).scenario_sha256
            != self.scenario.scenario_sha256
        ):
            raise ValidationError(
                "Candidate screenshot request or scenario differs from registration"
            )
        _, stager_hash = GodotStager(
            candidate, candidate / ".gamefactory" / "scratch"
        ).source_manifest(self.request_path)
        receipts: list[str] = []
        run = execute_gameplay_harness(
            candidate,
            self.executable,
            candidate / self.request_path,
            self.request,
            generate_id("WF-VISUAL-CAPTURE"),
            execution_id,
            int(parameters.get("attempt_number", 1)),
            timeout_seconds=self.timeout_seconds,
            expected_execution_id=execution_id,
            expected_request_sha256=self.request_sha256,
            expected_source_sha256=stager_hash,
            expected_executable_sha256=self.executable_sha256,
            expected_harness_sha256=self.harness_sha256,
            durable_receipt=lambda phase, receipt: receipts.append(
                _intent(
                    parameters,
                    "visual",
                    candidate_sha256,
                    self.config_fingerprint,
                    phase,
                    {
                        "scenario_sha256": self.scenario.scenario_sha256,
                        "request_sha256": self.request_sha256,
                        "timeout_seconds": self.timeout_seconds,
                        "profile": "candidate-viewport-capture",
                    },
                )
            ),
        )
        if len(run.screenshot_paths) != 1 or len(run.screenshot_sha256) != 1:
            raise ValidationError("Godot did not produce exactly one requested visual screenshot")
        _, art = _safe_file(
            candidate, self.art_bible_path, label="Visual art bible", max_bytes=2_000_000
        )
        refs = {
            path: _safe_file(candidate, path, label="Visual reference", max_bytes=16_000_000)[1]
            for path in self.reference_paths
        }
        reviews: list[dict[str, Any]] = []
        files: list[dict[str, str]] = []
        for index, (path, digest) in enumerate(
            zip(run.screenshot_paths, run.screenshot_sha256, strict=True), 1
        ):
            image = path.read_bytes()
            if hashlib.sha256(image).hexdigest() != digest:
                raise ValidationError("Candidate screenshot changed before artifact capture")
            refs_payload = [hashlib.sha256(raw).hexdigest() for raw in refs.values()]
            review = QualityVisualReview.from_dict(
                {
                    "schema_version": "visual-review-1.0.0",
                    "screenshot_sha256": digest,
                    "reference_sha256": refs_payload,
                    "art_bible_sha256": hashlib.sha256(art).hexdigest(),
                    "reviewer": "capture-only",
                    "findings": [],
                }
            )
            reviews.append(review.to_dict())
            files.append(
                _evidence_file(
                    "candidate.png" if index == 1 else f"candidate-{index}.png",
                    image,
                    "screenshot",
                    "image/png",
                )
            )
        receipt_bytes = json.dumps(
            {
                "execution_id": execution_id,
                "process_receipt_ids": receipts,
                "source_sha256": run.source_sha256,
                "request_sha256": self.request_sha256,
                "engine_identity_sha256": run.engine_identity_sha256,
                "execution_mode": "visual-capture",
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        files.append(
            _evidence_file(
                "visual-capture-receipt.json",
                receipt_bytes,
                "Godot viewport capture process evidence",
            )
        )
        return {
            "candidate_sha256": candidate_sha256,
            "review": reviews[0],
            "screenshot_path": files[0]["name"],
            "screenshot_sha256": run.screenshot_sha256[0],
            "evidence_files": files,
            "process_receipt_ids": receipts,
            "capture_only": True,
        }

    def _review(
        self, candidate_sha256: str, task_spec: Any, parameters: dict[str, Any]
    ) -> dict[str, Any]:
        provenance = parameters.get("visual_capture_provenance")
        if (
            not isinstance(provenance, list)
            or len(provenance) != 1
            or provenance[0].get("candidate_sha256") != candidate_sha256
        ):
            raise ValidationError(
                "Current candidate hash does not match the immutable reviewed screenshot capture"
            )
        sources = parameters.get("visual_source_files")
        selected = parameters.get("visual_source_hashes")
        if (
            not isinstance(sources, list)
            or not isinstance(selected, list)
            or not sources
            or not selected
        ):
            raise ValidationError(
                "Final visual review requires selected screenshot/reference/art-bible source bytes"
            )
        source_map: dict[str, dict[str, Any]] = {}
        for item in sources:
            if not isinstance(item, dict) or set(item) != {
                "path",
                "sha256",
                "purpose",
                "content_base64",
            }:
                raise ValidationError("Visual source file envelope is malformed")
            relative = item["path"]
            if (
                not isinstance(relative, str)
                or not relative
                or relative.startswith("/")
                or re.match(r"^[A-Za-z]:", relative)
                or "\\" in relative
                or any(part in {"", ".", ".."} or ":" in part for part in relative.split("/"))
            ):
                raise ValidationError("Visual source path is not normalized and project-relative")
            try:
                raw = base64.b64decode(item["content_base64"], validate=True)
            except (ValueError, TypeError) as exc:
                raise ValidationError("Visual source content is not valid base64") from exc
            if len(raw) > 16_000_000 or hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValidationError("Visual source file hash or size is invalid")
            if relative in source_map:
                raise ValidationError("Visual source file paths must be unique")
            source_map[item["path"]] = {**item, "raw": raw}
        selected_map = {
            item["path"]: item["sha256"]
            for item in selected
            if isinstance(item, dict)
            and isinstance(item.get("path"), str)
            and isinstance(item.get("sha256"), str)
            and isinstance(item.get("purpose"), str)
            and any(
                label in item["purpose"].lower()
                for label in ("screenshot", "reference", "art bible", "art_bible")
            )
        }
        if not selected_map or selected_map != {
            path: item["sha256"] for path, item in source_map.items()
        }:
            raise ValidationError(
                "Selected visual source hashes do not match supplied immutable bytes"
            )
        screenshots = [
            item for item in source_map.values() if "screenshot" in item["purpose"].lower()
        ]
        bibles = [
            item
            for item in source_map.values()
            if "art bible" in item["purpose"].lower() or "art_bible" in item["purpose"].lower()
        ]
        reference_items = [
            item for item in source_map.values() if "reference" in item["purpose"].lower()
        ]
        if len(screenshots) != 1 or len(bibles) != 1 or not reference_items:
            raise ValidationError(
                "Visual review requires one screenshot, an art bible, and explicit references"
            )
        screenshot = screenshots[0]
        if (
            screenshot["sha256"] != provenance[0].get("evidence_sha256")
            or provenance[0].get("evidence_name") != "candidate.png"
        ):
            raise ValidationError(
                "Selected screenshot bytes do not match the unique immutable capture report entry"
            )
        width, height = (
            struct.unpack(">II", screenshot["raw"][16:24])
            if len(screenshot["raw"]) >= 24
            else (0, 0)
        )
        decoded = decode_png(screenshot["raw"], width, height)
        if (
            width <= 1
            or height <= 1
            or all(
                low == high
                for low, high in cast(
                    tuple[tuple[int, int], tuple[int, int], tuple[int, int]],
                    decoded.image.convert("RGB").getextrema(),
                )
            )
        ):
            raise ValidationError("Provider-reviewed screenshot is blank or invalid")
        expected_sources = {path: item["sha256"] for path, item in source_map.items()}
        task = _task_spec(task_spec)
        reviews = parameters.get("provider_visual_reviews")
        if not isinstance(reviews, list) or len(reviews) != 1:
            raise ValidationError("Exactly one persisted advisory provider review is required")
        envelope = reviews[0]
        payload = envelope.get("payload", {}) if isinstance(envelope, dict) else {}
        if not isinstance(payload, dict):
            raise ValidationError("Persisted visual provider review payload is invalid")
        if payload.get("executor_config_sha256") != self.provider_fingerprint:
            raise ValidationError(
                "Persisted visual review provider fingerprint does not match runner configuration"
            )
        raw_review = payload.get("provider_review")
        advisory = AgentVisualReview.model_validate_json(json.dumps(raw_review, allow_nan=False))
        if advisory.task_id != task.get("task_id") or advisory.provider_id != task.get(
            "executor_id"
        ):
            raise ValidationError("Provider visual review does not match task/executor identity")
        observed = {source.path: source.sha256 for source in advisory.reviewed_sources}
        if observed != expected_sources:
            raise ValidationError(
                "Provider visual review source hashes differ from physical captured bytes"
            )
        severity: Literal["info", "warning", "concern"] = (
            "info"
            if advisory.conclusion == "LIKELY_MATCH"
            else "concern"
            if advisory.conclusion == "POSSIBLE_ISSUE"
            else "warning"
        )
        findings = tuple(
            VisualFinding(
                "provider_advisory",
                severity,
                0.0,
                text,
                (f"screenshot-sha256:{screenshot['sha256']}",),
            )
            for text in (advisory.findings or (advisory.conclusion,))[:128]
        )
        review = QualityVisualReview.from_dict(
            {
                "schema_version": "visual-review-1.0.0",
                "screenshot_sha256": screenshot["sha256"],
                "reference_sha256": [item["sha256"] for item in reference_items],
                "art_bible_sha256": bibles[0]["sha256"],
                "reviewer": advisory.provider_id,
                "findings": [item.to_dict() for item in findings],
            }
        )
        evidence_files = [
            _evidence_file("visual-screenshot.png", screenshot["raw"], "screenshot", "image/png")
        ]
        return {
            "candidate_sha256": candidate_sha256,
            "review": review.to_dict(),
            "screenshot_path": screenshot["path"],
            "screenshot_sha256": screenshot["sha256"],
            "evidence_refs": [f"screenshot-sha256:{screenshot['sha256']}"],
            "evidence_files": evidence_files,
            "process_receipt_ids": [],
        }


class _GateRunnerSet:
    """Select a registered deterministic runner by immutable workflow/task identity."""

    def __init__(self, gate: str, runners: dict[tuple[str, str], Any]) -> None:
        self.gate = gate
        self.runners = dict(runners)

    @property
    def config_fingerprint(self) -> str:
        return _canonical_hash(
            {
                "gate": self.gate,
                "runners": {
                    f"{manifest}:{task}": runner.config_fingerprint
                    for (manifest, task), runner in sorted(self.runners.items())
                },
            }
        )

    def evaluate(
        self,
        candidate_workspace: Path | str,
        candidate_sha256: str,
        *,
        gate: str,
        task_spec: Any,
        parameters: Any,
    ) -> dict[str, Any]:
        spec = _task_spec(task_spec)
        if gate != self.gate:
            raise ValidationError("Gate runner was dispatched for the wrong gate type")
        params = parameters if isinstance(parameters, dict) else {}
        manifest_hash = params.get("manifest_sha256")
        key = (manifest_hash, spec.get("task_id"))
        runner = self.runners.get(key)
        if runner is None:
            raise ValidationError("No trusted gate runner matches this approved manifest and task")
        result = runner.evaluate(
            candidate_workspace,
            candidate_sha256,
            gate=gate,
            task_spec=task_spec,
            parameters=parameters,
        )
        if not isinstance(result, dict):
            raise ValidationError("Trusted gate runner returned a non-object result")
        return result


def register_manifest_gates(
    root: Path,
    manifest: FactoryWorkflowManifest,
    registry: Any,
    executor_registry: Any | None = None,
) -> list[dict[str, Any]]:
    """Register only reproducibly configured local gates for manifest tasks."""
    runners, providers = manifest_gate_runners(root, manifest, executor_registry)
    for gate, values in runners.items():
        if values:
            dispatcher = _GateRunnerSet(gate, values)
            existing = registry.get(gate)
            if existing is not None and isinstance(existing[0], _GateRunnerSet):
                merged = dict(existing[0].runners)
                merged.update(values)
                dispatcher = _GateRunnerSet(gate, merged)
                registry._runners[gate] = (dispatcher, dispatcher.config_fingerprint)
            else:
                registry.register(
                    gate, dispatcher, config_fingerprint=dispatcher.config_fingerprint
                )
    return providers


def manifest_gate_runners(
    root: Path, manifest: FactoryWorkflowManifest, executor_registry: Any | None = None
) -> tuple[dict[str, dict[tuple[str, str], Any]], list[dict[str, Any]]]:
    """Create candidate-specific runners without launching Godot or a provider."""
    config = ConfigLoader.load_config(root)
    providers: list[dict[str, Any]] = []
    by_gate: dict[str, dict[tuple[str, str], Any]] = {
        "code": {},
        "gameplay": {},
        "performance": {},
        "visual": {},
    }
    for spec in manifest.tasks:
        if not spec.required_gates:
            continue
        parameters = dict(spec.parameters)
        executable = parameters.get("godot_executable", config.engine.executable_path)
        runner: GodotImportGateRunner | GodotGameplayGateRunner | CandidateVisualGateRunner
        if "code" in spec.required_gates:
            if not executable:
                providers.append(
                    {
                        "task_id": spec.task_id,
                        "gate": "code",
                        "status": "NOT_VERIFIED",
                        "reason": "No configured Godot executable",
                    }
                )
            else:
                exe = _godot_executable(root, parameters)
                runner = GodotImportGateRunner(
                    root, exe, timeout_seconds=parameters.get("timeout_seconds", 300.0)
                )
                by_gate["code"][(manifest.sha256, spec.task_id)] = runner
                providers.append(
                    {
                        "task_id": spec.task_id,
                        "gate": "code",
                        "status": "AVAILABLE",
                        "config_fingerprint": runner.config_fingerprint,
                    }
                )
        for gate, performance in (("gameplay", False), ("performance", True)):
            if gate not in spec.required_gates:
                continue
            scenario_path = parameters.get("gameplay_scenario_path") or parameters.get(
                "scenario_path"
            )
            request_path = parameters.get("harness_request_path")
            if not executable or not scenario_path or not request_path:
                providers.append(
                    {
                        "task_id": spec.task_id,
                        "gate": gate,
                        "status": "NOT_VERIFIED",
                        "reason": "Gate requires godot_executable, gameplay_scenario_path, and harness_request_path task parameters",
                    }
                )
                continue
            exe = _godot_executable(root, parameters)
            runner = GodotGameplayGateRunner(
                root,
                exe,
                request_path,
                scenario_path,
                timeout_seconds=parameters.get("timeout_seconds", 300.0),
                require_performance=performance,
            )
            by_gate[gate][(manifest.sha256, spec.task_id)] = runner
            providers.append(
                {
                    "task_id": spec.task_id,
                    "gate": gate,
                    "status": "AVAILABLE",
                    "config_fingerprint": runner.config_fingerprint,
                }
            )
        if "visual" in spec.required_gates:
            art_bible = parameters.get("visual_art_bible_path")
            reference_paths = parameters.get("visual_reference_paths", [])
            request_path = parameters.get("harness_request_path")
            scenario_path = parameters.get("gameplay_scenario_path") or parameters.get(
                "scenario_path"
            )
            executor = (
                executor_registry.get(spec.executor_id) if executor_registry is not None else None
            )
            provider_fingerprint = executor[3] if executor else None
            if (
                not executable
                or not request_path
                or not scenario_path
                or not art_bible
                or not isinstance(reference_paths, list)
                or not provider_fingerprint
            ):
                providers.append(
                    {
                        "task_id": spec.task_id,
                        "gate": "visual",
                        "status": "NOT_VERIFIED",
                        "reason": "Visual gate requires Godot, a screenshot-enabled harness request/scenario, art bible, references, and configured reviewer",
                    }
                )
            else:
                exe = _godot_executable(root, parameters)
                runner = CandidateVisualGateRunner(
                    root,
                    exe,
                    request_path,
                    scenario_path,
                    art_bible,
                    tuple(reference_paths),
                    provider_fingerprint,
                    timeout_seconds=parameters.get("timeout_seconds", 300.0),
                )
                by_gate["visual"][(manifest.sha256, spec.task_id)] = runner
                providers.append(
                    {
                        "task_id": spec.task_id,
                        "gate": "visual",
                        "status": "AVAILABLE",
                        "config_fingerprint": runner.config_fingerprint,
                    }
                )
    return by_gate, providers
