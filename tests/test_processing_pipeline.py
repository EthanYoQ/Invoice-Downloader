from __future__ import annotations

from collections import Counter
from concurrent.futures import Future
from pathlib import Path
import threading
import time

import pytest

from archive_service import ArchivedOutcome, ArchiveReport, ArchiveService
from app_api import _ProcessingPipelineSession
from candidate_pipeline import (
    CandidatePipeline,
    DocumentCandidate,
    partition_redundant_provider_candidates,
)
from extraction_pipeline import ExtractionOutcome, ExtractionPipeline
from glm_runtime import GlmRequestError


def _candidate(sequence: int, name: str | None = None, **metadata) -> DocumentCandidate:
    filename = name or f"invoice-{sequence}.pdf"
    return CandidatePipeline().collect(
        [
            {
                "filepath": f"C:/staging/{filename}",
                "subject": f"mail-{sequence}",
                "message_uid": f"uid-{sequence}",
                **metadata,
            }
        ],
        sequence_offset=sequence,
    )[0]


def test_candidate_collection_is_frozen_stable_and_preserves_legacy_order():
    source = [
        {"filepath": "C:/staging/a.pdf", "message_uid": "11", "tier": 1},
        {"filepath": "C:/staging/b.pdf", "message_uid": "12", "tier": 2},
    ]

    first = CandidatePipeline(channel="qq").collect(source)
    second = CandidatePipeline(channel="qq").collect(source)

    assert [item.sequence for item in first] == [0, 1]
    assert [item.identity for item in first] == [item.identity for item in second]
    assert [item.source_path for item in first] == [row["filepath"] for row in source]
    assert [item.to_legacy()["tier"] for item in first] == [1, 2]
    with pytest.raises((AttributeError, TypeError)):
        first[0].source_path = "changed"  # type: ignore[misc]


def test_candidate_nested_metadata_cannot_be_mutated_and_thaws_for_legacy():
    source = [{"filepath": "C:/staging/a.pdf", "nested": {"values": [1, 2]}}]
    candidate = CandidatePipeline().collect(source)[0]

    source[0]["nested"]["values"].append(3)

    assert candidate.to_legacy()["nested"] == {"values": [1, 2]}
    with pytest.raises((AttributeError, TypeError)):
        candidate.metadata["nested"]["values"] += (3,)  # type: ignore[index,operator]


def test_candidate_document_identity_is_stable_when_input_order_changes():
    rows = [
        {"filepath": "C:/staging/a.pdf", "message_uid": "11"},
        {"filepath": "C:/staging/b.pdf", "message_uid": "12"},
    ]

    forward = CandidatePipeline().collect(rows)
    reverse = CandidatePipeline().collect(list(reversed(rows)))

    forward_ids = {candidate.source_path: candidate.identity.document_id for candidate in forward}
    reverse_ids = {candidate.source_path: candidate.identity.document_id for candidate in reverse}
    assert forward_ids == reverse_ids
    assert [candidate.sequence for candidate in reverse] == [0, 1]


def test_canonical_identity_uses_email_alias_and_attachment_content_digest(tmp_path: Path):
    first = tmp_path / "same.pdf"
    second_dir = tmp_path / "other"
    second_dir.mkdir()
    second = second_dir / "same.pdf"
    first.write_bytes(b"first-content")
    second.write_bytes(b"second-content")

    candidates = CandidatePipeline().collect(
        [
            {"filepath": str(first), "email_id": "mail-1"},
            {"filepath": str(second), "source_email_id": "mail-1"},
        ]
    )

    assert candidates[0].identity.source_message_uid == "mail-1"
    assert candidates[1].identity.source_message_uid == "mail-1"
    assert candidates[0].identity.document_id != candidates[1].identity.document_id
    assert all(len(candidate.identity.document_id) == 64 for candidate in candidates)


def test_canonical_url_identity_is_scoped_by_email_without_storing_raw_url():
    raw_url = "HTTPS://Example.COM/path/invoice?id=secret#fragment"
    candidates = CandidatePipeline().collect(
        [
            {"filepath": raw_url, "source_url": raw_url, "email_id": "mail-1", "is_url": True},
            {"filepath": raw_url, "source_url": raw_url, "email_id": "mail-2", "is_url": True},
        ]
    )

    assert candidates[0].identity.document_id != candidates[1].identity.document_id
    assert raw_url not in candidates[0].identity.source_locator
    assert candidates[0].identity.source_locator.startswith("url_sha256:")
    assert "secret" not in candidates[0].source_filename
    assert raw_url not in repr(candidates[0].trace_context)


def test_candidate_pipeline_never_uses_incoming_legacy_document_id_for_lookup(tmp_path: Path):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"canonical-content")

    candidate = CandidatePipeline().collect(
        [{"filepath": str(source), "email_id": "mail-1", "document_id": "legacy-md5"}]
    )[0]

    assert candidate.identity.document_id != "legacy-md5"
    assert len(candidate.identity.document_id) == 64
    assert candidate.trace_context["legacy_document_id"] != candidate.identity.document_id


def test_malformed_candidate_is_retained_as_explicit_manual_legacy_row():
    candidates = CandidatePipeline().collect([None, {"subject": "missing path"}])

    assert len(candidates) == 2
    for candidate in candidates:
        legacy = candidate.to_legacy()
        assert legacy["filepath"] == ""
        assert legacy["candidate_action"] == "manual_review"
        assert legacy["prefilter_reason_code"] == "MALFORMED_DOCUMENT_CANDIDATE"


def test_local_result_bypasses_remote_runtime():
    remote_calls: list[str] = []
    candidate = _candidate(0)
    pipeline = ExtractionPipeline(
        local_parser=lambda item: {"source": item.source_path},
        remote_extractor=lambda item: remote_calls.append(item.source_path),
        max_workers=2,
    )

    outcomes = pipeline.extract([candidate])

    assert remote_calls == []
    assert outcomes[0].status == "resolved"
    assert outcomes[0].payload == {"source": candidate.source_path}


