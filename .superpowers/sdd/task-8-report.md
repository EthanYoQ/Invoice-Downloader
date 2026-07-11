# Task 8 Report: Candidate, Extraction, and Archive Pipeline Boundaries

## Status

PASS pending independent review. Base was `a75a5d9`. The preserved
`stash@{0}` was listed to verify its continued presence and was never read,
applied, popped, dropped, or rewritten.

## RED

The first focused run failed during collection because `archive_service`,
`candidate_pipeline`, and `extraction_pipeline` did not exist. Subsequent RED
cycles proved the following defects before their fixes:

- no batch compatibility delegate and no AppAPI pipeline boundary;
- nested candidate metadata remained mutable through the source mapping;
- duplicate sequence values overwrote an earlier terminal result;
- a trace sink exception hid all later terminal outcomes;
- URL/provider recovery candidates were treated as parallel-safe by default.

## GREEN

- `DocumentCandidate`, `ExtractionOutcome`, `ArchivedOutcome`, and
  `ArchiveReport` are frozen values. Candidate metadata is recursively frozen
  and thawed back to its legacy shape only at the compatibility boundary.
- Candidate collection preserves input count and order, creates the same
  legacy MD5 trace identity seed, and carries source locator, channel, message
  UID, provider group, sequence, and trace context separately.
- Local deterministic results bypass remote work. Only unresolved candidates
  marked parallel-safe enter the executor; URL, provider, and browser recovery
  remain serial by default.
- Worker count is bounded to the requested and verified ceilings and hard-capped
  at the currently validated value of two. Pending workers re-check stop before
  any remote side effect.
- Every input position receives exactly one explicit terminal outcome. Timeout,
  auth, quota, cancellation, exception, identity mismatch, and missing outcome
  paths are deterministic and sanitized.
- Worker completion order never affects returned or archived order. Trace and
  progress callbacks run after ordered outcome assembly, percentages are stable
  and monotonic, and callback failures cannot hide candidate outcomes.
- `ArchiveService` is single-threaded, identity-idempotent, ordered, and fails
  closed through `ArchiveReport.can_complete` for unresolved/manual outcomes.
- AppAPI now delegates through all three boundaries. Safe local documents are
  rendered serially first, then unresolved GLM extraction actually overlaps at
  the verified shared-runtime ceiling (currently at most two). URL, provider,
  browser, retain-only, and manual candidates stay on the legacy serial path.
- Each worker owns an isolated InvoiceExtractor mutable trace state but shares
  the run-owned GlmRuntime without owning/closing it. Worker instances always
  close; constructor, extraction, and close failures are sanitized, retained by
  the legacy path, and fail the run closed.
- Prepared images/results live in an in-memory sidecar keyed by DocumentIdentity,
  not attachment metadata, traces, events, or persisted diagnostics. The batch
  delegate restores exact original dictionaries and invokes naming, retention,
  pairing, CWT, trace, progress, and frontend events once in original order.

## Thread-Safety Audit

- GLM concurrency remains enforced by the shared run-owned `GlmRuntime`; the
  pipeline does not create runtimes, sessions, or per-candidate limiters.
- `InvoiceExtractor.last_extraction_trace`, `last_route_trace`, and processed
  record state are not shared by pipeline workers in the compatibility path.
- Provider adapters and browser recovery default to serial execution.
- Archive writes, dedupe/history mutation, naming, pairing, adjacency renames,
  retention, and frontend/trace side effects remain on the calling thread.
- Stop is checked before local work, before submission, and again inside each
  pending worker. Executor context exit deterministically joins running work.

## Verification

```text
Task 8 focused: 33 passed
Lifecycle/GLM/email/provider/URL/P2 focus: 288 passed, 33 subtests passed
Full pytest: 433 passed, 109 subtests passed
py_compile on Task 8 modules, AppAPI, InvoiceExtractor, and tests: passed
Ruff on Task 8 modules/tests: passed
git diff --check: passed
```

## Files

- `candidate_pipeline.py`
- `extraction_pipeline.py`
- `archive_service.py`
- `app_api.py`
- `tests/test_processing_pipeline.py`

## Concerns

- The production delegate intentionally keeps the established all-at-once
  archive/pairing implementation intact. Task 9 may move that private legacy
  implementation behind the coordinator only after equivalent integration
  coverage exists.
- Running model requests are not force-killed by Python futures. Stop prevents
  queued remote side effects, while an already admitted request finishes under
  the GLM runtime timeout/close contract before executor shutdown completes.

