"""Stage and execute the V0.8-3B candidate capsule + nine-view Godot harness."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import uuid
from pathlib import Path
from typing import Any

from gamefactory.adapters.assets.internal_rig_canonical import reviewed_text_sha256
from gamefactory.adapters.assets.v08_candidate_runtime_json import (
    MAX_RUNTIME_OBSERVATION_BYTES,
    parse_strict_runtime_json_object,
)
from gamefactory.adapters.assets.v08_candidate_runtime_paths import (
    assert_no_link_in_path,
    path_crosses_link,
)
from gamefactory.adapters.assets.v08_candidate_runtime_pins import packaged_candidate_harness_path
from gamefactory.adapters.assets.v08_candidate_runtime_request import (
    build_bound_candidate_runtime_request,
)
from gamefactory.adapters.assets.v08_candidate_runtime_verify import (
    CandidateRuntimeObservationError,
    verify_candidate_runtime_observation,
)
from gamefactory.adapters.engines.godot_execution import _ENGINE_ERROR_PATTERNS
from gamefactory.core.domain.errors import ToolExecutionError
from gamefactory.core.domain.v08_candidate_contracts import (
    AssetProfileV08Candidate,
    AssetSpecificationV08Candidate,
)
from gamefactory.core.domain.v08_candidate_runtime_contract import (
    load_packaged_candidate_runtime_contract,
)
from gamefactory.core.domain.v08_candidate_runtime_integers import (
    CandidateRuntimeIntegerError,
    strict_process_exit_code,
)
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner

MAX_RUNTIME_REQUEST_BYTES = 65536


class CandidateGuardedProcessRunner(ProcessRunner):
    """Wrap a ProcessRunner: strict Godot --version probes and command provenance records."""

    def __init__(self, inner: ProcessRunner) -> None:
        super().__init__(sanitize_output=getattr(inner, "sanitize_output", True))
        self._inner = inner
        self.command_records: list[CommandRequest] = []

    @property
    def inner(self) -> ProcessRunner:
        return self._inner

    def run(self, request: CommandRequest) -> CommandResult:
        self.command_records.append(request)
        result = self._inner.run(request)
        if result.timed_out:
            raise ToolExecutionError(
                "candidate guarded process command timed out",
                exit_code=result.exit_code,
                details={"timed_out": True, "args": request.args[-3:]},
            )
        try:
            exit_code = strict_process_exit_code(result.exit_code, "exit_code")
        except CandidateRuntimeIntegerError as exc:
            raise ToolExecutionError(
                "candidate guarded process returned a non-integer exit code",
                exit_code=-1,
                details={"reason": str(exc)},
            ) from exc
        if "--version" in request.args:
            if result.timed_out or exit_code != 0:
                raise ToolExecutionError(
                    "Godot --version probe failed",
                    exit_code=exit_code,
                    details={"timed_out": result.timed_out, "stderr": (result.stderr or "")[-500:]},
                )
            lines = (result.stdout or result.stderr or "").strip().splitlines()
            if not lines or not lines[0].strip():
                raise ToolExecutionError("Godot --version produced no output")
        return result


def guard_candidate_process_runner(
    runner: ProcessRunner | CandidateGuardedProcessRunner | None,
) -> CandidateGuardedProcessRunner:
    """Return a guarded runner, or wrap an existing non-guarded runner once."""
    if runner is None:
        return CandidateGuardedProcessRunner(ProcessRunner(sanitize_output=True))
    if isinstance(runner, CandidateGuardedProcessRunner):
        return runner
    return CandidateGuardedProcessRunner(runner)


class CandidateRuntimeStageError(ValueError):
    """Candidate runtime staging path is unsafe or already present."""


class CandidateRuntimeExecutionError(RuntimeError):
    """Godot candidate runtime failed or produced unusable output."""


def _assert_safe_output_path(path: Path) -> None:
    if path_crosses_link(path):
        raise CandidateRuntimeStageError(
            f"candidate runtime path crosses a link or junction: {path}"
        )


def _assert_fresh_stage_dir(stage: Path) -> None:
    if stage.exists():
        raise CandidateRuntimeStageError(f"candidate runtime stage already exists: {stage}")
    _assert_safe_output_path(stage)


def _embed_glb_scene(scene_path: Path) -> None:
    scene_path.write_text(
        """[gd_scene load_steps=2 format=3]