def test_extraction_outcome_payload_is_deeply_immutable_and_can_be_thawed():
    source = {"nested": {"values": [1, 2]}}
    outcome = ExtractionOutcome.resolved(_candidate(0), source)
    source["nested"]["values"].append(3)

    assert outcome.to_legacy_payload() == {"nested": {"values": [1, 2]}}
    with pytest.raises((AttributeError, TypeError)):
        outcome.payload["nested"]["values"] += (3,)  # type: ignore[index,operator]


def test_extraction_outcome_trace_context_is_deeply_immutable():
    trace = {"nested": {"events": ["start"]}}
    outcome = ExtractionOutcome(
        candidate=_candidate(0),
        status="resolved",
        payload={},
        trace_context=trace,
    )
    trace["nested"]["events"].append("late")

    assert outcome.to_legacy_trace_context() == {"nested": {"events": ["start"]}}
    with pytest.raises((AttributeError, TypeError)):
        outcome.trace_context["nested"]["events"] += ("late",)  # type: ignore[index,operator]


def test_extraction_outcome_inherits_candidate_legacy_trace_shape_by_default():
    base = _candidate(0)
    candidate = DocumentCandidate(
        identity=base.identity,
        sequence=0,
        trace_context={"legacy_document_id": "trace-only"},
    )

    outcome = ExtractionOutcome.resolved(candidate, {"ok": True})

    assert outcome.to_legacy_trace_context() == {"legacy_document_id": "trace-only"}


def test_unresolved_safe_remote_work_overlaps_but_never_exceeds_verified_two():
    lock = threading.Lock()
    active = 0
    maximum = 0
    barrier = threading.Barrier(2)

    def remote(candidate):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            barrier.wait(timeout=2)
            time.sleep(0.02)
            return {"sequence": candidate.sequence}
        finally:
            with lock:
                active -= 1

    pipeline = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=remote,
        max_workers=8,
        verified_ceiling=lambda: 2,
    )

    outcomes = pipeline.extract([_candidate(index) for index in range(4)])

    assert maximum == 2
    assert [outcome.payload["sequence"] for outcome in outcomes] == [0, 1, 2, 3]


def test_unsafe_provider_and_browser_candidates_stay_serial():
    active = 0
    maximum = 0
    lock = threading.Lock()

    def remote(_candidate):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.01)
        with lock:
            active -= 1
        return {"ok": True}

    candidates = [
        _candidate(0, parallel_safe=False, provider_family="baiwang"),
        _candidate(1, parallel_safe=False, browser_recovery=True),
    ]
    outcomes = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=remote,
        max_workers=2,
    ).extract(candidates)

    assert maximum == 1
    assert [outcome.status for outcome in outcomes] == ["resolved", "resolved"]


def test_url_and_provider_recovery_candidates_are_unsafe_by_default():
    candidates = CandidatePipeline().collect(
        [
            {"filepath": "https://example.com/invoice", "is_url": True},
            {"filepath": "C:/staging/provider.pdf", "provider_family": "baiwang"},
            {"filepath": "C:/staging/local.pdf"},
        ]
    )

    assert [candidate.parallel_safe for candidate in candidates] == [False, False, True]


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_reason"),
    [
        (TimeoutError("secret URL"), "timeout", "REMOTE_TIMEOUT"),
        (PermissionError("secret key"), "auth_failed", "REMOTE_AUTH_FAILED"),
        (RuntimeError("quota exhausted: secret"), "quota_exhausted", "REMOTE_QUOTA_EXHAUSTED"),
        (ValueError("secret payload"), "unresolved", "REMOTE_EXTRACTION_FAILED"),
    ],
)
def test_worker_failures_become_sanitized_terminal_outcomes(
    failure, expected_status, expected_reason
):
    def remote(_candidate):
        raise failure

    outcome = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=remote,
    ).extract([_candidate(0)])[0]

    assert outcome.status == expected_status
    assert outcome.reason_code == expected_reason
    assert "secret" not in outcome.message
    assert outcome.is_terminal


@pytest.mark.parametrize(
    ("failure", "expected_status", "expected_reason"),
    [
        (
            GlmRequestError("text", http_status=401, reason="http_error"),
            "auth_failed",
            "REMOTE_AUTH_FAILED",
        ),
        (
            GlmRequestError("text", http_status=402, reason="http_error"),
            "quota_exhausted",
            "REMOTE_QUOTA_EXHAUSTED",
        ),
    ],
)
def test_real_glm_terminal_errors_map_to_explicit_outcomes(
    failure, expected_status, expected_reason
):
    def remote(_candidate):
        raise failure

    outcome = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=remote,
    ).extract([_candidate(0)])[0]

    assert outcome.status == expected_status
    assert outcome.reason_code == expected_reason


def test_stop_is_explicit_and_every_candidate_has_one_terminal_outcome():
    stopped = threading.Event()
    candidates = [_candidate(index) for index in range(5)]

    def local(candidate):
        if candidate.sequence == 1:
            stopped.set()
        return None

    outcomes = ExtractionPipeline(
        local_parser=local,
        remote_extractor=lambda candidate: {"sequence": candidate.sequence},
        stop_requested=stopped.is_set,
    ).extract(candidates)

    assert len(outcomes) == len(candidates)
    assert len({outcome.candidate.identity.document_id for outcome in outcomes}) == len(candidates)
    assert all(outcome.is_terminal for outcome in outcomes)
    assert any(outcome.status == "cancelled" for outcome in outcomes)


