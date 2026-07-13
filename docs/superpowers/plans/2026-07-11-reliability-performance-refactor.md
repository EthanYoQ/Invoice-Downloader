# Reliability and Performance Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox syntax for tracking.

**Goal:** Preserve the Windows desktop product while making P0/P1/P2 validation sound, moving core work into a modular monolith, and reducing the accepted clean-run wall clock by at least 30%.

**Architecture:** AppAPI remains a frontend compatibility facade over a new RunCoordinator. Mailbox scanning, extraction, archive/pairing, network recovery, state, and reports become focused modules with canonical domain types; strict truth validation remains an independent boundary.

**Tech Stack:** Python 3.11+, pywebview, imaplib, requests, tenacity, PyMuPDF, Pillow, pytest, unittest, Ruff, PyInstaller, PowerShell.

## Global Constraints

- The complete same-scope wall clock must be at most 2283.79 seconds versus the accepted 3262.55 second baseline.
- Final strict results must be P0=0, P1=0, P2=0, manual=0.
- Any nonzero P0, P1, P2, or manual count must make strict_truth_audit exit nonzero.
- QQ and 163 inclusion must use stable UIDs and local Asia/Shanghai date filtering; Date-header early stop is forbidden.
- Candidate discovery must retain subject, sender, body, direct attachment, HTML-link, and provider coverage.
- Pairing and truth matching must be one-to-one; ambiguity is a failure, never a guessed success.
- Hotel invoice/folio and ride invoice/itinerary artifacts must never deduplicate against each other.
- A run is terminal only after report finalization and cleanup complete.
- Email-derived URLs must be public HTTP(S) before connect, after DNS, and after every redirect.
- Model switches require local correctness and latency calibration; a newer model is not sufficient evidence.
- GLM concurrency is independently bounded per model, defaults to 2, and drops to 1 on HTTP 429 or business code 1302.
- Archive writes, deduplication, pairing, and final naming remain serialized and deterministic.
- Frontend behavior, product copy, archive layout, Excel output, packaging, and user settings remain compatible.
- API keys, authorization codes, mailbox data, truth data, and run artifacts must not be committed or packaged.
- Preserve the unrelated uid-imap-scan worktree exactly as found.

---

### Task 1: Make Strict Truth Audit a Sound Gate

**Files:**
- Create: tests/test_strict_truth_audit.py
- Modify: strict_truth_audit.py

**Interfaces:**
- Consumes: finalized manifest rows and load_artifacts output.
- Produces: assign_truth_matches(rows: list[dict], artifacts: list[dict], output_hashes: dict) -> dict[str, tuple[dict | None, str]].
- Produces: strict_exit_code(summary: dict) -> int.
- Preserves: main CLI arguments and JSON/Markdown report formats.

- [ ] **Step 1: Add failing one-to-one and exit-gate tests**

~~~python
def test_assignment_cannot_reuse_one_artifact_for_two_truth_rows():
    rows = [
        {"truth_id": "t1", "invoice_number": "12345678"},
        {"truth_id": "t2", "invoice_number": "12345678"},
    ]
    artifacts = [{"document_id": "a1", "invoice_number": "12345678", "path": "a.pdf"}]
    assigned = audit.assign_truth_matches(rows, artifacts, {})
    assert [assigned[key][0] is not None for key in ("t1", "t2")].count(True) == 1

@pytest.mark.parametrize("field", ["p0", "p1", "p2", "manual"])
def test_any_strict_failure_returns_nonzero(field):
    summary = {"p0": 0, "p1": 0, "p2": 0, "manual": 0}
    summary[field] = 1
    assert audit.strict_exit_code(summary) == 1
~~~

- [ ] **Step 2: Add failing ambiguous pair-inference tests**

Create two hotel invoices plus two folios with the same date/amount and no discriminating evidence, and the equivalent ride case. Assert that infer_required_hotel_pairs and infer_required_ride_pairs return an explicit ambiguity record whose status is ambiguous and whose truth IDs contain all four rows.

- [ ] **Step 3: Verify RED**

Run: python -m pytest tests/test_strict_truth_audit.py -q

Expected: fail because assignment is reusable, ambiguity is omitted, and strict_exit_code does not exist.

- [ ] **Step 4: Implement deterministic one-to-one assignment**

Build ranked candidate edges by the current match methods. Sort edges by:

~~~python
(
    method_rank,
    is_retention_artifact(artifact),
    str(artifact.get("path", "")),
    str(row.get("truth_id", "")),
)
~~~

Assign unique, unambiguous invoice-number and hash edges first. Resolve remaining constrained composite edges by maximum-cardinality matching with stable tie detection. Return unmatched for any row whose equally ranked alternatives can change membership. Never mutate input rows/artifacts.

- [ ] **Step 5: Make ambiguous pair groups explicit**

Return pair requirement dictionaries with:

~~~python
{
    "pair_key": str,
    "status": "required" | "ambiguous",
    "invoice_truth_ids": list[str],
    "companion_truth_ids": list[str],
    "reason": str,
}
~~~

Audit required groups for correct partner and adjacency; audit ambiguous groups as P2.

- [ ] **Step 6: Make all strict categories fail the CLI**

Normalize the generated summary to integer p0, p1, p2, and manual counts and return 1 when any count is nonzero. main must raise SystemExit(strict_exit_code(summary)).

- [ ] **Step 7: Verify GREEN and regressions**

Run: python -m pytest tests/test_strict_truth_audit.py tests/test_audit_email_truth.py tests/test_invoice_regression_p2.py -q

Expected: all pass.

- [ ] **Step 8: Commit**

~~~powershell
git add strict_truth_audit.py tests/test_strict_truth_audit.py
git commit -m "fix: make strict truth audit one-to-one"
~~~

### Task 2: Replace Greedy Pairing with Deterministic Assignment

**Files:**
- Create: pairing_engine.py
- Create: tests/test_pairing_engine.py
- Modify: archive_pairing.py
- Modify: app_api.py
- Modify: tests/test_invoice_regression_p2.py

**Interfaces:**
- Consumes: PairingDocument values adapted from existing archive dictionaries.
- Produces: pair_documents(family: str, invoices: Sequence[PairingDocument], companions: Sequence[PairingDocument]) -> PairingResult.
- Produces: PairingResult.pairs, unmatched_invoices, unmatched_companions, ambiguities.
- Preserves: match_ride_pairs and match_hotel_pairs wrappers for existing callers.

- [ ] **Step 1: Add failing provider-cross and ambiguity tests**

~~~python
def test_ride_pairing_never_crosses_known_providers():
    invoices = [doc("di", "ride_invoice", "100.00", provider="didi"),
                doc("ga", "ride_invoice", "100.00", provider="gaode")]
    companions = [doc("git", "ride_itinerary", "100.00", provider="gaode"),
                  doc("dit", "ride_itinerary", "100.00", provider="didi")]
    result = pair_documents("ride", invoices, companions)
    assert {(a.id, b.id) for a, b in result.pairs} == {("di", "dit"), ("ga", "git")}

def test_equal_score_membership_tie_is_not_guessed():
    result = pair_documents("hotel", two_identical_invoices(), two_identical_folios())
    assert result.pairs == ()
    assert len(result.ambiguities) == 1
~~~

Add tests for one-to-one use, deterministic ordering under shuffled input, 3% ride tolerance, hotel date window, and unmatched artifacts.

- [ ] **Step 2: Verify RED**

Run: python -m pytest tests/test_pairing_engine.py -q

Expected: fail because pairing_engine does not exist.

- [ ] **Step 3: Define immutable pairing types**

~~~python
@dataclass(frozen=True)
class PairingDocument:
    id: str
    role: str
    amount: Decimal | None
    business_date: date | None
    provider: str
    merchant_tokens: frozenset[str]
    source_message_uid: str
    path: str

@dataclass(frozen=True)
class PairingAmbiguity:
    document_ids: tuple[str, ...]
    reason: str

@dataclass(frozen=True)
class PairingResult:
    pairs: tuple[tuple[PairingDocument, PairingDocument], ...]
    unmatched_invoices: tuple[PairingDocument, ...]
    unmatched_companions: tuple[PairingDocument, ...]
    ambiguities: tuple[PairingAmbiguity, ...]
~~~

- [ ] **Step 4: Implement scored bipartite matching**

Reject incompatible family/provider/amount/date edges. Score provider, merchant tokens, source UID, exact amount, and date proximity. Enumerate connected components, compute maximum-cardinality maximum-score assignments for each small component, and return ambiguity when multiple optimal assignments change pair membership. Sort every output by stable document ID.

- [ ] **Step 5: Adapt legacy wrappers and archive adjacency**