[ext_resource type="PackedScene" path="res://asset.glb" id="1"]

[node name="Root" type="Node3D"]

[node name="Asset" parent="." instance=ExtResource("1")]
""",
        encoding="utf-8",
    )


def _scan_engine_diag(text: str) -> list[str]:
    return [pattern.pattern for pattern in _ENGINE_ERROR_PATTERNS if pattern.search(text)]


def run_v08_candidate_capsule_runtime(
    godot_executable: Path,
    glb_path: Path,
    spec: AssetSpecificationV08Candidate,
    profile: AssetProfileV08Candidate,
    output_dir: Path,
    *,
    workflow_id: str,
    revision: int,
    execution_id: str,
    strict_attempt_number: int,
    runner: ProcessRunner | None = None,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    """Run real Godot candidate capsule runtime and return a verified observation."""
    proc = runner or ProcessRunner(sanitize_output=True)
    contract = load_packaged_candidate_runtime_contract()
    _assert_safe_output_path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    glb_bytes = glb_path.read_bytes()
    glb_sha256_snapshot = hashlib.sha256(glb_bytes).hexdigest()
    if glb_sha256_snapshot != spec.processed_glb_sha256:
        raise CandidateRuntimeExecutionError(
            "GLB bytes do not match specification hash before staging"
        )

    stage = output_dir / f"godot_candidate_stage_{uuid.uuid4().hex}"
    _assert_fresh_stage_dir(stage)
    stage.mkdir(parents=True)
    staged_glb = stage / "asset.glb"
    staged_glb.write_bytes(glb_bytes)
    if hashlib.sha256(staged_glb.read_bytes()).hexdigest() != glb_sha256_snapshot:
        raise CandidateRuntimeExecutionError("staged GLB bytes disagree with pre-stage snapshot")
    _embed_glb_scene(stage / "Main.tscn")
    harness = packaged_candidate_harness_path()
    harness_reviewed, harness_raw = reviewed_text_sha256(harness)
    shutil.copyfile(harness, stage / "candidate_capsule_harness.gd")
    bound = build_bound_candidate_runtime_request(
        glb_path,
        spec,
        profile,
        workflow_id=workflow_id,
        revision=revision,
        execution_id=execution_id,
        strict_attempt_number=strict_attempt_number,
        godot_executable=godot_executable,
        runner=proc,
        harness_path=harness,
    )
    capture_dir = stage / "captures"
    capture_dir.mkdir()
    observation_path = stage / "observation.json"
    stage_request = {
        **bound,
        "glb": "res://Main.tscn",
        "capture_dir": str(capture_dir),
        "observation_path": str(observation_path),
    }
    request_path = stage / "request.json"
    request_text = json.dumps(stage_request, sort_keys=True, allow_nan=False)
    if len(request_text.encode("utf-8")) > min(
        contract.max_request_bytes, MAX_RUNTIME_REQUEST_BYTES
    ):
        raise CandidateRuntimeExecutionError("runtime request exceeds byte limit")
    request_path.write_text(request_text, encoding="utf-8")
    (stage / "project.godot").write_text(
        "\n".join(
            [
                "config_version=5",
                "",
                "[application]",
                'config/name="V08 Candidate Capsule Runtime"',
                "",
                "[display]",
                "window/size/viewport_width=1280",
                "window/size/viewport_height=720",
                "",
                "[rendering]",
                'renderer/rendering_method="gl_compatibility"',
                'renderer/rendering_method.mobile="gl_compatibility"',
            ]
        ),
        encoding="utf-8",
    )
    render_env: dict[str, str] = {}
    if sys.platform.startswith("linux"):
        display = os.environ.get("DISPLAY")
        if not display:
            raise CandidateRuntimeExecutionError(
                "Linux candidate runtime requires DISPLAY; no headless capture fallback"
            )
        render_env["DISPLAY"] = display
        for name in ("XAUTHORITY", "LIBGL_ALWAYS_SOFTWARE"):
            if value := os.environ.get(name):
                render_env[name] = value

    import_result = proc.run(
        CommandRequest(
            args=[
                str(godot_executable),
                "--headless",
                "--path",
                str(stage),
                "--import",
                "--quit",
            ],
            cwd=stage,
            timeout_seconds=timeout_seconds,
            env_overrides=render_env,
        )
    )
    import_log = stage / "godot-import.log"
    import_log.write_text(import_result.stdout + "\n" + import_result.stderr, encoding="utf-8")
    import_diag = import_result.stdout + "\n" + import_result.stderr
    import_errors = _scan_engine_diag(import_diag)
    if import_result.timed_out:
        raise CandidateRuntimeExecutionError("Godot --import timed out")
    if import_result.exit_code != 0 or import_errors:
        raise CandidateRuntimeExecutionError(
            f"Godot --import failed (exit={import_result.exit_code}, errors={import_errors})"
        )
    post_import_sha = hashlib.sha256(staged_glb.read_bytes()).hexdigest()
    if post_import_sha != glb_sha256_snapshot:
        raise CandidateRuntimeExecutionError(
            "processed GLB bytes changed during Godot import; byte identity violated"
        )
    staged_harness = stage / "candidate_capsule_harness.gd"
    staged_harness_reviewed, staged_harness_raw = reviewed_text_sha256(staged_harness)
    if staged_harness_raw != harness_raw or staged_harness_reviewed != harness_reviewed:
        raise CandidateRuntimeExecutionError("staged harness bytes do not match reviewed harness")

    script_result = proc.run(
        CommandRequest(
            args=[
                str(godot_executable),
                "--path",
                str(stage),
                "--script",
                "res://candidate_capsule_harness.gd",
                "--",
                "--request",
                str(request_path),
            ],
            cwd=stage,
            timeout_seconds=timeout_seconds,
            env_overrides=render_env,
        )
    )
    render_log = stage / "godot-render.log"
    render_log.write_text(script_result.stdout + "\n" + script_result.stderr, encoding="utf-8")
    render_errors = _scan_engine_diag(script_result.stdout + "\n" + script_result.stderr)
    if script_result.timed_out:
        raise CandidateRuntimeExecutionError("Godot render script timed out")
    if render_errors:
        raise CandidateRuntimeExecutionError(
            f"Godot render log matched engine error patterns: {render_errors}"
        )
    if not observation_path.is_file():
        raise CandidateRuntimeExecutionError(
            "candidate runtime did not write observation.json "
            f"(script exit {script_result.exit_code})"
        )
    try:
        assert_no_link_in_path(observation_path, label="observation_path")
    except ValueError as exc:
        raise CandidateRuntimeExecutionError(str(exc)) from exc
    obs_size = observation_path.stat().st_size
    if obs_size <= 0 or obs_size > MAX_RUNTIME_OBSERVATION_BYTES:
        raise CandidateRuntimeExecutionError("observation.json size out of bounds")
    observation = parse_strict_runtime_json_object(
        observation_path.read_bytes(),
        max_bytes=MAX_RUNTIME_OBSERVATION_BYTES,
    )
    try:
        verification = verify_candidate_runtime_observation(
            observation,
            bound,
            spec=spec,
            profile=profile,
            capture_dir=capture_dir,
            glb_path=staged_glb,
            glb_bytes=glb_bytes,
            harness_path=harness,
            import_exit_code=import_result.exit_code,
            process_exit_code=script_result.exit_code,
        )
    except CandidateRuntimeObservationError as exc:
        raise CandidateRuntimeExecutionError(str(exc)) from exc
    provenance = {
        "stage_dir": str(stage),
        "request_path": str(request_path),
        "import_log": str(import_log),
        "render_log": str(render_log),
        "bound_request": bound,
        "import_exit_code": import_result.exit_code,
        "process_exit_code": script_result.exit_code,
        "import_timed_out": import_result.timed_out,
        "process_timed_out": script_result.timed_out,
        "verification": verification,
    }
    (stage / "provenance.json").write_text(
        json.dumps(provenance, sort_keys=True, allow_nan=False),
        encoding="utf-8",
    )
    return observation


__all__ = [
    "CandidateGuardedProcessRunner",
    "CandidateRuntimeExecutionError",
    "CandidateRuntimeStageError",
    "guard_candidate_process_runner",
    "run_v08_candidate_capsule_runtime",
]
