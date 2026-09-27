"""The CI acceptance step creates the caller-owned workspace before launch."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_asset_acceptance import AcceptanceFailure, _run  # noqa: E402


def _logical_lines(script: str) -> list[str]:
    lines: list[str] = []
    buffer = ""
    for raw in script.splitlines():
        piece = raw.strip()
        if not piece or piece.startswith("#"):
            continue
        buffer = f"{buffer[:-1].rstrip()} {piece}" if buffer.endswith("\\") else piece
        if buffer.endswith("\\"):
            continue
        lines.append(buffer)
        buffer = ""
    if buffer:
        lines.append(buffer.rstrip("\\").strip())
    return lines


def _acceptance_step() -> str:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["godot-rendered"]
    for step in job["steps"]:
        script = step.get("run")
        if isinstance(script, str) and "verify_asset_acceptance.py" in script:
            return script
    raise AssertionError("godot-rendered has no verify_asset_acceptance.py step")


def test_acceptance_workspace_is_created_before_the_script_runs() -> None:
    lines = _logical_lines(_acceptance_step())
    invocation = next(
        index for index, line in enumerate(lines) if "verify_asset_acceptance.py" in line
    )
    workspace_flag = "--workspace"
    command = lines[invocation]
    flag_at = command.index(workspace_flag)
    workspace = command[flag_at + len(workspace_flag) :].strip().split()[0]
    mkdir_at = [
        index for index, line in enumerate(lines) if line.startswith("mkdir ") and workspace in line
    ]
    assert mkdir_at, f"no mkdir for {workspace}"
    assert min(mkdir_at) < invocation


def test_acceptance_script_still_requires_the_caller_to_create_the_workspace(
    tmp_path: Path,
) -> None:
    missing = tmp_path / "asset-production-workspace"
    with pytest.raises(AcceptanceFailure, match="workspace directory must exist"):
        _run(
            argparse.Namespace(
                workspace=missing,
                output=tmp_path / "acceptance.json",
                cli=None,
                python=None,
                blender=Path("blender"),
                godot=Path("godot"),
            ),
            {},
        )