Convert current dictionaries to PairingDocument. Keep existing public wrapper return shapes. Generate existing names only after a non-ambiguous pair result; order paired output by stable pair key then invoice/companion role. Record ambiguity/unmatched reasons in trace for strict audit.

- [ ] **Step 6: Verify GREEN and archive regressions**

Run: python -m pytest tests/test_pairing_engine.py tests/test_invoice_regression_p2.py tests/test_refactor_contracts.py -q

Expected: all pass.

- [ ] **Step 7: Commit**

~~~powershell
git add pairing_engine.py archive_pairing.py app_api.py tests/test_pairing_engine.py tests/test_invoice_regression_p2.py
git commit -m "fix: make archive pairing deterministic"
~~~

### Task 3: Enforce a Public URL Boundary

**Files:**
- Create: url_security.py
- Create: tests/test_url_security.py
- Modify: pdf_converter.py
- Modify: tests/test_provider_url_recovery.py

**Interfaces:**
- Produces: PublicUrlPolicy.validate(url: str) -> ValidatedPublicUrl.
- Produces: PublicUrlPolicy.resolve_redirect(current: ValidatedPublicUrl, location: str) -> ValidatedPublicUrl.
- Consumes: injectable resolver(host, port) -> Sequence[str] for deterministic tests.
- Preserves: PDFConverter public methods and provider adapters.

- [ ] **Step 1: Add failing address and redirect tests**

Parametrize rejection of localhost, 127.0.0.1, ::1, 10/8, 172.16/12, 192.168/16, 100.64/10, 169.254/16, fc00::/7, fe80::/10, multicast, unspecified, reserved, file URLs, credential-bearing URLs, and a public hostname that resolves to any private address. Assert a public HTTPS hostname is accepted. Assert a public first hop redirecting to a private destination is rejected.

- [ ] **Step 2: Verify RED**

Run: python -m pytest tests/test_url_security.py -q

Expected: fail because url_security does not exist.

- [ ] **Step 3: Implement canonical validation**

Use urllib.parse, ipaddress, and socket.getaddrinfo. Require scheme http or https, a nonempty hostname, no username/password, and an allowed port. Resolve all addresses and reject unless every result is globally routable. Return normalized URL, host, port, and immutable resolved-address tuple.

- [ ] **Step 4: Integrate with requests/browser recovery**

Validate before each requests call. Disable automatic redirects and validate every Location before the next request. Apply the same policy before handing a URL to browser automation. Preserve public provider behavior and produce a sanitized URL_POLICY_REJECTED trace reason without response-body or credential logging.

- [ ] **Step 5: Verify GREEN and recovery regressions**

Run: python -m pytest tests/test_url_security.py tests/test_provider_url_recovery.py -q

Expected: all pass, including 127.0.0.1 rejection.

- [ ] **Step 6: Commit**

~~~powershell
git add url_security.py pdf_converter.py tests/test_url_security.py tests/test_provider_url_recovery.py
git commit -m "fix: block private email URL recovery"
~~~

### Task 4: Make Run Finalization a Real Lifecycle Barrier

**Files:**
- Create: run_lifecycle.py
- Create: tests/test_run_lifecycle.py
- Modify: app_api.py
- Modify: frontend_run_context.py

**Interfaces:**
- Produces: RunLifecycle.begin(run_id: str, staging_dir: Path) -> RunHandle.
- Produces: RunHandle.advance(state: RunState), finalize(callbacks), fail(exc).
- Produces: RunLifecycle.can_begin -> bool.
- Preserves: AppAPI status dictionaries and frontend events.

- [ ] **Step 1: Add failing race tests**

Use threading.Event callbacks to hold cleanup. Assert a run remains finalizing while cleanup is blocked, AppAPI cannot start a second run, completed is not emitted early, callback exceptions produce failed plus a sanitized error, and the worker slot is released only after finalize returns.

- [ ] **Step 2: Verify RED**

Run: python -m pytest tests/test_run_lifecycle.py -q

Expected: fail because terminal completion currently precedes joined cleanup.

- [ ] **Step 3: Implement the state machine**

~~~python
class RunState(str, Enum):
    CREATED = "created"
    SCANNING = "scanning"
    RECOVERING = "recovering"
    EXTRACTING = "extracting"
    ARCHIVING = "archiving"
    REPORTING = "reporting"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"
~~~

