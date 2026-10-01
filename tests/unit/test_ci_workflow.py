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


_FULL_CI_GATE_JOB_NAMES = (
    "test",
    "godot-real",
    "godot-rendered",
    "candidate-unit-installed",
)

_CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES = (
    "candidate-wheel",
    "candidate-v083-real",
)

_DIAGNOSTICS_ONLY_DISPATCH_GUARD = (
    "github.event_name == 'workflow_dispatch'"
    " && github.event.inputs.candidate_diagnostics_only == 'true'"
)

_FULL_CI_PR_LABEL_GUARD = "contains(github.event.pull_request.labels.*.name, 'full-ci')"


def _normalized_job_if(job_data: dict[str, Any]) -> str:
    return " ".join(job_data["if"].split())


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
    dispatch = triggers["workflow_dispatch"]
    assert isinstance(dispatch, dict)
    inputs = dispatch["inputs"]
    assert set(inputs.keys()) == {"candidate_diagnostics_only"}
    diag_input = inputs["candidate_diagnostics_only"]
    assert diag_input["type"] == "boolean"
    assert diag_input["default"] is False
    description = diag_input["description"]
    assert isinstance(description, str)
    assert "diagnostic" in description.lower()
    assert (
        "not release" in description.lower() or "notrelease" in description.replace(" ", "").lower()
    )


def test_ci_workflow_cancels_superseded_pull_request_runs() -> None:
    data, _ = _load_ci_workflow()
    concurrency = data["concurrency"]
    assert "github.event.pull_request.number" in concurrency["group"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_ci_workflow_jobs_and_matrix() -> None:
    data, _ = _load_ci_workflow()
    jobs = data.get("jobs", {})
    assert set(jobs.keys()) == {
        "quick",
        "test",
        "godot-real",
        "godot-rendered",
        "candidate-wheel",
        "candidate-unit-installed",
        "candidate-v083-real",
    }

    test_matrix = jobs["test"]["strategy"]["matrix"]
    assert set(test_matrix["os"]) == {"ubuntu-latest", "windows-latest"}
    assert {str(v) for v in test_matrix["python-version"]} == {"3.11", "3.12"}

    candidate_matrix = jobs["candidate-unit-installed"]["strategy"]["matrix"]["include"]
    assert len(candidate_matrix) == 16
    expected_shards = {(row["os"], row["python-version"], row["shard"]) for row in candidate_matrix}
    assert expected_shards == {
        (os_name, python_version, shard)
        for os_name in ("ubuntu-latest", "windows-latest")
        for python_version in ("3.11", "3.12")
        for shard in (1, 2, 3, 4)
    }


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
        'python -m pytest -ra -p no:cacheprovider -m "not candidate_slow"',
    ):
        assert required in commands
    test_commands = [step.get("run", "") for step in jobs["test"]["steps"]]
    assert 'python -m pytest -ra -p no:cacheprovider -m "not candidate_slow"' in test_commands
    reference_full_gate_if = _normalized_job_if(jobs["test"])
    for name in _FULL_CI_GATE_JOB_NAMES:
        condition = _normalized_job_if(jobs[name])
        assert condition == reference_full_gate_if
        assert condition.startswith("(github.event_name != 'pull_request'")
        assert _FULL_CI_PR_LABEL_GUARD in condition
        assert _DIAGNOSTICS_ONLY_DISPATCH_GUARD.replace(" ", "") in condition.replace(" ", "")
        assert "github.event.label.name == 'full-ci'" in condition
    for name in _CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES:
        condition = _normalized_job_if(jobs[name])
        assert condition.startswith("github.event_name != 'pull_request'")
        assert _FULL_CI_PR_LABEL_GUARD in condition
        assert _DIAGNOSTICS_ONLY_DISPATCH_GUARD.replace(" ", "") in condition.replace(" ", "")
        assert "||" in condition
        assert condition != reference_full_gate_if
    assert "github.event.label.name == 'full-ci'" in quick_if


def test_ci_workflow_candidate_diagnostics_only_truth_table() -> None:
    """Job guards must not treat missing dispatch inputs as diagnostic-only on push/PR."""
    data, _ = _load_ci_workflow()
    jobs = data["jobs"]

    def runs(job_name: str, *, event_name: str, diagnostics_only: bool, pr_full_ci: bool) -> bool:
        assert job_name in jobs
        if event_name == "pull_request" and not pr_full_ci:
            full_ci = False
        elif event_name == "pull_request" and pr_full_ci:
            full_ci = True
        else:
            full_ci = event_name != "pull_request"
        diagnostic_dispatch = event_name == "workflow_dispatch" and diagnostics_only
        if job_name in _FULL_CI_GATE_JOB_NAMES:
            return full_ci and not diagnostic_dispatch
        if job_name in _CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES:
            return full_ci or diagnostic_dispatch
        raise AssertionError(f"unexpected job {job_name}")

    # Default manual dispatch (input false): same expensive set as push to main.
    for job_name in (*_FULL_CI_GATE_JOB_NAMES, *_CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES):
        assert runs(
            job_name, event_name="workflow_dispatch", diagnostics_only=False, pr_full_ci=False
        )

    # Diagnostic-only manual dispatch: wheel + v083 real only.
    for job_name in _FULL_CI_GATE_JOB_NAMES:
        assert not runs(
            job_name,
            event_name="workflow_dispatch",
            diagnostics_only=True,
            pr_full_ci=False,
        )
    for job_name in _CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES:
        assert runs(
            job_name,
            event_name="workflow_dispatch",
            diagnostics_only=True,
            pr_full_ci=False,
        )

    # Push to main: full gate; dispatch input must not matter.
    for job_name in (*_FULL_CI_GATE_JOB_NAMES, *_CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES):
        assert runs(job_name, event_name="push", diagnostics_only=True, pr_full_ci=False)

    # PR without full-ci label: expensive jobs off (quick tested elsewhere).
    for job_name in (*_FULL_CI_GATE_JOB_NAMES, *_CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES):
        assert not runs(
            job_name,
            event_name="pull_request",
            diagnostics_only=False,
            pr_full_ci=False,
        )

    # PR with full-ci label: full expensive set.
    for job_name in (*_FULL_CI_GATE_JOB_NAMES, *_CANDIDATE_DIAGNOSTICS_SLICE_JOB_NAMES):
        assert runs(
            job_name,
            event_name="pull_request",
            diagnostics_only=False,
            pr_full_ci=True,
        )