def test_pending_workers_check_stop_before_remote_side_effects():
    stop = threading.Event()
    first_started = threading.Event()
    release_first = threading.Event()
    remote_calls: list[int] = []

    def remote(candidate):
        remote_calls.append(candidate.sequence)
        if candidate.sequence == 0:
            first_started.set()
            assert release_first.wait(timeout=2)
        return {"sequence": candidate.sequence}

    pipeline = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=remote,
        max_workers=1,
        verified_ceiling=lambda: 1,
        stop_requested=stop.is_set,
    )
    result: list[list[ExtractionOutcome]] = []
    worker = threading.Thread(
        target=lambda: result.append(pipeline.extract([_candidate(i) for i in range(3)]))
    )
    worker.start()
    assert first_started.wait(timeout=2)
    stop.set()
    release_first.set()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert remote_calls == [0]
    assert [outcome.status for outcome in result[0]] == [
        "resolved",
        "cancelled",
        "cancelled",
    ]


def test_incremental_scheduler_stops_submitting_after_first_quota_terminal():
    from glm_runtime import GlmRequestError

    remote_calls = []

    def remote(candidate):
        remote_calls.append(candidate.sequence)
        if candidate.sequence == 0:
            raise GlmRequestError("text", http_status=402, reason="http_error")
        time.sleep(0.05)
        return {"sequence": candidate.sequence}

    outcomes = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=remote,
        max_workers=2,
        verified_ceiling=lambda: 2,
    ).extract([_candidate(index) for index in range(5)])

    assert len(remote_calls) <= 2
    assert len(outcomes) == 5
    assert outcomes[0].status == "quota_exhausted"
    assert all(outcome.is_terminal for outcome in outcomes)
    assert all(
        outcome.status == "quota_exhausted" for outcome in outcomes[2:]
    )
    assert all("secret" not in outcome.message for outcome in outcomes)


def test_executor_submit_exception_becomes_outcome_and_later_candidate_continues(monkeypatch):
    import extraction_pipeline as module

    real_executor = module.ThreadPoolExecutor
    submit_count = 0

    class SubmitFailingExecutor(real_executor):
        def submit(self, fn, /, *args, **kwargs):
            nonlocal submit_count
            submit_count += 1
            if submit_count == 1:
                raise RuntimeError("submit secret")
            return super().submit(fn, *args, **kwargs)

    monkeypatch.setattr(module, "ThreadPoolExecutor", SubmitFailingExecutor)
    outcomes = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=lambda candidate: {"sequence": candidate.sequence},
        max_workers=1,
    ).extract([_candidate(0), _candidate(1)])

    assert [outcome.status for outcome in outcomes] == ["unresolved", "resolved"]
    assert "secret" not in outcomes[0].message


def test_remote_completion_emits_progress_before_slower_batch_finishes():
    first_done = threading.Event()
    release_second = threading.Event()
    progress = []

    def remote(candidate):
        if candidate.sequence == 0:
            first_done.set()
            return {"sequence": 0}
        assert release_second.wait(timeout=2)
        return {"sequence": 1}

    pipeline = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=remote,
        max_workers=2,
        progress_callback=lambda completed, total, percent: progress.append(
            (completed, total, percent)
        ),
    )
    result = []
    worker = threading.Thread(
        target=lambda: result.extend(pipeline.extract([_candidate(0), _candidate(1)]))
    )
    worker.start()
    assert first_done.wait(timeout=2)
    deadline = time.time() + 2
    while time.time() < deadline and not any(completed == 1 for completed, *_ in progress):
        time.sleep(0.01)

    assert any(completed == 1 for completed, *_ in progress)
    release_second.set()
    worker.join(timeout=2)
    assert not worker.is_alive()


def test_duplicate_sequences_do_not_overwrite_or_hide_terminal_outcomes():
    first = _candidate(0, name="a.pdf")
    second = _candidate(0, name="b.pdf")

    outcomes = ExtractionPipeline(
        local_parser=lambda candidate: {"path": candidate.source_path},
        remote_extractor=lambda _candidate: pytest.fail("local result must bypass remote"),
    ).extract([first, second])

    assert len(outcomes) == 2
    assert [outcome.payload["path"] for outcome in outcomes] == [
        "C:/staging/a.pdf",
        "C:/staging/b.pdf",
    ]


def test_trace_sink_failure_does_not_hide_later_terminal_outcomes():
    traces = 0

    def broken_trace(_event):
        nonlocal traces
        traces += 1
        raise RuntimeError("sink failed")

    outcomes = ExtractionPipeline(
        local_parser=lambda candidate: {"sequence": candidate.sequence},
        remote_extractor=lambda _candidate: None,
        trace_sink=broken_trace,
    ).extract([_candidate(0), _candidate(1)])

    assert len(outcomes) == 2
    assert traces == 2


def test_archive_finalizer_can_publish_final_renamed_path_for_lineage(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"invoice")
    candidate = CandidatePipeline().collect(
        [{"filepath": str(source), "message_uid": "100"}]
    )[0]
    outcome = ExtractionOutcome.resolved(candidate, {"InvoiceNumber": "12345678"})

    def writer(_outcome, root):
        initial = root / "餐饮" / "before.pdf"
        initial.parent.mkdir(parents=True)
        initial.write_bytes(source.read_bytes())
        return str(initial)

    def finalizer(report, _root):
        initial = Path(report.outcomes[0].archive_path)
        renamed = initial.with_name("after.pdf")
        initial.replace(renamed)
        return {candidate.identity.document_id: str(renamed)}

    report = ArchiveService(writer=writer, finalizer=finalizer).archive(
        [outcome], tmp_path / "output"
    )

    assert report.outcomes[0].archive_path.endswith("after.pdf")
    assert Path(report.outcomes[0].archive_path).read_bytes() == b"invoice"