Guard transitions with a Lock. finalize executes each registered callback synchronously in documented order, records failures, and enters one terminal state exactly once.

- [ ] **Step 4: Replace daemon finalizers**

Change _start_async_finalizers into an awaited finalization method or compatibility wrapper that joins its worker. Move _finish_run and worker clearing after the lifecycle terminal transition. Ensure staging ownership is per run.

- [ ] **Step 5: Verify GREEN and API contracts**

Run: python -m pytest tests/test_run_lifecycle.py tests/test_refactor_contracts.py -q

Expected: all pass.

- [ ] **Step 6: Commit**

~~~powershell
git add run_lifecycle.py app_api.py frontend_run_context.py tests/test_run_lifecycle.py
git commit -m "fix: await run cleanup before completion"
~~~

### Task 5: Introduce Canonical Domain Types and Rule Adapters

**Files:**
- Create: invoice_domain.py
- Create: tests/test_invoice_domain.py
- Modify: document_types.py
- Modify: company_rules.py
- Modify: archive_pairing.py
- Modify: invoice_extractor.py

**Interfaces:**
- Produces: DocumentIdentity, RouteInfo, InvoiceRecord, ArchivedArtifact.
- Produces: InvoiceRecord.from_legacy(dict, identity) and to_legacy() compatibility adapters.
- Produces: parse_amount(value) -> Decimal | None and parse_local_date(value) -> date | None.

- [ ] **Step 1: Add failing normalization and round-trip tests**

Assert Decimal parsing for commas, negatives, and invalid blanks; Asia/Shanghai date normalization; canonical document-type validation; preservation of invoice code/number; company classification parity for current fixtures; and legacy dict round trip for every key used by AppAPI.

- [ ] **Step 2: Verify RED**

Run: python -m pytest tests/test_invoice_domain.py -q

Expected: fail because invoice_domain does not exist.

- [ ] **Step 3: Implement frozen domain values**

Use dataclasses with frozen=True, Decimal for amount, datetime.date for invoice/travel dates, tuple/frozenset for collections, and DocumentType from document_types. Reject booleans as amounts and normalize legacy unknown markers to None only inside adapters.

- [ ] **Step 4: Centralize type and company rules**

Expose one canonical type normalization function and one company classification function. Existing helpers delegate to them. Do not change current category outputs; parity tests lock the behavior.

- [ ] **Step 5: Adopt types at pairing and extractor boundaries**

Convert extractor results once before archive/pairing and convert back only for existing AppAPI/trace payloads. Do not rewrite the orchestration in this task.

- [ ] **Step 6: Verify GREEN and regressions**

Run: python -m pytest tests/test_invoice_domain.py tests/test_invoice_regression_p2.py tests/test_email_body_receipts.py tests/test_refactor_contracts.py -q

Expected: all pass.

- [ ] **Step 7: Commit**

~~~powershell
git add invoice_domain.py document_types.py company_rules.py archive_pairing.py invoice_extractor.py tests/test_invoice_domain.py
git commit -m "refactor: unify invoice domain values"
~~~

### Task 6: Replace Server Date Search with Stable UID Batch Scanning

**Files:**
- Create: mailbox_scanner.py
- Create: tests/test_mailbox_scanner.py
- Modify: email_fetcher.py
- Modify: tests/test_email_fetcher_imap_filter.py

**Interfaces:**
- Produces: MailboxScanner.scan(since: date, before: date | None, mailbox: str = "INBOX") -> list[MessageRef].
- Produces: MessageRef(uid: bytes, message_date: datetime | None, internal_date: datetime | None).
- Consumes: an IMAP adapter supporting uid("SEARCH", None, "ALL") and uid("FETCH", uid_set, query).
- Preserves: EmailFetcher.fetch_emails_by_date return type list[bytes].

- [ ] **Step 1: Add failing UID correctness tests**

Test that SEARCH uses UID ALL, not SINCE/BEFORE; aware Date is converted to Asia/Shanghai before filtering; malformed/missing Date falls back to INTERNALDATE; unknown dates are retained; non-monotonic dates never early-stop; duplicate UID responses deduplicate stably; a failed batch is recursively split; and boundary before is exclusive.

- [ ] **Step 2: Add failing batched body-fetch tests**

Use a fake IMAP server with 205 retained UIDs. Assert header requests use bounded sets, full-body requests are batched, response sequence numbers do not replace UIDs, a malformed response retains/retries only its UID, and existing candidate extraction receives every retained message exactly once.