def test_candidate_jobs_consume_shared_wheel_artifact() -> None:
    data, raw_text = _load_ci_workflow()
    jobs = data["jobs"]
    assert jobs["candidate-unit-installed"]["needs"] == "candidate-wheel"
    assert jobs["candidate-v083-real"]["needs"] == "candidate-wheel"
    consumer_run = next(
        step["run"]
        for step in jobs["candidate-unit-installed"]["steps"]
        if isinstance(step, dict)
        and step.get("name") == "Install shared candidate wheel and run shard"
    )
    assert "find \"$artifact_dir\" -maxdepth 1 -name 'gamefactory-*.whl'" in consumer_run
    assert "python -m build --wheel" not in consumer_run
    assert "candidate-v083-wheel" in raw_text
    assert raw_text.count("name: candidate-v083-wheel") == 3


def test_cached_godot_archive_is_still_verified() -> None:
    data, raw_text = _load_ci_workflow()
    assert raw_text.count("actions/cache@v5") == 3
    assert raw_text.count("sha512sum --check --strict") == 3
    for job_name in ("godot-real", "godot-rendered", "candidate-v083-real"):
        steps = data["jobs"][job_name]["steps"]
        cache_idx = next(
            i for i, step in enumerate(steps) if step.get("uses") == "actions/cache@v5"
        )
        verify_step = steps[cache_idx + 1]
        assert "Download and verify pinned official Godot" in verify_step.get("name", "")
        assert "sha512sum --check --strict" in verify_step.get("run", "")


def _godot_rendered_step_run(name_fragment: str) -> str:
    data, _ = _load_ci_workflow()
    steps = data["jobs"]["godot-rendered"]["steps"]
    for step in steps:
        if isinstance(step, dict) and name_fragment in step.get("name", ""):
            run = step.get("run")
            assert isinstance(run, str)
            return run
    raise AssertionError(f"no godot-rendered step matching {name_fragment!r}")


def test_godot_rendered_internal_rig_skin_verification_step() -> None:
    """Full CI must run six real-tool rig/skin tests from an installed wheel under Xvfb."""
    data, raw_text = _load_ci_workflow()
    run = _godot_rendered_step_run("Verify internal rig and skin with installed wheel")
    steps = data["jobs"]["godot-rendered"]["steps"]
    step = next(
        s
        for s in steps
        if isinstance(s, dict)
        and s.get("name") == "Verify internal rig and skin with installed wheel"
    )
    assert step.get("working-directory") == "${{ runner.temp }}"
    v07_idx = next(
        i
        for i, s in enumerate(steps)
        if isinstance(s, dict) and "Verify V0.7 assemblies" in s.get("name", "")
    )
    internal_idx = steps.index(step)
    archive_idx = next(
        i
        for i, s in enumerate(steps)
        if isinstance(s, dict) and s.get("name") == "Archive offline asset evidence"
    )
    assert v07_idx < internal_idx < archive_idx
    assert "${wheel}[dev]" in run or "${wheel}[dev]" in run
    assert "PYTHONNOUSERSITE=1" in run
    assert "unset PYTHONPATH" in run
    assert 'export GAMEFACTORY_TEST_BLENDER="$(command -v blender)"' in run
    assert "GAMEFACTORY_TEST_GODOT" in run
    assert "GAMEFACTORY_BLENDER_PYTHONPATH" in run
    assert 'xvfb-run -a --server-args="-screen 0 1600x1200x24"' in run
    assert "tests/integration/test_internal_skin_real_tools.py" in run
    assert "tests/integration/test_internal_rig_real_tools.py" in run
    assert "grep -E '(^|[[:space:]])6 tests collected'" in run
    assert "(t,f,e,s)==(6,0,0,0)" in run
    assert ".verification/ci-internal-rig" in run
    assert "assert not str(mod).startswith(str(ws)), mod" in run
    for unit in (
        "test_internal_rig_cold.py",
        "test_internal_rig_evidence_export.py",
        "test_internal_rig_schemas.py",
        "test_internal_skin_region.py",
        "test_internal_skin_validation.py",
        "test_skin_oracle_runner.py",
        "test_skin_oracle_verify.py",
    ):
        assert unit in run
    assert "internal-rig-skin-ci-evidence" in raw_text
    assert "pip install -e" not in run
