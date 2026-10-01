"""Simulated ProcessRunner responses for V0.8-3B candidate runtime execution."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

from gamefactory.adapters.engines.v08_candidate_runtime_runner import (
    CandidateRuntimeExecutionError,
    run_v08_candidate_capsule_runtime,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.v08_candidate_contracts import load_packaged_candidate_profile
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner


@pytest.fixture
def fake_linux_display(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fake-process tests simulate Godot I/O; set DISPLAY so Linux guard does not win first."""
    monkeypatch.setenv("DISPLAY", ":99")


class _ArgvRecordingRunner(ProcessRunner):
    """Record CommandRequests; succeed import, leave render without observation for argv checks."""

    def __init__(self) -> None:
        super().__init__(sanitize_output=True)
        self.command_requests: list[CommandRequest] = []

    def run(self, request: CommandRequest) -> CommandResult:
        self.command_requests.append(request)
        if "--version" in request.args:
            return CommandResult(
                exit_code=0, stdout="4.7.2.stable.official.test\n", stderr="", timed_out=False
            )
        if "--import" in request.args:
            return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)
        return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)


def _import_and_render_requests(
    runner: _ArgvRecordingRunner,
) -> tuple[CommandRequest, CommandRequest]:
    imports = [r for r in runner.command_requests if "--import" in r.args]
    renders = [r for r in runner.command_requests if "--script" in r.args]
    assert len(imports) == 1
    assert len(renders) == 1
    return imports[0], renders[0]


