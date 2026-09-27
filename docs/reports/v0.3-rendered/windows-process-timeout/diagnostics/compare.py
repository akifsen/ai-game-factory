import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

repo = Path.cwd()
area = repo / '.verification/windows-process-timeout'
evidence = repo / 'docs/reports/v0.3-rendered/windows-process-timeout'
baseline = repo / '.verification/linux-regression-fix/baseline'
(area / 'runs').mkdir(exist_ok=True)
for name, source in (('base', baseline), ('cand', repo)):
    dest = area / name
    if dest.exists():
        continue
    dest.mkdir(exist_ok=False)
    for directory in ('src', 'tests'):
        shutil.copytree(source / directory, dest / directory, ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copy2(source / 'pyproject.toml', dest / 'pyproject.toml')
    manifest = {str(p.relative_to(dest)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(dest.rglob('*')) if p.is_file()}
    (evidence / (name + '-manifest.json')).write_text(json.dumps(manifest, indent=2))

plan = [('b1','base',False), ('c1','cand',False), ('c2','cand',False), ('b2','base',False), ('b3','base',False), ('c3','cand',False), ('b-stage','base',True), ('c-stage','cand',True)]
plan = [('ready-' + label, snapshot, stages) for label, snapshot, stages in plan]
(evidence / 'comparison-plan.json').write_text(json.dumps({'python': sys.executable, 'fixed_attempts': plan, 'outer_process_timeout': 30}, indent=2))
outcomes = []
for label, snapshot, stages in plan:
    root = area / snapshot
    env = dict(os.environ)
    env.update(PYTHONPATH=os.pathsep.join((str(area), str(root/'src'), str(root))),
               GF_EXPECTED_SOURCE=str(root/'src'), GF_DIAG_STAGES='1' if stages else '0',
               GF_DIAG_OUTPUT=str(evidence/(label+'.json')), PYTEST_DISABLE_PLUGIN_AUTOLOAD='1')
    args = [sys.executable, '-m', 'pytest', '-ra', '-p', 'no:cacheprovider', '-p', 'diag_plugin',
            'tests/integration/test_process_runner.py::TestProcessRunner::test_parent_exit_descendant_bounded_and_cleaned',
            '--basetemp='+str(area/'runs'/label), '--junitxml='+str(evidence/(label+'.xml'))]
    with (evidence/(label+'.txt')).open('w', encoding='utf-8') as log:
        try:
            result = subprocess.run(args, cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=30)
            code = result.returncode
        except subprocess.TimeoutExpired:
            code = 'WATCHDOG_TIMEOUT'
    outcomes.append({'attempt': label, 'snapshot': snapshot, 'instrumented': stages, 'exit_code': code, 'argv': args})
    (evidence/'comparison-results.json').write_text(json.dumps(outcomes, indent=2))
    print(label, code, flush=True)
