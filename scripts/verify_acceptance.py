"""Independent user-facing acceptance: every Factory command is a fresh process.

Run with the installed CLI; no application internals are imported. All approvals
are explicit decisions on deterministic fake-provider acceptance fixtures only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--godot", type=Path)
    parser.add_argument("--blender", type=Path)
    args = parser.parse_args()
    repo = Path(__file__).resolve().parents[1]
    workspace = repo / ".verification"
    workspace.mkdir(exist_ok=True)
    records: list[dict[str, Any]] = []
    result: dict[str, Any] = {
        "schema_version": 1,
        "started_at": datetime.now(UTC).isoformat(),
        "platform": sys.platform,
        "commands": records,
        "checks": {},
    }

    def execute(command: list[str], *, expected: int = 0, structured: bool = True) -> Any:
        proc = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=60,
            shell=False,
            cwd=repo,
        )
        record: dict[str, Any] = {
            "argv": command,
            "exit_code": proc.returncode,
            "stdout": proc.stdout.strip(),
            "stderr": proc.stderr.strip(),
        }
        records.append(record)
        assert proc.returncode == expected, record
        return json.loads(proc.stdout) if structured else proc.stdout

    try:
        cli = str(args.cli.resolve())
        execute([cli, "--version"], structured=False)
        execute([cli, "--help"], structured=False)
        with tempfile.TemporaryDirectory(prefix="acceptance game ", dir=workspace) as temp:
            project = Path(temp) / "fixture game"
            shutil.copytree(
                args.fixture.resolve(),
                project,
                ignore=shutil.ignore_patterns(".godot", ".gamefactory"),
            )
            baseline = {
                str(path.relative_to(project)): hashlib.sha256(path.read_bytes()).hexdigest()
                for path in project.rglob("*")
                if path.is_file()
            }

            def factory(*command: str, expected: int = 0) -> Any:
                options = [cli, "--json", "--project", str(project)]
                if args.godot:
                    options.extend(["--godot-path", str(args.godot.resolve())])
                if args.blender:
                    options.extend(["--blender-path", str(args.blender.resolve())])
                return execute([*options, *command], expected=expected)

            doctor = factory("doctor")
            assert doctor["storage"]["status"] == "OK"
            assert doctor["capabilities"]["asset.3d.meshy"]["status"] == "UNAVAILABLE"
            if args.godot:
                assert doctor["capabilities"]["engine.godot.detect"]["status"] == "AVAILABLE"
            if args.blender:
                assert doctor["capabilities"]["dcc.blender.detect"]["status"] == "AVAILABLE"
            factory("init")
            factory("init")
            for relative, original_hash in baseline.items():
                assert (
                    hashlib.sha256((project / relative).read_bytes()).hexdigest() == original_hash
                )
            result["checks"]["init_preserves_original_game_files"] = True
            factory("status")
            factory("approvals")

            def inspect(workflow_id: str) -> dict[str, Any]:
                return factory("inspect", workflow_id)

            def approve(workflow_id: str) -> None:
                approvals = factory("approvals", "--workflow", workflow_id)
                pending = [item for item in approvals if item["status"] == "PENDING"]
                assert len(pending) == 1, approvals
                factory(
                    "approve",
                    pending[0]["id"],
                    "--comment",
                    "Explicit acceptance decision: deterministic fake provider only",
                )

            demo = factory("run", "demo", expected=3)
            demo_id = demo["workflow_id"]
            before = inspect(demo_id)
            assert demo["status"] == "BLOCKED"
            assert before["artifacts"] and before["evidence"]
            assert before["paid_provider_invocations"] == 0
            old_execution_ids = {item["id"] for item in before["executions"]}
            approve(demo_id)
            assert factory("resume", demo_id)["status"] == "COMPLETED"
            after = inspect(demo_id)
            assert old_execution_ids <= {item["id"] for item in after["executions"]}
            assert after["paid_provider_invocations"] == 1
            assert after["gates"] and all(item["status"] == "PASSED" for item in after["gates"])
            assert all(item["status"] == "COMPLETED" for item in after["tasks"])
            factory("resume", demo_id)
            again = inspect(demo_id)
            assert len(again["executions"]) == len(after["executions"])
            assert again["paid_provider_invocations"] == 1
            factory("artifacts", "--workflow", demo_id)
            result["checks"]["demo_restart_approval_completion"] = demo_id

            negative = factory("run", "failure", expected=2)
            failure_id = negative["workflow_id"]
            failed = inspect(failure_id)
            failed_tasks = [item for item in failed["tasks"] if item["status"] == "FAILED"]
            assert len(failed_tasks) == 1
            assert any(item["status"] == "PENDING" for item in failed["tasks"])
            failed_attempts = [item for item in failed["executions"] if item["status"] == "FAILED"]
            assert failed_attempts and failed["evidence"]
            factory("retry", failure_id, failed_tasks[0]["id"])
            retried = inspect(failure_id)
            assert retried["workflow"]["status"] == "COMPLETED"
            final_attempts = {item["id"]: item for item in retried["executions"]}
            for item in failed_attempts:
                assert final_attempts[item["id"]]["status"] == "FAILED"
            result["checks"]["failure_dependency_block_retry_history"] = failure_id

            paid = factory("run", "paid-safety", expected=3)
            paid_id = paid["workflow_id"]
            assert inspect(paid_id)["paid_provider_invocations"] == 0
            approve(paid_id)
            factory("resume", paid_id)
            assert inspect(paid_id)["paid_provider_invocations"] == 1
            factory("resume", paid_id)
            assert inspect(paid_id)["paid_provider_invocations"] == 1
            result["checks"]["fake_paid_invocations"] = [0, 1, 1]
            result["checks"]["paid_workflow"] = paid_id

            rejected = factory("run", "paid-safety", expected=3)
            rejected_id = rejected["workflow_id"]
            pending = factory("approvals", "--workflow", rejected_id)
            assert len(pending) == 1
            factory(
                "reject", pending[0]["id"], "--comment", "Explicit fake-provider rejection test"
            )
            factory("resume", rejected_id, expected=2)
            rejected_state = inspect(rejected_id)
            assert rejected_state["paid_provider_invocations"] == 0
            assert rejected_state["workflow"]["status"] == "FAILED"
            result["checks"]["rejected_approval_never_invokes_provider"] = rejected_id
            factory("status")

            # Read edited project policy in new CLI processes, not internal objects.
            project = Path(temp) / "policy game"
            shutil.copytree(
                args.fixture.resolve(),
                project,
                ignore=shutil.ignore_patterns(".godot", ".gamefactory"),
            )
            factory("init")
            config_path = project / ".gamefactory" / "factory.yml"
            config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
            config.setdefault("policies", {})["project_budget"] = 1
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            limited = factory("run", "paid-safety", expected=3)
            limited_state = inspect(limited["workflow_id"])
            assert limited_state["workflow"]["status"] == "BLOCKED"
            assert limited_state["paid_provider_invocations"] == 0
            assert not limited_state["approvals"]
            result["checks"]["configured_budget_blocks_dispatch"] = limited["workflow_id"]
            config["policies"]["project_budget"] = 500
            config["policies"]["require_approval_for_repo_write"] = True
            config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
            write_blocked = factory("run", "demo", expected=3)
            write_state = inspect(write_blocked["workflow_id"])
            assert write_state["approvals"][0]["approval_type"] == "repository_write"
            assert not any(a["artifact_type"] == "concept_spec" for a in write_state["artifacts"])
            assert write_state["paid_provider_invocations"] == 0
            result["checks"]["configured_repository_write_requires_approval"] = write_blocked[
                "workflow_id"
            ]

            # Run an untouched copy with no .gamefactory: development-time boundary.
            if args.godot:
                standalone = Path(temp) / "standalone game"
                shutil.copytree(
                    args.fixture.resolve(),
                    standalone,
                    ignore=shutil.ignore_patterns(".godot", ".gamefactory"),
                )
                execute(
                    [
                        str(args.godot.resolve()),
                        "--headless",
                        "--path",
                        str(standalone),
                        "--editor",
                        "--import",
                    ],
                    structured=False,
                )
                assert "ERROR:" not in records[-1]["stdout"] + records[-1]["stderr"], records[-1]
                execute(
                    [
                        str(args.godot.resolve()),
                        "--headless",
                        "--path",
                        str(standalone),
                        "--quit-after",
                        "2",
                    ],
                    structured=False,
                )
                assert "ERROR:" not in records[-1]["stdout"] + records[-1]["stderr"], records[-1]
                assert not (standalone / ".gamefactory").exists()
                result["checks"]["godot_without_factory_runtime"] = True
        result["status"] = "PASSED"
    except Exception as exc:
        result["status"] = "FAILED"
        result["failure"] = f"{type(exc).__name__}: {exc}"
        result["traceback"] = traceback.format_exc()
    finally:
        result["finished_at"] = datetime.now(UTC).isoformat()
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
    print(
        json.dumps(
            {"status": result["status"], "commands": len(records), "report": str(args.output)},
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "PASSED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
