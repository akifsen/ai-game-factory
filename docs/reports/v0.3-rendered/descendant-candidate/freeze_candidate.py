"""Record final candidate source and compare all executed snapshot hashes."""
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET

root = Path.cwd()
evidence = root / 'docs/reports/v0.3-rendered/descendant-candidate'
tracked = subprocess.check_output(['git', 'ls-files', '-z']).decode().split('\0')
paths = [p for p in tracked if p and (p.startswith(('src/', 'tests/', 'scripts/', 'examples/', '.github/')) or p in ('pyproject.toml', 'README.md'))]
hashes = {}
for name in sorted(paths):
    data = (root / name).read_bytes()
    hashes[name] = {'sha256_raw': hashlib.sha256(data).hexdigest(), 'sha256_lf': hashlib.sha256(data.replace(b'\r\n', b'\n')).hexdigest()}
comparisons = {}
for label in ('windows-312', 'linux-311', 'linux-312'):
    summary = json.loads((evidence / f'{label}-summary.json').read_text())
    generated = [p for p in summary['source_sha256_lf'] if p.startswith('src/gamefactory.egg-info/')]
    mismatches = [p for p, value in summary['source_sha256_lf'].items() if p not in generated and value != hashes[p]['sha256_lf']]
    assert not mismatches, (label, mismatches)
    comparisons[label] = {'source_match': True, 'excluded_generated_install_metadata': generated, 'commands': summary['commands']}
    assert all(command['exit_code'] == 0 for command in summary['commands']), label
    results = {}
    for scope in ('target', 'full'):
        suite = ET.parse(evidence / f'{label}-{scope}.xml').getroot().find('testsuite')
        assert suite is not None
        case = next(c for c in suite.findall('testcase') if c.get('name') == 'test_parent_exit_descendant_bounded_and_cleaned')
        assert not any(case.find(kind) is not None for kind in ('failure', 'error', 'skipped'))
        results[scope] = {'suite': suite.attrib, 'descendant_case': case.attrib, 'descendant_result': 'passed'}
    comparisons[label]['results'] = results
base = subprocess.check_output(['git', 'rev-parse', 'HEAD'], text=True).strip()
changed = subprocess.check_output(['git', 'diff', '--name-only'], text=True).splitlines()
manifest = {'base_commit': base, 'candidate': 'uncommitted test-only delta; no final candidate SHA yet', 'source_hashes': hashes, 'validation_source_comparisons': comparisons, 'tracked_files_changed': changed}
(evidence / 'final-source-manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
print(json.dumps({'base': base, 'files_hashed': len(hashes), 'validated_snapshots': list(comparisons), 'changed': changed}, indent=2))
