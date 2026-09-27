"""Focused integration tests for the verify_godot_live_crash tooling."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.verify_godot_live_crash import (  # noqa: E402
    CheckFailure,
    LinuxProcessRef,
    RealProcProbe,
    _check_sentinel_alive,
    _cleanup_sentinel,
    _get_process_identity,
    _linux_fate_record,
    _prepare_controlled_fixture,
    _spawn_sentinel,
    _win32_close_handle,
    observe_owned_posix,
    open_owned_linux_process,
    poll_linux_pidfd,
    signal_retained_process,
    wait_for_owned_thread_group,
)


def test_process_identity_query_current() -> None:
    """_get_process_identity must identify the current process and verify liveness."""
    current_exe = Path(getattr(sys, "_base_executable", sys.executable)).resolve()
    info, handle = _get_process_identity(os.getpid(), current_exe)
    try:
        assert info["pid"] == os.getpid()
        assert info["is_alive"] is True
        assert Path(info["exe_path"]).resolve() == current_exe
        if sys.platform == "win32":
            assert info["creation_filetime"] > 0
            assert handle is not None
    finally:
        if handle is not None and sys.platform == "win32":
            _win32_close_handle(handle)


def test_process_identity_rejection_wrong_executable() -> None:
    """_get_process_identity must fail when process image does not match expected executable."""
    wrong_exe = Path(sys.executable).parent / "completely_wrong_executable_name.exe"
    with pytest.raises(CheckFailure, match="executable mismatch"):
        info, handle = _get_process_identity(os.getpid(), wrong_exe)
        if handle is not None and sys.platform == "win32":
            _win32_close_handle(handle)


def test_sentinel_lifecycle() -> None:
    """Sentinel process must spawn, report alive, and be cleanly terminated."""
    sentinel = _spawn_sentinel(Path(sys.executable))
    try:
        assert sentinel.pid > 0
        assert _check_sentinel_alive(sentinel) is True
    finally:
        _cleanup_sentinel(sentinel)
    assert _check_sentinel_alive(sentinel) is False


def test_controlled_fixture_preparation(tmp_path: Path) -> None:
    """Fixture preparation must create handshake configuration with journal, token, and valid GDScript."""
    fixture_src = REPO_ROOT / "examples" / "godot-verification"
    dest = tmp_path / "fixture_dest"
    ready = tmp_path / "ready.json"
    release = tmp_path / "release.txt"
    journal = tmp_path / "journal.jsonl"
    token = "test-token-12345"

    _prepare_controlled_fixture(
        fixture_src,
        dest,
        ready,
        release,
        journal_file=journal,
        token=token,
        max_wait_ms=10000,
    )

    assert (dest / "project.godot").is_file()
    assert (dest / "scenario.json").is_file()
    assert (dest / "handshake_config.json").is_file()
    cfg = json.loads((dest / "handshake_config.json").read_text(encoding="utf-8"))
    assert cfg["ready_file"] == ready.as_posix()
    assert cfg["release_file"] == release.as_posix()
    assert cfg["journal_file"] == journal.as_posix()
    assert cfg["token"] == token
    assert cfg["max_wait_ms"] == 10000

    main_gd = (dest / "main.gd").read_text(encoding="utf-8")
    assert "func verification_snapshot() -> Dictionary:" in main_gd
    assert "func apply_damage(amount: int) -> void:" in main_gd
    assert "func defeat_enemy() -> void:" in main_gd
    assert "func _perform_handshake() -> void:" in main_gd
    assert "handshake_config.json" in main_gd
    assert "journal_file" in main_gd


def test_wrapper_intercepts_subprocess_launch(tmp_path: Path) -> None:
    """Test-only entry wrapper must intercept subprocess.Popen and prove counted."""
    launch_log = tmp_path / "probe-observations.jsonl"
    installed_python = Path(sys.executable).absolute()
    script_path = REPO_ROOT / "scripts" / "verify_godot_live_crash.py"

    cmd = [
        str(installed_python),
        str(script_path),
        "--entry-wrapper",
        "--launch-log",
        str(launch_log),
        "--wrapper-probe",
    ]

    res = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
        shell=False,
    )
    assert res.returncode == 0, (
        f"Wrapper probe failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )

    assert launch_log.is_file(), "Launch observations log was not created"
    lines = [
        json.loads(line)
        for line in launch_log.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    events = [entry.get("event") for entry in lines]
    assert "wrapper_invocation_start" in events
    assert "subprocess_launch" in events
    assert "wrapper_invocation_end" in events

    launches = [entry for entry in lines if entry.get("event") == "subprocess_launch"]
    assert len(launches) == 1
    launch = launches[0]
    assert isinstance(launch["pid"], int) and launch["pid"] > 0
    assert any("import sys" in arg or "-c" in arg for arg in launch["argv"])
    assert launch["create_time"] is not None


@pytest.mark.real_godot
def test_real_godot_live_crash(tmp_path: Path) -> None:
    """End-to-end verification of Godot live crash recovery against installed wheel CLI."""
    godot_val = os.environ.get("GAMEFACTORY_TEST_REAL_GODOT") or os.environ.get(
        "GAMEFACTORY_TEST_GODOT"
    )
    if not godot_val:
        pytest.skip(
            "Opt-in real Godot test requires GAMEFACTORY_TEST_REAL_GODOT or GAMEFACTORY_TEST_GODOT env var"
        )

    godot = Path(godot_val).resolve()
    if not godot.is_file():
        pytest.fail(f"Specified Godot executable does not exist: {godot}")

    # Determine installed Python and CLI from caller env or installation manifest
    installed_python_env = os.environ.get("GAMEFACTORY_INSTALLED_PYTHON")
    installed_cli_env = os.environ.get("GAMEFACTORY_INSTALLED_CLI")
    if installed_python_env and installed_cli_env:
        installed_python = Path(installed_python_env).absolute()
        installed_cli = Path(installed_cli_env).resolve()
    else:
        cli_name = "gamefactory.exe" if sys.platform == "win32" else "gamefactory"
        sibling_cli = Path(sys.executable).parent / cli_name
        if sibling_cli.is_file():
            installed_python = Path(sys.executable).absolute()
            installed_cli = sibling_cli.resolve()
        else:
            install_json = REPO_ROOT / "docs" / "reports" / "v0.2-closeout" / "installation.json"
            if install_json.is_file():
                data = json.loads(install_json.read_text(encoding="utf-8"))
                p = data.get("installed_python")
                c = data.get("installed_cli")
                if p and c and Path(p).is_file() and Path(c).is_file():
                    installed_python = Path(p).absolute()
                    installed_cli = Path(c).resolve()
                else:
                    pytest.skip(
                        "Installed wheel environment not found from closeout installation report"
                    )
            else:
                pytest.skip("Installed wheel environment not found")

    if not installed_cli.is_file() or not installed_python.is_file():
        pytest.skip("Installed wheel environment not found")

    report_file = tmp_path / "live_crash_report.json"
    workspace = tmp_path / "live_crash_ws"

    cmd = [
        str(installed_python),
        str(REPO_ROOT / "scripts" / "verify_godot_live_crash.py"),
        "--cli",
        str(installed_cli),
        "--python",
        str(installed_python),
        "--fixture",
        str(REPO_ROOT / "examples" / "godot-verification"),
        "--godot",
        str(godot),
        "--output",
        str(report_file),
        "--workspace",
        str(workspace),
        "--timeout",
        "120",
    ]

    result = subprocess.run(
        cmd,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
        shell=False,
    )

    if result.returncode != 0:
        details = (
            report_file.read_text(encoding="utf-8") if report_file.is_file() else "<no report>"
        )
        pytest.fail(
            f"Live crash script failed with returncode {result.returncode}.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}\nreport:\n{details}"
        )

    assert report_file.is_file(), "Report file was not generated"
    data = json.loads(report_file.read_text(encoding="utf-8"))
    assert data.get("status") == "PASSED", f"Report status is not PASSED: {data}"
    assert data.get("checks", {}).get("sentinel", {}).get("survived_full_recovery") is True
    assert (
        data.get("checks", {}).get("after_retry", {}).get("workflow", {}).get("status") == "BLOCKED"
    )
    assert (
        data.get("checks", {}).get("launch_evidence", {}).get("no_duplicate_launches_proven")
        is True
    )


@pytest.mark.parametrize(
    "reason",
    [
        "os_error: denied",
        "os_error: [Errno 2] No such file or directory: '/proc/3117/exe'",
        "malformed_stat",
        "starttime_mismatch: reused",
        "exe_mismatch: other",
    ],
)
def test_unknown_child_state_is_not_successful_cleanup(reason: str) -> None:
    from scripts.verify_godot_live_crash import _require_terminal_posix_reason

    with pytest.raises(CheckFailure, match="Cannot confirm"):
        _require_terminal_posix_reason(reason)


class _Proc:
    def __init__(
        self,
        tasks: dict[int, str] | None | BaseException,
        exe: str | BaseException,
        start: str | None | BaseException = "100",
    ) -> None:
        self._tasks = tasks
        self._exe = exe
        self._start = start

    def task_states(self, pid: int) -> dict[int, str] | None:
        if isinstance(self._tasks, BaseException):
            raise type(self._tasks)(*self._tasks.args)
        return self._tasks

    def read_exe(self, pid: int) -> str:
        if isinstance(self._exe, BaseException):
            raise type(self._exe)(*self._exe.args)
        return self._exe

    def starttime(self, pid: int) -> str | None:
        if isinstance(self._start, BaseException):
            raise type(self._start)(*self._start.args)
        return self._start


def _enoent(pid: int) -> FileNotFoundError:
    return FileNotFoundError(2, "No such file or directory", f"/proc/{pid}/exe")


def _ref(pid: int = 40, starttime: str = "100", pidfd: int = -1) -> LinuxProcessRef:
    return LinuxProcessRef(pid=pid, starttime=starttime, pidfd=pidfd, method="pidfd")


def test_live_thread_group_is_not_terminal() -> None:
    obs = observe_owned_posix(40, pidfd_readable=False, probe=_Proc({40: "S"}, "/usr/bin/python"))
    assert obs.status == "alive"
    assert obs.reason == "live_threads"


def test_unreaped_zombie_is_terminal_only_with_pidfd() -> None:
    probe = _Proc({40: "Z"}, _enoent(40))
    confirmed = observe_owned_posix(40, pidfd_readable=True, probe=probe)
    assert confirmed.status == "terminal"
    assert confirmed.reason == "pidfd_readable_thread_group_exited"
    assert confirmed.method == "pidfd"
    unconfirmed = observe_owned_posix(40, pidfd_readable=False, probe=probe)
    assert unconfirmed.status == "unknown"
    assert unconfirmed.reason == "leader_exit_without_pidfd"


def test_leader_zombie_with_live_worker_is_not_terminal() -> None:
    probe = _Proc({40: "Z", 41: "S"}, _enoent(40))
    obs = observe_owned_posix(40, pidfd_readable=True, probe=probe)
    assert obs.status == "alive"
    assert obs.leader_state == "Z"
    assert obs.exe_result is not None and obs.exe_result.startswith("error:")
    assert 41 in (obs.task_ids or [])


def test_exe_enoent_between_reads_is_not_success_or_uncaught() -> None:
    torn = observe_owned_posix(
        40,
        pidfd_readable=False,
        probe=_Proc(_enoent(40), _enoent(40)),
    )
    assert torn.status == "unknown"
    assert torn.reason.startswith("torn_proc_read:")

    class _Flip:
        def __init__(self) -> None:
            self.calls = 0

        def task_states(self, pid: int) -> dict[int, str] | None:
            self.calls += 1
            if self.calls == 1:
                raise _enoent(pid)
            return None

        def read_exe(self, pid: int) -> str:
            raise _enoent(pid)

        def starttime(self, pid: int) -> str | None:
            return None

    flip = _Flip()
    obs = wait_for_owned_thread_group(
        _ref(),
        time.monotonic() + 1.0,
        probe=flip,
        poll_pidfd=lambda _fd, _timeout: (True, None),
    )
    assert flip.calls >= 2
    assert obs.status == "terminal"
    assert obs.reason == "pidfd_readable_proc_gone"


def test_permission_error_stays_unknown() -> None:
    probe = _Proc(PermissionError(13, "Permission denied", "/proc/40/task"), "/bin/x")
    obs = wait_for_owned_thread_group(
        _ref(),
        time.monotonic() + 1.0,
        probe=probe,
        poll_pidfd=lambda _fd, _timeout: (True, None),
    )
    assert obs.status == "unknown"
    assert obs.reason.startswith("permission:")


def test_starttime_mismatch_does_not_signal() -> None:
    calls: list[tuple[int, int]] = []
    probe = _Proc({40: "S"}, "/bin/other", start="999")
    with pytest.raises(CheckFailure, match="refusing to signal"):
        signal_retained_process(
            _ref(pidfd=7),
            9,
            probe=probe,
            sender=lambda fd, sig: calls.append((fd, sig)),
        )
    assert calls == []


def test_matching_identity_signals_the_retained_fd_not_a_pid() -> None:
    calls: list[tuple[int, int]] = []
    probe = _Proc({40: "S"}, "/bin/same", start="100")
    signal_retained_process(
        _ref(pidfd=7),
        9,
        probe=probe,
        sender=lambda fd, sig: calls.append((fd, sig)),
    )
    assert calls == [(7, 9)]


def test_live_process_at_deadline_fails_with_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    current_time = 0.0

    def fake_monotonic() -> float:
        return current_time

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    poll_timeouts: list[float] = []

    def fake_poll(_fd: int, timeout: float) -> tuple[bool, str | None]:
        nonlocal current_time
        poll_timeouts.append(timeout)
        current_time += timeout
        return False, None

    deadline = current_time + 0.05
    obs = wait_for_owned_thread_group(
        _ref(),
        deadline,
        probe=_Proc({40: "R"}, "/bin/sleep"),
        poll_pidfd=fake_poll,
    )
    assert obs.status == "unknown"
    assert obs.reason.startswith("deadline:")
    assert "alive" in obs.reason
    assert obs.leader_state == "R"
    assert obs.elapsed_seconds == 0.05
    assert poll_timeouts == [0.05]

    record = _linux_fate_record(
        obs,
        initial_pid=40,
        initial_starttime="100",
        cleaned_up=False,
        final_status="alive",
        post_interrupt_status="alive",
        runtime_launches_count=1,
        sentinel_survived=True,
    )
    assert record["cleaned_up"] is False
    assert record["wait_result"] == "unknown"


def test_wait_for_owned_thread_group_early_poll_and_decreasing_remaining(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 0.0

    def fake_monotonic() -> float:
        return current_time

    monkeypatch.setattr(time, "monotonic", fake_monotonic)
    poll_timeouts: list[float] = []
    step_increments = [0.0, 0.015625, 0.015625, 0.015625]
    step_idx = 0

    def fake_poll(_fd: int, timeout: float) -> tuple[bool, str | None]:
        nonlocal current_time, step_idx
        poll_timeouts.append(timeout)
        delta = step_increments[step_idx]
        step_idx += 1
        current_time += delta
        return False, None

    deadline = 0.0625
    current_time = 0.015625
    obs = wait_for_owned_thread_group(
        _ref(),
        deadline,
        probe=_Proc({40: "S"}, "/bin/sleep"),
        poll_pidfd=fake_poll,
    )
    assert obs.status == "unknown"
    assert "alive" in obs.reason
    assert poll_timeouts == [0.046875, 0.046875, 0.03125, 0.015625]
    assert current_time == 0.0625
    assert obs.elapsed_seconds == 0.046875


def test_wait_for_owned_thread_group_terminal_before_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    current_time = 0.0

    def fake_monotonic() -> float:
        return current_time

    monkeypatch.setattr(time, "monotonic", fake_monotonic)

    def fake_poll(_fd: int, timeout: float) -> tuple[bool, str | None]:
        nonlocal current_time
        current_time += 0.02
        return True, None

    deadline = current_time + 0.10
    obs = wait_for_owned_thread_group(
        _ref(),
        deadline,
        probe=_Proc({40: "Z"}, _enoent(40)),
        poll_pidfd=fake_poll,
    )
    assert obs.status == "terminal"
    assert obs.reason == "pidfd_readable_thread_group_exited"
    assert obs.elapsed_seconds == 0.02

    record = _linux_fate_record(
        obs,
        initial_pid=40,
        initial_starttime="100",
        cleaned_up=True,
        final_status="terminated",
        post_interrupt_status="alive",
        runtime_launches_count=1,
        sentinel_survived=True,
    )
    assert record["cleaned_up"] is True
    assert record["wait_result"] == "terminal"


def test_process_ref_closes_descriptor_when_observation_fails() -> None:
    read_fd, write_fd = os.pipe()
    os.close(write_fd)
    ref = LinuxProcessRef(pid=1, starttime="1", pidfd=read_fd, method="pidfd")
    with pytest.raises(RuntimeError, match="boom"):
        with ref:
            raise RuntimeError("boom")
    assert ref.pidfd == -1
    with pytest.raises(OSError):
        os.read(read_fd, 1)


def test_pidfd_reference_is_not_offered_outside_linux() -> None:
    if sys.platform.startswith("linux"):
        return
    with pytest.raises(CheckFailure, match="Linux-only"):
        open_owned_linux_process(os.getpid(), Path(sys.executable), "1")


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfd thread group")
def test_linux_unreaped_exit_is_observed_without_waitpid() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read(1)"],
        stdin=subprocess.PIPE,
    )
    ref: LinuxProcessRef | None = None
    try:
        assert proc.stdin is not None
        starttime = RealProcProbe().starttime(proc.pid)
        assert starttime is not None
        ref = open_owned_linux_process(proc.pid, Path(sys.executable), starttime)
        alive = observe_owned_posix(proc.pid, pidfd_readable=False, probe=RealProcProbe())
        assert alive.status == "alive"
        proc.stdin.write(b"x")
        proc.stdin.close()
        obs = wait_for_owned_thread_group(ref, time.monotonic() + 2.0)
        assert obs.status == "terminal"
        assert obs.method == "pidfd"
        assert proc.wait(timeout=2) == 0
    finally:
        if ref is not None:
            ref.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="Linux pidfd thread group")
def test_linux_leader_exit_leaves_worker_alive_until_worker_exits(tmp_path: Path) -> None:
    """Not a Godot render. Proves leader Z or exe ENOENT is not process termination."""
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    nonce_req = tmp_path / "nonce_req"
    nonce_ack = tmp_path / "nonce_ack"
    stop = tmp_path / "stop"
    stderr_log = tmp_path / "child_stderr.log"

    script = r"""
