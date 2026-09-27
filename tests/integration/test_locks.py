"""Integration tests for ExecutionLock concurrency protection and lease recovery."""

import json
import multiprocessing
import os
import socket
import time
from pathlib import Path

import pytest

from gamefactory.core.domain.errors import LockError
from gamefactory.core.execution.locks import ExecutionLock


class TestExecutionLock:
    def test_acquire_and_release(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path, "WF-001")
        claim = lock.acquire()
        assert claim["workflow_id"] == "WF-001"
        assert claim["pid"] == os.getpid()
        assert lock.lock_file.exists()

        lock.release()
        assert lock.lock_file.exists()  # Stable inode is retained to prevent unlink/recreate races.

    def test_hold_context_manager(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path, "WF-002")
        with lock.hold() as claim:
            assert claim["workflow_id"] == "WF-002"
            assert lock.lock_file.exists()
        assert lock.lock_file.exists()

    def test_competing_runner_blocked_when_active(self, tmp_path: Path) -> None:
        lock1 = ExecutionLock(tmp_path, "WF-003")
        lock1.acquire()

        # Try acquiring same workflow lock from another instance
        lock2 = ExecutionLock(tmp_path, "WF-003")
        with pytest.raises(LockError, match="locked by active process PID"):
            lock2.acquire()

        lock1.release()

    def test_stale_lock_reclaimed_when_process_is_dead(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path, "WF-004")
        tmp_path.mkdir(parents=True, exist_ok=True)
        # Write a lockfile with an unreachable PID (e.g. 99999999)
        fake_claim = {
            "workflow_id": "WF-004",
            "pid": 99999999,
            "host": socket.gethostname(),
            "claimed_at": "2026-01-01T00:00:00Z",
        }
        lock.lock_file.write_text(json.dumps(fake_claim), encoding="utf-8")

        # Now acquire should succeed by reclaiming the stale lock
        new_claim = lock.acquire()
        assert new_claim["pid"] == os.getpid()
        lock.release()

    def test_workflow_id_never_becomes_a_path(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path, "../outside")
        assert lock.lock_file.parent == tmp_path.resolve()

    def test_symlink_lock_leaf_cannot_write_outside_lock_directory(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path / "locks", "symlink-workflow")
        lock.lock_dir.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text("keep me", encoding="utf-8")
        try:
            lock.lock_file.symlink_to(outside)
        except (OSError, NotImplementedError):
            pytest.skip("Symlink creation is unavailable on this host")
        with pytest.raises(LockError):
            lock.acquire()
        assert outside.read_text(encoding="utf-8") == "keep me"

    def test_hardlink_lock_leaf_is_rejected(self, tmp_path: Path) -> None:
        lock = ExecutionLock(tmp_path / "locks", "hardlink-workflow")
        lock.lock_dir.mkdir()
        outside = tmp_path / "outside.json"
        outside.write_text("keep me", encoding="utf-8")
        try:
            os.link(outside, lock.lock_file)
        except OSError:
            pytest.skip("Hardlink creation is unavailable on this host")
        with pytest.raises(LockError):
            lock.acquire()
        assert outside.read_text(encoding="utf-8") == "keep me"


def _race_lock(lock_dir: str, barrier: object, results: object) -> None:
    lock = ExecutionLock(lock_dir, "same-workflow")
    barrier.wait()  # type: ignore[attr-defined]
    try:
        lock.acquire()
    except LockError:
        results.put("blocked")  # type: ignore[attr-defined]
    else:
        results.put("acquired")  # type: ignore[attr-defined]
        time.sleep(0.35)
        lock.release()


def test_two_processes_cannot_acquire_same_workflow(tmp_path: Path) -> None:
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(target=_race_lock, args=(str(tmp_path), barrier, results)) for _ in range(2)
    ]
    for process in processes:
        process.start()
    outcomes = [results.get(timeout=10) for _ in processes]
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0
    assert sorted(outcomes) == ["acquired", "blocked"]