## Post-Commit Strict Evidence

```text
HEAD: f5eda19
Timestamp (UTC): 20260711T165927Z
Output: tmp/strict_truth_audit_f5eda19_20260711T165927Z.json
Exit: 0
Truth: finalized, included 215, excluded 253, pending 0
Matched: 215/215
Artifacts: 1259
P0=0, P1=0, P2=0, manual=0
```

## Structural Review Remediation

The rejected pre-review implementation was remediated in place after
`5c8814e` without reading or modifying `stash@{0}`.

- `CandidatePipeline` is the sole producer of canonical SHA256 document
  identity. Email aliases, attachment content/part identity, and normalized
  URL digests are included; incoming legacy IDs are trace-only evidence.
- `CandidatePreflight` now performs serial current/history dedupe by canonical
  identity, candidate qualification, URL recovery, every deterministic local
  probe, and image preparation before remote submission.
- `InvoiceExtractor` exposes explicit local-only and remote-only entry points.
  Duplicate candidates make zero model calls and each eligible unresolved
  candidate invokes the model path exactly once.
- `ExtractionPipeline` uses incremental bounded scheduling with at most two
  active futures, propagates quota/auth breakers to every unscheduled
  candidate, sanitizes submit/worker failures, and preserves one terminal
  outcome per candidate.
- `ArchiveService` directly owns ordered serialized archive decisions through
  `AppArchiveAdapter`; the whole-batch delegate and the 2,249-line legacy loop
  were removed. AppAPI now contains a 130-line dependency composition method.
- Pairing checks both target names before moving either artifact, updates trace
  archive targets and combine results, and fails closed on ambiguity,
  collision, unresolved, retained, or manual outcomes.
- Outcome payload and trace context are recursively frozen. Progress, trace,
  and event callbacks are isolated, and the prepared-image sidecar is cleared
  in `finally`.

Pre-commit remediation evidence:

```text
Focused reviewer suite: 279 passed, 20 subtests passed
Full pytest: 446 passed, 109 subtests passed
Task 8 Ruff: passed
py_compile: passed
git diff --check: passed
Strict output: tmp/strict_truth_audit_task8_remediation_precommit_20260711T180522Z.json
Strict: finalized 215/215, artifacts 1259, P0/P1/P2/manual all zero
```

Post-commit evidence is recorded after the remediation commit and fresh
verification below.

```text
Remediation commit: fefd0828e53a5421162e54ff6a99da5dee1401a1
Final test-adaptation commit: 867d7095953d2f18678901e83c273b7783d8d1da
Full pytest: 446 passed, 109 subtests passed
Focused processing/URL reviewer probes: 54 passed
py_compile (AppAPI, Task 8 modules, InvoiceExtractor, tests): passed
Task 8 Ruff (new modules and focused tests): passed
git diff --check 5c8814e..HEAD: passed
Strict output: tmp/strict_truth_audit_867d709_20260711T180929Z.json
Truth: finalized, included 215, excluded 253, pending 0
Matched: 215/215
Artifacts: 1259
P0=0, P1=0, P2=0, manual=0
```

## Pre-Review Hardening

An implementer-side adversarial pass after `f5eda19` added four RED
regressions and closed them before independent review:

- outcome payloads are now recursively immutable and explicitly thawed only at
  the legacy boundary;
- real `GlmRequestError` HTTP 401/403 and 402 failures map to auth and quota
  terminal outcomes;
- archive event-sink exceptions cannot hide the report or later outcomes;
- batch delegation deep-thaws payloads before restoring legacy dictionaries.

Fresh verification after these changes: `26 passed` focused,
`426 passed, 109 subtests passed` full, Task 8 Ruff passed, py_compile passed,
and `git diff --check` passed.

Fresh post-remediation strict evidence:

```text
HEAD: c58daf4
Timestamp (UTC): 20260711T170503Z
Output: tmp/strict_truth_audit_c58daf4_20260711T170503Z.json
Exit: 0
Truth: finalized, included 215, excluded 253, pending 0
Matched: 215/215
Artifacts: 1259
P0=0, P1=0, P2=0, manual=0
```

## Production Parallel-Path Remediation

Commit `9f1a02a` replaces the initial pass-through production adapter with the
real bounded extraction path. Additional RED tests covered actual overlap two,
serial URL/provider/retain bypass, local conversion retention, shared-runtime
ownership, sidecar consumption, worker-constructor failure, and worker-close
failure containment.

