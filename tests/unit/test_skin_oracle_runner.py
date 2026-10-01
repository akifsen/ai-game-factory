"""Process integrity tests for skin_deformation_oracle staging."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gamefactory.adapters.engines.skin_deformation_oracle import (
    OracleExecutionError,
    run_skin_deformation_oracle,
)
from gamefactory.adapters.fakes.humanoid_skin_fixture import build_humanoid_skinned_glb
from gamefactory.core.domain.internal_skin_contract import load_internal_skin_contract
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner

CONTRACT = load_internal_skin_contract()


class _FakeRunner(ProcessRunner):
    def __init__(self, import_code: int, script_code: int, *, write_result: bool) -> None:
        super().__init__(sanitize_output=True)
        self._import_code = import_code
        self._script_code = script_code
        self._write_result = write_result
        self.calls = 0

    def run(self, request: CommandRequest) -> CommandResult:
        self.calls += 1
        stage = Path(request.cwd)
        if "--version" in request.args:
            return CommandResult(
                exit_code=0,
                stdout="4.7.2.stable.official.fake\n",
                stderr="",
                timed_out=False,
            )
        if "--import" in request.args:
            return CommandResult(
                exit_code=self._import_code,
                stdout="",
                stderr="import failed" if self._import_code else "",
                timed_out=False,
            )
        if self._write_result:
            result_path = stage / "result.json"
            result_path.write_text(
                '{"status":"PASS","method":"bake_mesh_from_current_skeleton_pose",'
                '"max_affected_displacement":0.1,"max_unaffected_displacement":0.0,'
                '"vertex_count":16,"affected_vertex_count":8,"unaffected_vertex_count":8}',
                encoding="utf-8",
            )
        return CommandResult(
            exit_code=self._script_code,
            stdout="",
            stderr="",
            timed_out=False,
        )


def test_oracle_raises_when_import_fails(tmp_path: Path) -> None:
    glb = tmp_path / "x.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    runner = _FakeRunner(import_code=1, script_code=0, write_result=False)
    with pytest.raises(OracleExecutionError, match="import"):
        run_skin_deformation_oracle(
            Path("godot.exe"),
            glb,
            CONTRACT,
            tmp_path / "out",
            runner=runner,
        )


def test_oracle_surfaces_harness_fail_reason_before_digest(tmp_path: Path) -> None:
    glb = tmp_path / "x.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))

    class _EarlyFailRunner(_FakeRunner):
        def run(self, request: CommandRequest) -> CommandResult:
            if "--version" in request.args:
                return CommandResult(
                    exit_code=0,
                    stdout="4.7.2.stable.official.fake\n",
                    stderr="",
                    timed_out=False,
                )
            if "--import" in request.args:
                return CommandResult(exit_code=0, stdout="", stderr="", timed_out=False)
            result_path = Path(request.cwd) / "result.json"
            result_path.write_text(
                json.dumps(
                    {
                        "schema_version": "rig-runtime-observation-0.8.0",
                        "status": "FAIL",
                        "reason": "request region populations do not match baked rest geometry",
                        "method": "bake_mesh_from_current_skeleton_pose",
                        "request_digest": "dead" * 16,
                    }
                ),
                encoding="utf-8",
            )
            return CommandResult(exit_code=1, stdout="", stderr="", timed_out=False)

    runner = _EarlyFailRunner(import_code=0, script_code=1, write_result=False)
    with pytest.raises(OracleExecutionError, match="region populations"):
        run_skin_deformation_oracle(
            Path("godot.exe"),
            glb,
            CONTRACT,
            tmp_path / "out",
            runner=runner,
        )


def test_oracle_raises_when_result_missing(tmp_path: Path) -> None:
    glb = tmp_path / "x.glb"
    glb.write_bytes(build_humanoid_skinned_glb("positive"))
    runner = _FakeRunner(import_code=0, script_code=1, write_result=False)
    with pytest.raises(OracleExecutionError, match="result.json"):
        run_skin_deformation_oracle(
            Path("godot.exe"),
            glb,
            CONTRACT,
            tmp_path / "out",
            runner=runner,
        )
