# V0.3 descendant candidate verification — 2026-09-27

Platform **NOT CLOSED**. Human review **PENDING**. The final candidate is a test-only worktree delta on `80d127182e2d13e3592bbf3bd17737fb32dfeb6c`; it has no committed/pushed SHA yet. No commit, push, release, CI rerun, browser load, or human approval was performed in this task.

## Evidence gap and narrow change

No equivalent existing test established the complete sequence of normal parent exit, a genuinely started pipe-inheriting descendant, bounded runner return, and independently observed descendant termination. `test_timeout_kills_descendant_process` exercises timeout, not normal parent exit. `test_posix_parent_exit_kills_descendant_ignoring_sigterm` uses readiness plus delayed marker absence, without an independent identity/handle termination observation. The live-Godot interruption tests exercise a different parent interruption scenario.

Only `tests/integration/test_process_runner.py` changed in test/product implementation scope. The existing `TestProcessRunner::test_parent_exit_descendant_bounded_and_cleaned` now uses atomic descendant READY publication after a flushed pipe write. A bounded coordinator opens a Windows process handle or Linux pidfd, verifies that identity is live, and releases the parent. The parent requires READY and release to exit zero. Descendant stdout/stderr remain inherited. The test asserts normal exit, no timeout, pipe output, cleanup metadata, and independently observes the retained identity becoming terminal before the absolute two-second bound. The descendant's natural sleep is three seconds, so natural completion cannot satisfy that bound. Marker absence remains supplementary.

The request remains **0.5 seconds**. There is no new skip, xfail, removed assertion, or product/ProcessRunner change. Handle acquisition fails closed; there is no bare-PID liveness fallback. Failure cleanup signals the retained handle/pidfd, checks bounded termination, and closes the handle after a bounded coordinator join. The prior Linux worker/deadline correction is unchanged: LF SHA-256 `9cfd426a6afa5a95e19c216c6be5860300399fe07cbe778eb855781c25edaad0`.

## Verification provenance

The initial delegated implementation was rejected for a PID fallback, successful parent exit on handshake expiry, and a coordinator cleanup race. Its reported five-run repetition was unnecessary and is not final-candidate evidence. The revision was independently inspected. A final integration correction moved READY publication after stdout flush to remove a test-only output race. Final verification uses `validate_candidate.py`, without diagnostic plugins or automatic retries.

The first Team Lead Windows harness attempt failed at collection (`ModuleNotFoundError: gamefactory`); its target/full XML, logs, and summaries are retained as `initial-setup-windows-312-*`. A local `pip install --no-deps --no-build-isolation -e .` then failed because `setuptools.build_meta` was unavailable. `pip install -e '.[dev]'` installed the editable project successfully; see `windows-environment-install.txt`. These are environment preparation failures, not executed test failures and not evidence about the historical timeout trigger. Source was unchanged between these setup attempts and final validation.

Final validation results and source comparisons are recorded in the per-platform summaries and `final-source-manifest.json`. Local Linux containers use the existing official `python:3.11-slim` and `python:3.12-slim` images, a read-only repository mount, and isolated copied source; they are not Ubuntu CI jobs.

The first Linux 3.11 harness attempt omitted the parent `.verification` directory for `--basetemp`, so tmp-path fixture setup raised `FileNotFoundError`: module 1 passed / 2 skipped / 24 errors; full suite 131 passed / 5 skipped / 179 errors. Logs/XML/summaries remain as `initial-setup-linux-311-*`. The harness now creates that directory before invoking pytest. No test/product source changed to address this setup failure. Windows already had that parent directory and its successful final run was not repeated.

## Final local results

| Final source validation | Module | Full normal suite | Ruff lint / format / Mypy |
| --- | --- | --- | --- |
| Windows Python 3.12.14 | 26 passed, 1 skipped | 302 passed, 13 skipped | all passed |
| Linux Python 3.11 | 25 passed, 2 skipped | 309 passed, 6 skipped | all passed |
| Linux Python 3.12 | 25 passed, 2 skipped | 309 passed, 6 skipped | all passed |

Exact descendant test ID: `tests/integration/test_process_runner.py::TestProcessRunner::test_parent_exit_descendant_bounded_and_cleaned`. It **passed, without skip**, in both module and full-suite runs on all three environments. Its JUnit testcase and timing are bound in `final-source-manifest.json`; the full test duration includes the retained supplementary one-second marker wait, not just runner execution. Existing environment/opt-in skips are listed in the suite logs and do not substitute for actual Godot acceptance.

The final manifest hashes 109 tracked source/configuration/example files, with raw and LF-normalized SHA-256. All three validation snapshots match the final tracked source. Generated `src/gamefactory.egg-info` installation metadata is explicitly excluded from source comparison and listed separately. Actual changed tracked files are `tests/integration/test_process_runner.py` and `docs/reports/v0.3-completion-report.md`; the new `docs/reports/v0.3-rendered/descendant-candidate/` directory contains this report, validation scripts, logs, XML, summaries, prior-CI records, and manifest. No other tracked implementation file changed.

## Remote revision boundary

[CI run 36309156428](https://github.com/akifsen/ai-game-factory/actions/runs/36309156428) tested `80d127182e2d13e3592bbf3bd17737fb32dfeb6c`. Read-only job metadata and actual logs are retained in `previous-ci-80d1271.json` and `.log`. All four normal-suite/static-check jobs succeeded: Ubuntu 3.11 and 3.12 each 309 passed / 6 skipped; Windows 3.11 and 3.12 each 310 passed / 5 skipped. Real headless acceptance and recovery/live-crash steps passed; the rendered capture and interruption steps passed, with the interrupted child's final state `terminated`.

That run predates this strengthened descendant test and is **not acceptance of the final candidate**. Neither `8af661f` / `36305916243` nor reruns of any prior SHA can close this candidate. After the user's normal commit/push, compare the pushed source against this manifest (LF hashes account for checkout line endings), then verify on that exact new SHA: Ubuntu 3.11/3.12 normal suite and static checks; Windows 3.11/3.12 normal suite and static checks; real headless `godot-real`; real `godot-rendered`, HUD mutation, and rendered interruption. Final candidate CI remains **PENDING**.

## Historical timeout diagnosis — unresolved, not FIXED

The failing phase is known: `proc.wait` exhausted the remaining approximately 0.487 seconds of the 0.5-second execution deadline before cleanup/reader waiting. The triggering root cause is unknown. The timeout did not reproduce in the last controlled baseline/candidate runs. New green CI does not prove that the historical cause was fixed. Preserve the original failures and [diagnosis](../windows-process-timeout/README.md); do not classify the cause as a product or environment defect without evidence. This coverage improvement does not explain that historical failure.

Platform closure remains **NOT CLOSED** until the final candidate's required same-SHA jobs and acceptance evidence are complete. Human visual review may remain PENDING and has not been approved on the user's behalf.
