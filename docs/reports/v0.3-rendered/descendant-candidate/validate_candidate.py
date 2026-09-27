"""Single-pass final-source validation; preserve all command results."""
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys

repo, evidence, label = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]
if sys.platform.startswith('linux'):
    work = Path('/tmp/descendant-candidate')
    work.mkdir(exist_ok=False)
    for name in ('src', 'tests', 'scripts', 'examples'):
        shutil.copytree(repo / name, work / name, ignore=shutil.ignore_patterns('__pycache__', '.godot', '.gamefactory'))
    for name in ('pyproject.toml', 'README.md'):
        shutil.copy2(repo / name, work / name)
else:
    work = repo
evidence.mkdir(parents=True, exist_ok=True)
(work / '.verification').mkdir(exist_ok=True)
files = sorted(p for directory in ('src', 'tests', 'scripts') for p in (work / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts)
record = {'python': sys.version, 'platform': platform.platform(), 'source_sha256_lf': {p.relative_to(work).as_posix(): hashlib.sha256(p.read_bytes().replace(b'\r\n', b'\n')).hexdigest() for p in files}, 'commands': []}
commands = []
if sys.platform.startswith('linux'):
    commands.append(('install', ['-m', 'pip', 'install', '-e', '.[dev]']))
commands.extend([
    ('target', ['-m', 'pytest', '-ra', '-p', 'no:cacheprovider', 'tests/integration/test_process_runner.py', f'--junitxml={evidence / (label + "-target.xml")}']),
    ('full', ['-m', 'pytest', '-ra', '-p', 'no:cacheprovider', f'--junitxml={evidence / (label + "-full.xml")}']),
    ('ruff-check', ['-m', 'ruff', 'check', 'src', 'tests']),
    ('ruff-format', ['-m', 'ruff', 'format', '--check', 'src', 'tests']),
    ('mypy', ['-m', 'mypy', 'src/gamefactory']),
])
for name, args in commands:
    if name in ('target', 'full'):
        args.append(f'--basetemp={work / ".verification" / (label + "-" + name)}')
    argv = [sys.executable, *args]
    with (evidence / f'{label}-{name}.txt').open('w', encoding='utf-8') as log:
        try:
            code = subprocess.run(argv, cwd=work, stdout=log, stderr=subprocess.STDOUT, timeout=300).returncode
        except subprocess.TimeoutExpired:
            code = 'TIMEOUT'
    record['commands'].append({'name': name, 'argv': argv, 'exit_code': code})
    (evidence / f'{label}-summary.json').write_text(json.dumps(record, indent=2), encoding='utf-8')
    print(f'{label} {name}: {code}', flush=True)
    if name == 'install' and code != 0:
        break
raise SystemExit(0 if all(c['exit_code'] == 0 for c in record['commands']) else 1)
