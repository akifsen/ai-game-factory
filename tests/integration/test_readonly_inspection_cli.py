"""`accounting ledger` and `recovery inspect` never migrate or write the database."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

PROJECT = Path(__file__).resolve().parents[2]


def _cli(root: Path, *arguments: str) -> tuple[int, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(PROJECT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    proc = subprocess.run(
        [sys.executable, "-m", "gamefactory", "--json", "--project", str(root), *arguments],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=120,
    )
    return proc.returncode, json.loads(proc.stdout.strip() or proc.stderr.strip())


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _project(tmp_path: Path) -> tuple[Path, Path, str]:
    root = tmp_path / "game"
    root.mkdir()
    (root / "project.godot").write_text('config_version=5\n[application]\nconfig/name="T"\n')
    assert _cli(root, "init")[0] == 0
    code, payload = _cli(root, "run", "failure")
    workflow_id = payload["workflow_id"]
    db = root / ".gamefactory/state/factory.db"
    with sqlite3.connect(db) as conn:  # flush WAL so the file bytes are the full state
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    return root, db, workflow_id


def test_inspection_commands_leave_the_database_bytes_unchanged(tmp_path: Path) -> None:
    root, db, workflow_id = _project(tmp_path)
    before = _digest(db)
    code, payload = _cli(root, "recovery", "inspect", workflow_id)
    assert code == 0, payload
    code, payload = _cli(root, "accounting", "ledger", "--workflow", workflow_id)
    assert code == 0, payload
    assert _digest(db) == before


def test_inspection_refuses_an_older_schema_instead_of_migrating(tmp_path: Path) -> None:
    root, db, workflow_id = _project(tmp_path)
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "DELETE FROM schema_migrations "
            "WHERE version = (SELECT MAX(version) FROM schema_migrations)"
        )
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        conn.close()
    before = _digest(db)
    code, payload = _cli(root, "accounting", "ledger", "--workflow", workflow_id)
    assert code != 0
    assert "will not migrate" in json.dumps(payload)
    assert _digest(db) == before
