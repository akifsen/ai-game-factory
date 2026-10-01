"""Bound candidate runtime request builder tests."""

from __future__ import annotations

import hashlib
from importlib import resources
from pathlib import Path

import pytest

from gamefactory.adapters.assets.v08_candidate_runtime_digest import (
    candidate_runtime_request_digest,
)
from gamefactory.adapters.assets.v08_candidate_runtime_request import (
    CandidateRuntimeRequestError,
    build_bound_candidate_runtime_request,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.v08_candidate_contracts import (
    load_packaged_candidate_profile,
    load_packaged_candidate_specification,
)
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner


class _FakeRunner(ProcessRunner):
    def run(self, request: CommandRequest) -> CommandResult:
        if "--version" in request.args:
            return CommandResult(
                exit_code=0, stdout="4.7.2.stable.official.test\n", stderr="", timed_out=False
            )
        return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)


class _VersionProbeRunner(ProcessRunner):
    def __init__(
        self,
        *,
        exit_code: int = 0,
        stdout: str = "",
        stderr: str = "",
        timed_out: bool = False,
    ) -> None:
        self._exit_code = exit_code
        self._stdout = stdout
        self._stderr = stderr
        self._timed_out = timed_out

    def run(self, request: CommandRequest) -> CommandResult:
        if "--version" in request.args:
            return CommandResult(
                exit_code=self._exit_code,
                stdout=self._stdout,
                stderr=self._stderr,
                timed_out=self._timed_out,
            )
        return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)


def _packaged_harness_path() -> Path:
    return Path(
        str(resources.files("gamefactory.resources.godot").joinpath("candidate_capsule_harness.gd"))
    )


def _invoke_build_request(
    tmp_path: Path,
    runner: ProcessRunner,
) -> dict[str, object]:
    glb = tmp_path / "ok.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    return build_bound_candidate_runtime_request(
        glb,
        spec,
        profile,
        workflow_id="WF-CAND",
        revision=2,
        execution_id="EXEC-CAND",
        strict_attempt_number=1,
        godot_executable=Path("godot"),
        runner=runner,
        harness_path=_packaged_harness_path(),
    )


def _spec_for_glb(path: Path):
    spec = load_packaged_candidate_specification()
    data = spec.model_dump(mode="json")
    data["processed_glb_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    from gamefactory.core.domain.v08_candidate_contracts import (
        parse_asset_specification_v08_candidate,
    )

    return parse_asset_specification_v08_candidate(data)


def test_build_request_requires_pass_validation(tmp_path: Path) -> None:
    bound = _invoke_build_request(tmp_path, _FakeRunner())
    assert bound["godot_version"] == "4.7.2.stable.official.test"
    assert bound["request_digest"] == candidate_runtime_request_digest(bound)
    assert bound["production_eligible"] is False
    assert bound["public_status"] == "UNSUPPORTED"


def test_build_request_blocks_failed_validation(tmp_path: Path) -> None:
    glb = tmp_path / "bad.glb"
    glb.write_bytes(build_humanoid_skinned_glb("missing_bone"))
    spec = _spec_for_glb(glb)
    profile = load_packaged_candidate_profile()
    with pytest.raises(CandidateRuntimeRequestError, match="preflight"):
        build_bound_candidate_runtime_request(
            glb,
            spec,
            profile,
            workflow_id="WF-CAND",
            revision=1,
            execution_id="EXEC-CAND",
            strict_attempt_number=1,
            godot_executable=Path("godot"),
            runner=_FakeRunner(),
            harness_path=_packaged_harness_path(),
        )


_MISLEADING_VERSION_LINE = "4.7.2.stable.official.test\n"


@pytest.mark.parametrize(
    ("exit_code", "timed_out", "stdout", "stderr", "match"),
    [
        (0, True, _MISLEADING_VERSION_LINE, "", "timed out"),
        (1, False, _MISLEADING_VERSION_LINE, "", r"failed with exit 1"),
        (2, False, "", _MISLEADING_VERSION_LINE, r"failed with exit 2"),
    ],
)
def test_build_request_rejects_unsuccessful_godot_version_probe(
    tmp_path: Path,
    exit_code: int,
    timed_out: bool,
    stdout: str,
    stderr: str,
    match: str,
) -> None:
    runner = _VersionProbeRunner(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        timed_out=timed_out,
    )
    with pytest.raises(CandidateRuntimeRequestError, match=match):
        _invoke_build_request(tmp_path, runner)