- [ ] **Step 3: Verify RED**

Run: python -m pytest tests/test_mailbox_scanner.py tests/test_email_fetcher_imap_filter.py -q

Expected: fail because current server search uses sequence IDs and SINCE/BEFORE.

- [ ] **Step 4: Implement MailboxScanner**

Default header batch size is 200 and full-message batch size is 25. Use uid commands exclusively after select. Parse response metadata for UID and INTERNALDATE, parse header Date, normalize aware values to ZoneInfo("Asia/Shanghai"), and retain unknown dates to prevent false negatives.

- [ ] **Step 5: Add divide-and-conquer retry**

For a failed multi-UID fetch, split the tuple in half and retry. For one failed UID, return an unknown/refetch marker and preserve it for individual full fetch. Cap retries using the existing retry policy and trace sanitized failures.

- [ ] **Step 6: Adapt EmailFetcher without changing candidate logic**

fetch_emails_by_date delegates to MailboxScanner and returns UIDs. extract_attachments detects UID mode and uses batched UID FETCH while passing each parsed message through the existing extraction path. Keep sender/body/attachment/link/provider rules byte-for-byte where practical.

- [ ] **Step 7: Verify GREEN and full unit suite**

Run: python -m pytest tests/test_mailbox_scanner.py tests/test_email_fetcher_imap_filter.py -q

Run: python -m pytest -q

Expected: all pass.

- [ ] **Step 8: Commit**

~~~powershell
git add mailbox_scanner.py email_fetcher.py tests/test_mailbox_scanner.py tests/test_email_fetcher_imap_filter.py
git commit -m "perf: batch mailbox scanning by stable UID"
~~~

### Task 7: Add GLM Profiles, Adaptive Concurrency, and Calibration

**Files:**
- Create: glm_runtime.py
- Create: model_calibration.py
- Create: tests/test_glm_runtime.py
- Create: tests/test_model_calibration.py
- Modify: invoice_extractor.py
- Modify: app_api.py
- Modify: user_settings.py

**Interfaces:**
- Produces: ModelProfile(name, endpoint, max_concurrency, timeout_seconds, fallback_name).
- Produces: AdaptiveConcurrencyLimiter.acquire(), record_success(), record_limit(error_code).
- Produces: GlmRuntime.request(profile_name, payload, parser) -> parsed result.
- Produces: compare_calibration(reference_path: Path, candidate_path: Path) -> CalibrationVerdict.
- Preserves: InvoiceExtractor.extract_info_via_llm and AppAPI.test_connection.

- [ ] **Step 1: Add failing limiter tests**

Assert a profile defaults to 2, no more than two requests enter concurrently, 429 and code 1302 reduce the affected profile to 1, another model remains at its own limit, successful probes restore only up to configured ceiling, retry delay includes bounded jitter, and logs never contain an API key.

- [ ] **Step 2: Add failing model-gate tests**

Create reference/candidate JSONL fixtures. Assert approval requires identical accepted artifact identities, P0/P1/P2/manual all zero, schema validity 100%, candidate p50 and p95 no worse than reference, and explicit entitlement success. Any missing metric rejects the candidate.

- [ ] **Step 3: Verify RED**

Run: python -m pytest tests/test_glm_runtime.py tests/test_model_calibration.py -q

Expected: fail because runtime and calibration modules do not exist.

- [ ] **Step 4: Implement profiles and sanitized error parsing**

Load profile names and ceilings from local settings with defaults:

~~~python
DEFAULT_PROFILES = {
    "ocr": ModelProfile("glm-ocr", LAYOUT_ENDPOINT, 2, 90, "vision_quality"),
    "text": ModelProfile("glm-4-flash", CHAT_ENDPOINT, 2, 60, "vision_quality"),
    "vision_quality": ModelProfile("glm-4.5v", CHAT_ENDPOINT, 2, 120, None),
}
~~~

These defaults preserve behavior. Candidate profiles such as glm-4.6v-flashx are local calibration inputs and do not become defaults in this step.

- [ ] **Step 5: Implement session reuse, limiter, and retry**

Use one requests.Session per runtime. Acquire a per-profile condition/semaphore, parse HTTP and GLM business codes, apply exponential backoff plus jitter, and always release capacity in finally. Expose sanitized timing and route metadata.

