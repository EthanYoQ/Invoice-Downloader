from __future__ import annotations

from collections import Counter
from concurrent.futures import Future
from pathlib import Path
import threading
import time

import pytest

from archive_service import ArchiveService
from candidate_pipeline import CandidatePipeline, DocumentCandidate
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


def test_legacy_batch_delegate_receives_original_order_and_exact_candidate_count(tmp_path: Path):
    received: list[list[dict]] = []
    candidates = [_candidate(index) for index in range(3)]
    outcomes = [
        ExtractionOutcome.resolved(candidate, candidate.to_legacy())
        for candidate in reversed(candidates)
    ]
    service = ArchiveService(writer=lambda *_args: pytest.fail("single writer not used"))

    result = service.delegate_batch(
        outcomes,
        tmp_path,
        lambda legacy_rows: received.append(legacy_rows) or "legacy-result",
    )

    assert result == "legacy-result"
    assert len(received[0]) == 3
    assert [row["message_uid"] for row in received[0]] == ["uid-0", "uid-1", "uid-2"]


def test_app_api_processing_compatibility_path_delegates_to_all_three_pipeline_modules():
    import inspect

    from app_api import InvoiceAppAPI

    source = inspect.getsource(InvoiceAppAPI._run_processing_loop_with_extractor)
    assert "CandidatePipeline" in source
    assert "ExtractionPipeline" in source
    assert "ArchiveService" in source
    assert "delegate_batch" in source


def test_app_api_pipeline_bridge_preserves_rows_extractor_and_call_shape():
    from types import MethodType

    from app_api import InvoiceAppAPI

    api = object.__new__(InvoiceAppAPI)
    calls = []

    def legacy(
        _self,
        rows,
        api_key,
        save_path,
        since_date,
        before_date,
        rules_text,
        _extractor,
        _prepared_extractions=None,
    ):
        assert _prepared_extractions == {}
        calls.append(
            (rows, api_key, save_path, since_date, before_date, rules_text, _extractor)
        )
        return "done"

    api._run_processing_loop_legacy_with_extractor = MethodType(legacy, api)
    rows = [
        {
            "filepath": "C:/staging/a.pdf",
            "subject": "subject",
            "nested": {"list": [1, 2]},
        }
    ]
    extractor = object()

    result = api._run_processing_loop_with_extractor(
        rows,
        "key",
        "C:/output",
        "2026-01-01",
        "2026-01-02",
        "rules",
        _extractor=extractor,
    )

    assert result == "done"
    assert calls == [
        (
            rows,
            "key",
            "C:/output",
            "2026-01-01",
            "2026-01-02",
            "rules",
            extractor,
        )
    ]


def test_app_api_safe_local_documents_use_shared_runtime_with_actual_overlap_max_two():
    from types import MethodType, SimpleNamespace

    from app_api import InvoiceAppAPI

    api = object.__new__(InvoiceAppAPI)
    api._stop_requested = False
    received = []
    prepared_results = {}
    lock = threading.Lock()
    barrier = threading.Barrier(2)
    active = maximum = closed = 0

    class OwnerExtractor:
        glm_runtime = SimpleNamespace(
            profiles={
                "ocr": SimpleNamespace(max_concurrency=2),
                "text": SimpleNamespace(max_concurrency=2),
                "vision_quality": SimpleNamespace(max_concurrency=2),
            }
        )

        @staticmethod
        def pdf_to_base64_image(path):
            return [f"image:{Path(path).name}"]

    class WorkerExtractor:
        last_extraction_trace = {}
        last_timing_trace = {}

        def extract_info_via_llm(self, _images, **context):
            nonlocal active, maximum
            with lock:
                active += 1
                maximum = max(maximum, active)
            try:
                if Path(context["pdf_path"]).stem in {"0", "1"}:
                    barrier.wait(timeout=2)
                else:
                    time.sleep(0.02)
                return {"Seller": Path(context["pdf_path"]).name}
            finally:
                with lock:
                    active -= 1

        def close(self):
            nonlocal closed
            closed += 1

    def legacy(_self, rows, *_args, **kwargs):
        received.extend(rows)
        prepared_results.update(kwargs.get("_prepared_extractions") or {})
        return "done"

    api._run_processing_loop_legacy_with_extractor = MethodType(legacy, api)
    rows = [{"filepath": f"C:/staging/{index}.pdf"} for index in range(4)]

    result = api._run_processing_loop_with_extractor(
        rows,
        "key",
        "C:/output",
        "2026-01-01",
        "2026-01-02",
        "rules",
        _extractor=OwnerExtractor(),
        _worker_extractor_factory=lambda _runtime: WorkerExtractor(),
    )

    assert result == "done"
    assert maximum == 2
    assert closed == 4
    assert [row["filepath"] for row in received] == [row["filepath"] for row in rows]
    assert sorted(
        prepared["info_json"]["Seller"] for prepared in prepared_results.values()
    ) == [
        "0.pdf",
        "1.pdf",
        "2.pdf",
        "3.pdf",
    ]
    assert all("_pipeline_prepared" not in row for row in received)


