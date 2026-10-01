"""Separate-process CLI tests covering output, exit codes, and persisted paid safety."""

import json
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import yaml

from gamefactory import __version__

PROJECT = Path(__file__).parents[2]
FIXTURE = PROJECT / "examples" / "minimal-godot"


def invoke(root: Path, *arguments: str) -> tuple[int, dict | list]:
    result = subprocess.run(
        [sys.executable, "-m", "gamefactory", *arguments, "--json"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.stdout.strip(), result.stderr
    return result.returncode, json.loads(result.stdout)


def test_help_version_and_json_usage_error() -> None:
    help_result = subprocess.run(
        [sys.executable, "-m", "gamefactory", "--help"], text=True, capture_output=True
    )
    assert help_result.returncode == 0
    assert "doctor" in help_result.stdout and "run" in help_result.stdout

    version_result = subprocess.run(
        [sys.executable, "-m", "gamefactory", "--version"], text=True, capture_output=True
    )
    assert version_result.returncode == 0
    assert version_result.stdout.strip() == f"gamefactory {__version__}"

    bad = subprocess.run(
        [sys.executable, "-m", "gamefactory", "run", "bogus", "--json"],
        text=True,
        capture_output=True,
    )
    assert bad.returncode == 1
    assert json.loads(bad.stderr)["error"] == "USAGE_ERROR"


def test_cli_paid_workflow_requires_approval_and_resumes_across_processes(tmp_path: Path) -> None:
    root = tmp_path / "game"
    shutil.copytree(FIXTURE, root)

    code, doctor = invoke(root, "doctor")
    assert code == 0
    assert doctor["configuration"]["status"] == "NOT_INITIALIZED"
    assert doctor["storage"]["status"] == "OK"

    code, initialized = invoke(root, "init")
    assert code == 0 and initialized["godot_project"]
    discovery = json.loads((root / ".gamefactory" / "discovery.json").read_text(encoding="utf-8"))
    assert discovery["counts"] == {"scripts": 1, "scenes": 1, "test_files": 0}
    assert discovery["godot"]["main_scene"] == "res://Main.tscn"
    code, result = invoke(root, "run", "paid-safety")
    assert code == 3 and result["status"] == "BLOCKED"
    workflow_id = result["workflow_id"]
    approval_id = result["pending_approval_id"]

    code, before = invoke(root, "inspect", workflow_id)
    assert code == 0 and before["paid_provider_invocations"] == 0
    code, approvals = invoke(root, "approvals")
    assert code == 0 and approvals[0]["id"] == approval_id

    code, decision = invoke(root, "approve", approval_id, "--comment", "Test approval")
    assert code == 0 and decision["status"] == "APPROVED"
    code, completed = invoke(root, "resume", workflow_id)
    assert code == 0 and completed["status"] == "COMPLETED"
    code, after = invoke(root, "inspect", workflow_id)
    assert code == 0 and after["paid_provider_invocations"] == 1
    assert after["artifacts"] and after["evidence"] and after["gates"]


def test_doctor_keeps_invalid_explicit_paths_misconfigured(tmp_path: Path) -> None:
    code, report = invoke(tmp_path, "doctor", "--godot-path", str(tmp_path / "missing-godot.exe"))
    assert code == 4
    assert report["capabilities"]["engine.godot.detect"]["status"] == "MISCONFIGURED"


def test_database_lock_and_readonly_errors_are_actionable_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    from gamefactory.adapters.persistence.database import Database
    from gamefactory.cli import main as cli
    from gamefactory.config.loader import ConfigLoader

    ConfigLoader.init_project(tmp_path)
    database = Database(tmp_path / ".gamefactory" / "state" / "factory.db")
    lock_connection = database.connect()
    lock_connection.execute("BEGIN IMMEDIATE")
    original_connect = Database.connect

    def short_wait(self):
        connection = original_connect(self)
        connection.execute("PRAGMA busy_timeout = 50")
        return connection

    monkeypatch.setattr(Database, "connect", short_wait)
    try:
        assert cli.main(["--json", "--project", str(tmp_path), "approvals"]) == 5
        payload = json.loads(capsys.readouterr().err)
        assert payload["error"] == "DATABASE_ERROR"
        assert "Wait for the active Factory command" in payload["message"]
    finally:
        lock_connection.rollback()
        lock_connection.close()

    # ACL-based read-only checks vary by Windows account; exercise their error translation directly.
    def fail_readonly(_args):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(cli, "_dispatch", fail_readonly)
    assert cli.main(["--json", "doctor"]) == 5
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"] == "DATABASE_ERROR"
    assert "Check write access" in payload["message"]


def test_cli_engine_uses_project_policy_values(tmp_path: Path) -> None:
    from gamefactory.adapters.persistence.database import Database
    from gamefactory.cli.main import _engine
    from gamefactory.config.loader import ConfigLoader

    ConfigLoader.init_project(tmp_path)
    config_path = tmp_path / ".gamefactory" / "factory.yml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    config["policies"] = {
        "paid_operations_require_approval": False,
        "destructive_operations_require_approval": False,
        "require_approval_for_repo_write": True,
        "max_operation_cost": 12,
        "project_budget": 34,
    }
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    database = Database(tmp_path / ".gamefactory" / "state" / "factory.db")
    engine = _engine(tmp_path, database)
    rule = engine.policy_engine.rule
    assert rule.require_approval_for_paid is False
    assert rule.require_approval_for_destructive is False
    assert rule.require_approval_for_repo_write is True
    assert rule.max_operation_cost == 12
    assert rule.project_budget == 34
