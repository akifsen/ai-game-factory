"""Temporary, opt-in pytest diagnosis; no product instrumentation."""
import importlib.metadata
import inspect
import json
import os
from pathlib import Path
import sys
import time

import pytest

EVENTS = []
META = {}

def event(name, **data):
    EVENTS.append({'event': name, 'perf_counter': time.perf_counter(), **data})

def pytest_sessionstart(session):
    import gamefactory
    from gamefactory.core.execution import process_runner as module
    expected = Path(os.environ['GF_EXPECTED_SOURCE']).resolve()
    assert Path(gamefactory.__file__).resolve().is_relative_to(expected)
    assert Path(module.__file__).resolve().is_relative_to(expected)
    META.update(python=sys.executable, version=sys.version, source=str(module.__file__),
                gamefactory=str(gamefactory.__file__), cwd=str(Path.cwd()),
                instrumentation=os.environ.get('GF_DIAG_STAGES') == '1',
                dependencies={n: importlib.metadata.version(n) for n in ('pytest', 'pydantic', 'pyyaml', 'pillow')})

@pytest.fixture(autouse=True)
def diagnose(request, monkeypatch, tmp_path):
    if request.node.name != 'test_parent_exit_descendant_bounded_and_cleaned':
        yield
        return
    META['linux_module_imported'] = any('test_verify_godot_live_crash' in n for n in sys.modules)
    if os.environ.get('GF_DIAG_STAGES') != '1':
        yield
        return
    from gamefactory.core.execution import process_runner as m
    original_run = m.ProcessRunner.run
    original_popen = m.subprocess.Popen
    original_read = m.ProcessRunner._read_bounded
    original_attach = m.ProcessRunner._attach_windows_job
    original_close = m.ProcessRunner._close_windows_job
    original_kill = m.ProcessRunner._kill_tree
    streams = {}
    source_lines, first_line = inspect.getsourcelines(original_run)
    text_by_line = {first_line+i: text.strip() for i, text in enumerate(source_lines)}
    deadline_seen = False

    def trace(frame, kind, arg):
        nonlocal deadline_seen
        if frame.f_code is not original_run.__code__:
            return None
        if kind == 'line':
            local = frame.f_locals
            if 'deadline' in local and not deadline_seen:
                event('runner_deadline', start_time=local['start_time'], deadline=local['deadline'])
                deadline_seen = True
            if text_by_line.get(frame.f_lineno) == 'timed_out = True':
                event('timeout_branch', line=frame.f_lineno, parent_returncode=local['proc'].returncode,
                      readers_alive=local.get('readers_alive'), observed_now=local.get('now'),
                      timeout_exception=str(local.get('timeout_exc')))
        return trace

    class Popen(original_popen):
        def __init__(self, *args, **kwargs):
            event('popen_begin', executable=str(args[0][0]))
            super().__init__(*args, **kwargs)
            if self.stdout:
                streams[id(self.stdout)] = 'stdout'
            if self.stderr:
                streams[id(self.stderr)] = 'stderr'
            event('popen_created', pid=self.pid)

        def wait(self, timeout=None):
            event('parent_wait_begin', pid=self.pid, timeout=timeout)
            try:
                result = super().wait(timeout)
            except Exception as exc:
                event('parent_wait_error', pid=self.pid, exception=type(exc).__name__)
                raise
            event('parent_exit_observed', pid=self.pid, exit_code=result)
            return result

    def attach(proc):
        event('job_attach_begin', pid=proc.pid)
        result = original_attach(proc)
        event('job_attach_end', handle=result)
        return result

    def close(job):
        event('job_cleanup_begin', handle=job)
        try:
            return original_close(job)
        finally:
            event('job_cleanup_return', handle=job)

    def kill(proc):
        event('kill_tree_begin', pid=proc.pid)
        try:
            return original_kill(proc)
        finally:
            event('kill_tree_return', pid=proc.pid)

    def read(stream, max_bytes=m._MAX_CAPTURE_CHARS):
        channel = streams.get(id(stream), 'unknown')
        event('reader_begin', channel=channel)
        result = original_read(stream, max_bytes)
        event('reader_completed', channel=channel, eof=result.read_completed, bytes=len(result.data))
        return result

    def run(self, command):
        # Separate instrumented trials only: add child checkpoints to this test's existing fixture.
        descendant = Path(command.cwd) / 'grandchild.py'
        descendant_log = tmp_path / 'descendant-ready.json'
        parent_log = tmp_path / 'parent-exit-requested.json'
        descendant.write_text(
            'import os,json,time\n'
            f'open({str(descendant_log)!r}, "w").write(json.dumps({{"event":"descendant_ready","perf_counter":time.perf_counter(),"pid":os.getpid()}}))\n'
            + descendant.read_text(), encoding='utf-8')
        from dataclasses import replace
        args = list(command.args)
        args[-1] = args[-1].replace('sys.exit(0)',
            'import os,json,time; '
            f'open({str(parent_log)!r}, "w").write(json.dumps({{"event":"parent_exit_requested","perf_counter":time.perf_counter(),"pid":os.getpid()}})); sys.exit(0)')
        command = replace(command, args=args)
        previous = sys.gettrace()
        event('runner_call', timeout=command.timeout_seconds)
        sys.settrace(trace)
        try:
            result = original_run(self, command)
            event('runner_return', result=result.to_dict())
            return result
        except Exception as exc:
            event('runner_exception', exception=type(exc).__name__, details=getattr(exc, 'details', {}))
            raise
        finally:
            sys.settrace(previous)
            for path in (descendant_log, parent_log):
                if path.exists():
                    EVENTS.append(json.loads(path.read_text()))
                else:
                    event('checkpoint_absent', file=path.name)

    monkeypatch.setattr(m.subprocess, 'Popen', Popen)
    monkeypatch.setattr(m.ProcessRunner, '_attach_windows_job', staticmethod(attach))
    monkeypatch.setattr(m.ProcessRunner, '_close_windows_job', staticmethod(close))
    monkeypatch.setattr(m.ProcessRunner, '_kill_tree', staticmethod(kill))
    monkeypatch.setattr(m.ProcessRunner, '_read_bounded', staticmethod(read))
    monkeypatch.setattr(m.ProcessRunner, 'run', run)
    yield

def pytest_sessionfinish(session, exitstatus):
    META['pytest_exit_code'] = int(exitstatus)
    Path(os.environ['GF_DIAG_OUTPUT']).write_text(json.dumps({'metadata': META, 'events': sorted(EVENTS, key=lambda e:e['perf_counter'])}, indent=2), encoding='utf-8')