import ctypes, os, platform, sys, threading, time

ready, release, nonce_req, nonce_ack, stop = sys.argv[1:6]
child_deadline = time.monotonic() + 15.0

def worker():
    tmp_ready = ready + ".tmp"
    with open(tmp_ready, "w", encoding="utf-8") as handle:
        handle.write(f"{os.getpid()}:{threading.get_native_id()}")
    os.replace(tmp_ready, ready)

    while not os.path.exists(stop):
        if time.monotonic() > child_deadline:
            sys.stderr.write("child worker exceeded deadline waiting for stop\n")
            sys.stderr.flush()
            os._exit(2)
        if os.path.exists(nonce_req) and not os.path.exists(nonce_ack):
            with open(nonce_req, "r", encoding="utf-8") as h:
                val = h.read().strip()
            if val:
                tmp_ack = nonce_ack + ".tmp"
                with open(tmp_ack, "w", encoding="utf-8") as h:
                    h.write(f"{os.getpid()}:{threading.get_native_id()}:{val}")
                os.replace(tmp_ack, nonce_ack)
        time.sleep(0.01)

threading.Thread(target=worker, daemon=False).start()

while not os.path.exists(release):
    if time.monotonic() > child_deadline:
        sys.stderr.write("child leader exceeded deadline waiting for release\n")
        sys.stderr.flush()
        sys.exit(3)
    time.sleep(0.01)