def test_linux_render_injects_audio_driver_dummy_before_user_args(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("DISPLAY", ":99")
    monkeypatch.setenv("XAUTHORITY", "/tmp/.Xauthority-test")
    monkeypatch.setenv("LIBGL_ALWAYS_SOFTWARE", "1")
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    godot = Path("godot")
    runner = _ArgvRecordingRunner()
    with pytest.raises(CandidateRuntimeExecutionError, match="observation.json"):
        run_v08_candidate_capsule_runtime(
            godot,
            glb,
            spec,
            profile,
            tmp_path / "out",
            workflow_id="WF-RUN",
            revision=1,
            execution_id="EXEC-RUN",
            strict_attempt_number=1,
            runner=runner,
        )
    import_req, render_req = _import_and_render_requests(runner)
    stage = import_req.cwd
    assert stage is not None
    assert import_req.args == [
        str(godot),
        "--headless",
        "--path",
        str(stage),
        "--import",
        "--quit",
    ]
    assert "--audio-driver" not in import_req.args
    sep = render_req.args.index("--")
    assert render_req.args[sep - 2 : sep] == ["--audio-driver", "Dummy"]
    assert render_req.args.count("--audio-driver") == 1
    assert "--headless" not in render_req.args
    assert import_req.env_overrides == {
        "DISPLAY": ":99",
        "XAUTHORITY": "/tmp/.Xauthority-test",
        "LIBGL_ALWAYS_SOFTWARE": "1",
    }
    assert render_req.env_overrides == import_req.env_overrides
    project_godot = (import_req.cwd / "project.godot").read_text(encoding="utf-8")
    assert 'renderer/rendering_method="gl_compatibility"' in project_godot


def test_non_linux_render_argv_unchanged_without_audio_driver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.delenv("DISPLAY", raising=False)
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    runner = _ArgvRecordingRunner()
    with pytest.raises(CandidateRuntimeExecutionError, match="observation.json"):
        run_v08_candidate_capsule_runtime(
            Path("godot.exe"),
            glb,
            spec,
            profile,
            tmp_path / "out",
            workflow_id="WF-RUN",
            revision=1,
            execution_id="EXEC-RUN",
            strict_attempt_number=1,
            runner=runner,
        )
    import_req, render_req = _import_and_render_requests(runner)
    assert "--audio-driver" not in import_req.args
    assert "--audio-driver" not in render_req.args
    assert "--headless" not in render_req.args
    assert import_req.env_overrides == {}
    assert render_req.env_overrides == {}
    stage = import_req.cwd
    assert stage is not None
    request_path = stage / "request.json"
    assert render_req.args == [
        "godot.exe",
        "--path",
        str(stage),
        "--script",
        "res://candidate_capsule_harness.gd",
        "--",
        "--request",
        str(request_path),
    ]


class _TypedFakeRunner(ProcessRunner):
    def __init__(self, responses: list[CommandResult]) -> None:
        super().__init__(sanitize_output=True)
        self._responses = list(responses)
        self.calls = 0

    def run(self, request: CommandRequest) -> CommandResult:
        if "--version" in request.args:
            return CommandResult(
                exit_code=0, stdout="4.7.2.stable.official.test\n", stderr="", timed_out=False
            )
        if self.calls >= len(self._responses):
            return CommandResult(
                exit_code=1, stdout="", stderr="unexpected extra run", timed_out=False
            )
        out = self._responses[self.calls]
        self.calls += 1
        return out


def _spec_for_glb(path: Path):
    from gamefactory.core.domain.v08_candidate_contracts import (
        load_packaged_candidate_specification,
        parse_asset_specification_v08_candidate,
    )

    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["processed_glb_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    return parse_asset_specification_v08_candidate(data)


def test_runner_rejects_engine_error_log_with_exit_zero(tmp_path: Path, fake_linux_display) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    runner = _TypedFakeRunner(
        [
            CommandResult(exit_code=0, stdout="", stderr="", timed_out=False),
            CommandResult(
                exit_code=0,
                stdout="",
                stderr="SCRIPT ERROR: simulated harness failure\n",
                timed_out=False,
            ),
        ]
    )
    with pytest.raises(CandidateRuntimeExecutionError, match="engine error"):
        run_v08_candidate_capsule_runtime(
            Path("godot"),
            glb,
            spec,
            profile,
            tmp_path / "out",
            workflow_id="WF-RUN",
            revision=1,
            execution_id="EXEC-RUN",
            strict_attempt_number=1,
            runner=runner,
        )


def test_runner_rejects_failed_import(tmp_path: Path, fake_linux_display) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    runner = _TypedFakeRunner(
        [
            CommandResult(exit_code=2, stdout="", stderr="import failed", timed_out=False),
        ]
    )
    with pytest.raises(CandidateRuntimeExecutionError, match="--import failed"):
        run_v08_candidate_capsule_runtime(
            Path("godot"),
            glb,
            spec,
            profile,
            tmp_path / "out",
            workflow_id="WF-RUN",
            revision=1,
            execution_id="EXEC-RUN",
            strict_attempt_number=1,
            runner=runner,
        )


def test_runner_rejects_import_timeout(tmp_path: Path, fake_linux_display) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    runner = _TypedFakeRunner(
        [
            CommandResult(exit_code=-1, stdout="", stderr="", timed_out=True),
        ]
    )
    with pytest.raises(CandidateRuntimeExecutionError, match="timed out"):
        run_v08_candidate_capsule_runtime(
            Path("godot"),
            glb,
            spec,
            profile,
            tmp_path / "out",
            workflow_id="WF-RUN",
            revision=1,
            execution_id="EXEC-RUN",
            strict_attempt_number=1,
            runner=runner,
            timeout_seconds=0.01,
        )


def test_runner_rejects_missing_display_on_linux_before_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Linux DISPLAY guard is independent of fake process outcomes (CI has no DISPLAY)."""
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    runner = _TypedFakeRunner(
        [
            CommandResult(exit_code=0, stdout="", stderr="", timed_out=False),
        ]
    )
    with pytest.raises(CandidateRuntimeExecutionError, match="requires DISPLAY"):
        run_v08_candidate_capsule_runtime(
            Path("godot"),
            glb,
            spec,
            profile,
            tmp_path / "out",
            workflow_id="WF-RUN",
            revision=1,
            execution_id="EXEC-RUN",
            strict_attempt_number=1,
            runner=runner,
        )
    assert runner.calls == 0
