# V0.4 continuation verification — 2026-09-27

The continuation began with 21 tracked files modified and the existing V0.4
implementation/evidence untracked. The baseline is retained under
`.verification/v04-resume-baseline/`; existing V0.3 changes were preserved.

## Independently executed checks

- Windows source suite: **468 passed, 16 skipped**, 252.81 seconds.
  [XML](windows-final-v3/pytest-resume.xml), [log](windows-final-v3/pytest-resume.txt).
  Skips cover POSIX/pidfd behavior, Windows symlink privileges and two opt-in
  engine tests. Real headless and asset acceptance were executed separately.
- Windows installed-wheel Godot regression: **PASSED**, 43 CLI/process commands.
  [Report](windows-final-v3/godot-acceptance-resume.json).
- Windows installed-wheel asset acceptance: **PASS**, real Blender 5.2.1 and
  Godot 4.7.2, three captures, cold bundle verification and tamper rejection.
  [Report](windows-final-v3/asset-acceptance-resume.json),
  [offline review](windows-final-v3/asset-acceptance-resume.bundle/index.html).
- Linux installed-wheel asset acceptance: **PASS**, real Blender 4.3.2 and
  Godot 4.7.2 under Xvfb/llvmpipe, cold bundle verification and tamper rejection.
  [Report](linux-resume/reports/asset-acceptance.json),
  [offline review](linux-resume/reports/asset-acceptance.bundle/index.html).
- Linux Ruff, format and mypy: **PASS**. [Log](linux-resume/quality.txt).
- The 76 application files match the tested frozen archive and the installed
  Windows wheel byte for byte. [Parity](application-source-parity-resume.json).
- Production workflow inspected again: `WF-ASSET-96f68a4f` remains `BLOCKED`,
  concept approval `APP-9878c627` remains `PENDING`, paid invocations **0**.
  [Inspection](human-review-inspect-resume.json).

## Failures preserved and corrected environment setup

The first source pytest attempt could not import `gamefactory` because `.venv`
had no installed package. Source validation used `PYTHONPATH=src`; wheel
acceptance used the separately installed wheel with an empty `PYTHONPATH`.
The Linux verification venv lacked the declared setuptools build backend;
setuptools and wheel were installed before building. An initial Windows asset
invocation had no workspace directory; a new directory was created and the full
acceptance rerun passed. These attempts were not counted as successful checks.

The earlier frozen Windows suite's real Godot import failure is preserved in
`windows-final-v3/pytest.xml`. The separate 43-command regression above passed.

The initial Linux suite produced **478 passed, 4 skipped, 2 failed**. Both
failures were in opt-in test launchers resolving a venv Python symlink into the
base interpreter, losing either the sibling CLI or installed package. The
original [XML](linux-resume/reports/pytest.xml) and logs are retained. Their
correction preserves the absolute venv invocation path without resolving its
symlink. The changed launcher group now passes: **51 passed** on Linux, including
both previously failing real-engine tests and Python resolution regressions
([log](linux-resume/launcher-correction.txt),
[XML](linux-resume/reports/pytest-launcher-correction.xml)). Windows targeted
verification also passed: **41 passed, 9 skipped**
([log](windows-final-v3/launcher-correction.txt)). These targeted results close
the two failures; the original full Linux run is not relabelled as a clean run.

Antigravity's initial inspection reported no application changes. The narrow
launcher revision delegation timed out after 15 minutes without a response or
file changes. The lead then applied the five mechanical `resolve()` to
`absolute()` corrections under the trivial-edit/tool-unavailable exception and
independently verified them. Process identity checks still resolve the actual
executable; only interpreter invocation paths changed.

Final Ruff/format: **PASS**, 113 files formatted
([lint](ruff-resume.txt), [format](format-resume.txt)). Application source remains
unchanged from the two tested wheels. The final source archive includes only
the two corrected test launchers beyond the prior frozen source:
`df4756e35c5a5986e091812e7b67122e4faf37dca34f1149d09362a91cc59855`.
See [final manifest](source-manifest-resume.json) and
[launcher diff](launcher-correction.diff).

**Engineering decision: APPROVED for V0.4 software implementation.** Production
concept/paid/final human decisions remain separate and pending as applicable.

## Evidence boundaries

Both asset reviews use a deterministic fake-generated GLB and test approvals.
They establish real local processing/runtime behavior, not Meshy generation or
human visual acceptance. The real SDXL concept still requires its separate human
decision. No production approval, paid request, commit, push or remote CI run was
performed in this continuation.
