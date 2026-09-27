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
