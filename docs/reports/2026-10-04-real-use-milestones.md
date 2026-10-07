# Real customer use checkpoint (2026-10-06)

Implementation is frozen under [the current instruction](../product-direction.md).
This report summarizes this task; historical report files and Git history were
not removed. Raw logs, captures, workflow databases and builds remain outside Git.
Customer commits below are local commits; no customer push is claimed.

| Milestone | Status | Workflow / customer commit | Human interventions | Cost / credits | Manual corrections | Elapsed |
| --- | --- | --- | --- | --- | --- | --- |
| M0 | Complete | Natural main push CI [37213340577](https://github.com/akifsen/ai-game-factory/actions/runs/37213340577), main `310fa521cb49f1b143218b3e761589008ecb0961` | 0 | No new provider call; existing CI billing unknown | None; no manual CI trigger/rerun | Individual CI reads recorded; total not measured |
| M1 | Complete | `WF-M1-TEXT-SCALE-RETRY-20261005` COMPLETED; Tide Bastion `1cbf6bafdd255928f08770422230ca9f86ce314e` | 4 recorded continuation/repair/write authorizations | 2 actual Codex calls; USD 2 reserved total, actual billed fee unknown | Saved-manifest and project-context parsing repairs; no manual proposal edits | Calls 135.16 s and 88.86 s; final checks 10 min 18 s; user waits excluded |
| M2 | Complete | `WF-GAMEPLAY-6fc2f444` COMPLETED, 11/11 rules PASS; Tide Bastion `ee2fb5a1ea7339d85fffe829faf3be0fda3bf87c` | 0 new user interventions | No Factory metered provider or Meshy generation; engineering-agent billing unknown | Fixture isolation/hash/process checks; lead fresh-path and full-rect layout corrections | Final Factory workflow 27.38 s; preparation/review about 34 min including delegated-tool waits |
| M3 | Complete | `WF-PROJECT-4c53361f`, build `EXEC-066c13b5`; Tide Bastion `b1fb5ecb824162f5271f0a7a99e0dab36978c23b` | 0 new user interventions | Local Windows export/startup; engineering-agent billing unknown | Windows preset only; Android preset unchanged | Export 15.013 s, native startup check 6.80 s; milestone about 17 min including audit/preparation |
| M4 | Blocked before plan invocation | No production workflow or customer commit | Specific new Codex plan approval pending | No M4 Codex dispatch; Meshy submissions 0; free account check balance 819, one image-to-3D CLI estimate 30 credits | None; no paid submission or approval bypass | Not executed |

## M1 evidence and real-use repairs

The actual defect was the text_scale HSlider's 0-100 bounds despite existing
85-140 percent consumers. Codex proposed only the two bounds in
`ui/screens/settings_screen.gd`. Output SHA:
`360af4675db5c52be2c6a5a08f6d76b15128099a756884a73daff06aa54a613f`.
Both durable code gates `GATE-1b5f9584` and `GATE-e97c82f3` passed 76/76 scripts.
Independent real SettingsScreen checks verified seven unchanged sliders,
retained stored 80 on opening, displayed minimum 85 and persistence of 140
in a separate verification save. Applied bytes matched the gated proposal.
Human write approval `APP-56d10c91` was recorded after the direct response to
the concrete application question; Factory applied the file and completed.

Original `WF-FACTORY-7356d61d` remains FAILED. Saved-manifest reconstruction
first rejected its false acceptance candidate before trying the actual true
candidate; the repair catches only validation failure and retains exact hash
matching. Its real Codex `EXEC-4a3d2256` completed, but gate `EXEC-36d03606`
failed because isolated per-script checks could not resolve project autoloads.
The repaired gate uses project-context ResourceLoader validation. No historical
status, approval, retry flag or DB record was changed to turn failure into PASS.
The new approved dispatch is `EXEC-1b0b1e40`; its manifest SHA is
`20f1a5c66296d6dcebfeae910c0ab749c7e93478bc05ad7a380bdaf725d3c9e7`.
The legacy paid invocation counter omits Codex; actual executions establish two
calls. Ledger reservations are not settled charges.

## M2 evidence and review corrections

The fixture runs the real battle UI, BattleSession, SimWorld and IslandStage
at campaign.s1.c01.l001. Actual state: tick120, core48, tower1, enemies5, wave1;
setup/build/wave start/pause/resume assertions passed. Scenario SHA:
`9f48c9dbb9483d2ba07c2aeb53757dd296536c325331742a35c648612446fb10`.
Final source SHA `5fae3c8c7e1254ecdbcb90fe98999bbaa9ab988c31e2af5493bb2f9b00aac87c`.
Independent fixture run checked live tick equality and original save hashes;
pre-existing `test_battle_flow.gd` separately passed. No human visual acceptance
is claimed. Artifact hashes and real rendered capture were inspected.

Antigravity's first fixture was CHANGES REQUIRED for original-save restoration
and conditional deletion code. Its revision removed those operations but a
frozen candidate failed parse (`PackedByteArray.sha256_text` is unavailable).
Antigravity returned without a collected verification result and its bridge
terminated a pending task. Cursor fallback repaired the hash helper and process
disable checks. Team Lead independently verified the result and made tiny
fresh-path/full-rect corrections. First Factory run `WF-GAMEPLAY-fc85abf7`
passed states but exposed compressed fixture layout; final run also passed states
and produced the full battle view. Raw evidence for both runs is preserved.
Original customer primary/backup hashes matched the pre-revision snapshot after
all final M2 and M3 checks. No original disk restore/deletion was used by the
accepted fixture. Initial rejected fixture runs preceded that snapshot; no
pre-snapshot preservation claim is made.

## M3 build inspection and independent startup

Windows x86_64 release EXE SHA:
`034f02ae27e54657f7ceb47e8f7084f8d32b482fc10ffa755aecca009596533b`.
PCK SHA `46735895cf235ffcbd151a62ee47efd8eacceacb66f2bc462fce34bef640b055`.
A separate empty Godot audit project inspected 268 exported files: no tests,
Factory harness/state/reference or credential-file paths; no selected provider
key patterns. Byte scans of EXE/PCK found no provider tokens or complete PEM
private-key blocks. EXE PEM header strings also occur in the installed Godot
release template; they are parser constants, not evidence of embedded keys.
This is a bounded pattern/source exclusion audit, not a universal secret proof.

Only EXE and PCK were copied into a fresh launch folder. Native startup used
System32-only PATH, no Factory/Python environment and separate APPDATA profile.
Vulkan rendered on the local GPU; existing production smoke boot returned
`PASS ui tick=7 towers=1`, exit0, no error log. Runtime-created GPU caches were
preserved. Factory was not invoked or required by the executable.

## Verification, limits and remaining work

Relevant repair suite: 63 passed in92.32 s, including three real Godot tests.
Scoped Ruff lint/format, mypy and diff whitespace checks passed. Subsequent
help-only checks: 12 tests passed in17.45 s with cacheprovider disabled;
scoped Ruff lint/format and mypy passed again. Earlier sandbox pytest-cache
warning did not fail tests. Accepted customer GDScript passed native parsing/type/runtime checks;
a separate GDScript linter is not configured. No versions, public schemas,
commands, adapters, ADRs or Factory capabilities were added.

M4 proposed request: one low-poly oak supply barrel with teal hoops for the
battle island. Meshy CLI0.4.0 verified the existing account; dry-run image route
has one textured image-to-3D request estimated30 credits, not an actual charge.
Automatic approval review rejected the new Codex `plan` invocation because the
M1-specific authorization did not cover it or its unknown fee. No process or
model call ran. One concrete plan-call approval question is pending. Concept,
paid Meshy generation and integration have not run; the one-submission cap stays.

OpenAI image/speech/vision, Meshy production, planner, other gameplay scenarios,
performance, assemblies, character/animation review, native editor/run,
release and operator process paths remain experimental unless narrow evidence
in integration status applies. Existing chest remains
`WF-REUSE-230ee7ad` / customer `0a0d834`; accepted assets still1.

Outside-project plan/manifest paths are addressed by documented copy-into-root
steps, with containment unchanged. The Oct18 removal-proposal heartbeat remains
`tide-bastion-kullan-m-kan-t-kald-rma-nerisi` (10:00 Europe/Istanbul).
Workflow-module split and historical-report archive plan remain deferred until
M1-M4; no archive, history removal or refactor was executed.

Initial unrelated `core/domain/__init__.py` status marker and untracked
`.verify-pytest/` and `data/` were left untouched and are excluded from task staging.
