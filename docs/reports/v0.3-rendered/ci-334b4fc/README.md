# V0.3 final candidate CI acceptance — CLOSED

Tested source revision: `334b4fc368ab880d0352728c04548562fe5c6ffe`. User push confirmed by live remote main ref; CI run [36310782274](https://github.com/akifsen/ai-game-factory/actions/runs/36310782274) completed successfully. Platform **CLOSED**; human review **PENDING**.

All 109 sources in `../descendant-candidate/final-source-manifest.json` match this commit, the unchanged local sources, and the CI headless source manifest using the recorded LF-normalized SHA-256 hashes. The READY handshake and retained Windows handle/Linux pidfd descendant test are included; test-file LF SHA-256 is `114d4065b3d7d74f6cc6fd273ca271f2cccd06c7d92736a0f9d66106439aec4c`. Old SHA `80d1271` / run `36309156428` is not this acceptance.

| Required job | Job ID | Result |
| --- | --- | --- |
| godot-real | 108596147399 | PASS: headless acceptance, recovery and live crash |
| test (windows-latest, 3.11) | 108596147502 | 2026-09-27T09:55:43.9616835Z ================= 310 passed, 5 skipped in 114.45s (0:01:54) ==================; lint/format/mypy PASS; descendant PASSED, not skipped |
| test (ubuntu-latest, 3.11) | 108596147519 | 2026-09-27T09:53:53.1363813Z ======================= 309 passed, 6 skipped in 28.95s ========================; lint/format/mypy PASS; descendant PASSED, not skipped |
| test (windows-latest, 3.12) | 108596147521 | 2026-09-27T09:55:43.4191998Z ================= 310 passed, 5 skipped in 109.74s (0:01:49) ==================; lint/format/mypy PASS; descendant PASSED, not skipped |
| test (ubuntu-latest, 3.12) | 108596147589 | 2026-09-27T09:53:54.4950125Z ======================= 309 passed, 6 skipped in 28.25s ========================; lint/format/mypy PASS; descendant PASSED, not skipped |
| godot-rendered | 108596147603 | PASS: rendered capture, HUD mutation and rendered interruption |

Both engine artifacts identify the exact tested SHA in `commit.txt`. Rendered landscape and portrait report X11 / OpenGL Compatibility / Mesa llvmpipe. HUD mutation correctly yields state PASS, visual FAIL, workflow FAILED; this is expected rejection, not a CI failure. Headless and rendered interruptions record a live pidfd before the factory kill and final child termination/cleanup. The unrelated sentinel survives recovery.

Evidence:

- `remote-ref.json`, `run.json`, `run.log`, `artifacts.json`: retrieved GitHub metadata and actual logs.
- `source-verification.json`: per-file manifest/commit/local LF hashes, plus CI manifest comparison.
- `descendant-results.txt`: four actual PASSED lines with original run-log line numbers.
- `verification-summary.json`: decision, job IDs, outcomes and engine evidence paths.
- `artifacts/godot-real-evidence/reports/`: acceptance, recovery, live-crash JSON; adjacent logs and source/executable/wheel identity.
- `artifacts/godot-rendered-evidence/`: acceptance and rendered-interrupt JSON; `publish/landscape/` and `publish/portrait/` hold HTML, PNGs and render contexts.
- `evidence-sha256.json`: local file integrity index for this downloaded and derived evidence package.

The historical 0.5-second timeout remains **UNRESOLVED, not FIXED**. Its triggering root cause is unknown; green CI does not establish a fix. Human review remains **PENDING**; pipeline-test approval is not human approval. No browser page was loaded in this session.

No implementation, workflow or test source was changed. No new test scope, local test run, CI rerun, commit, push, release or V0.4 work was performed. These closure records and downloaded evidence were added after the tested source revision and do not redefine it. Future report-only commits must be distinguished from the accepted source SHA; they do not require an endless commit-test loop. Earlier candidate and completion sections are retained as historical records.
