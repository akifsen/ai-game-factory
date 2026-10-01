# V0.8 work plan: rigged character internal verification

Design input: ADR 0017 (skin contract), ADR 0018 (`skin_internal` group), ADR 0019 (bounded internal GLB subset). V0.7.0 is released; production profiles are unchanged until explicit promotion.

The [V0.8-1 completion report](reports/v0.8-1-completion-report.md) records the independent local acceptance and remaining milestone gates.

The [V0.8-2 completion report](reports/v0.8-2-completion-report.md) records final local and remote acceptance for evidence/cold verification. Checkpoint `897c92364d0c17ea155d07e81c71e53f0d91c6e2` passed full Windows/Linux CI; V0.8-3 candidate integration and its own final gates follow separately.

Invariants for V0.8-1 slice: no version bump, no production DB/artifact mutation, no paid provider calls, no evidence/cold-verifier promotion, `rigged_character` stays UNSUPPORTED.

| Gate | V0.8-1 status | Notes |
|---|---|---|
| ADR 0019 internal GLB subset | **Done (slice)** | Fail-closed decoder/validator; not production |
| Deterministic 12-bone humanoid fixture | **Done (slice)** | Python builder + package contract |
| `skin_internal` rule group (tests only) | **Done (slice)** | Not in `select_composition` |
| Internal validation negatives | **Done (slice)** | Weights, topology, IBM, accessors, animation, rest bases |
| Godot deformation oracle | **Done (slice)** | Import + script integrity; region displacement bounds |
| Blender authored export path | **Done (slice)** | `export_humanoid_12bone_fixture.py` (real bpy glTF export) |
| Real Blender → GLB → validate → Godot | **Passed (local evidence)** | 4/4 `test_internal_skin_real_tools` on host with Blender 5.2 + Godot 4.7.2 |
| Clean-wheel install acceptance | **Passed (local evidence)** | Offline wheel build/install (commands below) |
| Historical / V0.7 goldens + composition/profile regressions | **Passed (Lead run)** | Independent Lead verification; includes legacy goldens alongside skin/oracle tests |
| Evidence records + cold verifier for skin | **Done (V0.8-2 slice)** | `rig-evidence-0.8.0`, stdlib `verify_rig_bundle.py` |
| Profile promotion (`rigged_character` AVAILABLE) | **Not started** | Requires ADR + production groups |
| Windows + Linux CI for internal skin acceptance | **Passed (V0.8-2 checkpoint)** | Full Windows/Linux 3.11/3.12; installed-wheel Linux actual rig tests under Xvfb |

## V0.8-1 deliverables

- Internal modules under `gamefactory.adapters.assets.internal_skin*`
- Package resources: contract JSON (origins + rest bases + oracle bounds), Blender export script, Godot harness
- Unit tests + opt-in integration tests (no `pytest.skip` on export/validation failures)
- This work plan

## Scoped verification commands (V0.8-1)

From repository root with dev dependencies installed (`pip install -e ".[dev]"`):

```bash
# Internal skin + oracle unit tests
pytest tests/unit/test_internal_skin_validation.py tests/unit/test_skin_oracle_runner.py tests/unit/test_skin_oracle_verify.py -q

# Historical / V0.7 golden regressions (unchanged production paths)
pytest tests/golden/test_backward_compat_golden.py -q

# Lint and types
ruff format --check src tests
ruff check src tests
mypy src
```

Real tools (host must provide Blender and Godot; see `tests/integration/test_internal_skin_real_tools.py` env vars):

```bash
pytest tests/integration/test_internal_skin_real_tools.py -q
```

Offline wheel acceptance (requires `setuptools` and `wheel` already installed in the target environment; no network):

```bash
python -m pip wheel . --no-deps --no-build-isolation --no-index --no-cache-dir --wheel-dir dist
python -m pip install --no-deps --no-index --no-cache-dir --target /absolute/fresh/install-dir dist/gamefactory-0.7.0-py3-none-any.whl
# Use the dev-environment Python (dependencies already installed), change to a
# directory outside the checkout, and set PYTHONPATH to that install directory.
# Confirm gamefactory.__file__ is inside the install directory, then run the
# real-tool test file by its absolute path. Exporter/harness/contract resources
# must come from the installed package. A fully fresh venv additionally needs
# the dependency wheels in a local wheelhouse.
```

## Limitations (explicit)

- **T-pose reference only** (`rest_pose: T`); A-pose deferred.
- **Internal subset only** — not wired into production `validate_glb` / profiles.
- **Blender layout**: optional single armature object node between `HumanoidRoot` and `Hips`; exporter may omit `skin.skeleton` (inferred); wrapper must be identity transform.
- **No production availability** of rigged characters until a later ADR and milestone.

## V0.8-2 deliverables (internal rig evidence)

- `rig-evidence-0.8.0` export via `export_rig_evidence_bundle`
- Stdlib cold verifier: `python -I verify_rig_bundle.py BUNDLE` (`VERIFIED` / `CONSISTENT_BUT_UNAUTHENTICATED` / `FAILED`)
- Pinned reviewed contract, Blender export script, Godot harness; GLB static re-validation; runtime request digest + vertex-sample displacement recompute
- Schemas under `resources/internal_rig/schemas/`, ADR 0020, guide `docs/guides/v0.8-2-internal-rig-evidence.md`
- Tests: `tests/unit/test_internal_rig_cold.py`, `tests/unit/test_internal_rig_schemas.py`, `tests/integration/test_internal_rig_real_tools.py`

Scoped commands:

```bash
python scripts/verify_internal_rig_acceptance.py
pytest tests/integration/test_internal_rig_real_tools.py -q  # Blender + Godot host
python scripts/build_verify_rig_bundle.py  # after changing internal_skin cold logic
```

## Remaining full V0.8 milestone (not claimed by V0.8-1)

1. V0.8-3 candidate profile integration and promotion readiness, beginning from the independently reviewed, exact-SHA CI-verified V0.8-2 checkpoint.
2. Production validator groups mirroring every ADR 0017 item 4 rule with signed fixtures.
3. Operator-facing profile/schema promotion and CLI surfacing.
4. Parallel Windows/Linux CI jobs for Blender export and Godot deformation acceptance.
5. Retargeting, animation, IK, and autorig remain out of scope until separately designed.