def test_archive_can_defer_finalizer_until_complete_report(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"invoice")
    candidate = CandidatePipeline().collect(
        [{"filepath": str(source), "message_uid": "100"}]
    )[0]
    outcome = ExtractionOutcome.resolved(candidate, {"InvoiceNumber": "12345678"})
    calls = []

    def writer(_outcome, root):
        initial = root / "before.pdf"
        initial.parent.mkdir(parents=True)
        initial.write_bytes(source.read_bytes())
        return str(initial)

    def finalizer(report, _root):
        calls.append(report)
        initial = Path(report.outcomes[0].archive_path)
        renamed = initial.with_name("after.pdf")
        initial.replace(renamed)
        return {candidate.identity.document_id: str(renamed)}

    service = ArchiveService(writer=writer, finalizer=finalizer)
    report = service.archive([outcome], tmp_path / "output", finalize=False)

    assert calls == []
    assert Path(report.outcomes[0].archive_path).name == "before.pdf"

    finalized = service.finalize(report, tmp_path / "output")

    assert len(calls) == 1
    assert Path(finalized.outcomes[0].archive_path).name == "after.pdf"
    assert Path(finalized.outcomes[0].archive_path).is_file()


def test_archive_is_single_threaded_ordered_and_idempotent(tmp_path: Path):
    calls: list[tuple[int, int]] = []
    main_thread = threading.get_ident()

    def writer(outcome, _root):
        calls.append((outcome.candidate.sequence, threading.get_ident()))
        return str(tmp_path / f"{outcome.candidate.sequence}.pdf")

    candidates = [_candidate(index) for index in range(3)]
    outcomes = tuple(
        ExtractionOutcome.resolved(candidate, {"sequence": candidate.sequence})
        for candidate in reversed(candidates)
    )
    service = ArchiveService(writer=writer)

    report = service.archive(outcomes, tmp_path)
    duplicate_report = service.archive(outcomes, tmp_path)

    assert [sequence for sequence, _thread_id in calls] == [0, 1, 2]
    assert all(thread_id == main_thread for _sequence, thread_id in calls)
    assert report.archived_count == 3
    assert duplicate_report.archived_count == 0
    assert duplicate_report.duplicate_count == 3


def test_archive_report_fails_closed_for_unresolved_and_duplicate_input_identity(tmp_path: Path):
    candidate = _candidate(0)
    unresolved = ExtractionOutcome.unresolved(candidate, "FAILED", "safe")
    report = ArchiveService(writer=lambda *_args: pytest.fail("writer must not run")).archive(
        [unresolved, unresolved], tmp_path
    )

    assert report.terminal_count == 2
    assert report.unresolved_count == 1
    assert report.duplicate_count == 1
    assert report.can_complete is False


def test_terminal_retention_and_manual_review_do_not_block_run_completion():
    report = ArchiveReport(
        outcomes=(),
        archived_count=0,
        retained_count=10,
        manual_count=1,
        unresolved_count=0,
        duplicate_count=0,
    )

    assert report.can_complete is True


def test_archived_attachment_defers_matching_provider_url_without_network(tmp_path):
    candidates = CandidatePipeline().collect(
        [
            {"filepath": "C:/staging/invoice.pdf", "email_id": "mail-1"},
            {
                "filepath": "https://provider.example/fallback",
                "email_id": "mail-1",
                "provider_family": "baiwang",
                "provider_expected_fields": {
                    "invoice_number": "26110000000000000001"
                },
            },
        ]
    )
    attachment = ExtractionOutcome.resolved(
        candidates[0],
        {
            "info_json": {
                "InvoiceNumber": "26110000000000000001",
                "is_invoice": True,
            }
        },
    )

    archive_path = tmp_path / "invoice.pdf"
    archive_path.write_bytes(b"%PDF-1.4\n")
    skipped, pending = partition_redundant_provider_candidates(
        [candidates[1]],
        [ArchivedOutcome(outcome=attachment, archive_path=str(archive_path))],
        canonical_info_by_document_id={
            candidates[0].identity.document_id: attachment.payload["info_json"]
        },
    )

    assert pending == []
    assert skipped[0].status == "retained"
    assert skipped[0].reason_code == "PROVIDER_URL_REDUNDANT_WITH_ARCHIVED_ATTACHMENT"


def test_provider_url_remains_pending_without_exact_archived_identity(tmp_path):
    candidates = CandidatePipeline().collect(
        [
            {"filepath": "C:/staging/invoice.pdf", "email_id": "mail-1"},
            {
                "filepath": "https://provider.example/fallback",
                "email_id": "mail-1",
                "provider_family": "baiwang",
                "provider_expected_fields": {
                    "invoice_number": "26110000000000000001"
                },
            },
        ]
    )
    different = ExtractionOutcome.resolved(
        candidates[0],
        {
            "info_json": {
                "InvoiceNumber": "26110000000000000002",
                "is_invoice": True,
            }
        },
    )

    archive_path = tmp_path / "different.pdf"
    archive_path.write_bytes(b"%PDF-1.4\n")
    skipped, pending = partition_redundant_provider_candidates(
        [candidates[1]],
        [ArchivedOutcome(outcome=different, archive_path=str(archive_path))],
        canonical_info_by_document_id={
            candidates[0].identity.document_id: different.payload["info_json"]
        },
    )

    assert skipped == []
    assert pending == [candidates[1]]


def test_extracted_but_unarchived_attachment_never_suppresses_provider_url():
    candidates = CandidatePipeline().collect(
        [
            {"filepath": "C:/staging/invoice.pdf", "email_id": "mail-1"},
            {
                "filepath": "https://provider.example/fallback",
                "email_id": "mail-1",
                "provider_family": "baiwang",
                "provider_expected_fields": {
                    "invoice_number": "26110000000000000001"
                },
            },
        ]
    )
    attachment = ExtractionOutcome.resolved(
        candidates[0],
        {
            "info_json": {
                "InvoiceNumber": "26110000000000000001",
                "is_invoice": True,
            }
        },
    )

    skipped, pending = partition_redundant_provider_candidates(
        [candidates[1]],
        [ArchivedOutcome(outcome=attachment, archive_path="")],
        canonical_info_by_document_id={
            candidates[0].identity.document_id: attachment.payload["info_json"]
        },
    )

    assert skipped == []
    assert pending == [candidates[1]]


