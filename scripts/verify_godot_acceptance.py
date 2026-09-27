"""Black-box acceptance for the real Godot verification workflow.

Every Factory CLI operation runs in a fresh subprocess. The script keeps its run
workspace and report so failures remain inspectable; it never cleans evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import traceback
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


class AcceptanceFailure(AssertionError):
    pass


def _hashes(root: Path) -> dict[str, str]:
    ignored = {".gamefactory", ".godot", ".git", "__pycache__"}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or any(part in ignored for part in path.relative_to(root).parts):
            continue
        rel = str(path.relative_to(root)).replace("\\", "/")
        result[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _copy_fixture(source: Path, target: Path) -> None:
    ignored = shutil.ignore_patterns(".godot", ".gamefactory", ".git", "__pycache__")
    shutil.copytree(source, target, ignore=ignored)


def _json_artifacts(project: Path, artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for artifact in artifacts:
        path_value = artifact.get("relative_path")
        if not isinstance(path_value, str):
            continue
        path = (project / path_value).resolve()
        try:
            path.relative_to(project.resolve())
        except ValueError:
            continue
        if not path.is_file() or path.suffix.lower() != ".json":
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        found.append({"artifact": artifact, "path": str(path), "value": value})
    return found


def _walk(value: Any) -> Iterator[dict[str, Any]]:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk(child)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--cli", type=Path, required=True, help="absolute installed gamefactory executable"
    )
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--godot", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, help="persistent evidence/workspace parent")
    args = parser.parse_args()

    cli = args.cli.expanduser().resolve()
    fixture = args.fixture.expanduser().resolve()
    godot = args.godot.expanduser().resolve()
    output = args.output.expanduser().resolve()
    workspace_parent = (
        (args.workspace or output.parent / (output.stem + "-evidence")).expanduser().resolve()
    )
    run_root = (
        workspace_parent
        / f"godot-acceptance-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    )
    run_root.mkdir(parents=True, exist_ok=False)
    records: list[dict[str, Any]] = []
    checks: dict[str, Any] = {}
    result: dict[str, Any] = {
        "schema_version": 1,
        "status": "RUNNING",
        "started_at": datetime.now(UTC).isoformat(),
        "cli": str(cli),
        "fixture": str(fixture),
        "godot": str(godot),
        "workspace": str(run_root),
        "commands": records,
        "checks": checks,
        "projects": [],
    }

    def run(
        argv: list[str],
        *,
        expected: set[int] | frozenset[int] = frozenset({0}),
        timeout: float = 120.0,
        cwd: Path | None = None,
    ) -> tuple[subprocess.CompletedProcess[str], Any | None]:
        try:
            proc = subprocess.run(
                argv,
                cwd=str(cwd or run_root),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                shell=False,
            )
            item: dict[str, Any] = {
                "argv": argv,
                "cwd": str(cwd or run_root),
                "exit_code": proc.returncode,
                "stdout": proc.stdout,
                "stderr": proc.stderr,
                "timed_out": False,
            }
            records.append(item)
            parsed: Any | None = None
            if "--json" in argv and proc.stdout.strip():
                try:
                    parsed = json.loads(proc.stdout)
                    item["json"] = parsed
                except json.JSONDecodeError:
                    item["json_parse_error"] = True
            if proc.returncode not in expected:
                raise AcceptanceFailure(
                    f"Unexpected exit {proc.returncode}, expected {sorted(expected)}: {item}"
                )
            return proc, parsed
        except subprocess.TimeoutExpired as exc:
            item = {
                "argv": argv,
                "cwd": str(cwd or run_root),
                "exit_code": None,
                "stdout": _decode(exc.stdout),
                "stderr": _decode(exc.stderr),
                "timed_out": True,
                "timeout_seconds": timeout,
            }
            records.append(item)
            raise

    def factory(
        project: Path,
        *parts: str,
        expected: set[int] | frozenset[int] = frozenset({0}),
    ) -> tuple[subprocess.CompletedProcess[str], Any]:
        argv = [str(cli), "--json", "--project", str(project), "--godot-path", str(godot), *parts]
        try:
            proc, payload = run(argv, expected=expected, cwd=project)
        except AcceptanceFailure:
            record = records[-1] if records else {}
            payload = record.get("json")
            if (
                parts[:2] == ("run", "godot-verify")
                and 0 in expected
                and isinstance(payload, dict)
                and isinstance(payload.get("workflow_id"), str)
            ):
                workflow_id = payload["workflow_id"]
                _, inspected = run(
                    [
                        str(cli),
                        "--json",
                        "--project",
                        str(project),
                        "--godot-path",
                        str(godot),
                        "inspect",
                        workflow_id,
                    ],
                    expected={0},
                    cwd=project,
                )
                _, artifacts = run(
                    [
                        str(cli),
                        "--json",
                        "--project",
                        str(project),
                        "--godot-path",
                        str(godot),
                        "artifacts",
                        "--workflow",
                        workflow_id,
                    ],
                    expected={0},
                    cwd=project,
                )
                checks.setdefault("unexpected_workflow_failures", []).append(
                    {"response": payload, "inspect": inspected, "artifacts": artifacts}
                )
            raise
        if payload is None:
            raise AcceptanceFailure(f"Factory command did not emit JSON: {records[-1]}")
        return proc, payload

    def new_project(name: str) -> tuple[Path, Path]:
        project = run_root / name
        _copy_fixture(fixture, project)
        baseline = _hashes(project)
        factory(project, "init")
        result["projects"].append(
            {"name": name, "path": str(project), "source_hashes_before": baseline}
        )
        return project, project / "scenario.json"

    def inspect(project: Path, workflow_id: str) -> dict[str, Any]:
        _, payload = factory(project, "inspect", workflow_id)
        if not isinstance(payload, dict):
            raise AcceptanceFailure(f"inspect did not emit a JSON object: {records[-1]}")
        return payload

    def observations(project: Path, workflow_id: str) -> dict[str, Any]:
        _, payload = factory(project, "artifacts", "--workflow", workflow_id)
        artifacts = payload if isinstance(payload, list) else payload.get("artifacts", [])
        if not isinstance(artifacts, list):
            raise AcceptanceFailure(f"artifacts did not emit an array: {payload!r}")
        docs = _json_artifacts(project, artifacts)
        result["checks"].setdefault("artifact_documents", {})[workflow_id] = docs
        snapshots: dict[str, Any] = {}
        for doc in docs:
            for node in _walk(doc["value"]):
                if isinstance(node.get("snapshots"), list):
                    for sample in node["snapshots"]:
                        if isinstance(sample, dict):
                            tick = sample.get("tick")
                            state = sample.get("state", sample.get("observation", sample))
                            if tick is not None and isinstance(state, dict):
                                snapshots[str(tick)] = state
                if isinstance(node.get("tick"), int) and any(
                    k in node for k in ("player_hp", "enemies_remaining", "score")
                ):
                    snapshots[str(node["tick"])] = {
                        k: node[k] for k in ("player_hp", "enemies_remaining", "score") if k in node
                    }
        return snapshots

    def check_sources_unchanged(project: Path, name: str) -> None:
        baseline = next(p["source_hashes_before"] for p in result["projects"] if p["name"] == name)
        current = _hashes(project)
        if current != baseline:
            raise AcceptanceFailure(
                f"Factory changed project source files for {name}: baseline/current differ"
            )
        checks.setdefault("source_hashes_unchanged", []).append(name)

    def configure_process_approval(project: Path) -> None:
        import yaml

        config_path = project / ".gamefactory" / "factory.yml"
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        config.setdefault("policies", {})["require_approval_for_process_execution"] = True
        config_path.write_text(
            yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )

    try:
        for label, path in (("CLI", cli), ("fixture", fixture), ("Godot", godot)):
            if not path.exists():
                raise AcceptanceFailure(f"{label} path does not exist: {path}")
        if not cli.is_absolute() or not godot.is_absolute():
            raise AcceptanceFailure("--cli and --godot must resolve to absolute paths")
        run([str(cli), "--version"], expected={0})
        run([str(godot), "--version"], expected={0}, timeout=20)
        version = records[-1]["stdout"].strip()
        result["godot_version"] = version
        run([str(godot), "--help"], expected={0}, timeout=20)
        result["godot_help_observed"] = records[-1]["stdout"] + records[-1]["stderr"]

        project, scenario = new_project("fixture game Ω")
        semantic_runs: list[dict[str, Any]] = []
        expected_snapshots = {
            "0": {"player_hp": 100, "enemies_remaining": 3, "score": 0},
            "30": {"player_hp": 80, "enemies_remaining": 3, "score": 0},
            "60": {"player_hp": 80, "enemies_remaining": 2, "score": 100},
            "90": {"player_hp": 40, "enemies_remaining": 2, "score": 100},
        }
        for index in range(1, 4):
            _, response = factory(project, "run", "godot-verify", "--scenario", str(scenario))
            workflow_id = response.get("workflow_id")
            if not workflow_id or response.get("status") != "COMPLETED":
                raise AcceptanceFailure(f"positive run {index} did not complete: {response}")
            state = inspect(project, workflow_id)
            snapshots = observations(project, workflow_id)
            if snapshots != expected_snapshots:
                raise AcceptanceFailure(
                    f"positive run {index} observations {snapshots!r} != {expected_snapshots!r}"
                )
            final = snapshots.get("90")
            expected_final = {"player_hp": 40, "enemies_remaining": 2, "score": 100}
            if final != expected_final:
                raise AcceptanceFailure(
                    f"run {index} final observed state {final!r} != {expected_final!r}; inspect artifacts"
                )
            workflow_record = state.get("workflow", {})
            if workflow_record.get("status") != "COMPLETED":
                raise AcceptanceFailure(f"positive workflow is not completed: {workflow_record}")
            if len(state.get("tasks", [])) != 3 or any(
                task.get("status") != "COMPLETED" for task in state.get("tasks", [])
            ):
                raise AcceptanceFailure(
                    f"positive workflow task states are incomplete: {state.get('tasks')}"
                )
            gates = state.get("gates", [])
            if not gates or any(gate.get("status") != "PASSED" for gate in gates):
                raise AcceptanceFailure(
                    f"positive workflow did not pass all quality gates: {gates}"
                )
            docs = result["checks"]["artifact_documents"][workflow_id]
            process_records = {
                doc["artifact"].get("artifact_type"): doc["value"]
                for doc in docs
                if doc["artifact"].get("artifact_type")
                in {"godot-import-process", "godot-runtime-process"}
            }
            if set(process_records) != {"godot-import-process", "godot-runtime-process"}:
                raise AcceptanceFailure(
                    f"positive run lacks separate import/runtime process records: {process_records.keys()}"
                )
            for phase, process_record in process_records.items():
                if (
                    process_record.get("exit_code") != 0
                    or process_record.get("timed_out") is not False
                ):
                    raise AcceptanceFailure(
                        f"{phase} process metadata did not show clean completion: {process_record}"
                    )
            reports = [
                node
                for doc in docs
                for node in _walk(doc["value"])
                if isinstance(node.get("findings"), list) and node.get("status") == "PASS"
            ]
            pass_findings = [finding for report in reports for finding in report["findings"]]
            if len(pass_findings) != 6 or any(
                finding.get("status") != "PASS" for finding in pass_findings
            ):
                raise AcceptanceFailure(
                    f"positive run did not retain six independent PASS findings: {pass_findings}"
                )
            semantic_runs.append(
                {
                    "workflow_id": workflow_id,
                    "final": final,
                    "snapshot_ticks": sorted(snapshots, key=int),
                    "task_count": len(state.get("tasks", [])),
                    "execution_count": len(state.get("executions", [])),
                    "workflow_status": workflow_record.get("status"),
                    "gate_statuses": [gate.get("status") for gate in gates],
                    "independent_findings": pass_findings,
                    "process_records": process_records,
                }
            )
            if index == 1:
                before = len(state.get("executions", []))
                _, resumed = factory(project, "resume", workflow_id)
                after_state = inspect(project, workflow_id)
                if (
                    resumed.get("status") != "COMPLETED"
                    or len(after_state.get("executions", [])) != before
                ):
                    raise AcceptanceFailure(
                        "resume on completed verification started another execution"
                    )
                checks["completed_resume_does_not_reexecute"] = workflow_id
        if len({json.dumps(run["final"], sort_keys=True) for run in semantic_runs}) != 1:
            raise AcceptanceFailure(f"three real runs differed semantically: {semantic_runs}")
        checks["three_real_semantic_runs"] = semantic_runs
        check_sources_unchanged(project, "fixture game Ω")

        # Independent oracle negative: alter only expected value in an isolated project copy.
        wrong_project, wrong_scenario = new_project("wrong expectation Ω")
        wrong = json.loads(wrong_scenario.read_text(encoding="utf-8"))
        wrong["assertions"][-3]["expected"] = 41
        wrong_scenario.write_text(
            json.dumps(wrong, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        next(p for p in result["projects"] if p["name"] == "wrong expectation Ω")[
            "source_hashes_before"
        ] = _hashes(wrong_project)
        _, wrong_response = factory(
            wrong_project, "run", "godot-verify", "--scenario", str(wrong_scenario), expected={2}
        )
        if wrong_response.get("status") not in {"FAILED", "BLOCKED"}:
            raise AcceptanceFailure(f"wrong expected value unexpectedly passed: {wrong_response}")
        wrong_id = wrong_response.get("workflow_id")
        if not isinstance(wrong_id, str):
            raise AcceptanceFailure(
                f"Wrong-expectation run response omitted workflow id: {wrong_response}"
            )
        wrong_state = inspect(wrong_project, wrong_id)
        wrong_snapshots = observations(wrong_project, wrong_id)
        if wrong_snapshots.get("90", {}).get("player_hp") != 40:
            raise AcceptanceFailure(
                f"Wrong-expectation run did not observe actual player_hp=40: {wrong_snapshots}"
            )
        wrong_docs = result["checks"]["artifact_documents"][wrong_id]
        failed_hp = [
            finding
            for doc in wrong_docs
            for node in _walk(doc["value"])
            for finding in node.get("findings", [])
            if finding.get("assertion_id") == "final-player-hp"
        ]
        if not any(
            finding.get("expected") == 41
            and finding.get("actual") == 40
            and finding.get("status") == "FAIL"
            for finding in failed_hp
        ):
            raise AcceptanceFailure(
                f"Independent report lacks expected41/actual40 failure: {failed_hp}"
            )
        checks["wrong_expected_value_rejected"] = {
            "workflow_id": wrong_id,
            "response": wrong_response,
            "actual_final_state": wrong_snapshots.get("90"),
            "expected_final_player_hp": 41,
            "evidence_count": len(wrong_state.get("evidence", [])),
        }
        check_sources_unchanged(wrong_project, "wrong expectation Ω")

        # Fixture mutation: change game logic in a private copy while keeping the oracle fixed.
        mutated, mutated_scenario = new_project("logic mutation Ω")
        script_path = mutated / "main.gd"
        source = script_path.read_text(encoding="utf-8")
        if "amount" not in source or "apply_damage" not in source:
            raise AcceptanceFailure(
                "Fixture mutation anchor was not found; refusing to claim mutation coverage"
            )
        mutated_source = source.replace("_player_hp - amount", "_player_hp - amount - 1", 1)
        if mutated_source == source:
            mutated_source = source.replace("_player_hp - 20", "_player_hp - 21", 1)
        if mutated_source == source:
            raise AcceptanceFailure("Unable to mutate fixture damage behavior deterministically")
        script_path.write_text(mutated_source, encoding="utf-8")
        _, mutation_response = factory(
            mutated, "run", "godot-verify", "--scenario", str(mutated_scenario), expected={2}
        )
        if mutation_response.get("status") != "FAILED":
            raise AcceptanceFailure(
                f"logic mutation was not rejected as failure: {mutation_response}"
            )
        mutation_snapshots = observations(mutated, mutation_response["workflow_id"])
        if mutation_snapshots.get("90", {}).get("player_hp") != 38:
            raise AcceptanceFailure(
                f"Damage mutation did not produce observed final health38: {mutation_snapshots}"
            )
        checks["mutated_logic_rejected"] = {
            "workflow_id": mutation_response.get("workflow_id"),
            "source": str(script_path),
            "source_hash": hashlib.sha256(mutated_source.encode()).hexdigest(),
            "actual_final_state": mutation_snapshots.get("90"),
        }

        # A real GDScript parse failure must fail the workflow and retain its failure artifacts.
        parse_project, parse_scenario = new_project("script parse failure Ω")
        parse_script = parse_project / "main.gd"
        parse_script.write_text(
            "extends Node\nfunc _ready():\n    this is not valid gdscript !!!\n", encoding="utf-8"
        )
        _, parse_response = factory(
            parse_project, "run", "godot-verify", "--scenario", str(parse_scenario), expected={2}
        )
        if parse_response.get("status") != "FAILED":
            raise AcceptanceFailure(f"real script parse error was not rejected: {parse_response}")
        parse_state = inspect(parse_project, parse_response["workflow_id"])
        if (
            not parse_state.get("artifacts")
            and not parse_state.get("evidence")
            and not parse_state.get("executions")
        ):
            raise AcceptanceFailure("parse failure retained no process/evidence/artifact record")
        parse_docs = _json_artifacts(parse_project, parse_state.get("artifacts", []))
        parse_proof = (
            "\n".join(json.dumps(doc["value"], ensure_ascii=False) for doc in parse_docs)
            + "\n"
            + "\n".join(
                str(record.get("stdout", "")) + str(record.get("stderr", ""))
                for record in records
                if str(parse_project) in str(record.get("cwd", ""))
            )
        )
        if "parse" not in parse_proof.casefold() and "syntax" not in parse_proof.casefold():
            raise AcceptanceFailure(
                "Failure run did not retain identifiable GDScript parse diagnostics"
            )
        checks["script_parse_failure_retained"] = {
            "workflow_id": parse_response["workflow_id"],
            "artifacts": parse_state.get("artifacts", []),
            "executions": parse_state.get("executions", []),
        }

        # Finite blocking call in _ready provides a bounded runtime timeout without runaway CPU.
        timeout_project, timeout_scenario = new_project("finite timeout Ω")
        timeout_script = timeout_project / "main.gd"
        timeout_text = timeout_script.read_text(encoding="utf-8")
        timeout_text = timeout_text + "\nfunc _ready() -> void:\n\tOS.delay_msec(2500)\n"
        if timeout_text == timeout_script.read_text(encoding="utf-8"):
            raise AcceptanceFailure("Could not add finite delay to fixture _ready")
        timeout_script.write_text(timeout_text, encoding="utf-8")
        timeout_doc = json.loads(timeout_scenario.read_text(encoding="utf-8"))
        timeout_doc["timeout_seconds"] = 1
        timeout_scenario.write_text(json.dumps(timeout_doc, indent=2) + "\n", encoding="utf-8")
        _, timeout_response = factory(
            timeout_project,
            "run",
            "godot-verify",
            "--scenario",
            str(timeout_scenario),
            expected={2},
        )
        if timeout_response.get("status") != "FAILED":
            raise AcceptanceFailure(f"finite runtime timeout was not rejected: {timeout_response}")
        timeout_state = inspect(timeout_project, timeout_response["workflow_id"])
        checks["finite_runtime_timeout_retained"] = {
            "workflow_id": timeout_response["workflow_id"],
            "executions": timeout_state.get("executions", []),
            "artifacts": timeout_state.get("artifacts", []),
        }
        if not timeout_state.get("executions"):
            raise AcceptanceFailure("timeout attempt metadata was not retained")
        timeout_docs = _json_artifacts(timeout_project, timeout_state.get("artifacts", []))
        timeout_metadata = [
            node
            for doc in timeout_docs
            for node in _walk(doc["value"])
            if node.get("timed_out") is True
        ]
        if not timeout_metadata:
            raise AcceptanceFailure("No retained process metadata identifies the real timeout")
        checks["finite_runtime_timeout_retained"]["timeout_metadata"] = timeout_metadata

        # Process approval: no import/runtime attempt before explicit approval; approval binds inputs.
        approval_project, approval_scenario = new_project("approval and source binding Ω")
        configure_process_approval(approval_project)
        _, blocked = factory(
            approval_project,
            "run",
            "godot-verify",
            "--scenario",
            str(approval_scenario),
            expected={3},
        )
        workflow_id = blocked.get("workflow_id")
        approval_id = blocked.get("pending_approval_id")
        if not workflow_id or not approval_id or blocked.get("status") != "BLOCKED":
            raise AcceptanceFailure(f"process policy did not block before execution: {blocked}")
        blocked_state = inspect(approval_project, workflow_id)
        if blocked_state.get("executions"):
            raise AcceptanceFailure(
                f"approval-blocked workflow already has process execution records: {blocked_state['executions']}"
            )
        blocked_process_artifacts = [
            item
            for item in blocked_state.get("artifacts", [])
            if item.get("artifact_type") in {"godot-import-process", "godot-runtime-process"}
        ]
        if blocked_process_artifacts:
            raise AcceptanceFailure(
                f"approval-blocked workflow retained process-launch artifacts: {blocked_process_artifacts}"
            )
        checks["approval_blocks_before_process"] = {
            "workflow_id": workflow_id,
            "approval_id": approval_id,
            "tasks": blocked_state.get("tasks", []),
            "executions": [],
        }
        factory(
            approval_project,
            "approve",
            approval_id,
            "--comment",
            "Real Godot acceptance explicit approval",
        )
        _, approved = factory(approval_project, "resume", workflow_id)
        approved_state = inspect(approval_project, workflow_id)
        if approved.get("status") != "COMPLETED" or not approved_state.get("executions"):
            raise AcceptanceFailure(
                f"Explicit approval did not resume the Godot process workflow: {approved}"
            )
        approved_execution_count = len(approved_state["executions"])
        _, approved_again = factory(approval_project, "resume", workflow_id)
        if (
            approved_again.get("status") != "COMPLETED"
            or len(inspect(approval_project, workflow_id).get("executions", []))
            != approved_execution_count
        ):
            raise AcceptanceFailure(
                "completed approved workflow launched another process on resume"
            )
        checks["approved_resume_executes_and_completed_resume_is_idempotent"] = {
            "workflow_id": workflow_id,
            "execution_count": approved_execution_count,
        }

        # A separate approval is granted, then invalidated by changing its source input.
        _, stale_blocked = factory(
            approval_project,
            "run",
            "godot-verify",
            "--scenario",
            str(approval_scenario),
            expected={3},
        )
        stale_workflow_id = stale_blocked.get("workflow_id")
        stale_approval_id = stale_blocked.get("pending_approval_id")
        if (
            not stale_workflow_id
            or not stale_approval_id
            or stale_blocked.get("status") != "BLOCKED"
        ):
            raise AcceptanceFailure(
                f"second workflow was not blocked for process approval: {stale_blocked}"
            )
        if inspect(approval_project, stale_workflow_id).get("executions"):
            raise AcceptanceFailure("second workflow launched Godot before approval")
        factory(
            approval_project,
            "approve",
            stale_approval_id,
            "--comment",
            "Approval invalidation regression",
        )
        (approval_project / "main.gd").write_text(
            (approval_project / "main.gd").read_text(encoding="utf-8")
            + "\n# changed after approval\n",
            encoding="utf-8",
        )
        _, stale = factory(approval_project, "resume", stale_workflow_id, expected={3})
        if (
            stale.get("status") != "BLOCKED"
            or stale.get("pending_approval_id") == stale_approval_id
        ):
            raise AcceptanceFailure(f"changed source reused stale approval: {stale}")
        stale_state = inspect(approval_project, stale_workflow_id)
        if stale_state.get("executions"):
            raise AcceptanceFailure(
                "source change after approval launched Godot before renewed approval"
            )
        stale_process_artifacts = [
            item
            for item in stale_state.get("artifacts", [])
            if item.get("artifact_type") in {"godot-import-process", "godot-runtime-process"}
        ]
        if stale_process_artifacts:
            raise AcceptanceFailure(
                f"stale approval launched a Godot process: {stale_process_artifacts}"
            )
        checks["source_change_requires_new_approval"] = {
            "workflow_id": stale_workflow_id,
            "old_approval_id": stale_approval_id,
            "new_approval_id": stale.get("pending_approval_id"),
            "executions": [],
        }

        # User-facing normal Godot operation, outside Factory and its harness.
        standalone = run_root / "standalone ordinary Godot Ω"
        _copy_fixture(fixture, standalone)
        result["projects"].append(
            {
                "name": "standalone ordinary Godot Ω",
                "path": str(standalone),
                "source_hashes_before": _hashes(standalone),
            }
        )
        import_cmd = [str(godot), "--headless", "--path", str(standalone), "--import"]
        import_proc, _ = run(import_cmd, timeout=120, cwd=standalone)
        checks["standalone_import"] = {"exit_code": import_proc.returncode}
        normal_cmd = [str(godot), "--headless", "--path", str(standalone), "--quit-after", "2"]
        normal_proc, _ = run(normal_cmd, timeout=30, cwd=standalone)
        if "ERROR:" in normal_proc.stdout + normal_proc.stderr:
            raise AcceptanceFailure("standalone Godot emitted an ERROR line")
        checks["standalone_runtime_without_factory"] = {
            "exit_code": normal_proc.returncode,
            "factory_state_absent": not (standalone / ".gamefactory").exists(),
        }
        if (standalone / ".gamefactory").exists():
            raise AcceptanceFailure("standalone Godot unexpectedly required Factory state")

        for project_record in result["projects"]:
            if project_record["name"] in {"fixture game Ω", "wrong expectation Ω"}:
                project_record["source_hashes_after"] = _hashes(Path(project_record["path"]))
        checks["real_execution_record_count"] = len(records)
        result["status"] = "PASSED"
    except Exception as exc:
        result["status"] = "FAILED"
        result["failure"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat()
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "status": result["status"],
                "commands": len(records),
                "report": str(output),
                "workspace": str(run_root),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "PASSED" else 1


def _decode(value: bytes | str | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


if __name__ == "__main__":
    raise SystemExit(main())
