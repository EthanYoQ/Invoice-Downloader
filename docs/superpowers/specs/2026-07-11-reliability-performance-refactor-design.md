# Invoice-Downloader Reliability and Performance Refactor

**Date:** 2026-07-11
**Status:** Approved
**Architecture:** Modular monolith
**Priority:** Correctness first; performance only after P0/P1/P2 invariants hold

## 1. Objective

Refactor the existing Windows pywebview application without changing its product surface, archive rules, pairing semantics, or supported document flow.

The refactor is complete only when all of the following are demonstrated on the same clean QQ mailbox range and the finalized local truth manifest:

- P0 missing artifacts: 0
- P1 classification, naming, or field errors: 0
- P2 pairing or adjacency errors: 0
- manual-review outcomes: 0
- unit, regression, static, and strict-truth checks: pass
- complete wall-clock duration: at most 2283.79 seconds, a 30% reduction from the recorded 3262.55 second baseline

No performance optimization may weaken candidate discovery, date filtering, attachment recovery, classification, duplicate handling, or audit coverage.

## 2. Baseline Evidence

The accepted pre-refactor long-range run is:

manual_program_runs/refactor_range2_20251125_20260614_20260624_231421

| Metric | Value |
| --- | ---: |
| Complete elapsed time | 3262.55 s |
| Mail scan and candidate extraction | about 1267 s |
| Document processing and archive | about 1989 s |
| Emails | 352 |
| Candidates | 1259 |
| Included truth rows | 215 |
| Existing strict-audit result | P0=0, P1=0, P2=0, manual=0 |

The finalized local truth source is:

test_dataset/qq_20251125_20260614_rebuilt_20260614_1035/truth_manifest.json

It contains 215 included and 253 excluded rows with no pending rows. Truth data, mailbox evidence, credentials, and run outputs remain local and must not be committed or packaged.

## 3. Non-Negotiable Invariants

1. QQ and 163 server-side date search is not authoritative. Use stable UIDs and apply the requested date window locally.
2. Do not stop a UID scan based on non-monotonic message Date headers.
3. Continue candidate discovery across subject, sender, body, direct attachments, HTML links, and supported provider flows.
4. Hotel invoices and folios, and ride invoices and itineraries, are independent artifacts and are never deduplicated against one another.
5. Archive categories and naming remain compatible with the frontend, Excel output, and current user workflow.
6. Pairing is deterministic and bijective. Ambiguity becomes explicit P2 evidence rather than a guessed pair.
7. Strict truth matching is one-to-one. A produced artifact cannot satisfy two truth rows.
8. Any P0, P1, P2, or unresolved manual item makes strict audit exit nonzero.
9. Run completion means finalization and cleanup have finished. A new run cannot overlap prior staging cleanup.
10. Network recovery accepts only approved public HTTP(S) destinations, checked before connection, after DNS, and after redirects.
11. Keys, authorization codes, personal mailbox data, truth evidence, and test artifacts never enter tracked source or release archives.

## 4. Target Architecture

The frontend contract remains attached to AppAPI; business orchestration moves behind that compatibility facade.

~~~text
pywebview frontend
      |
      v
AppAPI compatibility facade
      |
      v
RunCoordinator
  |-- MailboxScanner
  |     |-- ImapSession
  |     `-- MessageBatchReader
  |-- CandidatePipeline
  |     |-- AttachmentExtractor
  |     `-- LinkCandidateClassifier
  |-- ArtifactRecovery
  |     |-- PublicUrlPolicy
  |     `-- provider adapters / browser fallback
  |-- ExtractionPipeline
  |     |-- local deterministic extractors
  |     |-- GlmClient
  |     `-- AdaptiveConcurrencyLimiter
  |-- ArchiveService
  |     |-- ClassificationPolicy
  |     |-- NamingPolicy
  |     `-- PairingEngine
  |-- RunStateStore
  `-- ReportService