- [ ] **Step 6: Route InvoiceExtractor and connection test through GlmRuntime**

Keep local fast paths and Track A/Track B decisions unchanged. Replace direct requests.post calls with runtime.request. AppAPI.test_connection probes the configured text profile and returns its current product-compatible response.

- [ ] **Step 7: Implement calibration verdict and local CLI**

The CLI accepts paths and model names, never a key argument. It emits JSON with correctness counts, schema rate, p50/p95, entitlement result, and approved boolean. Exit zero only when approved.

- [ ] **Step 8: Verify GREEN and extraction regressions**

Run: python -m pytest tests/test_glm_runtime.py tests/test_model_calibration.py tests/test_email_body_receipts.py tests/test_invoice_regression_p2.py -q

Expected: all pass.

- [ ] **Step 9: Commit**

~~~powershell
git add glm_runtime.py model_calibration.py invoice_extractor.py app_api.py user_settings.py tests/test_glm_runtime.py tests/test_model_calibration.py
git commit -m "perf: add adaptive GLM runtime"
~~~

### Task 8: Split Candidate and Extraction Processing from AppAPI

**Files:**
- Create: candidate_pipeline.py
- Create: extraction_pipeline.py
- Create: archive_service.py
- Create: tests/test_processing_pipeline.py
- Modify: app_api.py

**Interfaces:**
- Produces: CandidatePipeline.collect(message_refs) -> list[DocumentCandidate].
- Produces: ExtractionPipeline.extract(candidates) -> list[ExtractionOutcome].
- Produces: ArchiveService.archive(outcomes, output_root) -> ArchiveReport.
- Consumes: InvoiceRecord, GlmRuntime, PublicUrlPolicy, pairing engine, and existing provider adapters.
- Preserves: trace events, progress counts, output names, retention behavior, and AppAPI frontend events.

- [ ] **Step 1: Add failing behavior-parity tests**

Build fakes for local parser, GLM runtime, provider recovery, archive writer, and trace sink. Assert local results bypass GLM, each candidate produces one terminal outcome, model/provider work may overlap, archive calls are serialized in original input order, exceptions become explicit retention/manual outcomes, and progress totals never decrease or exceed 100%.

- [ ] **Step 2: Verify RED**

Run: python -m pytest tests/test_processing_pipeline.py -q

Expected: fail because the pipeline modules do not exist.

- [ ] **Step 3: Extract CandidatePipeline**

Move candidate decision and artifact recovery orchestration from _run_processing_loop without changing rule order. Return immutable DocumentCandidate records containing source identity, path/URL, channel, and trace context.

- [ ] **Step 4: Implement bounded ExtractionPipeline**

Run deterministic local extraction first. Submit only unresolved model/provider work to a ThreadPoolExecutor whose upper bound is derived from active model/provider profile ceilings. Preserve candidate order by storing outcomes by sequence index. Do not perform archive writes in workers.

- [ ] **Step 5: Extract ArchiveService**

Perform canonical normalization, company classification, dedupe, naming, writes, pairing, adjacency renames, retention, and report events on one serialized path. Make each outcome idempotent by document identity.

- [ ] **Step 6: Replace the corresponding AppAPI loop slices**

AppAPI constructs dependencies, forwards progress/events, and delegates. Keep compatibility methods during migration. Delete only code proven unreachable by focused tests and CodeGraph blast-radius review.

- [ ] **Step 7: Verify GREEN, contracts, and complexity**

Run: python -m pytest tests/test_processing_pipeline.py tests/test_refactor_contracts.py tests/test_invoice_regression_p2.py -q

Run: python -m ruff check candidate_pipeline.py extraction_pipeline.py archive_service.py

Expected: all pass; no new module-level lint errors.

- [ ] **Step 8: Commit**

~~~powershell
git add candidate_pipeline.py extraction_pipeline.py archive_service.py app_api.py tests/test_processing_pipeline.py
git commit -m "refactor: extract document processing pipeline"
~~~

### Task 9: Introduce RunCoordinator and Thin the AppAPI Facade

**Files:**
- Create: run_coordinator.py
- Create: run_state_store.py
- Create: report_service.py
- Create: tests/test_run_coordinator.py
- Modify: app_api.py
- Modify: frontend_run_context.py

