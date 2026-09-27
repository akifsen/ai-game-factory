# V0.4 Linux Verification Environment

Status: toolchain prepared; V0.4 source acceptance has not been run.

## Toolchain

An isolated Docker image, `gamefactory-v04-linux:local`, was built from the cached `python:3.12-slim` base on Debian 13 (trixie). It includes:

- Python 3.12.14 from the base image;
- Blender 4.3.2 from Debian trixie;
- Godot 4.7.2 stable, official build `4.7.2.stable.official.ed1daf0bf`;
- Xvfb, Mesa software rendering, and the OpenGL runtime libraries needed for headless/rendered probes.

Godot was downloaded from the official GitHub release URL used by `.github/workflows/ci.yml`. The archive SHA-512 was checked against the CI pin before extraction:

`9aa00f7a605200940bce3027a567b782f49bd8e940dd06ae9e987bd65aee1b1467edd56ed84fcdcbdd44354bf613bdbb4e5d2913e925850368e150c59ed54c65`

The image definition is [Dockerfile](../../../.verification/v04-linux-env/Dockerfile). Build command:

```powershell
docker build --pull=false -t gamefactory-v04-linux:local .verification/v04-linux-env
```

Build installed 430 Debian packages (412 MB download, approximately 1.6 GB installed) to obtain actual Blender and its runtime libraries. This build took about 2.5 minutes for package setup and about 100 seconds to export image layers. No existing containers were modified.

## Readiness probes

The persistent container `gamefactory-v04-linux-verify` is running as container ID `db59114e243b362a4cadf2b1bf6ad0ee939c54e3494a3ed835daaff639984ee2`. At creation, the repository was mounted at `/workspace` read-only. No host credentials, environment secrets, or other host directories were mounted.

Verified command results:

```text
python --version                       Python 3.12.14
blender --version | head -n 1          Blender 4.3.2
$GODOT_BIN --version                   4.7.2.stable.official.ed1daf0bf
xvfb-run ... glxinfo -B                Mesa llvmpipe; OpenGL 4.5 Compatibility Profile
findmnt -no OPTIONS /workspace         ro,...
```

Xvfb and Mesa results establish that the software display stack starts. They do not by themselves establish that the project’s rendered capture behavior passes.

## Reuse

Inspect the current container:

```powershell
docker exec gamefactory-v04-linux-verify bash -lc 'python --version; blender --version | head -n 1; "$GODOT_BIN" --version'
```

Run later verification work in this container with outputs directed to `/tmp` or a writable evidence directory. `/workspace` is a read-only view of the live checkout. Once V0.4 source is stable, record the source commit/status and source manifest before running its acceptance commands; use a read-only source snapshot if a frozen source state is needed. A clean venv exists at `/tmp/v04-venv`; it contains verification/build dependencies but no install of the project.

### Reusable verification venv

Prepared `/tmp/v04-venv` inside the running container, with no editable or wheel install of `gamefactory`. It contains build, lint, typing, test, and runtime dependencies needed for a future frozen source build. The install command was:

```bash
python -m venv /tmp/v04-venv
/tmp/v04-venv/bin/python -m pip install --upgrade pip
/tmp/v04-venv/bin/python -m pip install build pytest ruff mypy 'pydantic>=2' 'pyyaml>=6' 'pillow>=10' types-PyYAML
/tmp/v04-venv/bin/python -m pip check
```

`pip check` passed. Resolved direct tool and application dependency versions:

| Package | Version |
| --- | --- |
| pip | 26.2.1 |
| build | 1.6.1 |
| pytest | 9.1.1 |
| ruff | 0.16.9 |
| mypy | 2.3.1 |
| pydantic | 2.13.5 |
| PyYAML | 6.0.3 |
| Pillow | 12.3.0 |
| types-PyYAML | 6.0.12.20260906 |

The venv also resolved packaging 26.3, pyproject-hooks 1.3.3, pluggy 1.6.0, pygments 2.21.0, iniconfig 2.3.0, mypy-extensions 1.1.0, pathspec 1.1.1, and pydantic-core 2.46.5. These are live PyPI resolutions, not a lockfile. Once source is frozen, the lead can build its wheel from a writable snapshot and install that wheel plus `[dev]` into this venv, or use a fresh venv if further isolation is required.

### Suggested later acceptance commands

The generic Linux CI job runs editable install plus pytest, Ruff check/format, and mypy. Equivalent commands against a frozen writable snapshot are:

```bash
/tmp/v04-venv/bin/python -m pip install -e '.[dev]'
/tmp/v04-venv/bin/python -m pytest -ra -p no:cacheprovider
/tmp/v04-venv/bin/ruff check src tests
/tmp/v04-venv/bin/ruff format --check src tests
/tmp/v04-venv/bin/mypy src/gamefactory
```

For V0.4’s real Blender subprocess coverage, `tests/integration/test_blender_processing.py` exercises actual processing when Blender is available. Narrow command:

```bash
/tmp/v04-venv/bin/python -m pytest -ra -p no:cacheprovider tests/integration/test_blender_processing.py
```

For real Godot, follow the repository CI’s `godot-real` job: build/install the wheel into the venv, then run `scripts/verify_godot_acceptance.py`, `scripts/verify_godot_recovery.py`, and `scripts/verify_godot_live_crash.py` with `--cli`, `--python`, `--fixture`, `--godot`, `--output`, and `--workspace` arguments as in `.github/workflows/ci.yml`. The live-crash script has Linux-specific child identity/recovery code. For rendered capture, follow `godot-rendered` with `LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a --server-args='-screen 0 1600x1200x24'` around `scripts/verify_godot_capture.py` and the live-crash invocation with `--rendered-capture`. Those scripts write under the repository’s `.verification` folder, so run them from a writable frozen source copy; the prepared `/workspace` mount is read-only. Keep outputs and workspaces on the snapshot or `/tmp`, not the live checkout.

For a software-rendered graphical command:

```bash
LIBGL_ALWAYS_SOFTWARE=1 xvfb-run -a --server-args="-screen 0 1600x1200x24" <command>
```

The image sets `LIBGL_ALWAYS_SOFTWARE=1`; Godot’s executable is at `/opt/godot/Godot_v4.7.2-stable_linux.x86_64` and exported in `$GODOT_BIN`.

## Scope and limitations

Only environment probes were executed. No pytest, application command, acceptance script, or source-dependent check was run because V0.4 implementation is still in progress. This report must not be treated as Linux acceptance evidence. Debian’s Blender package version is not separately pinned by an apt snapshot, so a future rebuild can resolve a newer Blender package; the already-built local image remains available for this run. Rebuilding from the Dockerfile verifies the pinned Godot checksum but may fetch newer Debian package revisions.
