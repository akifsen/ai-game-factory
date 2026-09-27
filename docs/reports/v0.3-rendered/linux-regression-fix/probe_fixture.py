"""One-shot supervisor-free A/B diagnostic of the baseline inline fixture."""
import ast
import json
import pathlib
import platform
import subprocess
import sys
import tempfile
import time

source = pathlib.Path('/repo/.verification/linux-regression-fix/baseline/tests/integration/test_verify_godot_live_crash.py').read_text()
tree = ast.parse(source)
test = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == 'test_linux_leader_exit_leaves_worker_alive_until_worker_exits')
script = next(n.value.value for n in test.body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'script' for t in n.targets))
results = []
for variant in ('original', 'without_manual_save_thread'):
    code = script
    if variant != 'original':
        code = code.replace('save = ctypes.pythonapi.PyEval_SaveThread\nsave.restype = ctypes.c_void_p\nsave()\n', '')
    with tempfile.TemporaryDirectory() as folder:
        root = pathlib.Path(folder)
        ready, beat, release, stop = [root / n for n in ('ready', 'beat', 'release', 'stop')]
        proc = subprocess.Popen([sys.executable, '-X', 'faulthandler', '-c', code, *map(str, (ready, beat, release, stop))], stderr=subprocess.PIPE, text=True)
        record = {'variant': variant, 'pid': proc.pid}
        try:
            deadline = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.01)
            record['ready'] = ready.exists()
            release.write_text('go')
            deadline = time.monotonic() + 2
            leader = ''
            while time.monotonic() < deadline:
                raw = pathlib.Path(f'/proc/{proc.pid}/stat').read_text()
                leader = raw[raw.rfind(')') + 1:].split()[0]
                if leader == 'Z':
                    break
                time.sleep(.01)
            record['leader_state'] = leader
            first = beat.read_text() if beat.exists() else None
            deadline = time.monotonic() + 1
            second = first
            while second == first and time.monotonic() < deadline:
                time.sleep(.01)
                second = beat.read_text() if beat.exists() else None
            record.update(first=first, second=second, worker_progress=first != second)
            record['tasks'] = {}
            for stat in pathlib.Path(f'/proc/{proc.pid}/task').glob('*/stat'):
                raw = stat.read_text()
                record['tasks'][stat.parent.name] = raw[raw.rfind(')') + 1:].split()[0]
        except Exception as exc:
            record['error'] = repr(exc)
        finally:
            stop.write_text('stop')
            try:
                _, stderr = proc.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                _, stderr = proc.communicate(timeout=2)
                record['cleanup_killed'] = True
            record.update(exit_code=proc.returncode, stderr=stderr)
        results.append(record)
print(json.dumps({'python': sys.version, 'platform': platform.platform(), 'supervisor_used': False, 'results': results}, indent=2))