numbers = {"x86_64": 60, "aarch64": 93, "i386": 1, "i686": 1}
number = numbers.get(platform.machine())
if number is None:
    raise SystemExit("unsupported machine")

# ctypes.CDLL releases the GIL during foreign calls; worker thread keeps running.
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
libc.syscall.argtypes = [ctypes.c_long, ctypes.c_int]
libc.syscall(number, 0)
"""
    with open(stderr_log, "w", encoding="utf-8") as stderr_handle:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(ready),
                str(release),
                str(nonce_req),
                str(nonce_ack),
                str(stop),
            ],
            stderr=stderr_handle,
        )

    ref: LinuxProcessRef | None = None
    try:
        ready_deadline = time.monotonic() + 3.0
        while not ready.is_file():
            if time.monotonic() > ready_deadline:
                err = (
                    stderr_log.read_text(encoding="utf-8", errors="replace")
                    if stderr_log.is_file()
                    else ""
                )
                raise AssertionError(
                    f"worker did not become ready: poll={proc.poll()}, stderr={err}"
                )
            time.sleep(0.01)

        pid_str, tid_str = ready.read_text(encoding="utf-8").strip().split(":")
        worker_pid = int(pid_str)
        worker_tid = int(tid_str)
        assert worker_pid == proc.pid
        assert worker_tid != proc.pid

        starttime = RealProcProbe().starttime(proc.pid)
        assert starttime is not None
        ref = open_owned_linux_process(proc.pid, Path(sys.executable), starttime)

        release_tmp = release.with_suffix(".tmp")
        release_tmp.write_text("go\n", encoding="utf-8")
        os.replace(release_tmp, release)

        leader_deadline = time.monotonic() + 3.0
        leader_state = ""
        stat_file = Path(f"/proc/{proc.pid}/stat")
        while time.monotonic() < leader_deadline:
            if stat_file.is_file():
                raw = stat_file.read_text(encoding="utf-8")
                paren = raw.rfind(")")
                leader_state = raw[paren + 1 :].split()[0] if paren != -1 else ""
                if leader_state == "Z":
                    break
            time.sleep(0.01)
        if leader_state != "Z":
            err = (
                stderr_log.read_text(encoding="utf-8", errors="replace")
                if stderr_log.is_file()
                else ""
            )
            raise AssertionError(
                f"Expected leader state Z, got {leader_state!r}, poll={proc.poll()}, stderr={err}"
            )

        fresh_nonce = uuid.uuid4().hex
        req_tmp = nonce_req.with_suffix(".tmp")
        req_tmp.write_text(fresh_nonce, encoding="utf-8")
        os.replace(req_tmp, nonce_req)

        nonce_deadline = time.monotonic() + 3.0
        received_ack = ""
        while time.monotonic() < nonce_deadline:
            if nonce_ack.is_file():
                received_ack = nonce_ack.read_text(encoding="utf-8").strip()
                if received_ack:
                    break
            time.sleep(0.01)
        if not received_ack:
            err = (
                stderr_log.read_text(encoding="utf-8", errors="replace")
                if stderr_log.is_file()
                else ""
            )
            raise AssertionError(
                f"Worker failed to respond with fresh nonce: poll={proc.poll()}, stderr={err}"
            )

        ack_pid_str, ack_tid_str, ack_nonce = received_ack.split(":", 2)
        assert int(ack_pid_str) == worker_pid == proc.pid
        assert int(ack_tid_str) == worker_tid
        assert ack_nonce == fresh_nonce

        states = RealProcProbe().task_states(proc.pid)
        assert states is not None
        assert states.get(worker_tid) not in {None, "Z", "X"}

        readable, poll_err = poll_linux_pidfd(ref.pidfd, 0.05)
        assert poll_err is None
        obs = observe_owned_posix(proc.pid, pidfd_readable=readable, probe=RealProcProbe())
        assert obs.status == "alive"
        assert obs.leader_state == "Z"

        wait_started = time.monotonic()
        timeout_deadline = wait_started + 0.05
        timed_out = wait_for_owned_thread_group(ref, timeout_deadline)
        wait_finished = time.monotonic()
        assert wait_finished >= timeout_deadline
        assert timed_out.status == "unknown"
        assert timed_out.reason.startswith("deadline:")
        assert "alive" in timed_out.reason
        assert timed_out.leader_state == "Z"
        # Internal elapsed excludes caller-to-entry time; the outer clock proves the deadline.
        assert timed_out.elapsed_seconds is not None
        assert timed_out.elapsed_seconds <= wait_finished - wait_started
        uncleaned_record = _linux_fate_record(
            timed_out,
            initial_pid=proc.pid,
            initial_starttime=starttime,
            cleaned_up=False,
            final_status="alive",
            post_interrupt_status="alive",
            runtime_launches_count=1,
            sentinel_survived=True,
        )
        assert uncleaned_record["cleaned_up"] is False
        assert uncleaned_record["wait_result"] == "unknown"
        assert uncleaned_record["status"] == "alive"

        stop_tmp = stop.with_suffix(".tmp")
        stop_tmp.write_text("stop\n", encoding="utf-8")
        os.replace(stop_tmp, stop)

        done = wait_for_owned_thread_group(ref, time.monotonic() + 3.0)
        assert done.status == "terminal"
        assert done.method == "pidfd"
        assert done.reason.startswith("pidfd_readable_")
        cleaned_record = _linux_fate_record(
            done,
            initial_pid=proc.pid,
            initial_starttime=starttime,
            cleaned_up=True,
            final_status="terminated",
            post_interrupt_status="alive",
            runtime_launches_count=1,
            sentinel_survived=True,
        )
        assert cleaned_record["cleaned_up"] is True
        assert cleaned_record["wait_result"] == "terminal"
        assert cleaned_record["status"] == "terminated"

        assert proc.wait(timeout=3) == 0
    finally:
        stop_tmp = stop.with_suffix(".tmp")
        try:
            stop_tmp.write_text("stop\n", encoding="utf-8")
            os.replace(stop_tmp, stop)
        except OSError:
            pass
        if ref is not None:
            ref.close()
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
