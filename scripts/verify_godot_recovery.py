"""Black-box crash/recovery checks for real Godot workflow execution.

The helper fault modes run in a separate process and terminate only that helper
with os._exit at controlled persistence boundaries. All project and process
evidence is retained in the requested workspace.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import traceback
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class CheckFailure(AssertionError):
    pass


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _hashes(root: Path) -> dict[str, str]:
    ignored = {".gamefactory", ".godot", ".git", "__pycache__"}
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and not ignored.intersection(path.relative_to(root).parts)
    }


def _json_file(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise CheckFailure(f"Expected JSON object in {path}")
    return value


def _cli(
    records: list[dict[str, Any]],
    cli: Path,
    python: Path,
    project: Path,
    godot: Path,
    *parts: str,
    expected: set[int] | None = None,
    timeout: int = 120,
) -> Any:
    if expected is None:
        expected = {0}
    argv = [str(cli), "--json", "--project", str(project), "--godot-path", str(godot), *parts]
    proc = subprocess.run(
        argv,
        cwd=project,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )
    item: dict[str, Any] = {
        "argv": argv,
        "python": str(python),
        "cwd": str(project),
        "exit_code": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "timed_out": False,
    }
    try:
        item["json"] = json.loads(proc.stdout)
    except json.JSONDecodeError:
        pass
    records.append(item)
    if proc.returncode not in expected:
        raise CheckFailure(f"CLI exit {proc.returncode}, expected {sorted(expected)}: {item}")
    return item.get("json")


def _helper(args: argparse.Namespace) -> int:
    """Create and run one actual workflow, terminating at the requested fault point."""
    from gamefactory.adapters.persistence.database import Database
    from gamefactory.adapters.persistence.migrations import MigrationRunner
    from gamefactory.config.loader import ConfigLoader
    from gamefactory.core.execution.process_runner import ProcessRunner
    from gamefactory.core.policies.policy_engine import PolicyEngine, PolicyRule
    from gamefactory.workflows.engine import WorkflowEngine
    from gamefactory.workflows.godot_verification import (
        create_godot_verification_workflow,
        register_godot_handlers,
    )

    project = args.project.resolve(strict=True)
    package_file = getattr(__import__("gamefactory"), "__file__", None)
    if not isinstance(package_file, str):
        raise RuntimeError("Could not locate the installed gamefactory package")
    package_path = Path(package_file).resolve()
    source_tree = (Path(__file__).resolve().parents[1] / "src").resolve()
    if package_path.is_relative_to(source_tree):
        raise RuntimeError(
            f"Fault helper imported checkout source instead of installed package: {package_path}"
        )
    _write_json(
        Path(str(args.marker)).with_name("helper-ready.json"),
        {
            "package_path": str(package_path),
            "fault_mode": args.fault_mode,
        },
    )

    db = Database(project / ".gamefactory" / "state" / "factory.db")
    migrations = MigrationRunner(db)
    with db.transaction() as conn:
        migrations.init_migration_table(conn)
    migrations.apply_all()
    cfg = ConfigLoader.load_config(project)
    p = cfg.policies
    policy = PolicyEngine(
        PolicyRule(
            require_approval_for_paid=p.paid_operations_require_approval,
            require_approval_for_destructive=p.destructive_operations_require_approval,
            require_approval_for_repo_write=p.require_approval_for_repo_write,
            require_approval_for_process_execution=p.require_approval_for_process_execution,
            max_operation_cost=p.max_operation_cost,
            project_budget=p.project_budget,
        )
    )

    runner = ProcessRunner(sanitize_output=True)
    if args.fault_mode == "after-runtime-return":
        original_run = runner.run

        def faulting_run(request: Any) -> Any:
            result = original_run(request)
            if "--script" in request.args:
                output_arg = request.args[request.args.index("--output") + 1]
                observation = Path(output_arg)
                _write_json(
                    args.marker,
                    {
                        "phase": "after-runtime-return-before-terminal-receipt",
                        "command": result.to_dict(),
                        "observation_path": str(observation),
                        "observation": _json_file(observation) if observation.is_file() else None,
                    },
                )
                os._exit(91)
            return result

        setattr(runner, "run", faulting_run)  # noqa: B010 - test-only fault injection

    engine = WorkflowEngine(project, db, policy_engine=policy, process_runner=runner)
    handlers = register_godot_handlers(
        engine.handler_registry,
        project,
        engine.artifact_mgr,
        engine.art_repo,
        engine.exec_repo,
        engine.evi_repo,
        engine.gate_repo,
        runner,
    )
    if args.fault_mode == "after-terminal-journal":
        original_save = handlers._save_text

        def faulting_save(*values: Any, **kwargs: Any) -> Any:
            artifact_id = original_save(*values, **kwargs)
            filename = kwargs.get("filename")
            if filename is None and len(values) >= 5:
                filename = values[4]
            if filename == "runtime-terminal.json":
                artifact = engine.art_repo.get(artifact_id)
                _write_json(
                    args.marker,
                    {
                        "phase": "after-runtime-terminal-journal-durable",
                        "artifact_id": artifact_id,
                        "relative_path": artifact.relative_path if artifact else None,
                    },
                )
                os._exit(92)
            return artifact_id

        setattr(handlers, "_save_text", faulting_save)  # noqa: B010 - test-only fault injection

    workflow, tasks = create_godot_verification_workflow(
        cfg.project.id,
        project,
        args.godot,
        args.scenario,
    )
    engine.register_workflow(workflow, tasks)
    _write_json(
        args.workflow_file,
        {
            "workflow_id": workflow.id,
            "task_ids": [task.id for task in tasks],
            "execute_task_id": tasks[0].id,
        },
    )
    engine.run_workflow(workflow.id)
    return 0


def _run_helper(
    records: list[dict[str, Any]],
    python: Path,
    script: Path,
    project: Path,
    godot: Path,
    scenario: Path,
    workflow_file: Path,
    marker: Path,
    mode: str,
) -> int:
    argv = [
        str(python),
        str(script),
        "--fault-helper",
        "--fault-mode",
        mode,
        "--project",
        str(project),
        "--godot",
        str(godot),
        "--scenario",
        str(scenario),
        "--workflow-file",
        str(workflow_file),
        "--marker",
        str(marker),
    ]
    try:
        proc = subprocess.run(
            argv,
            cwd=project,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240,
            shell=False,
        )
        records.append(
            {
                "argv": argv,
                "cwd": str(project),
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "timed_out": False,
            }
        )
        expected = 91 if mode == "after-runtime-return" else 92
        if proc.returncode != expected:
            raise CheckFailure(
                f"Fault helper exit {proc.returncode}, expected {expected}: {records[-1]}"
            )
        return proc.returncode
    except subprocess.TimeoutExpired as exc:
        records.append(
            {
                "argv": argv,
                "cwd": str(project),
                "exit_code": None,
                "stdout": str(exc.stdout),
                "stderr": str(exc.stderr),
                "timed_out": True,
                "timeout_seconds": 240,
            }
        )
        raise


def _inspect(
    records: list[dict[str, Any]],
    cli: Path,
    python: Path,
    project: Path,
    godot: Path,
    workflow_id: str,
) -> dict[str, Any]:
    value = _cli(records, cli, python, project, godot, "inspect", workflow_id)
    if not isinstance(value, dict):
        raise CheckFailure("inspect did not return a JSON object")
    return value


def _artifacts(
    records: list[dict[str, Any]],
    cli: Path,
    python: Path,
    project: Path,
    godot: Path,
    workflow_id: str,
) -> list[dict[str, Any]]:
    value = _cli(records, cli, python, project, godot, "artifacts", "--workflow", workflow_id)
    value = (
        value
        if isinstance(value, list)
        else value.get("artifacts", [])
        if isinstance(value, dict)
        else []
    )
    if not isinstance(value, list):
        raise CheckFailure("artifacts did not return a JSON array")
    return [item for item in value if isinstance(item, dict)]


def _status(snapshot: dict[str, Any], expected: str, label: str) -> None:
    workflow = snapshot.get("workflow", {})
    actual = workflow.get("status") if isinstance(workflow, dict) else None
    if actual != expected:
        raise CheckFailure(f"{label}: workflow status {actual!r}, expected {expected!r}")


def _task_executions(snapshot: dict[str, Any], task_id: str) -> list[dict[str, Any]]:
    return [
        item
        for item in snapshot.get("executions", [])
        if isinstance(item, dict) and item.get("task_id") == task_id
    ]


def _runtime_snapshots(project: Path, artifacts: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    expected_name = {"0": (100, 3, 0), "30": (80, 3, 0), "60": (80, 2, 100), "90": (40, 2, 100)}
    for artifact in artifacts:
        if artifact.get("artifact_type") != "godot-runtime-observation":
            continue
        relative = artifact.get("relative_path")
        if not isinstance(relative, str):
            continue
        path = (project / relative).resolve()
        try:
            path.relative_to(project.resolve())
        except ValueError:
            continue
        if not path.is_file():
            continue
        value = _json_file(path)
        snapshots = value.get("snapshots")
        if not isinstance(snapshots, list):
            continue
        observed: dict[str, dict[str, Any]] = {}
        for sample in snapshots:
            if not isinstance(sample, dict):
                continue
            tick = sample.get("tick")
            state = sample.get("state", sample.get("observation", sample))
            if isinstance(state, dict):
                observed[str(tick)] = state
        normalized: dict[str, dict[str, Any]] = {}
        for tick, (hp, enemies, score) in expected_name.items():
            state = observed.get(tick)
            if not isinstance(state, dict):
                continue
            actual = {
                "player_hp": state.get("player_hp"),
                "enemies_remaining": state.get("enemies_remaining"),
                "score": state.get("score"),
            }
            if actual != {"player_hp": hp, "enemies_remaining": enemies, "score": score}:
                raise CheckFailure(f"Real runtime snapshot at tick {tick} differs: {actual}")
            normalized[tick] = actual
        if set(normalized) == set(expected_name):
            return normalized
    raise CheckFailure(
        "No runtime-observation artifact contained all four independently expected snapshots"
    )


def _main(args: argparse.Namespace) -> int:
    cli, python, fixture, godot = (
        p.expanduser().resolve(strict=True)
        for p in (args.cli, args.python, args.fixture, args.godot)
    )
    output = args.output.expanduser().resolve()
    workspace = (
        (args.workspace or output.parent / (output.stem + "-evidence")).expanduser().resolve()
    )
    checkout = Path(__file__).resolve().parents[1]
    if workspace.is_relative_to(checkout):
        raise CheckFailure("--workspace must be outside the source checkout")
    run_root = (
        workspace
        / f"godot-recovery-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
        "cli": str(cli),
        "python": str(python),
        "fixture": str(fixture),
        "godot": str(godot),
        "workspace": str(run_root),
        "commands": [],
        "checks": {},
        "projects": [],
    }
    records: list[dict[str, Any]] = report["commands"]
    try:
        if not fixture.is_dir() or not (fixture / "project.godot").is_file():
            raise CheckFailure(f"Not a Godot project fixture: {fixture}")
        doctor_project = run_root / "Godot doctor 空間"
        shutil.copytree(
            fixture,
            doctor_project,
            ignore=shutil.ignore_patterns(".godot", ".gamefactory", ".git", "__pycache__"),
        )
        _cli(records, cli, python, doctor_project, godot, "init")
        doctor = _cli(records, cli, python, doctor_project, godot, "doctor")
        if (
            not isinstance(doctor, dict)
            or doctor.get("capabilities", {}).get("engine.godot.detect", {}).get("status")
            != "AVAILABLE"
        ):
            raise CheckFailure(f"Explicit-Godot doctor did not report AVAILABLE: {doctor!r}")
        report["checks"]["doctor"] = doctor
        for name, mode in (
            ("uncertain", "after-runtime-return"),
            ("terminal", "after-terminal-journal"),
        ):
            project = run_root / f"Godot recuperación {name} Δ" / "juego 空間"
            project.parent.mkdir(parents=True, exist_ok=False)
            shutil.copytree(
                fixture,
                project,
                ignore=shutil.ignore_patterns(".godot", ".gamefactory", ".git", "__pycache__"),
            )
            baseline = _hashes(project)
            _cli(records, cli, python, project, godot, "init")
            scenario = project / "scenario.json"
            workflow_file = run_root / f"{name}-workflow.json"
            marker = run_root / f"{name}-fault.json"
            _run_helper(
                records,
                python,
                Path(__file__).resolve(),
                project,
                godot,
                scenario,
                workflow_file,
                marker,
                mode,
            )
            helper_ready = _json_file(marker.with_name("helper-ready.json"))
            workflow_info = _json_file(workflow_file)
            workflow_id = str(workflow_info["workflow_id"])
            task_id = str(workflow_info["execute_task_id"])
            pre = _inspect(records, cli, python, project, godot, workflow_id)
            pre_artifacts = _artifacts(records, cli, python, project, godot, workflow_id)
            mode_result: dict[str, Any] = {
                "mode": mode,
                "project": str(project),
                "workflow_id": workflow_id,
                "task_ids": workflow_info["task_ids"],
                "helper_package": helper_ready,
                "fault_marker": _json_file(marker),
                "pre_recovery_inspect": pre,
                "pre_recovery_artifacts": pre_artifacts,
                "source_hashes_before": baseline,
            }
            if mode == "after-runtime-return":
                _status(pre, "RUNNING", "before uncertain recovery")
                resume1 = _cli(
                    records, cli, python, project, godot, "resume", workflow_id, expected={3}
                )
                after1 = _inspect(records, cli, python, project, godot, workflow_id)
                arts1 = _artifacts(records, cli, python, project, godot, workflow_id)
                resume2 = _cli(
                    records, cli, python, project, godot, "resume", workflow_id, expected={3}
                )
                retry = _cli(
                    records,
                    cli,
                    python,
                    project,
                    godot,
                    "retry",
                    workflow_id,
                    task_id,
                    expected={1},
                )
                after2 = _inspect(records, cli, python, project, godot, workflow_id)
                arts2 = _artifacts(records, cli, python, project, godot, workflow_id)
                _status(after2, "BLOCKED", "uncertain repeated resume/retry")
                attempts = _task_executions(after2, task_id)
                initial_attempts = _task_executions(pre, task_id)
                if (
                    len(attempts) != 1
                    or len(initial_attempts) != 1
                    or attempts[0].get("status") != "UNCERTAIN"
                    or attempts[0].get("id") != initial_attempts[0].get("id")
                ):
                    raise CheckFailure(
                        "Uncertain execution identity/status changed across recovery"
                    )
                if not {item.get("id") for item in pre_artifacts}.issubset(
                    {item.get("id") for item in arts2}
                ):
                    raise CheckFailure("Uncertain recovery removed previously recorded artifacts")
                if any(a.get("relative_path", "").endswith("runtime-terminal.json") for a in arts2):
                    raise CheckFailure("Uncertain runtime unexpectedly has a terminal receipt")
                fault = mode_result["fault_marker"]
                command = fault.get("command", {})
                if (
                    command.get("exit_code") != 0
                    or command.get("timed_out")
                    or command.get("pid") is None
                    or command.get("cleanup_completed") is not True
                ):
                    raise CheckFailure(
                        "Fault point was not reached after a completed real runtime process"
                    )
                mode_result.update(
                    {
                        "resume1": resume1,
                        "after_resume1": after1,
                        "artifacts_after_resume1": arts1,
                        "resume2": resume2,
                        "retry": retry,
                        "after_resume_and_retry": after2,
                        "artifacts_after_resume_and_retry": arts2,
                        "assertion": "blocked/uncertain; one attempt retained; no terminal receipt",
                    }
                )
            else:
                _status(pre, "RUNNING", "before terminal recovery")
                if not any(
                    a.get("relative_path", "").endswith("runtime-terminal.json")
                    for a in pre_artifacts
                ):
                    raise CheckFailure("Terminal fault did not retain runtime terminal journal")
                resume = _cli(
                    records, cli, python, project, godot, "resume", workflow_id, expected={2}
                )
                recovered = _inspect(records, cli, python, project, godot, workflow_id)
                retained = _artifacts(records, cli, python, project, godot, workflow_id)
                if not {item.get("id") for item in pre_artifacts}.issubset(
                    {item.get("id") for item in retained}
                ):
                    raise CheckFailure(
                        "Safe-terminal recovery removed previously recorded artifacts"
                    )
                _status(recovered, "FAILED", "safe-terminal recovery")
                retry = _cli(
                    records, cli, python, project, godot, "retry", workflow_id, task_id, timeout=300
                )
                completed = _inspect(records, cli, python, project, godot, workflow_id)
                final_artifacts = _artifacts(records, cli, python, project, godot, workflow_id)
                _status(completed, "COMPLETED", "explicit retry completion")
                attempts = _task_executions(completed, task_id)
                if (
                    len(attempts) != 2
                    or attempts[0].get("status") != "FAILED"
                    or attempts[1].get("status") != "COMPLETED"
                    or attempts[0].get("id")
                    not in {e.get("id") for e in _task_executions(recovered, task_id)}
                ):
                    raise CheckFailure(
                        "Safe-terminal recovery did not retain its failed attempt and new completion"
                    )
                if any(task.get("status") != "COMPLETED" for task in completed.get("tasks", [])):
                    raise CheckFailure("Explicit retry did not complete every workflow task")
                if not completed.get("gates") or any(
                    gate.get("status") != "PASSED" for gate in completed.get("gates", [])
                ):
                    raise CheckFailure(
                        "Explicit retry did not independently pass all quality gates"
                    )
                mode_result["verified_runtime_snapshots"] = _runtime_snapshots(
                    project, final_artifacts
                )
                if not any(
                    a.get("relative_path", "").endswith("runtime-terminal.json")
                    for a in final_artifacts
                ):
                    raise CheckFailure("Old terminal journal/artifacts were not retained")
                if not {item.get("id") for item in retained}.issubset(
                    {item.get("id") for item in final_artifacts}
                ):
                    raise CheckFailure(
                        "Explicit retry removed artifacts from the retained failed attempt"
                    )
                mode_result.update(
                    {
                        "resume": resume,
                        "after_recovery": recovered,
                        "artifacts_after_recovery": retained,
                        "retry": retry,
                        "after_retry": completed,
                        "artifacts_after_retry": final_artifacts,
                        "assertion": "safe terminal marked failed; explicit new real attempt completed",
                    }
                )
            current = _hashes(project)
            if current != baseline:
                raise CheckFailure(f"Godot source changed under Factory in {project}")
            mode_result["source_hashes_after"] = current
            report["projects"].append(mode_result)
        report["status"] = "PASSED"
    except Exception as exc:
        report["status"] = "FAILED"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    output.parent.mkdir(parents=True, exist_ok=True)
    _write_json(output, report)
    print(
        json.dumps(
            {
                "status": report["status"],
                "report": str(output),
                "workspace": str(run_root),
                "error": report.get("error"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if report["status"] == "PASSED" else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path)
    parser.add_argument("--python", type=Path)
    parser.add_argument("--fixture", type=Path)
    parser.add_argument("--godot", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--fault-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--fault-mode", choices=("after-runtime-return", "after-terminal-journal"))
    parser.add_argument("--project", type=Path)
    parser.add_argument("--scenario", type=Path)
    parser.add_argument("--workflow-file", type=Path)
    parser.add_argument("--marker", type=Path)
    args = parser.parse_args()
    if args.fault_helper:
        if not all(
            (
                args.fault_mode,
                args.project,
                args.godot,
                args.scenario,
                args.workflow_file,
                args.marker,
            )
        ):
            parser.error("fault helper arguments are incomplete")
        return _helper(args)
    if not all((args.cli, args.python, args.fixture, args.godot, args.output)):
        parser.error("--cli, --python, --fixture, --godot, and --output are required")
    return _main(args)


if __name__ == "__main__":
    raise SystemExit(main())
