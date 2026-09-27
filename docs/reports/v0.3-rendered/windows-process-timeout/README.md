# Windows ProcessRunner timeout diagnosis — 2026-09-27

Decision: **NOT CLOSED**. Historical timeout trigger remains unproven. No ProcessRunner, timeout budget, target test, supervisor, workflow, or capture change was made during this diagnosis. The existing Linux test correction is retained. Human review **PENDING**; no browser launch, commit, push, or release.

## Source and actual contract

Base commit: `8af661f9e2e407c6ec0ac22a9a9ed91f43d3de92`. Candidate is the uncommitted worktree; see `final-source-manifest.json` (raw and LF-normalized hashes). Snapshot manifests and each JSON record identify the actual imported source and interpreter. Source identity alone is not proof of correctness.

`CommandRequest.timeout_seconds=0.5` is the requested execution deadline. ProcessRunner starts it before Popen and uses the remaining time for parent wait and reader joins. It is neither the test's external watchdog nor a dedicated post-parent cleanup allowance. Safety cleanup can outlast the execution deadline. The test's `elapsed < 2.0` is a post-return assertion, not an interrupting watchdog.

The fixture starts a descendant inheriting PIPE handles and immediately requests parent exit. The descendant waits 3 seconds before writing a marker. Existing assertions require return within 2 seconds, marker absence after another 1 second, and `cleanup_completed is True`. There is no explicit parent exit-code assertion, stdout/stderr assertion, descendant READY handshake, or process-liveness assertion. The marker check can happen before the scheduled write even if cleanup failed; a passing result alone does not establish that the descendant reached Python execution. This is a coverage limitation, not a proven cause of the old timeout.

## Historical failure: phase established, trigger unknown

Preserved logs: `../linux-regression-fix/windows-final.txt` and `../linux-regression-fix/windows-candidate-isolated.txt` (the original single baseline/candidate comparison remains there).

Both exception chains enter `process_runner.py:490`, `proc.wait(timeout=remaining_proc_wait)`, then Windows `subprocess._wait` / `WaitForSingleObject` raises `subprocess.TimeoutExpired`. Remaining parent-wait budgets are approximately 0.4869975 and 0.4870784 seconds. Thus approximately 13 ms had elapsed before the wait; the timeout originated while awaiting the parent handle, before cleanup and reader completion. The domain TimeoutError at line 652 preserves that cause. The Popen repr's later `returncode=0` does not establish that the parent exited before the deadline. The test expects normal return, so this exception fails it.

Old logs lack monotonic stage records and READY evidence. They cannot distinguish the reason the parent wait exceeded its allocation. Current passes do not establish a fixture-budget defect, product defect, leaked test state, or environmental cause. No speculative budget increase or product fix was applied.

## Controlled experiments

All planned attempts and failures are retained; no pass-until-green loop was used. Each attempt uses a separate pytest process and basetemp. The paired snapshots use the same Windows Python 3.12.14 interpreter, dependencies, options, explicit source path, and disabled plugin autoload. JSON metadata asserts both gamefactory and process_runner imports belong to the selected source tree.

- `setup-comparison-*` and unprefixed `b1/c1/...` records: eight invalid harness attempts failed because the basetemp parent directory was missing. They are not product failures. After creating that directory, the complete predefined series was run.
- `comparison-*`, `ready-*`: three baseline and three candidate identity-only target runs all passed; one separately instrumented run per source also passed. The Linux test module was not imported in isolated attempts.
- `root-comparison-*`: four predefined actual-worktree runs passed, covering isolated versus normal collection and identity-only versus stage instrumentation, with normal plugin autoload.
- `sequence-*`: related module 26 passed / 1 skipped; full suite with instrumentation confined to the target 302 passed / 13 skipped. This checks collection and preceding-test execution without asserting that historical leakage is impossible.
- `final-normal.*`: final full normal suite **302 passed / 13 skipped**, exit 0, with no diagnostic plugin or tracing. Target passed. Existing platform/opt-in skips are recorded in the log; no skips were added.
- `ruff-check.txt`, `ruff-format.txt`, `mypy.txt`: all exit 0; 85 files formatted, Mypy 55 source files. Shared source did not change, so previously verified Linux results were preserved without repeating Linux execution in this diagnosis.

## Monotonic stage observations

Times below are milliseconds relative to each record's own runner `start_time`, not relative to the outer call or another process. Deadline is +500 ms. Raw records retain absolute perf_counter values and PIDs. Parent-exit-requested is a child checkpoint; parent-exit-observed is successful wait on the actual Popen handle. Those are distinct observations.

| Stage | Baseline `ready-b-stage` | Candidate `ready-c-stage` |
| --- | ---: | ---: |
| Popen returned | 11.96 | 10.26 |
| Parent exit observed | 74.94 | 81.65 |
| Job close began | 74.98 | 81.70 |
| Job close returned | 75.11 | 81.88 |
| stdout EOF | 75.72 | 82.77 |
| stderr EOF | 75.88 | 82.83 |
| Runner returned | 76.68 | 83.65 |

Both attached a real Windows Job Object handle, closed that handle, observed both readers at EOF with zero bytes, and returned exit code 0 / cleanup completed. The `_kill_tree` fallback was not invoked on these successful paths. Descendant READY was **not observed** in either trace; its checkpoint was absent. Consequently no descendant-ready timestamp or independently confirmed descendant terminal state is invented. Job-close completion and EOF are observed; they do not repair the target's readiness coverage gap.

The temporary plugin records in-memory monotonic events, wraps existing process/job/reader calls, traces only the runner frame, and adds small fixture checkpoint writes. It changes timing and fixture execution slightly. Instrumented successes cannot retroactively explain the old timeout. Identity-only pairs and the final plugin-free suite provide complementary evidence.

Diagnostic scripts are preserved under `diagnostics/`; plans/results contain executed argv. They expect repository-root execution and the separately archived baseline under `.verification/linux-regression-fix/baseline`. Do not replay into existing evidence paths: preserve these records and use fresh output/temp paths for any future experiment.

## CI and closure

No new committed/pushed revision was produced. Last verified remote source and run remain `8af661f` / `36305916243`; its engine successes do not accept this local candidate. After user review and commit/push, Ubuntu 3.11/3.12 (including both real thread-group regressions), Windows matrix, headless Godot, rendered capture/HUD mutation, and Linux rendered interruption must all be verified on the same new SHA. Historical Windows root cause remains explicitly unresolved; local passing runs are not a proven fix. Platform **NOT CLOSED**.
