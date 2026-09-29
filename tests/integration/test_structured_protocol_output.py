"""Integration tests for CommandResult.protocol_stdout, structured process runner output, and Meshy parsing."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from gamefactory.adapters.external.meshy_cli import MeshyCliRunner
from gamefactory.core.execution.process_runner import CommandRequest, CommandResult, ProcessRunner


def test_process_runner_protocol_stdout_with_hostile_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Set hostile env variable that contains "AUTH" and value "1"
    monkeypatch.setenv("SOME_AUTH_FLAG", "1")
    monkeypatch.setenv("CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH", "1")

    task_id = "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d"
    digest = "1111111122222222333333334444444455555555666666667777777788888888"
    script = f"import json\nprint(json.dumps({{'task_id': '{task_id}', 'sha256': '{digest}'}}))\n"

    runner = ProcessRunner(sanitize_output=True)
    req = CommandRequest(
        args=[sys.executable, "-c", script],
        cwd=tmp_path,
        structured_json_output=True,
    )
    result = runner.run(req)

    assert result.exit_code == 0
    # protocol_stdout holds raw unredacted stream
    assert result.protocol_stdout is not None
    data = json.loads(result.protocol_stdout)
    assert data["task_id"] == task_id
    assert data["sha256"] == digest

    # stdout is also uncorrupted because '1' is not an eligible exact secret
    assert "[REDACTED]" not in result.stdout
    assert json.loads(result.stdout)["task_id"] == task_id

    # protocol_stdout must NOT appear in to_dict() or repr()
    assert "protocol_stdout" not in result.to_dict()
    assert "protocol_stdout" not in repr(result)


def test_process_runner_redaction_with_long_secret_in_env_overrides(tmp_path: Path) -> None:
    long_secret = "secret_credential_long_token_abc_12345"
    script = (
        "import os, json\n"
        "secret_val = os.environ.get('MY_API_KEY_SECRET', '')\n"
        "print(json.dumps({'status': 'ok', 'secret': secret_val}))\n"
    )

    runner = ProcessRunner(sanitize_output=True)
    req = CommandRequest(
        args=[sys.executable, "-c", script],
        cwd=tmp_path,
        env_overrides={"MY_API_KEY_SECRET": long_secret},
        structured_json_output=True,
    )
    result = runner.run(req)

    assert result.exit_code == 0
    # protocol_stdout is raw in-memory protocol data
    assert result.protocol_stdout is not None
    assert json.loads(result.protocol_stdout)["secret"] == long_secret

    # stdout is redacted and safe
    assert long_secret not in result.stdout
    assert "[REDACTED]" in result.stdout
    assert json.loads(result.stdout)["secret"] == "[REDACTED]"


def test_meshy_cli_runner_get_task_uses_protocol_stdout() -> None:
    task_id = "01a0e9c1-f324-75f4-ab7c-59ee3b6c541d"
    raw_v1_json = json.dumps(
        {
            "result": {
                "task": {
                    "task_id": task_id,
                    "status": "SUCCEEDED",
                    "progress": 100,
                    "consumed_credits": 15,
                }
            }
        }
    )
    # Simulate a corrupted/redacted stdout where the task_id or credits was replaced
    corrupted_stdout = json.dumps(
        {
            "result": {
                "task": {
                    "task_id": "[REDACTED]",
                    "status": "SUCCEEDED",
                    "progress": 100,
                    "consumed_credits": "[REDACTED]",
                }
            }
        }
    )

    fake_runner = MagicMock(spec=ProcessRunner)
    fake_runner.run.return_value = CommandResult(
        exit_code=0,
        stdout=corrupted_stdout,
        stderr="",
        protocol_stdout=raw_v1_json,
    )

    cli_runner = MeshyCliRunner(runner=fake_runner)
    # Bypass resolve_runner_cmd
    cli_runner._cached_runner_cmd = ["meshy"]

    task_res = cli_runner.get_task(task_id)
    assert task_res["task_id"] == task_id
    assert task_res["status"] == "SUCCEEDED"
    assert task_res["progress"] == 100
    assert task_res["actual_cost"] == 15.0


def test_meshy_cli_runner_doctor_uses_protocol_stdout() -> None:
    raw_v1_json = json.dumps(
        {
            "result": {
                "cli": {"version": "0.4.0", "node": "v22.12.0", "platform": "win32"},
                "credential_sources": {"env": True},
                "local_ready": True,
            }
        }
    )
    corrupted_stdout = '{"result": "[REDACTED]"}'

    fake_runner = MagicMock(spec=ProcessRunner)
    fake_runner.run.return_value = CommandResult(
        exit_code=0,
        stdout=corrupted_stdout,
        stderr="",
        protocol_stdout=raw_v1_json,
    )

    cli_runner = MeshyCliRunner(runner=fake_runner)
    cli_runner._cached_runner_cmd = ["meshy"]

    doctor_res = cli_runner.doctor()
    assert doctor_res.available is True
    assert doctor_res.status == "AVAILABLE"
    assert doctor_res.cli_version == "0.4.0"
    assert doctor_res.has_credential is True