def test_archived_itinerary_quoting_invoice_number_never_suppresses_provider_url(
    tmp_path,
):
    candidates = CandidatePipeline().collect(
        [
            {"filepath": "C:/staging/itinerary.pdf", "email_id": "mail-1"},
            {
                "filepath": "https://provider.example/fallback",
                "email_id": "mail-1",
                "provider_family": "baiwang",
                "provider_expected_fields": {
                    "invoice_number": "26110000000000000001"
                },
            },
        ]
    )
    itinerary = ExtractionOutcome.resolved(
        candidates[0],
        {
            "info_json": {
                "InvoiceNumber": "26110000000000000001",
                "is_invoice": True,
                "Type": "打车行程单",
                "_is_itinerary": True,
            }
        },
    )
    archive_path = tmp_path / "itinerary.pdf"
    archive_path.write_bytes(b"%PDF-1.4\n")

    skipped, pending = partition_redundant_provider_candidates(
        [candidates[1]],
        [ArchivedOutcome(outcome=itinerary, archive_path=str(archive_path))],
        canonical_info_by_document_id={
            candidates[0].identity.document_id: {
                "InvoiceNumber": "26110000000000000001",
                "is_invoice": True,
                "Type": "打车行程单",
                "_is_itinerary": True,
            }
        },
    )

    assert skipped == []
    assert pending == [candidates[1]]


def test_extraction_progress_supports_monotonic_multi_phase_offsets():
    progress = []
    candidates = [_candidate(0), _candidate(1)]
    pipeline = ExtractionPipeline(
        local_parser=lambda candidate: {"sequence": candidate.sequence},
        remote_extractor=lambda _candidate: pytest.fail("remote must not run"),
        progress_callback=lambda completed, total, percent: progress.append(
            (completed, total, percent)
        ),
    )

    pipeline.extract(candidates, progress_offset=3, progress_total=5)

    assert progress[0] == (3, 5, 60)
    assert progress[-1] == (5, 5, 100)


def test_processing_session_never_downloads_exact_archived_provider_url(tmp_path):
    candidates = CandidatePipeline().collect(
        [
            {"filepath": "C:/staging/invoice.pdf", "email_id": "mail-1"},
            {
                "filepath": "https://provider.example/fallback",
                "email_id": "mail-1",
                "provider_family": "baiwang",
                "provider_expected_fields": {
                    "invoice_number": "26110000000000000001"
                },
            },
        ]
    )

    class FakePipeline:
        def __init__(self):
            self.calls = []

        def extract(self, batch, **progress):
            batch = list(batch)
            self.calls.append((batch, progress))
            if not batch:
                return []
            assert batch == [candidates[0]]
            return [
                ExtractionOutcome.resolved(
                    candidates[0],
                    {
                        "info_json": {
                            "InvoiceNumber": "26110000000000000001",
                            "is_invoice": True,
                        }
                    },
                )
            ]

    class FakeTraceStore:
        def __init__(self):
            self.events = []
            self.records = {
                candidates[0].identity.document_id: {
                    "normalized_fields": {
                        "InvoiceNumber": "26110000000000000001",
                        "is_invoice": True,
                        "Type": "餐饮",
                    }
                }
            }

        def set_fields(self, document_id, **fields):
            self.events.append((document_id, fields))

        def get_record(self, document_id):
            return self.records.get(document_id)

    class FakeApi:
        def __init__(self):
            self.events = []

        def _safe_emit_stage_event(self, stage, event, extra=None):
            self.events.append((stage, event, extra))

        def _commit_output_state(self, *_args):
            return None

    pipeline = FakePipeline()
    trace_store = FakeTraceStore()
    api = FakeApi()
    archive_path = tmp_path / "invoice.pdf"
    archive_path.write_bytes(b"%PDF-1.4\n")

    class FakeArchiveService:
        def __init__(self):
            self.calls = []

        def archive(self, outcomes, _root, *, finalize=True):
            assert finalize is False
            outcomes = list(outcomes)
            self.calls.append(outcomes)
            if len(self.calls) == 1:
                return ArchiveReport(
                    outcomes=(
                        ArchivedOutcome(
                            outcome=outcomes[0], archive_path=str(archive_path)
                        ),
                    ),
                    archived_count=1,
                    retained_count=0,
                    manual_count=0,
                    unresolved_count=0,
                    duplicate_count=0,
                )
            return ArchiveReport(
                outcomes=tuple(ArchivedOutcome(outcome=item) for item in outcomes),
                archived_count=0,
                retained_count=len(outcomes),
                manual_count=0,
                unresolved_count=0,
                duplicate_count=0,
            )

        def finalize(self, report, _root):
            return report

    archive_service = FakeArchiveService()
    session = _ProcessingPipelineSession(
        api=api,
        candidates=candidates,
        pipeline=pipeline,
        archive_service=archive_service,
        save_path=str(tmp_path),
        output_state_dir=str(tmp_path / "state"),
        working_history={},
        business_records={},
        sidecar={},
        trace_store=trace_store,
        owned_extractor=None,
    )

    outcomes = session.extract()
    report = session.archive(outcomes)

    assert len(pipeline.calls) == 2
    assert pipeline.calls[1][0] == []
    assert report.can_complete is True
    assert archive_service.calls[1][0].reason_code == (
        "PROVIDER_URL_REDUNDANT_WITH_ARCHIVED_ATTACHMENT"
    )
    assert trace_store.events[0][0] == candidates[1].identity.document_id


def test_archive_event_sink_failure_does_not_hide_report_or_later_outcomes(tmp_path: Path):
    events = 0

    def broken_sink(_event):
        nonlocal events
        events += 1
        raise RuntimeError("event sink failed")

    outcomes = [
        ExtractionOutcome.resolved(_candidate(index), {"sequence": index})
        for index in range(2)
    ]
    report = ArchiveService(
        writer=lambda outcome, _root: f"{outcome.candidate.sequence}.pdf",
        event_sink=broken_sink,
    ).archive(outcomes, tmp_path)

    assert report.archived_count == 2
    assert report.terminal_count == 2
    assert events == 2