```text
HEAD: 9f1a02a
Full pytest: 433 passed, 109 subtests passed
Task 8 focused: 33 passed
Ruff on Task 8 modules/tests: passed
py_compile: passed
git diff --check: passed
Strict output: tmp/strict_truth_audit_9f1a02a_20260711T171528Z.json
Strict: finalized 215/215, artifacts 1259, P0/P1/P2/manual all zero
```

## Stable Identity and Malformed-Input Remediation

The final pre-review RED cycle identified two root causes:

- `DocumentIdentity` incorrectly included sequence, so reordering the same
  mailbox candidates changed identity and weakened archive idempotency;
- non-mapping or pathless candidates lacked a legacy `filepath`, allowing a
  `KeyError` before retention/manual handling.

Stable identity now derives from source UID/locator/filename/provider/subject
and tier, while the exact historical sequence-based MD5 is retained separately
as `legacy_document_id` for trace and prepared-sidecar compatibility. Malformed
inputs become explicit `manual_review` rows with reason
`MALFORMED_DOCUMENT_CANDIDATE`.

Fresh verification: `35 passed` focused, `435 passed, 109 subtests passed`
full, Task 8 Ruff passed, py_compile passed, and `git diff --check` passed.

```text
HEAD: 5c8814e
Strict output: tmp/strict_truth_audit_5c8814e_20260711T171755Z.json
Strict exit: 0
Truth: finalized, included 215, excluded 253, pending 0
Matched: 215/215
Artifacts: 1259
P0=0, P1=0, P2=0, manual=0
```

## Final Structural Remediation Evidence

```text
Remediation commit: fefd0828e53a5421162e54ff6a99da5dee1401a1
Final HEAD: 867d7095953d2f18678901e83c273b7783d8d1da
Full pytest: 446 passed, 109 subtests passed
Focused processing/URL reviewer probes: 54 passed
py_compile: passed
Task 8 Ruff: passed
git diff --check 5c8814e..HEAD: passed
Strict output: tmp/strict_truth_audit_867d709_20260711T180929Z.json
Truth: finalized, included 215, excluded 253, pending 0
Matched: 215/215, artifacts 1259
P0=0, P1=0, P2=0, manual=0
Worktree: clean
Stash: stash@{0} preserved and untouched
```

## Reviewer Acceptance, Supporting-Document, History, and Pairing Remediation

The follow-up review findings were addressed on top of `867d709` without
touching `stash@{0}` or starting Task 9.

- `DocumentAcceptanceService` is now an explicit post-extraction,
  pre-archive boundary that delegates the exact legacy acceptance matrix.
  Rejections retain provider diagnostics, never route to archive, and remain
  fail-closed even when the extracted payload also says `is_invoice=False`.
- Archive normalization now classifies CWT and registered supporting document
  types before applying the ordinary non-invoice filter. Existing exempt
  travel, hotel, flight, and service-fee documents continue to archive.
- CWT classification uses the canonical classifier with the original payload,
  filename, and local fast-path evidence. Cancellation registration and final
  matching retain the legacy ordering and reason codes.
- Every candidate carries canonical identity plus the exact compatibility
  history key. Preflight checks both, successful runs persist both, and direct
  candidate construction cannot create a shared empty compatibility key.
- `AppArchiveAdapter` restores legacy frontend success, retention, manual,
  category, stats, and stage-event shapes without restoring the removed giant
  loop.
- Pairing inputs are built from archived outcome metadata and trace evidence.
  Matched, unmatched, and ambiguous results are traced; required counterparts
  fail closed only when the canonical grouping indicates that a counterpart
  is available.

TDD and pre-commit evidence:

```text
Acceptance precedence RED: provider rejection was overwritten by model_rejected
Acceptance precedence GREEN: 1 passed; reviewer suite 38 passed
Empty compatibility-key RED: direct candidate key was empty
Empty compatibility-key GREEN: 1 passed; reviewer suite 39 passed
Reviewer/pipeline/URL probes: 93 passed
Focused Task 8/refactor/P2/lifecycle/GLM/provider/email/security: 343 passed, 33 subtests passed
Full pytest: 485 passed, 109 subtests passed
py_compile: passed
Task 8 Ruff: passed
git diff --check: passed
Strict output: tmp/strict_truth_audit_task8_review_precommit_20260711T184244Z.json
Strict: finalized 215/215, artifacts 1259, P0/P1/P2/manual all zero
```

The strict result above re-audits an older accepted output directory. It does
not prove the new acceptance and archive path against a fresh mailbox run. A
real clean batch on the current code remains the final release gate.
