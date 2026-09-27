"""Opt-in black-box acceptance against a locally installed Godot executable."""

from __future__ import annotations

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest


@pytest.mark.real_godot
def test_real_godot_acceptance() -> None:
    godot_value = os.environ.get("GAMEFACTORY_TEST_GODOT")
    if not godot_value:
        pytest.skip("Set GAMEFACTORY_TEST_GODOT to run real-engine acceptance")

    repo = Path(__file__).resolve().parents[2]
    godot = Path(godot_value).expanduser().resolve()
    scripts_dir = Path(sys.executable).resolve().parent
    cli_candidates = [scripts_dir / "gamefactory.exe", scripts_dir / "gamefactory"]
    cli = next((path for path in cli_candidates if path.is_file()), None)
    if cli is None:
        pytest.fail(
            f"Installed gamefactory CLI not found beside {sys.executable}: {cli_candidates}"
        )

    evidence_root = repo / ".verification" / "ci-godot"
    evidence_root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report = evidence_root / f"real-godot-{stamp}-{os.getpid()}.json"
    workspace = evidence_root / f"workspace-{stamp}-{os.getpid()}"
    script = repo / "scripts" / "verify_godot_acceptance.py"
    command = [
        sys.executable,
        str(script),
        "--cli",
        str(cli),
        "--fixture",
        str(repo / "examples" / "godot-verification"),
        "--godot",
        str(godot),
        "--output",
        str(report),
        "--workspace",
        str(workspace),
    ]
    completed = subprocess.run(
        command,
        cwd=repo,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=900,
        shell=False,
    )
    if completed.returncode != 0:
        details = report.read_text(encoding="utf-8") if report.exists() else "<report not written>"
        pytest.fail(
            "Real Godot acceptance failed. "
            f"Command: {command!r}\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}\n"
            f"Report: {report}\n{details}"
        )
    assert report.exists(), f"Acceptance script returned success without report: {completed.stdout}"
    result = __import__("json").loads(report.read_text(encoding="utf-8"))
    assert result.get("status") == "PASSED", f"Report is not passed: {report}\n{result}"
