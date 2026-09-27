"""Fixed five-run series and full validation; records every result without retries."""
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

label = sys.argv[1]
revision = sys.argv[2]
repo = Path('/repo')
work = Path('/tmp') / ('gf-' + revision)
evidence = Path('/evidence')
work.mkdir(exist_ok=False)
for name in ('src', 'tests', 'scripts', 'examples'):
    shutil.copytree(repo / name, work / name, ignore=shutil.ignore_patterns('__pycache__', '.godot', '.gamefactory'))
for name in ('pyproject.toml', 'README.md'):
    shutil.copy2(repo / name, work / name)
manifest = {str(p.relative_to(work)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(work.rglob('*')) if p.is_file()}
record = {'python': sys.version, 'platform': platform.platform(), 'revision': revision, 'manifest': manifest, 'commands': []}
summary = evidence / f'{revision}-{label}-summary.json'

def run(name, args, timeout=180):
    log = evidence / f'{revision}-{label}-{name}.txt'
    with log.open('w') as output:
        try:
            result = subprocess.run(args, cwd=work, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 'TIMEOUT'
    record['commands'].append({'name': name, 'argv': args, 'exit_code': code, 'log': log.name})
    summary.write_text(json.dumps(record, indent=2))
    print(f'{label} {name}: {code}', flush=True)
    return code

if run('install', [sys.executable, '-m', 'pip', 'install', '-e', '.[dev]']) != 0:
    raise SystemExit(1)
target = 'tests/integration/test_verify_godot_live_crash.py'
for index in range(1, 6):
    run(f'targeted-{index}', [sys.executable, '-m', 'pytest', '-ra', '-p', 'no:cacheprovider', target, '-k', 'linux_leader_exit_leaves_worker_alive_until_worker_exits or live_process_at_deadline_fails_with_evidence', f'--junitxml=/evidence/{revision}-{label}-targeted-{index}.xml'])
run('full', [sys.executable, '-m', 'pytest', '-ra', '-p', 'no:cacheprovider', f'--junitxml=/evidence/{revision}-{label}-full.xml'], timeout=300)
run('ruff-check', [sys.executable, '-m', 'ruff', 'check', 'src', 'tests', 'scripts/verify_godot_live_crash.py'])
run('ruff-format', [sys.executable, '-m', 'ruff', 'format', '--check', 'src', 'tests', 'scripts/verify_godot_live_crash.py'])
run('mypy', [sys.executable, '-m', 'mypy', 'src/gamefactory', 'scripts/verify_godot_live_crash.py'])
raise SystemExit(0 if all(c['exit_code'] == 0 for c in record['commands']) else 1)
