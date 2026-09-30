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
    assert triggers["push"] == {"branches": ["main"], "paths-ignore": ["**/*.md"]}
    assert triggers["pull_request"] == {
        "branches": ["main"],
        "types": ["opened", "synchronize", "reopened", "labeled"],
        "paths-ignore": ["**/*.md"],
    }
    assert triggers["workflow_dispatch"] is None


def test_ci_workflow_cancels_superseded_pull_request_runs() -> None:
    data, _ = _load_ci_workflow()
    concurrency = data["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_ci_workflow_jobs_and_matrix() -> None:
    data, _ = _load_ci_workflow()
    jobs = data.get("jobs", {})
    assert set(jobs.keys()) == {"quick", "test", "godot-real", "godot-rendered"}

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


def test_pull_requests_run_quick_and_full_ci_is_label_or_main() -> None:
    """PRs get the quick job; the expensive set runs on main, dispatch or `full-ci`."""
    data, _ = _load_ci_workflow()
    jobs = data["jobs"]
    quick_if = " ".join(jobs["quick"]["if"].split())
    assert "github.event_name == 'pull_request'" in quick_if
    assert "!contains(github.event.pull_request.labels.*.name, 'full-ci')" in quick_if
    assert jobs["quick"]["runs-on"] == "ubuntu-latest"
    commands = [step.get("run", "") for step in jobs["quick"]["steps"]]
    for required in (
        "ruff check src tests",
        "ruff format --check src tests",
        "mypy src/gamefactory",
        "python -m pytest -ra -p no:cacheprovider",
    ):
        assert required in commands
    for name in ("test", "godot-real", "godot-rendered"):
        condition = " ".join(jobs[name]["if"].split())
        assert condition.startswith("github.event_name != 'pull_request'")
        assert "contains(github.event.pull_request.labels.*.name, 'full-ci')" in condition
        # An unrelated label added to a PR must not start any job.
        assert "github.event.label.name == 'full-ci'" in condition
    assert "github.event.label.name == 'full-ci'" in quick_if


def test_cached_godot_archive_is_still_verified() -> None:
    _, raw_text = _load_ci_workflow()
    assert raw_text.count("actions/cache@v5") == 2
    assert raw_text.count("sha512sum --check --strict") == 2
