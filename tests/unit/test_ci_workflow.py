"""Unit tests verifying GitHub Actions CI workflow configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def _load_ci_workflow() -> tuple[dict[str, Any], str]:
    repo_root = Path(__file__).resolve().parents[2]
    ci_path = repo_root / ".github" / "workflows" / "ci.yml"
    raw_text = ci_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw_text)
    return data, raw_text


def test_ci_workflow_triggers() -> None:
    data, _ = _load_ci_workflow()
    # PyYAML parses unquoted 'on' as boolean True
    triggers = data.get("on", data.get(True))
    assert isinstance(triggers, dict)
    assert set(triggers.keys()) == {"push", "pull_request", "workflow_dispatch"}
    assert triggers["push"] == {"branches": ["main"]}
    assert triggers["pull_request"] == {"branches": ["main"]}
    assert triggers["workflow_dispatch"] is None


def test_ci_workflow_jobs_and_matrix() -> None:
    data, _ = _load_ci_workflow()
    jobs = data.get("jobs", {})
    assert set(jobs.keys()) == {"test", "godot-real", "godot-rendered"}

    test_matrix = jobs["test"]["strategy"]["matrix"]
    assert set(test_matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    assert {str(v) for v in test_matrix["python-version"]} == {"3.11", "3.12"}


def test_ci_workflow_action_versions() -> None:
    _, raw_text = _load_ci_workflow()
    # P3-2 requirement: Node 24 action runtimes
    assert "actions/checkout@v4" not in raw_text
    assert "actions/setup-python@v5" not in raw_text
    assert "actions/upload-artifact@v4" not in raw_text
    assert "actions/checkout@v5" in raw_text
    assert "actions/setup-python@v6" in raw_text
    assert "actions/upload-artifact@v6" in raw_text


def test_ci_workflow_no_secrets_or_meshy_api_key() -> None:
    data, raw_text = _load_ci_workflow()
    assert "MESHY_API_KEY" not in raw_text
    assert "secrets." not in raw_text

    jobs = data.get("jobs", {})
    for _job_name, job_data in jobs.items():
        assert isinstance(job_data, dict)
        job_env = job_data.get("env", {})
        if isinstance(job_env, dict):
            for k in job_env:
                assert "MESHY" not in str(k).upper()
                assert "SECRET" not in str(k).upper()
        for step in job_data.get("steps", []):
            if isinstance(step, dict):
                step_str = str(step)
                assert "MESHY_API_KEY" not in step_str
                assert "secrets." not in step_str
