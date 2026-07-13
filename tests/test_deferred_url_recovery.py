from __future__ import annotations

from collections import Counter
import threading
import time

from candidate_pipeline import CandidatePipeline
from deferred_url_recovery import DeferredUrlRecoveryScheduler
from extraction_pipeline import ExtractionOutcome


def _candidate(sequence: int, *, provider_group: str = "", provider_family: str = ""):
    return CandidatePipeline().collect(
        [
            {
                "filepath": f"https://example.test/invoice/{sequence}",
                "source_url": f"https://example.test/invoice/{sequence}",
                "email_id": f"mail-{sequence}",
                "provider_group_key": provider_group,
                "provider_family": provider_family,
                "is_url": True,
            }
        ],
        sequence_offset=sequence,
    )[0]


def test_production_default_allows_ten_independent_url_groups():
    import inspect

    from app_api import InvoiceAppAPI, _ProcessingPipelineSession
    import deferred_url_recovery

    configured = getattr(
        deferred_url_recovery, "DEFAULT_URL_RECOVERY_MAX_WORKERS", None
    )

    assert configured == 10
    assert DeferredUrlRecoveryScheduler()._max_workers == configured
    assert DeferredUrlRecoveryScheduler()._max_strong_workers == 4
    assert "max_workers=DEFAULT_URL_RECOVERY_MAX_WORKERS" in inspect.getsource(
        _ProcessingPipelineSession.__init__
    )
    assert "max_workers=DEFAULT_URL_RECOVERY_MAX_WORKERS" in inspect.getsource(
        InvoiceAppAPI._create_processing_pipeline_session
    )


def test_scheduler_reaches_four_concurrent_groups_and_returns_sequence_order():
    candidates = [
        _candidate(index, provider_group=f"group-{index}", provider_family="provider")
        for index in range(4)
    ]
    state_lock = threading.Lock()
    active = 0
    peak = 0

    def recover_one(candidate):
        nonlocal active, peak
        with state_lock:
            active += 1
            peak = max(peak, active)
        time.sleep(0.04)
        with state_lock:
            active -= 1
        return ExtractionOutcome.resolved(candidate, {"sequence": candidate.sequence})

    outcomes = DeferredUrlRecoveryScheduler(max_workers=4).recover(
        list(reversed(candidates)), recover_one
    )

    assert peak == 4
    assert [outcome.candidate.sequence for outcome in outcomes] == [0, 1, 2, 3]


def test_scheduler_caps_strong_provider_pressure_but_keeps_generic_slots_busy():
    strong = [
        _candidate(index, provider_group=f"strong-{index}", provider_family="provider")
        for index in range(6)
    ]
    generic = [_candidate(index + 6) for index in range(4)]
    state_lock = threading.Lock()
    active = 0
    active_strong = 0
    peak = 0
    peak_strong = 0

    def recover_one(candidate):
        nonlocal active, active_strong, peak, peak_strong
        is_strong = bool(candidate.metadata.get("provider_family"))
        with state_lock:
            active += 1
            active_strong += int(is_strong)
            peak = max(peak, active)
            peak_strong = max(peak_strong, active_strong)
        time.sleep(0.05)
        with state_lock:
            active -= 1
            active_strong -= int(is_strong)
        return ExtractionOutcome.resolved(candidate, {})

    outcomes = DeferredUrlRecoveryScheduler(
        max_workers=10, max_strong_workers=4
    ).recover([*strong, *generic], recover_one)

    assert peak_strong == 4
    assert peak >= 8
    assert len(outcomes) == 10


