# Targeted Linux regression verification

Baseline: `8af661f9e2e407c6ec0ac22a9a9ed91f43d3de92`, CI run `36305916243`.
This directory records local, uncommitted candidate verification. It does not establish a new remote SHA or CI closure.

## Root-cause evidence collected before candidate validation

The original inline worker fixture was extracted with Python AST from a `git archive` of the baseline. `probe_fixture.py` executes it without importing or invoking the supervisor, then performs one controlled comparison with only the manual `PyEval_SaveThread` sequence removed. The original fixture crashes with SIGSEGV (exit -11) on both Linux Python 3.11.16 and 3.12.14; faulthandler identifies the main-thread `save()` call. Only the leader zombie remains, and the worker heartbeat does not advance. Removing the manual call leaves the leader in Z and a live worker in the same thread group, advancing its heartbeat after leader exit; controlled stop exits with code 0. See `fixture-ab-311.json` and `fixture-ab-312.json`.

The defect is the fixture's unsafe interpreter transition, not a failure of pidfd to observe a living thread group. `PyEval_SaveThread` releases the GIL and detaches the thread state; returning to Python through that call is unsafe. `ctypes.CDLL` already releases the GIL around the native syscall. References: [Python thread-state API](https://docs.python.org/3.12/c-api/init.html#thread-state-and-the-global-interpreter-lock), [ctypes library loading](https://docs.python.org/3.12/library/ctypes.html#loading-shared-libraries).

The original two pytest cases were also run once per Linux version, without retries:

- Python 3.11.16: 1 failed (worker), 1 passed (timing). `baseline-311.txt` / `.xml`.
- Python 3.12.14: 2 failed (worker and timing). `baseline-312.txt` / `.xml`.

The timing assertion compared different intervals. The caller creates an absolute monotonic deadline before entering `wait_for_owned_thread_group`; the helper starts elapsed measurement after resolving its probe/poll functions. Each loop checks the current monotonic time against the same absolute deadline. The final observation is timestamped after the timeout decision. The real poll converts seconds to integer milliseconds, so it can return early, but the next loop rechecks the absolute deadline. The baseline does not return early in the controlled demonstration: `clock-contract-baseline.json` shows deadline creation at 0, helper entry at 0.015625, exact deadline/return at 0.0625, and honest internal elapsed 0.046875. Early empty polls progressively reduce remaining time. No reported elapsed value was clamped, rounded, or given an arbitrary epsilon.

## Reproduction layout

Baseline source was extracted into `.verification/linux-regression-fix/baseline/` using `git archive` for `scripts`, `src`, `tests`, `pyproject.toml`, `README.md`, and `examples`. Diagnostics were executed from `.verification/linux-regression-fix/`. Copies of their scripts are retained here for inspection. Linux containers mount the repository read-only at `/repo` and this evidence directory at `/evidence`; their kernel is recorded in the JSON results. Containers use official `python:3.11-slim` and `python:3.12-slim` images. These are real Linux runs under Docker/WSL2, not GitHub Ubuntu runner jobs.

Candidate validation uses a fixed five-run series per version for the two target names, followed by the full normal suite, Ruff lint/format, and Mypy. `validate_linux.py` records every command and exit code, along with hashes of the copied candidate source. No automatic flaky retries are used.

## Final results and limits

Both Linux versions passed all five two-test runs without skips, and each full normal suite returned 309 passed / 6 skipped. Both actual Linux thread-group cases passed in each full suite. Ruff lint/format and Mypy passed on both versions. The source manifests in `candidate-final-311-summary.json` and `candidate-final-312-summary.json` were independently compared to the final worktree, including every copied source file; they match.

Windows 3.12.14 full suite returned **1 failed, 301 passed, 13 skipped** (`windows-final.txt` / `.xml`). The failure is the unchanged `TestProcessRunner::test_parent_exit_descendant_bounded_and_cleaned`, which raised `TimeoutError` after 0.5 seconds. A bounded diagnostic comparison ran that case once on the archived baseline (passed) and once on the candidate (failed); both results are retained. The process-runner and its test source match baseline. No cause such as scheduler load or flakiness is claimed, and no unrelated implementation change was made. The full Windows failure has not been erased or replaced by a retry result.

Windows Ruff checks passed. The first expanded Mypy invocation explicitly naming both the script and importing test produced a duplicate-module-name error (`windows-mypy.txt`). The corrected invocation used `MYPYPATH=src` and `--explicit-package-bases`, with no source changes, and passed all 55 source files (`windows-mypy-explicit.txt`).

See `validation-summary.json` for the audit, normalized candidate test hash, and baseline-matching file hashes. The remote still has only baseline SHA `8af661f9e2e407c6ec0ac22a9a9ed91f43d3de92` / run `36305916243`. New-SHA Windows matrix and headless/rendered engine jobs are pending the user's commit/push. Old successful engine jobs are not relabeled as candidate results. Platform closure is NOT CLOSED, human visual review PENDING, and no browser load or approval was performed.