def test_archive_pairing_finalizer_renames_hotel_pair_adjacent_and_reports_count(tmp_path: Path):
    from archive_pairing_service import reconcile_archive_pairs

    hotel = tmp_path / "住宿发票"
    hotel.mkdir()
    invoice = hotel / "20260610_住宿发票_424.15_上海锐丞酒店管理有限公司.pdf"
    folio = hotel / "20260610_住宿水单_424.15_上海张江CitiGO欢阁酒店.pdf"
    invoice.write_bytes(b"invoice")
    folio.write_bytes(b"folio")

    counts = reconcile_archive_pairs(tmp_path)
    names = sorted(path.name for path in hotel.iterdir())

    assert counts == {"ride": 0, "hotel": 1}
    assert names == [
        "20260610-住宿-01-发票_424.15元.pdf",
        "20260610-住宿-01-水单_424.15元.pdf",
    ]


def test_archive_pairing_updates_trace_targets_and_combine_results(tmp_path: Path):
    from archive_pairing_service import reconcile_archive_pairs

    hotel = tmp_path / "住宿发票"
    hotel.mkdir()
    invoice = hotel / "20260610_住宿发票_424.15_酒店.pdf"
    folio = hotel / "20260610_住宿水单_424.15_酒店.pdf"
    invoice.write_bytes(b"invoice")
    folio.write_bytes(b"folio")

    class TraceStore:
        ids = {str(invoice): "invoice-id", str(folio): "folio-id"}
        fields = {}

        def get_document_id_by_archive_target(self, path):
            return self.ids.get(str(path))

        def move_archive_target(self, source, target):
            document_id = self.ids.pop(str(source))
            self.ids[str(target)] = document_id

        def set_fields(self, document_id, **fields):
            self.fields[document_id] = fields

    trace_store = TraceStore()
    reconcile_archive_pairs(tmp_path, trace_store=trace_store)

    assert trace_store.fields["invoice-id"]["combine_result"]["status"] == "matched"
    assert trace_store.fields["folio-id"]["combine_result"]["status"] == "matched"
    assert all(Path(path).exists() for path in trace_store.ids)


def test_archive_pairing_fails_closed_before_overwriting_existing_target(tmp_path: Path):
    from archive_pairing_service import reconcile_archive_pairs

    hotel = tmp_path / "住宿发票"
    hotel.mkdir()
    invoice = hotel / "20260610_住宿发票_424.15_酒店.pdf"
    folio = hotel / "20260610_住宿水单_424.15_酒店.pdf"
    invoice.write_bytes(b"invoice")
    folio.write_bytes(b"folio")
    (hotel / "20260610-住宿-01-发票_424.15元.pdf").mkdir()

    with pytest.raises(RuntimeError, match="ARCHIVE_PAIR_TARGET_COLLISION"):
        reconcile_archive_pairs(tmp_path)

    assert invoice.exists()
    assert folio.exists()


def test_progress_is_monotonic_bounded_and_has_stable_total():
    events: list[tuple[int, int, int]] = []
    candidates = [_candidate(index) for index in range(4)]

    ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=lambda candidate: {"sequence": candidate.sequence},
        progress_callback=lambda completed, total, percent: events.append(
            (completed, total, percent)
        ),
        max_workers=2,
    ).extract(candidates)

    assert events[0] == (0, 4, 0)
    assert events[-1] == (4, 4, 100)
    assert {total for _completed, total, _percent in events} == {4}
    assert [percent for _completed, _total, percent in events] == sorted(
        percent for _completed, _total, percent in events
    )
    assert all(0 <= percent <= 100 for _completed, _total, percent in events)


def test_no_progress_or_trace_events_are_emitted_after_terminal_return():
    events: list[tuple[str, int]] = []
    pipeline = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=lambda candidate: {"sequence": candidate.sequence},
        progress_callback=lambda completed, _total, _percent: events.append(
            ("progress", completed)
        ),
        trace_sink=lambda _event: events.append(("trace", len(events))),
    )

    pipeline.extract([_candidate(0), _candidate(1)])
    snapshot = list(events)
    time.sleep(0.05)

    assert events == snapshot


