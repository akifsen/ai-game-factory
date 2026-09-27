"""Show the baseline absolute-deadline / internal-elapsed contract deterministically."""
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

repo = next(p for p in Path(__file__).resolve().parents if (p / 'pyproject.toml').exists())
root = repo / '.verification/linux-regression-fix'
spec = importlib.util.spec_from_file_location('baseline_verifier', root / 'baseline/scripts/verify_godot_live_crash.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
clock = SimpleNamespace(now=0.0)
deadline = clock.now + 0.0625
clock.now = 0.015625  # Time between caller constructing deadline and function entry.
polls = []

def poll(fd, timeout):
    polls.append({'at': clock.now, 'timeout': timeout})
    clock.now += min(timeout, 0.0078125)  # Early/empty poll returns.
    return False, None

module.time = SimpleNamespace(monotonic=lambda: clock.now)
probe = SimpleNamespace(task_states=lambda pid: {pid: 'R'}, read_exe=lambda pid: '/bin/sleep')
observation = module.wait_for_owned_thread_group(module.LinuxProcessRef(40, '100', -1, 'pidfd'), deadline, probe=probe, poll_pidfd=poll)
assert clock.now == deadline
assert observation.elapsed_seconds == 0.046875
assert observation.status == 'unknown'
print(json.dumps({'baseline': '8af661f9e2e407c6ec0ac22a9a9ed91f43d3de92', 'deadline_created_at': 0.0, 'deadline': deadline, 'entry_and_elapsed_start': 0.015625, 'return_time': clock.now, 'observation': observation.as_report(), 'early_empty_polls': polls, 'conclusion': 'Deadline reached exactly; internal elapsed excludes caller-to-entry delay.'}, indent=2))
