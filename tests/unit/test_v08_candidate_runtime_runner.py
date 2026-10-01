"""Simulated ProcessRunner responses for V0.8-3B candidate runtime execution."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from gamefactory.adapters.engines.v08_candidate_runtime_runner import (
    CandidateRuntimeExecutionError,
    run_v08_candidate_capsule_runtime,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.v08_candidate_contracts import load_packaged_candidate_profile
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner


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


def test_runner_rejects_engine_error_log_with_exit_zero(tmp_path: Path) -> None:
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


def test_runner_rejects_failed_import(tmp_path: Path) -> None:
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


def test_runner_rejects_import_timeout(tmp_path: Path) -> None:
    glb = tmp_path / "humanoid.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    runner = _TypedFakeRunner(
        [
            CommandResult(exit_code=-1, stdout="", stderr="", timed_out=True),
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
            timeout_seconds=0.01,
        )
