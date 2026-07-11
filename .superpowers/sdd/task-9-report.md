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

## Independent Review Remediation

Task 9 remained unapproved after four Important findings. They were repaired
with new RED tests without starting Task 10 or touching `stash@{0}`.

### Atomic Facade Admission

- AppAPI now owns one admission lock covering the final active-run check,
  terminal-handle retirement, new `RunLifecycle.begin`, run-directory capture,
  worker ownership assignment, and `Thread.start`.
- `_prepare_run_lifecycle` rejects every existing handle; it never returns or
  reuses one. The reserved handle is passed explicitly to the worker and
  coordinator. `RunCoordinator.run` no longer has a staging-dir path and
  rejects calls without a reserved handle.
- Dependency-build and thread-start failures finalize and release that exact
  handle once. A barrier test with two real caller threads proves one worker
  starts and the second caller deterministically receives the busy response.

### Single Terminal State Owner

- `RunStateStore.terminalize` atomically commits terminal state, status, error,
  reason, log entries, statistics, legacy-state synchronization, and one event.
- AppAPI legacy fields are synchronized by the store's state adapter. The
  coordinator computes the established IMAP, quota, auth, mailbox, startup,
  processing, cancellation, and success presentation before terminalizing.
- The worker no longer appends logs or calls `_finish_run` after coordinator
  return. Missing-credential rejection remains an idle pre-admission response.
- Tests assert exact legacy/frontend parity for success, cancellation, IMAP,
  quota, processing, report-start, and finalizer failures.

### Independent Resource Close

- Pipeline close is an idempotent synchronous finalizer owned by the
  coordinator, not part of the report callback.
- Finalizer order is `pipeline_close -> report -> disconnect -> cleanup ->
  terminal`. A close failure is aggregated and fails closed while every later
  finalizer still runs.
- A forced report-thread start failure proves the pipeline runtime and mailbox
  session both close exactly once and cleanup still executes.

### PII Boundary

- Runtime connection logs use irreversible account hash plus channel label.
  EmailFetcher progress replaces the exact account, authorization code, and API
  key before state/log persistence.
- Mail-auth errors no longer echo raw exception text. Packaged diagnostics keep
  domain-only summaries and hashed exception/status data.
- An end-to-end admission/worker test seeds a distinctive full address, auth
  code, and API key and proves they are absent from legacy state, frontend
  snapshots, store snapshots, events, diagnostics, lifecycle errors, and reprs,
  while the useful QQ channel remains visible.
- Source scan result: `NO_FULL_EMAIL_RENDER_PATHS`.

### Remediation Verification

```text
Coordinator/lifecycle/pipeline/refactor plus GLM/email/provider/URL/P2:
  337 passed, 33 subtests passed
Full pytest: 528 passed, 109 subtests passed
Ruff, py_compile, git diff --check: passed
Strict output: tmp/strict_truth_audit_task9_reviewfix_precommit_20260711T194001Z.json
Strict: finalized 215/215, artifacts 1259, P0/P1/P2/manual all zero
```

Post-remediation-commit evidence:

```text
Commit: 66cc1f1
Focused: 337 passed, 33 subtests passed
Full pytest: 528 passed, 109 subtests passed
Ruff, py_compile, git diff --check: passed
Strict output: tmp/strict_truth_audit_66cc1f1_20260711T194111Z.json
Strict: finalized 215/215, artifacts 1259, P0/P1/P2/manual all zero
Worktree: clean
Stash: stash@{0} preserved and untouched
```

## Final Critical Admission Remediation

The final review found that pre-admission validation was request-local but its
derived dates, paths, active config, and settings persistence still mutated the
AppAPI facade before the admission lock. Two concurrent callers could therefore
start only one worker while the rejected caller overwrote the accepted run's
configuration.

- Added a deterministic two-party barrier test with distinct A/B dates, paths,
  rules, account credentials, settings, and diagnostics. It runs with A as the
  forced winner and again with B as the forced winner.
- Validation now produces a frozen, credential-free admission candidate plus a
  process-only redacted secret container without mutating AppAPI state.
- The single admission lock now owns the active-run recheck, terminal-handle
  retirement, new handle and run-directory reservation, winner-only settings
  persistence, exact `RunRequest` construction, dependency construction,
  diagnostics, truth-audit startup, worker ownership, and `Thread.start`.
- The worker receives only the exact frozen `RunRequest`, reserved `RunHandle`,
  and process-only `RunDependencies`; it cannot reread facade effective dates,
  requested output, or active config for run behavior.
- Settings-load, dependency-build, truth-audit-start, and thread-start failures
  finalize the same reserved handle once, release the lifecycle, sanitize the
  terminal failure, and restore prior compatibility config. A dedicated RED
  test covers settings-load failure before any settings write.
- The rejected request writes no settings or diagnostics, creates no output
  directory, owns no staging directory, and changes no compatibility fields.

Pre-commit verification:

```text
Coordinator/lifecycle: 68 passed
Coordinator/lifecycle/pipeline/refactor plus GLM/email/provider/security:
  317 passed, 20 subtests passed
Full pytest: 531 passed, 109 subtests passed
Ruff, py_compile, git diff --check: passed
Strict output: tmp/strict_truth_audit_task9_finalcritical_precommit_20260712T063021Z.json
Strict: finalized 215/215, artifacts 1259, P0/P1/P2/manual all zero
Stash: stash@{0} preserved and untouched
```

Post-commit final Critical evidence:

```text
Code/report commit: 92e7e4c
Coordinator/lifecycle: 68 passed
Full pytest: 531 passed, 109 subtests passed
Ruff, py_compile, git diff --check: passed
Strict output: tmp/strict_truth_audit_92e7e4c_20260712T063206Z.json
Strict: finalized 215/215, pending 0, artifacts 1259,
        P0/P1/P2/manual all zero
Stash: stash@{0} preserved and untouched
```
