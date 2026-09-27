import json
import os
from pathlib import Path
import subprocess
import sys

repo = Path.cwd()
area = repo / '.verification/windows-process-timeout'
evidence = repo / 'docs/reports/v0.3-rendered/windows-process-timeout'
plan = [('root-plain',False,False), ('root-stages',True,False), ('collection-plain',False,True), ('collection-stages',True,True)]
(evidence / 'root-comparison-plan.json').write_text(json.dumps(plan, indent=2))
results = []
for label, stages, collection in plan:
    env = dict(os.environ)
    env.update(PYTHONPATH=os.pathsep.join((str(area), str(repo/'src'), str(repo))), GF_EXPECTED_SOURCE=str(repo/'src'), GF_DIAG_STAGES='1' if stages else '0', GF_DIAG_OUTPUT=str(evidence/(label+'.json')))
    # Preserve normal plugin autoload for comparison with the original failing runs.
    env.pop('PYTEST_DISABLE_PLUGIN_AUTOLOAD', None)
    selection = ['tests', '-k', 'test_parent_exit_descendant_bounded_and_cleaned'] if collection else ['tests/integration/test_process_runner.py::TestProcessRunner::test_parent_exit_descendant_bounded_and_cleaned']
    args = [sys.executable, '-m', 'pytest', '-ra', '-p', 'no:cacheprovider', '-p', 'diag_plugin', *selection, '--basetemp='+str(area/'runs'/label), '--junitxml='+str(evidence/(label+'.xml'))]
    with (evidence/(label+'.txt')).open('w', encoding='utf-8') as log:
        result = subprocess.run(args, cwd=repo, env=env, stdout=log, stderr=subprocess.STDOUT, timeout=30)
    results.append({'label':label, 'stages':stages, 'normal_collection':collection, 'exit_code':result.returncode, 'argv':args})
    (evidence/'root-comparison-results.json').write_text(json.dumps(results, indent=2))
    print(label, result.returncode, flush=True)
