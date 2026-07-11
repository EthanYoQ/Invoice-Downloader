# Task 9 Report: RunCoordinator and AppAPI Facade

## Status

Implementation complete pending independent review. Base was `fd7e5b7`. The
preserved `stash@{0}` was listed before and after implementation and was never
read, applied, popped, dropped, or rewritten.

## RED

The first Task 9 run produced `20 failed` because `run_coordinator`,
`run_state_store`, and `report_service` did not exist and AppAPI still owned the
whole mailbox-to-finalizer flow. Later RED cycles reproduced:

- live URL diagnostics disappearing from frontend snapshots;
- IMAP login and quota failures losing their established frontend status;
- a connect failure skipping disconnect finalization;
- coordinator dependency setup failure escaping before lifecycle release;
- report finalizer timeout and thread-start failure paths.

## Implementation

- `RunRequest` is frozen and contains only run dates, output path, rules, a
  hashed account identifier, and a channel identifier. It has no credential or
  API-key fields.
- `RunDependencies` is a process-only, non-dataclass container with a redacted
  representation. Scanner, candidate, extraction, archive, runtime ownership,
  callbacks, and secrets remain injected process objects.
- `RunCoordinator` consumes the existing atomic `RunLifecycle`/`RunHandle` and
  owns connect/scan, candidate, extraction, serialized archive, report,
  finalization, and terminal decisions. It does not recreate Task 4 state.
- Cancellation is checked before each stage side effect. Zero-mail runs skip
  candidate/extraction/archive while still running report, disconnect, cleanup,
  and one terminal transition.
- Any stage exception, quota/auth terminal outcome, incomplete ArchiveReport,
  report timeout, report-start failure, disconnect failure, or cleanup failure
  fails closed. A failed run cannot emit a completed terminal event.
- `ReportService` uses immutable run/output/staging context. The truth/report
  callback retains a hard timeout; disconnect and cleanup remain synchronous so
  Task 4's staging ownership cannot be released while late cleanup is running.
- `RunStateStore` lock-protects frontend status, logs, statistics, records, and
  categories; progress is monotonic, snapshots are deep independent copies,
  callback exceptions are isolated, and updates stop after terminal.
- AppAPI now validates and starts the worker as before, builds the request and
  process-only dependencies, delegates to `RunCoordinator.run`, and adapts
  deep frontend snapshots. Existing public signatures and return keys remain.
- Task 8 composition is split into a real extraction session with separate
  `extract()` and serialized `archive()` calls. No production whole-run
  pass-through returns to the removed worker.
- Dead simulation, mojibake pre-release workers, and obsolete finalizer wrapper
  copies were removed after a repository-wide caller check found no callers.
  `app_api.py` is net smaller than the Task 8 base.

## Verification

```text
Coordinator/lifecycle/pipeline/refactor focused: 108 passed
GLM/email/provider/URL/security/P2 focus: 326 passed, 33 subtests passed
Full pytest: 518 passed, 109 subtests passed
Ruff: run_coordinator.py, run_state_store.py, report_service.py, app_api.py,
      frontend_run_context.py, tests/test_run_coordinator.py passed
py_compile: Task 9 modules, AppAPI, frontend_run_context passed
git diff --check: passed
```

Pre-commit strict evidence:

```text
Output: tmp/strict_truth_audit_task9_precommit_20260711T191608Z.json
Truth: finalized, included 215, excluded 253, pending 0
Matched: 215/215
Artifacts: 1259
P0=0, P1=0, P2=0, manual=0
Exit: 0
```

## Files

- `run_coordinator.py`
- `run_state_store.py`
- `report_service.py`
- `app_api.py`
- `tests/test_run_coordinator.py`

`frontend_run_context.py` was reviewed but did not require a behavior change;
the immutable finalization context is deliberately defined in ReportService to
avoid coupling coordinator modules to frontend context loading.

## Remaining Gate

The strict check above re-audits the accepted pre-refactor output. It proves
the hardened audit still reports the finalized 215-row truth consistently, but
does not prove this coordinator code produced those artifacts. The fresh clean
mailbox run remains a later whole-project release gate and is not part of Task
9 acceptance.

## Post-Commit Evidence

```text
Code commit: fa28b28
Evidence report commit: abc9205
Coordinator/GLM/email/provider/URL/security/P2 focus: 327 passed, 33 subtests passed
Full pytest: 518 passed, 109 subtests passed
Ruff, py_compile, git diff --check: passed
Strict output: tmp/strict_truth_audit_abc9205_20260711T191850Z.json
Strict: finalized 215/215, artifacts 1259, P0/P1/P2/manual all zero
Worktree: clean
Stash: stash@{0} preserved and untouched
```