Independent validation boundary
  |-- TruthDatasetBuilder
  `-- StrictTruthAudit
~~~

This remains one Python desktop process and one Windows package. It does not introduce microservices, Electron, .NET, a database server, or a new frontend framework.

## 5. Core Domain Model

Canonical immutable types replace ad-hoc dictionaries at module boundaries while compatibility adapters preserve existing serialized keys.

~~~python
@dataclass(frozen=True)
class DocumentIdentity:
    source_message_uid: str
    source_kind: str
    source_name: str
    content_sha256: str | None

@dataclass(frozen=True)
class InvoiceRecord:
    identity: DocumentIdentity
    document_type: DocumentType
    category: str
    invoice_date: date | None
    purchaser: str
    seller: str
    amount: Decimal | None
    invoice_code: str
    invoice_number: str
    route: RouteInfo | None
~~~

Amounts use Decimal, dates use timezone-aware parsing followed by a canonical local calendar date, document types come from document_types, and company classification comes from company_rules. Legacy dictionaries remain only at compatibility boundaries.

## 6. Mailbox Scan and Fetch

MailboxScanner performs:

1. authenticate and select the mailbox;
2. issue UID SEARCH ALL;
3. fetch headers in bounded UID batches;
4. normalize each message date to Asia/Shanghai;
5. retain the complete requested local date range;
6. fetch full bodies for retained UIDs in bounded batches;
7. run existing candidate discovery for every retained message;
8. trace UID, normalized date, candidate count, and exclusion reason.

A failed batch is subdivided and retried so one malformed message cannot discard adjacent messages. Ordering cannot change inclusion.

## 7. Extraction and GLM Strategy

### 7.1 Local-first routing

Deterministic XML, embedded PDF text, email-body, provider, folio, itinerary, and known-layout parsers run before a model request. Models process only documents whose local evidence cannot produce a complete canonical record.

### 7.2 Account-aware concurrency

GLM concurrency is account- and model-dependent, so the application must not hard-code a universal maximum. Each model gets an independent adaptive limiter:

- conservative default concurrency of 2;
- optional validated local override;
- HTTP 429 or business code 1302 reduces that model to one active request and retries with exponential backoff plus jitter;
- overload codes such as 1305/1312 back off and use only a previously calibrated fallback;
- successful sustained requests can cautiously restore the configured ceiling;
- provider-browser concurrency is independent;
- archive, dedupe, and pairing remain serialized.

Sessions are reused. Timings, attempts, model names, routes, and sanitized error codes are traceable; keys and payload contents are not logged.

### 7.3 Model selection gate

The current code references glm-ocr, glm-4.5v, and glm-4-flash. A replacement becomes default only after a representative local fallback corpus proves:

- identical P0/P1/P2 outcomes;
- no new manual-review records;
- schema-valid structured output for every accepted response;
- lower or equal p50 and p95 latency for the replaced route;
- successful entitlement probe without printing the key.

Candidate direction:

- retain glm-ocr where calibrated;
- evaluate glm-4.6v-flashx for fast vision fallback and glm-4.6v for quality fallback;
- evaluate a currently supported Flash/FlashX text model for structured extraction;
- do not select a model merely because it is newer or larger.

If calibration is inconclusive, preserve the proven model route and obtain the speed target from UID batching, local fast paths, session reuse, and bounded parallelism.

Official references:

- https://docs.bigmodel.cn/cn/api/rate-limit
- https://docs.bigmodel.cn/cn/guide/start/model-overview
- https://docs.bigmodel.cn/cn/guide/models/vlm/glm-ocr
- https://docs.bigmodel.cn/cn/guide/models/vlm/glm-4.6v
- https://docs.bigmodel.cn/cn/guide/capabilities/struct-output
- https://docs.bigmodel.cn/cn/guide/develop/http/introduction

## 8. Deterministic Archive and Pairing

Pairing uses a compatibility-scored bipartite graph rather than greedy amount-only matching. Hard constraints include pair family, provider or brand when known, normalized amount, business-date window, seller/hotel identity, and source-message evidence.

The engine computes a deterministic maximum-cardinality, maximum-score one-to-one assignment. A top-score tie that changes membership is ambiguous and fails as P2. Required missing companions, reused files, wrong partners, and non-adjacent archive names also fail as P2.

## 9. Strict Truth Audit

StrictTruthAudit is independent of runtime candidate and classification decisions. Matching proceeds through unique invoice identity, artifact fingerprint, constrained composite identity, and deterministic global one-to-one assignment. Unresolved or ambiguous rows fail explicitly.

- P0: an included truth artifact has no produced match.
- P1: a matched artifact has wrong classification, naming, fields, company status, duplicate behavior, or placement.
- P2: a required pair is absent, wrong, non-adjacent, reused, or ambiguous.
- manual: evidence cannot be resolved automatically; this also fails the gate.

Reports contain row-to-path assignments and duplicate/reuse detection. The CLI exits nonzero for any nonzero category.

## 10. Run Lifecycle

Each run owns a unique staging directory and explicit lifecycle:

~~~text
created -> scanning -> recovering -> extracting -> archiving
        -> reporting -> finalizing -> completed
                               -> failed
~~~

Cleanup and report finalization are awaited before a terminal transition and before releasing the worker slot. Cleanup failures are retried or surfaced, never hidden in an unjoined daemon thread.

## 11. Security Boundary

PublicUrlPolicy rejects loopback, link-local, private, carrier-grade NAT, multicast, reserved, unspecified, and non-HTTP(S) IPv4/IPv6 targets. Every hostname result and redirect target is validated. Provider allowlists may narrow public hosts but cannot permit private resolved addresses.

## 12. Migration Sequence

1. Harden strict audit.
2. Replace greedy pairing and expose ambiguity.
3. add URL policy.
4. make finalization synchronous and test run exclusion.
5. introduce canonical domain types and adapters.
6. extract UID/batch mailbox scanning.
7. extract model client, adaptive limiters, and calibration harness.
8. move orchestration from AppAPI into RunCoordinator in behavior-preserving slices.
9. decouple truth construction from runtime private helpers.
10. run complete verification, clean mailbox batch, strict audit, and performance comparison.

Each slice is committed only after focused tests pass. A failed slice is reverted without discarding earlier verified work.

## 13. Verification and Rollback

Required evidence includes pre-change failing tests, unit/regression/static output, strict-audit report and exit status, clean-run timings, sanitized model/provider metrics, artifact inventory, one-to-one truth assignment, and a same-scope performance comparison.

Rollback points:

- backup/pre-reliability-refactor-20260711
- codex/reliability-refactor-20260711
- one verified commit per migration slice

The release branch is not updated until the complete gate passes.
