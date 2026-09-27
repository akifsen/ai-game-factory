"""Cross-process workflow execution locks backed by operating-system locks."""

import ctypes
import hashlib
import json
import os
import socket
import stat
import sys
import threading
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from typing import Any

from gamefactory.core.domain.errors import LockError
from gamefactory.core.domain.models import utc_now_iso


def is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is currently active."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [ctypes.c_ulong, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000 | 0x00100000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5
        exit_code = wintypes.DWORD()
        try:
            return bool(
                kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
                and exit_code.value == 259
            )
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except (OSError, ProcessLookupError):
        return False


class ExecutionLock:
    """An advisory OS lock. The lock file remains as a stable synchronization point."""

    def __init__(self, lock_dir: Path | str, workflow_id: str) -> None:
        if not isinstance(workflow_id, str) or not workflow_id or "\x00" in workflow_id:
            raise LockError("Workflow id must be a non-empty string")
        self.lock_dir = Path(lock_dir).resolve()
        self.workflow_id = workflow_id
        fingerprint = hashlib.sha256(workflow_id.encode("utf-8")).hexdigest()
        self.lock_file = self.lock_dir / f"workflow-{fingerprint}.lock"
        self._handle: Any = None
        self._claim: dict[str, Any] | None = None
        self._guard = threading.Lock()

    def _validate_lock_path(self) -> None:
        """Reject link/reparse/hardlink leaves before reading or changing lock data."""
        try:
            resolved = self.lock_file.resolve(strict=False)
            resolved.relative_to(self.lock_dir)
            try:
                info = self.lock_file.lstat()
            except FileNotFoundError:
                return
            reparse = bool(getattr(info, "st_file_attributes", 0) & 0x400)
            if stat.S_ISLNK(info.st_mode) or reparse or info.st_nlink > 1:
                raise LockError("Workflow lock path must be a regular single-link file")
        except LockError:
            raise
        except (OSError, RuntimeError, ValueError) as exc:
            raise LockError("Workflow lock path resolves outside its lock directory") from exc

    def _validate_open_handle(self, handle: Any) -> None:
        """Ensure the opened object still matches its non-link directory entry."""
        try:
            self._validate_lock_path()
            opened = os.fstat(handle.fileno())
            entry = self.lock_file.lstat()
            if (opened.st_dev, opened.st_ino) != (entry.st_dev, entry.st_ino):
                raise LockError("Workflow lock path changed while opening")
        except OSError as exc:
            raise LockError("Workflow lock path changed while opening") from exc

    def acquire(self) -> dict[str, Any]:
        """Acquire without waiting; OS releases ownership automatically on process death."""
        with self._guard:
            if self._handle is not None:
                raise LockError(f"Workflow '{self.workflow_id}' is already locked by this instance")
            self.lock_dir.mkdir(parents=True, exist_ok=True)
            self._validate_lock_path()
            try:
                with self.lock_file.open("rb") as metadata_file:
                    metadata_file.seek(1 if sys.platform == "win32" else 0)
                    previous = json.loads(metadata_file.read().decode("utf-8"))
                active_pid = int(previous.get("pid", 0))
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                active_pid = 0
            flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(self.lock_file, flags, 0o600)
            handle = os.fdopen(descriptor, "a+b")
            try:
                handle.seek(0)
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except (OSError, BlockingIOError) as exc:
                handle.close()
                raise LockError(
                    f"Workflow '{self.workflow_id}' is locked by active process PID {active_pid}",
                    details={"workflow_id": self.workflow_id, "active_pid": active_pid},
                ) from exc

            claim = {
                "workflow_id": self.workflow_id,
                "pid": os.getpid(),
                "host": socket.gethostname(),
                "claimed_at": utc_now_iso(),
                "owner_token": uuid.uuid4().hex,
            }
            try:
                self._validate_open_handle(handle)
                if sys.platform == "win32" and self.lock_file.stat().st_size == 0:
                    handle.seek(0)
                    handle.write(b"\0")
                handle.seek(1 if sys.platform == "win32" else 0)
                handle.truncate(1 if sys.platform == "win32" else 0)
                handle.write(json.dumps(claim).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            except (OSError, LockError):
                try:
                    if sys.platform == "win32":
                        import msvcrt

                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl

                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                finally:
                    handle.close()
                raise
            self._handle, self._claim = handle, claim
            return dict(claim)

    def release(self) -> None:
        """Release only the OS lock held by this instance; keep the file to avoid inode races."""
        with self._guard:
            if self._handle is None:
                return
            handle, self._handle = self._handle, None
            self._claim = None
            try:
                if sys.platform == "win32":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            finally:
                handle.close()

    @contextmanager
    def hold(self) -> Generator[dict[str, Any], None, None]:
        """Context manager to acquire on entry and release on exit."""
        claim = self.acquire()
        try:
            yield claim
        finally:
            self.release()
