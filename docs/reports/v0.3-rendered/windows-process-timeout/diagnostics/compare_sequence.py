import json
import os
from pathlib import Path
import subprocess
import sys

repo = Path.cwd()
area = repo / '.verification/windows-process-timeout'
evidence = repo / 'docs/reports/v0.3-rendered/windows-process-timeout'
plan = [('module-stages','tests/integration/test_process_runner.py'), ('full-stages','tests')]
(evidence/'sequence-plan.json').write_text(json.dumps(plan, indent=2))
results = []
for label, selection in plan:
    env = dict(os.environ)
    env.update(PYTHONPATH=os.pathsep.join((str(area), str(repo/'src'), str(repo))), GF_EXPECTED_SOURCE=str(repo/'src'), GF_DIAG_STAGES='1', GF_DIAG_OUTPUT=str(evidence/(label+'.json')))
    args = [sys.executable, '-m', 'pytest', '-ra', '-p', 'no:cacheprovider', '-p', 'diag_plugin', selection, '--basetemp='+str(area/'runs'/label), '--junitxml='+str(evidence/(label+'.xml'))]
    with (evidence/(label+'.txt')).open('w', encoding='utf-8') as log:
        result = subprocess.run(args, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=180)
    results.append({'label':label, 'exit_code':result.returncode, 'argv':args})
    (evidence/'sequence-results.json').write_text(json.dumps(results, indent=2))
    print(label, result.returncode, flush=True)
