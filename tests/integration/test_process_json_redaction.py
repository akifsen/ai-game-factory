"""Structured subprocess transport remains valid JSON without leaking credentials."""

import json
import sys
from pathlib import Path

from gamefactory.core.execution.process_runner import CommandRequest, ProcessRunner


def test_json_redaction_preserves_transport_and_hides_nested_secrets(tmp_path: Path) -> None:
    payload = {
        "result": {
            "detail": "API commands need `meshy auth login`.",
            "credential_sources": {"env": True, "stored_profile": {"exists": False}},
            "api_key": "synthetic-secret-value",
            "token": 3141592653,
            "secret": {"nested": ["other-synthetic-value"]},
            "task": {"task_id": "task-123", "consumed_credits": 10},
        }
    }
    script = tmp_path / "emit.py"
    script.write_text("import json\nprint(json.dumps(" + repr(payload) + "))\n", encoding="utf-8")
    result = ProcessRunner().run(
        CommandRequest([sys.executable, str(script)], tmp_path, structured_json_output=True)
    )
    decoded = json.loads(result.stdout)
    assert decoded["result"]["task"] == payload["result"]["task"]
    assert decoded["result"]["credential_sources"]["env"] is True
    assert "synthetic-secret-value" not in result.stdout
    assert "other-synthetic-value" not in result.stdout
    assert decoded["result"]["api_key"] == "[REDACTED]"
    assert decoded["result"]["token"] == "[REDACTED]"


def test_invalid_json_falls_back_to_redacted_text(tmp_path: Path) -> None:
    result = ProcessRunner().run(
        CommandRequest(
            [sys.executable, "-c", "print('invalid JSON password=synthetic-sensitive')"],
            tmp_path,
            structured_json_output=True,
        )
    )
    assert "synthetic-sensitive" not in result.stdout
    assert "[REDACTED]" in result.stdout
