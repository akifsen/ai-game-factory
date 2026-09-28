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


def test_blender_dependency_verification_step_structure() -> None:
    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["godot-rendered"]
    steps = job["steps"]

    preflight_step_idx = next(
        idx
        for idx, s in enumerate(steps)
        if "verify_blender_dependencies.py" in str(s.get("run", ""))
    )
    acceptance_step_idx = next(
        idx for idx, s in enumerate(steps) if "verify_asset_acceptance.py" in str(s.get("run", ""))
    )
    fixture_step_idx = next(
        idx for idx, s in enumerate(steps) if "verify_profile_fixtures.py" in str(s.get("run", ""))
    )

    # Dependency verification precedes offline acceptance and profile fixtures
    assert preflight_step_idx < acceptance_step_idx < fixture_step_idx

    preflight_script = steps[preflight_step_idx]["run"]
    # numpy path resolved via /usr/bin/python3
    assert "/usr/bin/python3" in preflight_script
    assert "numpy" in preflight_script
    # GAMEFACTORY_BLENDER_PYTHONPATH exported to GITHUB_ENV
    assert 'echo "GAMEFACTORY_BLENDER_PYTHONPATH=' in preflight_script
    assert '>> "$GITHUB_ENV"' in preflight_script

    # No hard-coded dist-packages anywhere in workflow
    workflow_text = (REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "dist-packages" not in workflow_text

    # No if: always() on fixture or acceptance steps
    assert "always()" not in str(steps[acceptance_step_idx].get("if", ""))
    assert "always()" not in str(steps[fixture_step_idx].get("if", ""))

    # Verifier invoked with --forbid-python-prefix /opt/hostedtoolcache
    assert "--forbid-python-prefix /opt/hostedtoolcache" in preflight_script


def test_godot_rendered_system_python_isolation_invariants() -> None:
    import re

    workflow = yaml.safe_load((REPO_ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8"))
    job = workflow["jobs"]["godot-rendered"]
    steps = job["steps"]

    # 1. godot-rendered has no actions/setup-python step
    for step in steps:
        assert "actions/setup-python" not in step.get("uses", "")

    # 2. test matrix and godot-real still have actions/setup-python
    assert any(
        "actions/setup-python" in step.get("uses", "") for step in workflow["jobs"]["test"]["steps"]
    )
    assert any(
        "actions/setup-python" in step.get("uses", "")
        for step in workflow["jobs"]["godot-real"]["steps"]
    )

    # 3. apt install step precedes build step and includes python3-venv
    apt_step_idx = next(
        idx for idx, s in enumerate(steps) if "Install a local software OpenGL" in s.get("name", "")
    )
    build_step_idx = next(idx for idx, s in enumerate(steps) if "Build wheel" in s.get("name", ""))
    assert apt_step_idx < build_step_idx
    apt_script = steps[apt_step_idx]["run"]
    assert "python3-venv" in apt_script

    # 4. build step contains /usr/bin/python3 -m venv
    build_script = steps[build_step_idx]["run"]
    assert "/usr/bin/python3 -m venv" in build_script

    # 5. no bare python -m, pip install, python3 -m (other than /usr/bin/python3) in run scripts
    for step in steps:
        run_text = step.get("run", "")
        if not run_text:
            continue
        for line in run_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if re.search(r"\bpip\d*\s+install\b", line):
                assert re.search(r"-m\s+pip\b", line), f"bare pip install: {line}"
            if re.search(r"\bpython3\s+-m\b", line):
                assert "/usr/bin/python3" in line, f"bare python3 -m: {line}"
            if re.search(r"\bpython\s+-m\b", line):
                assert re.search(r"[/\$]python\s+-m\b", line), f"bare python -m: {line}"


def test_verify_blender_dependencies_prefix_containment() -> None:
    from scripts.verify_blender_dependencies import is_path_under_prefix

    forbidden = "/opt/hostedtoolcache"

    # Contaminated paths
    assert (
        is_path_under_prefix("/opt/hostedtoolcache/Python/3.12.14/x64/bin/python3.12", forbidden)
        is True
    )
    assert is_path_under_prefix("/opt/hostedtoolcache", forbidden) is True
    assert is_path_under_prefix("/opt/hostedtoolcache/", forbidden) is True

    # Clean paths
    assert is_path_under_prefix("/usr", forbidden) is False
    assert is_path_under_prefix("/usr/bin/python3.12", forbidden) is False
    assert is_path_under_prefix("/opt/hostedtoolcacheX", forbidden) is False
    assert is_path_under_prefix(None, forbidden) is False