**Interfaces:**
- Produces: RunRequest, RunResult, RunCoordinator.run(request, callbacks) -> RunResult.
- Produces: RunStateStore snapshot/event methods.
- Produces: ReportService.finalize(run_result) -> ReportArtifacts.
- Preserves: AppAPI.start_processing, stop_processing, get_status, get_logs, get_statistics, open_folder, and connection/settings methods.

- [ ] **Step 1: Add failing coordinator integration tests**

With fake scanner/pipelines/services, assert lifecycle order, cancellation checks between stages, one active run, finalizers before completed, error-to-failed mapping, frontend-compatible status/log/statistics snapshots, and no dependency on pywebview inside coordinator modules.

- [ ] **Step 2: Verify RED**

Run: python -m pytest tests/test_run_coordinator.py -q

Expected: fail because run_coordinator does not exist.

- [ ] **Step 3: Implement RunRequest and dependency-injected coordinator**

RunRequest contains dates, save path, rules, and sanitized account/channel identifiers but no serialized credentials. Credentials remain in process-only dependency objects. run advances the lifecycle through scanner, candidate, extraction, archive, report, finalization, and terminal state.

- [ ] **Step 4: Extract state and report services**

RunStateStore owns lock-protected status/log/statistics snapshots. ReportService owns Excel and diagnostic finalization. Neither imports AppAPI or pywebview.

- [ ] **Step 5: Make AppAPI a facade**

Keep constructor and frontend-callable method signatures. AppAPI validates input, builds RunRequest/dependencies, starts one worker, forwards callbacks, and exposes snapshots. Remove migrated orchestration only after CodeGraph confirms no remaining caller.

- [ ] **Step 6: Verify GREEN and complete unit suite**

Run: python -m pytest tests/test_run_coordinator.py tests/test_run_lifecycle.py tests/test_processing_pipeline.py tests/test_refactor_contracts.py -q

Run: python -m pytest -q

Run: python -m ruff check run_coordinator.py run_state_store.py report_service.py

Expected: all pass.

- [ ] **Step 7: Commit**

~~~powershell
git add run_coordinator.py run_state_store.py report_service.py app_api.py frontend_run_context.py tests/test_run_coordinator.py
git commit -m "refactor: move runs behind coordinator"
~~~

### Task 10: Decouple Truth Construction and Add Reproducible Batch Evidence

**Files:**
- Create: truth_contracts.py
- Create: batch_validation.py
- Create: tests/test_truth_contracts.py
- Create: tests/test_batch_validation.py
- Modify: build_truth_dataset.py
- Modify: strict_truth_audit.py

**Interfaces:**
- Produces: TruthRow and TruthManifest schema validation independent of runtime extractors.
- Produces: BatchValidator.validate(manifest, run_root) -> BatchValidationResult.
- Produces: compare_performance(baseline_json, candidate_json) -> PerformanceVerdict.
- Preserves: existing truth manifest field names and strict audit CLI.

- [ ] **Step 1: Add failing independence tests**

Use an import guard to assert truth_contracts and strict truth matching do not import invoice_extractor, app_api, candidate_pipeline, archive_service, or private runtime helpers. Validate required identity, inclusion/exclusion decision, role/type, dates, amount, seller, category, and pair evidence.

- [ ] **Step 2: Add failing batch gate tests**

Assert BatchValidator rejects non-finalized/pending truth, any P0/P1/P2/manual count, duplicate artifact assignment, missing timing boundaries, and source/run scope mismatch. Assert 2283.79 seconds passes against 3262.55 and 2283.80 fails.

- [ ] **Step 3: Verify RED**

Run: python -m pytest tests/test_truth_contracts.py tests/test_batch_validation.py -q

Expected: fail because independent contracts and validator do not exist.

- [ ] **Step 4: Implement truth schema boundary**

Parse manifests into frozen TruthRow values using only standard-library normalization owned by truth_contracts. Build_truth_dataset may use mailbox evidence gathering but must not call runtime private candidate/classifier functions to decide expected truth.

- [ ] **Step 5: Implement batch validator and performance verdict**

Require finalized true, pending count zero, exact date/account-channel scope, strict audit exit zero, all four counts zero, unique assignments, and complete start/end timestamps. Emit a machine-readable result with baseline seconds, candidate seconds, speedup fraction, threshold, and pass.

- [ ] **Step 6: Add a credential-free command entry**

The command receives manifest and run-root paths, writes reports inside the run diagnostics directory, and prints only aggregate counts/paths. It never accepts or prints mailbox credentials or API keys.