def test_executor_is_closed_when_submission_fails(monkeypatch):
    cancelled = Counter()

    class BrokenExecutor:
        def __init__(self, **_kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            cancelled["closed"] += 1

        def submit(self, *_args, **_kwargs):
            future = Future()
            future.set_exception(RuntimeError("submit failure"))
            return future

    monkeypatch.setattr("extraction_pipeline.ThreadPoolExecutor", BrokenExecutor)

    outcomes = ExtractionPipeline(
        local_parser=lambda _candidate: None,
        remote_extractor=lambda _candidate: None,
    ).extract([_candidate(0)])

    assert cancelled["closed"] == 1
    assert outcomes[0].status == "unresolved"


def test_archive_service_owns_ordered_normalize_classify_write_and_finalize(tmp_path: Path):
    calls = []
    candidates = [_candidate(index) for index in range(3)]
    outcomes = [
        ExtractionOutcome.resolved(candidate, {"sequence": candidate.sequence})
        for candidate in reversed(candidates)
    ]
    service = ArchiveService(
        normalizer=lambda outcome: calls.append(("normalize", outcome.candidate.sequence))
        or outcome.to_legacy_payload(),
        classifier=lambda payload: calls.append(("classify", payload["sequence"]))
        or "餐饮",
        archive_operation=lambda outcome, payload, category, _root: calls.append(
            ("write", outcome.candidate.sequence, payload["sequence"], category)
        )
        or str(tmp_path / f"{outcome.candidate.sequence}.pdf"),
        finalizer=lambda report, _root: calls.append(
            ("finalize", tuple(item.outcome.candidate.sequence for item in report.outcomes))
        ),
    )

    report = service.archive(outcomes, tmp_path)

    assert report.can_complete is True
    assert calls == [
        ("normalize", 0),
        ("classify", 0),
        ("write", 0, 0, "餐饮"),
        ("normalize", 1),
        ("classify", 1),
        ("write", 1, 1, "餐饮"),
        ("normalize", 2),
        ("classify", 2),
        ("write", 2, 2, "餐饮"),
        ("finalize", (0, 1, 2)),
    ]


def test_app_api_processing_compatibility_path_delegates_to_all_three_pipeline_modules():
    import inspect

    from app_api import InvoiceAppAPI

    source = inspect.getsource(InvoiceAppAPI._run_processing_loop_with_extractor)
    assert "CandidatePipeline" in source
    assert "CandidatePreflight" in source
    assert "ExtractionPipeline" in source
    assert "SharedRuntimeRemoteExtractor" in source
    assert "ArchiveService" in source
    assert "AppArchiveAdapter" in source
    assert ".archive(" in source
    assert "delegate_batch" not in source
    assert "_run_processing_loop_legacy_with_extractor" not in source
    assert "def _prepare_local" not in source
    assert "def _extract_remote" not in source
    assert "def _default_archive_operation" not in source
    assert len(source.splitlines()) < 180


def test_whole_batch_archive_delegate_is_removed():
    assert not hasattr(ArchiveService, "delegate_batch")


def test_production_app_bridge_models_once_archives_in_order_and_cleans_sidecar(tmp_path: Path):
    from types import SimpleNamespace

    from app_api import InvoiceAppAPI

    api = InvoiceAppAPI()
    api._stop_requested = False
    progress_events = []
    api._safe_emit_stage_event = lambda stage, event, payload: progress_events.append(
        (stage, event, payload)
    )
    api._run_processing_loop_legacy_with_extractor = lambda *_args, **_kwargs: pytest.fail(
        "whole-batch legacy path must be unreachable"
    )
    paths = []
    for index in range(2):
        path = tmp_path / f"{index}.pdf"
        path.write_bytes((f"content-{index}" * 200).encode())
        paths.append(path)
    rows = [
        {"filepath": str(paths[0]), "email_id": "mail-1"},
        {"filepath": str(paths[1]), "email_id": "mail-1"},
        {"filepath": str(paths[0]), "email_id": "mail-1"},
    ]
    model_calls = []
    archive_order = []

    class OwnerExtractor:
        glm_runtime = SimpleNamespace(
            profiles={"text": SimpleNamespace(max_concurrency=2)}
        )

        @staticmethod
        def load_processed_records():
            return {}

        @staticmethod
        def probe_local_only(_path, document_context=None):
            del document_context
            return SimpleNamespace(status="needs_remote", result=None, reason_code="LOCAL_PROBE_UNRESOLVED", engine="")

        @staticmethod
        def pdf_to_base64_image(path):
            return [f"image:{Path(path).name}"]

    class Worker:
        last_extraction_trace = {}
        last_timing_trace = {}

        @staticmethod
        def extract_remote_only(_images, **context):
            model_calls.append(context["pdf_path"])
            return {
                "is_invoice": True,
                "Date": "20260601",
                "Purchaser": "辉瑞投资有限公司",
                "Seller": Path(context["pdf_path"]).stem,
                "Amount": "1.00",
                "Type": "餐饮",
            }

        @staticmethod
        def close():
            pass

    def archive_operation(outcome, payload, _category, _root):
        archive_order.append(outcome.candidate.sequence)
        return payload["pdf_path"]

    report = api._run_processing_loop_with_extractor(
        rows,
        "key",
        str(tmp_path / "output"),
        _extractor=OwnerExtractor(),
        _worker_extractor_factory=lambda _runtime: Worker(),
        _archive_operation=archive_operation,
        _pairing_finalizer=lambda _report, _root: None,
    )

    assert len(model_calls) == 2
    assert archive_order == [0, 1]
    assert report.duplicate_count == 1
    assert report.can_complete is True
    assert any(event == "progress" for _stage, event, _payload in progress_events)
    assert not hasattr(api, "_pipeline_sidecar")


def test_production_app_bridge_quota_is_p0_fail_closed_and_never_completes(tmp_path: Path):
    from types import SimpleNamespace

    from app_api import InvoiceAppAPI, ProcessingLoopFailure
    from glm_runtime import GlmRequestError

    api = InvoiceAppAPI()
    api._stop_requested = False
    path = tmp_path / "invoice.pdf"
    path.write_bytes(b"invoice" * 300)

    class OwnerExtractor:
        glm_runtime = SimpleNamespace(
            profiles={"text": SimpleNamespace(max_concurrency=2)}
        )

        @staticmethod
        def load_processed_records():
            return {}

        @staticmethod
        def probe_local_only(_path, document_context=None):
            del document_context
            return SimpleNamespace(status="needs_remote", result=None, reason_code="LOCAL_PROBE_UNRESOLVED", engine="")

        @staticmethod
        def pdf_to_base64_image(_path):
            return ["image"]

    class Worker:
        @staticmethod
        def extract_remote_only(*_args, **_kwargs):
            raise GlmRequestError("text", http_status=402, reason="http_error")

        @staticmethod
        def close():
            pass

    terminal_statuses = []

    def retain_terminal(outcome, *_args):
        from archive_service import ArchiveDecision

        terminal_statuses.append(outcome.status)
        return ArchiveDecision(path=str(path), status="unresolved")

    with pytest.raises(ProcessingLoopFailure, match="PROCESSING_PIPELINE_INCOMPLETE"):
        api._run_processing_loop_with_extractor(
            [{"filepath": str(path), "email_id": "mail-1"}],
            "key",
            str(tmp_path / "output"),
            _extractor=OwnerExtractor(),
            _worker_extractor_factory=lambda _runtime: Worker(),
            _archive_operation=retain_terminal,
            _pairing_finalizer=lambda _report, _root: None,
        )
    assert terminal_statuses == ["quota_exhausted"]
    assert not hasattr(api, "_pipeline_sidecar")


def test_production_app_default_archive_operation_routes_local_result_once(tmp_path: Path):
    from types import SimpleNamespace

    from app_api import InvoiceAppAPI

    api = InvoiceAppAPI()
    api._stop_requested = False
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"invoice" * 300)
    routed = []

    class Extractor:
        glm_runtime = SimpleNamespace(profiles={})
        last_route_trace = {"status": "archived"}

        @staticmethod
        def load_processed_records():
            return {}

        @staticmethod
        def probe_local_only(_path, document_context=None):
            del document_context
            return SimpleNamespace(
                status="resolved",
                result={
                    "is_invoice": True,
                    "Date": "20260601",
                    "Purchaser": "辉瑞投资有限公司",
                    "Seller": "Local Seller",
                    "Amount": "1.00",
                    "InvoiceCode": "",
                    "InvoiceNumber": "",
                    "Type": "餐饮",
                },
                reason_code="LOCAL_STANDARD_EINVOICE_PDF_FAST_PATH",
                engine="local_standard_einvoice_pdf",
            )

        @staticmethod
        def route_and_rename_file(path, info, custom_rules=None):
            routed.append((path, info["Seller"], custom_rules))
            return True, path

    report = api._run_processing_loop_with_extractor(
        [{"filepath": str(source), "email_id": "mail-1"}],
        "key",
        str(tmp_path / "output"),
        _extractor=Extractor(),
        _worker_extractor_factory=lambda _runtime: pytest.fail("local result bypasses model"),
        _pairing_finalizer=lambda _report, _root: None,
    )

    assert report.archived_count == 1
    assert report.can_complete is True
    assert len(routed) == 1