def test_app_api_retain_only_and_url_candidates_bypass_parallel_model_workers():
    from types import MethodType, SimpleNamespace

    from app_api import InvoiceAppAPI

    api = object.__new__(InvoiceAppAPI)
    api._stop_requested = False
    received = []
    prepared_results = {}
    factories = 0

    class OwnerExtractor:
        glm_runtime = SimpleNamespace(profiles={})

        @staticmethod
        def pdf_to_base64_image(_path):
            pytest.fail("retained and URL candidates stay on the legacy path")

    def factory(_runtime):
        nonlocal factories
        factories += 1
        pytest.fail("worker must not be created")

    def legacy(_self, rows, *_args, **kwargs):
        received.extend(rows)
        prepared_results.update(kwargs.get("_prepared_extractions") or {})
        return "done"

    api._run_processing_loop_legacy_with_extractor = MethodType(legacy, api)
    rows = [
        {"filepath": "C:/staging/retain.pdf", "candidate_action": "retain_only"},
        {"filepath": "https://example.com/invoice", "is_url": True},
    ]

    result = api._run_processing_loop_with_extractor(
        rows,
        "key",
        "C:/output",
        _extractor=OwnerExtractor(),
        _worker_extractor_factory=factory,
    )

    assert result == "done"
    assert factories == 0
    assert received == rows


def test_app_api_local_conversion_exception_is_retained_for_legacy_handling():
    from types import MethodType, SimpleNamespace

    from app_api import InvoiceAppAPI

    api = object.__new__(InvoiceAppAPI)
    api._stop_requested = False
    received = []
    prepared_results = {}

    class OwnerExtractor:
        glm_runtime = SimpleNamespace(profiles={})

        @staticmethod
        def pdf_to_base64_image(_path):
            raise ValueError("private local path")

    def legacy(_self, rows, *_args, **kwargs):
        received.extend(rows)
        prepared_results.update(kwargs.get("_prepared_extractions") or {})
        return "done"

    api._run_processing_loop_legacy_with_extractor = MethodType(legacy, api)

    result = api._run_processing_loop_with_extractor(
        [{"filepath": "C:/staging/broken.pdf"}],
        "key",
        "C:/output",
        _extractor=OwnerExtractor(),
        _worker_extractor_factory=lambda _runtime: pytest.fail("remote must not run"),
    )

    assert result == "done"
    assert list(prepared_results.values()) == [
        {
            "base64_img": None,
            "preprocess_error_type": "ValueError",
        }
    ]
    assert "_pipeline_prepared" not in received[0]


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


def test_app_api_worker_factory_failure_reaches_legacy_retention_then_fails_closed():
    from types import MethodType, SimpleNamespace

    from app_api import InvoiceAppAPI, ProcessingLoopFailure

    api = object.__new__(InvoiceAppAPI)
    api._stop_requested = False
    api.quota_exhausted = False
    received = []
    prepared_results = {}

    class OwnerExtractor:
        glm_runtime = SimpleNamespace(profiles={})

        @staticmethod
        def pdf_to_base64_image(_path):
            return ["image"]

    def legacy(_self, rows, *_args, **kwargs):
        received.extend(rows)
        prepared_results.update(kwargs.get("_prepared_extractions") or {})
        return "done"

    api._run_processing_loop_legacy_with_extractor = MethodType(legacy, api)

    with pytest.raises(ProcessingLoopFailure, match="PROCESSING_PIPELINE_UNRESOLVED"):
        api._run_processing_loop_with_extractor(
            [{"filepath": "C:/staging/a.pdf"}],
            "key",
            "C:/output",
            _extractor=OwnerExtractor(),
            _worker_extractor_factory=lambda _runtime: (_ for _ in ()).throw(
                RuntimeError("secret factory detail")
            ),
        )

    assert len(received) == 1
    prepared = next(iter(prepared_results.values()))
    assert prepared["error_reason"] == "PIPELINE_REMOTE_EXTRACTION_FAILED"
    assert prepared["exception_type"] == "RuntimeError"
    assert "secret" not in repr(prepared)


def test_app_api_worker_close_failure_does_not_override_success_or_leak_text():
    from types import MethodType, SimpleNamespace

    from app_api import InvoiceAppAPI

    api = object.__new__(InvoiceAppAPI)
    api._stop_requested = False
    api.quota_exhausted = False
    prepared_results = {}

    class OwnerExtractor:
        glm_runtime = SimpleNamespace(profiles={})

        @staticmethod
        def pdf_to_base64_image(_path):
            return ["image"]

    class Worker:
        last_extraction_trace = {}
        last_timing_trace = {}

        @staticmethod
        def extract_info_via_llm(_images, **_context):
            return {"Seller": "safe"}

        @staticmethod
        def close():
            raise RuntimeError("close secret")

    def legacy(_self, _rows, *_args, **kwargs):
        prepared_results.update(kwargs.get("_prepared_extractions") or {})
        return "done"

    api._run_processing_loop_legacy_with_extractor = MethodType(legacy, api)

    result = api._run_processing_loop_with_extractor(
        [{"filepath": "C:/staging/a.pdf"}],
        "key",
        "C:/output",
        _extractor=OwnerExtractor(),
        _worker_extractor_factory=lambda _runtime: Worker(),
    )

    assert result == "done"
    prepared = next(iter(prepared_results.values()))
    assert prepared["info_json"] == {"Seller": "safe"}
    assert prepared["worker_close_error_type"] == "RuntimeError"
    assert "secret" not in repr(prepared)


def test_legacy_archive_path_consumes_prepared_images_results_and_failures():
    import inspect

    from app_api import InvoiceAppAPI

    source = inspect.getsource(InvoiceAppAPI._run_processing_loop_legacy_with_extractor)
    assert "prepared_extractions.pop(document_id" in source
    assert 'prepared_extraction.get("base64_img")' in source
    assert 'prepared_extraction.get("info_json")' in source
    assert 'prepared_extraction.get("error_reason")' in source
