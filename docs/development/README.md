# Development

Use Python 3.11+. Install editable with `python -m pip install -e '.[dev]'`. The project uses pytest, Ruff, and mypy. Tests do not require paid-service credentials or network access.

Run checks from the repository root. Pytest uses its own temporary directory by default, so the command works on a clean checkout:

```bash
python -m pytest -p no:cacheprovider
ruff check src tests
ruff format --check src tests
mypy src/gamefactory
```

Windows is a first-class target. Prefer `pathlib`, argument arrays, and `shell=False` for subprocesses. Keep project and managed state paths inside the selected project root. Never store provider secrets in project YAML or logs.

For CLI smoke testing, use the bundled game fixture in `examples/minimal-godot`. The fixture is a normal Godot project and contains no Factory runtime integration.


For V0.2, pure contract tests and fake-process integration tests run without Godot.
Real-engine acceptance is opt-in:

```powershell
$env:GAMEFACTORY_TEST_GODOT = 'C:\tools\godot_console.exe'
python -m pytest -p no:cacheprovider -m real_godot
```

```bash
GAMEFACTORY_TEST_GODOT=/opt/godot/godot python -m pytest -p no:cacheprovider -m real_godot
```

The real test writes a persistent report and workspaces under `.verification/ci-godot`.
It launches fresh Factory CLI processes and real Godot processes. The included
`examples/godot-verification` project remains an ordinary standalone Godot game.
The dedicated CI job pins a release and verifies archive SHA-512 before execution.
A local Linux mypy target does not establish Linux runtime acceptance.

## V0.7 installed-package acceptance

Build a wheel and install it into a fresh virtual environment with the development
dependencies. Run the acceptance driver from a temporary directory outside the
checkout, using that environment's Python and the actual wheel file:

```bash
/path/to/venv/bin/python -I /path/to/checkout/scripts/verify_v07_installed_acceptance.py \
  --repo-root /path/to/checkout \
  --blender /path/to/blender \
  --godot /path/to/godot \
  --wheel /path/to/gamefactory.whl \
  --wheel-hash /path/to/wheel.sha256 \
  --evidence-dir /path/to/evidence
```

The driver requires all ten suites to pass without skips, checks installed module
provenance, clears live provider credentials from child processes, and compares
the actual wheel's SHA-256 with the supplied pin. Test authoring helpers remain in
the checkout; production modules, profiles, specs, and DCC resources must come
from the installed package. Only named offline fixture approvals are exercised.

Each invocation creates a separate run directory. Read its `results.json`, not a
historical report in the evidence base. A subset, source-checkout permission, or
skip permission produces diagnostic results and cannot satisfy the release gate.
Use a fresh private temporary directory if the system's shared pytest temporary
directory is inaccessible; do not delete another session's files.

The Linux rendered CI job runs this driver with Xvfb and software OpenGL. Local
Windows acceptance does not establish that Linux gate.