def test_scheduler_serializes_same_provider_group_and_continues_after_failure():
    candidates = [
        _candidate(0, provider_group="same", provider_family="provider"),
        _candidate(1, provider_group="same", provider_family="provider"),
        _candidate(2, provider_group="other", provider_family="provider"),
    ]
    state_lock = threading.Lock()
    active_by_group = Counter()
    peak_by_group = Counter()
    calls = []

    def recover_one(candidate):
        group = candidate.identity.provider_group_key
        with state_lock:
            calls.append(candidate.sequence)
            active_by_group[group] += 1
            peak_by_group[group] = max(peak_by_group[group], active_by_group[group])
        time.sleep(0.02)
        with state_lock:
            active_by_group[group] -= 1
        if candidate.sequence == 0:
            return ExtractionOutcome.unresolved(candidate, "URL_DOWNLOAD_FAILED")
        return ExtractionOutcome.resolved(candidate, {"sequence": candidate.sequence})

    outcomes = DeferredUrlRecoveryScheduler(max_workers=2).recover(candidates, recover_one)

    assert peak_by_group["same"] == 1
    assert calls.index(0) < calls.index(1)
    assert [outcome.status for outcome in outcomes] == ["unresolved", "resolved", "resolved"]


def test_scheduler_starts_strong_provider_groups_first_but_returns_sequence_order():
    candidates = [
        _candidate(0),
        _candidate(1, provider_group="provider", provider_family="provider"),
    ]
    calls = []

    def recover_one(candidate):
        calls.append(candidate.sequence)
        return ExtractionOutcome.resolved(candidate, {})

    outcomes = DeferredUrlRecoveryScheduler(max_workers=1).recover(candidates, recover_one)

    assert calls == [1, 0]
    assert [outcome.candidate.sequence for outcome in outcomes] == [0, 1]


def test_scheduler_uses_document_identity_as_generic_singleton_group():
    candidates = [_candidate(0), _candidate(1)]
    entered = threading.Barrier(2)

    def recover_one(candidate):
        entered.wait(timeout=1)
        return ExtractionOutcome.resolved(candidate, {})

    outcomes = DeferredUrlRecoveryScheduler(max_workers=2).recover(candidates, recover_one)

    assert [outcome.status for outcome in outcomes] == ["resolved", "resolved"]


def test_scheduler_stop_cancels_groups_not_started_with_deterministic_outcomes():
    stopped = threading.Event()
    candidates = [_candidate(0), _candidate(1)]
    calls = []

    def recover_one(candidate):
        calls.append(candidate.sequence)
        stopped.set()
        return ExtractionOutcome.resolved(candidate, {})

    outcomes = DeferredUrlRecoveryScheduler(
        max_workers=1, stop_requested=stopped.is_set
    ).recover(candidates, recover_one)

    assert calls == [0]
    assert outcomes[0].status == "resolved"
    assert outcomes[1].status == "cancelled"
    assert outcomes[1].reason_code == "STOP_REQUESTED"


def test_scheduler_progress_runs_on_owner_thread_once_per_candidate_and_retry_can_be_silent():
    owner_thread = threading.get_ident()
    worker_threads = set()
    progress = []
    candidates = [_candidate(index) for index in range(3)]

    def recover_one(candidate):
        worker_threads.add(threading.get_ident())
        return ExtractionOutcome.resolved(candidate, {})

    scheduler = DeferredUrlRecoveryScheduler(
        max_workers=3,
        progress_callback=lambda completed, total, percent: progress.append(
            (threading.get_ident(), completed, total, percent)
        ),
    )
    scheduler.recover(candidates, recover_one, progress_offset=2, progress_total=5)
    scheduler.recover(candidates, recover_one, emit_progress=False)

    assert worker_threads and owner_thread not in worker_threads
    assert [event[1:] for event in progress] == [
        (3, 5, 60),
        (4, 5, 80),
        (5, 5, 100),
    ]
    assert {event[0] for event in progress} == {owner_thread}


def test_scheduler_converts_callback_exception_without_losing_candidate():
    generic = _candidate(0)
    provider = _candidate(1, provider_group="provider", provider_family="provider")

    def broken(_candidate):
        raise RuntimeError("recovery failed")

    outcomes = DeferredUrlRecoveryScheduler(max_workers=2).recover(
        [generic, provider], broken
    )

    assert [(outcome.status, outcome.reason_code) for outcome in outcomes] == [
        ("retained", "URL_RECOVERY_CALLBACK_FAILED"),
        ("unresolved", "URL_RECOVERY_CALLBACK_FAILED"),
    ]