def test_production_app_history_duplicate_makes_zero_model_and_archive_calls(tmp_path: Path):
    from types import SimpleNamespace

    from app_api import InvoiceAppAPI

    api = InvoiceAppAPI()
    api._stop_requested = False
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"invoice" * 300)
    row = {"filepath": str(source), "email_id": "mail-1"}
    document_id = CandidatePipeline().collect([row])[0].identity.document_id
    api._load_committed_history = lambda _state_dir: {document_id}

    class Extractor:
        glm_runtime = SimpleNamespace(profiles={})

        @staticmethod
        def load_processed_records():
            return {}

        @staticmethod
        def probe_local_only(*_args, **_kwargs):
            pytest.fail("history duplicate must stop before local/model work")

    report = api._run_processing_loop_with_extractor(
        [row],
        "key",
        str(tmp_path / "output"),
        _extractor=Extractor(),
        _worker_extractor_factory=lambda _runtime: pytest.fail("no model"),
        _archive_operation=lambda *_args: pytest.fail("no archive"),
        _pairing_finalizer=lambda _report, _root: None,
    )

    assert report.duplicate_count == 1
    assert report.can_complete is True


def test_worker_local_extractor_close_does_not_close_shared_run_runtime(tmp_path: Path):
    from invoice_extractor import InvoiceExtractor

    closes = 0

    class SharedRuntime:
        def close(self):
            nonlocal closes
            closes += 1

    extractor = InvoiceExtractor(
        output_dir=tmp_path,
        glm_runtime=SharedRuntime(),
        close_glm_runtime=False,
    )

    extractor.close()
    extractor.close()

    assert closes == 0


def test_invoice_extractor_local_probe_resolves_without_glm(monkeypatch, tmp_path: Path):
    from invoice_extractor import InvoiceExtractor

    pdf_path = tmp_path / "local.pdf"
    pdf_path.write_bytes(b"local")

    class Runtime:
        @staticmethod
        def request(*_args, **_kwargs):
            pytest.fail("local-only probe must never invoke GLM")

    extractor = InvoiceExtractor(
        output_dir=tmp_path / "out",
        glm_runtime=Runtime(),
        close_glm_runtime=False,
    )
    local_result = {
        "is_invoice": True,
        "Date": "20260601",
        "Purchaser": "辉瑞投资有限公司",
        "Seller": "Local Seller",
        "Amount": "1.00",
        "Type": "餐饮",
    }
    monkeypatch.setattr(
        extractor, "_try_extract_email_body_receipt_from_pdf_text", lambda _path: local_result
    )

    probe = extractor.probe_local_only(str(pdf_path), document_context={})

    assert probe.status == "resolved"
    assert probe.result["Seller"] == "Local Seller"
    assert probe.reason_code == "LOCAL_EMAIL_BODY_RECEIPT_PDF_FAST_PATH"


def test_invoice_extractor_remote_only_skips_every_local_probe(monkeypatch, tmp_path: Path):
    import fitz

    from invoice_extractor import InvoiceExtractor

    pdf_path = tmp_path / "remote.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "remote extraction document")
    document.save(pdf_path)
    document.close()
    with pdf_path.open("ab") as handle:
        handle.write(b" " * 1024)

    calls = []

    class Runtime:
        @staticmethod
        def request(profile, _payload, parser, **_kwargs):
            calls.append(profile)
            if profile == "ocr":
                return parser({"md_results": "invoice date seller amount enough text"})
            return parser(
                {
                    "choices": [
                        {
                            "message": {
                                "content": '{"is_invoice":true,"Date":"20260601",'
                                '"Purchaser":"辉瑞投资有限公司","Seller":"Remote Seller",'
                                '"Amount":"2.00","Type":"餐饮"}'
                            }
                        }
                    ]
                }
            )

    extractor = InvoiceExtractor(
        output_dir=tmp_path / "out",
        glm_runtime=Runtime(),
        close_glm_runtime=False,
    )
    monkeypatch.setattr(
        extractor,
        "probe_local_only",
        lambda *_args, **_kwargs: pytest.fail("remote-only method must not call local probe"),
    )
    images = extractor.pdf_to_base64_image(str(pdf_path))

    result = extractor.extract_remote_only(images, pdf_path=str(pdf_path))

    assert result["Seller"] == "Remote Seller"
    assert calls == ["ocr", "text"]