- [ ] **Step 7: Verify GREEN and audit regressions**

Run: python -m pytest tests/test_truth_contracts.py tests/test_batch_validation.py tests/test_strict_truth_audit.py -q

Expected: all pass.

- [ ] **Step 8: Commit**

~~~powershell
git add truth_contracts.py batch_validation.py build_truth_dataset.py strict_truth_audit.py tests/test_truth_contracts.py tests/test_batch_validation.py
git commit -m "test: make batch truth validation independent"
~~~

### Task 11: Complete Static, Local, Real-Mailbox, and Performance Gates

**Files:**
- Create: docs/validation/2026-07-11-refactor-validation.md
- Modify only when a failing gate identifies a root cause: source/tests owned by Tasks 1-10
- Local untracked evidence: test_dataset and manual_program_runs

**Interfaces:**
- Consumes: finalized local truth manifest, clean baseline run, local settings, and current branch.
- Produces: reproducible commands, sanitized result paths, strict counts, timings, model profile, and final pass/fail.

- [ ] **Step 1: Run tracked-tree hygiene and secret checks**

Run:

~~~powershell
git status --short
git diff --check
git grep -n -I -E "(personal_mailbox_identifier|Authorization: Bearer|auth_code[ ]*=|api_key[ ]*=)"
~~~

Expected: only intended tracked changes; no credential/personal mailbox disclosure.

- [ ] **Step 2: Run complete unit and regression suites**

Run:

~~~powershell
python -m pytest -q
python -m ruff check .
python -m compileall -q .
~~~

Expected: zero failures and zero static errors. Exclude ignored local evidence/build caches from Ruff using configuration committed only if required.

- [ ] **Step 3: Validate the existing accepted run with the hardened audit**

Run strict_truth_audit and batch_validation against the finalized local manifest and accepted baseline run. Expected: the hardened mechanism produces an unambiguous one-to-one result. If it exposes a real prior mismatch, stop performance comparison, use systematic debugging, fix the product or truth evidence, and rerun.

- [ ] **Step 4: Execute one clean same-scope real QQ batch**

Create a fresh run directory, preserve local settings, clear only the run-owned staging/output/state artifacts, and run the exact 2025-11-25 through 2026-06-14 scope. Do not clean ignored evidence globally. Capture start/end/stage/model/provider timings.

- [ ] **Step 5: Apply the strict gate**

Run batch_validation against the new run. Required result:

~~~json
{"p0": 0, "p1": 0, "p2": 0, "manual": 0, "unique_assignments": true}
~~~

If it fails, invoke superpowers:systematic-debugging, identify the earliest divergent stage, add a failing regression test, implement the minimal fix, rerun focused and complete tests, then execute another clean real batch. Stop after the first fully clean run or after five real-batch cycles; five failed cycles is a reported blocker, never a success.

- [ ] **Step 6: Apply the speed gate**

Compare complete wall clock to 3262.55 seconds. Required candidate time is at most 2283.79 seconds. If correctness passes but speed fails, use stage timings to optimize the dominant proven wait without widening model limits beyond the adaptive profile or reducing scan coverage, then repeat focused tests and clean batch.

- [ ] **Step 7: Calibrate model candidates only if needed**

Probe entitlement without logging the key and compare current profiles with candidate supported profiles on the representative local fallback corpus. Change defaults only if model_calibration exits zero. Otherwise retain the current proven defaults.

- [ ] **Step 8: Verify Windows packaging smoke compatibility**

Run the existing release build preflight or PyInstaller smoke command without publishing. Start the built executable, verify pywebview loads, settings remain masked, maximize/minimize/close work, and no runtime dependency error appears. Do not modify UI copy or layout.

- [ ] **Step 9: Write the validation record**

Record backup branch, implementation commits, exact commands, test counts, strict counts, run paths, timings, speedup, calibrated models/limits, packaging smoke result, and any residual risk. Use aggregate identifiers only.

- [ ] **Step 10: Final whole-branch review and commit**

Dispatch the Superpowers final code reviewer over merge-base to HEAD. Fix every Critical/Important finding with focused tests and re-review. Then:

~~~powershell
git add docs/validation/2026-07-11-refactor-validation.md
git commit -m "docs: record refactor validation"
~~~

Do not merge, push, or publish until the final reviewer and every gate pass.
