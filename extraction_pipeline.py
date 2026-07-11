from __future__ import annotations

from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping
import copy

from candidate_pipeline import DocumentCandidate, freeze_legacy_value, thaw_legacy_value


_TERMINAL_STATUSES = frozenset(
    {
        "resolved",
        "retained",
        "manual_review",
        "unresolved",
        "cancelled",
        "quota_exhausted",
        "auth_failed",
        "timeout",
        "duplicate",
    }
)


@dataclass(frozen=True)
class ExtractionOutcome:
    candidate: DocumentCandidate
    status: str
    payload: Any = None
    reason_code: str = ""
    message: str = ""
    artifact_path: str = ""
    trace_context: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.status not in _TERMINAL_STATUSES:
            raise ValueError(f"Unsupported extraction terminal status: {self.status}")
        trace_context = self.trace_context or self.candidate.trace_context
        object.__setattr__(self, "trace_context", freeze_legacy_value(trace_context))
        object.__setattr__(self, "payload", freeze_legacy_value(self.payload))

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

    def to_legacy_payload(self) -> Any:
        return thaw_legacy_value(self.payload)

    def to_legacy_trace_context(self) -> dict[str, Any]:
        return thaw_legacy_value(self.trace_context)

    @classmethod
    def resolved(cls, candidate: DocumentCandidate, payload: Any) -> "ExtractionOutcome":
        return cls(candidate=candidate, status="resolved", payload=payload)

    @classmethod
    def unresolved(
        cls, candidate: DocumentCandidate, reason_code: str, message: str = ""
    ) -> "ExtractionOutcome":
        return cls(
            candidate=candidate,
            status="unresolved",
            reason_code=reason_code,
            message=message,
        )


def _safe_failure(candidate: DocumentCandidate, exc: BaseException) -> ExtractionOutcome:
    http_status = getattr(exc, "http_status", None)
    if http_status == 402:
        status, reason = "quota_exhausted", "REMOTE_QUOTA_EXHAUSTED"
    elif http_status in {401, 403}:
        status, reason = "auth_failed", "REMOTE_AUTH_FAILED"
    elif isinstance(exc, TimeoutError):
        status, reason = "timeout", "REMOTE_TIMEOUT"
    elif isinstance(exc, PermissionError):
        status, reason = "auth_failed", "REMOTE_AUTH_FAILED"
    elif "quota" in type(exc).__name__.lower() or "quota" in str(exc).lower():
        status, reason = "quota_exhausted", "REMOTE_QUOTA_EXHAUSTED"
    else:
        status, reason = "unresolved", "REMOTE_EXTRACTION_FAILED"
    return ExtractionOutcome(
        candidate=candidate,
        status=status,
        reason_code=reason,
        message=f"{reason}:{type(exc).__name__}",
    )


class ExtractionPipeline:
    """Resolve candidates without allowing worker threads to archive or pair files."""

    def __init__(
        self,
        *,
        local_parser: Callable[[DocumentCandidate], Any],
        remote_extractor: Callable[[DocumentCandidate], Any],
        max_workers: int = 2,
        verified_ceiling: Callable[[], int] | None = None,
        stop_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
        trace_sink: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> None:
        self._local_parser = local_parser
        self._remote_extractor = remote_extractor
        self._requested_workers = max(1, int(max_workers))
        self._verified_ceiling = verified_ceiling
        self._stop_requested = stop_requested or (lambda: False)
        self._progress_callback = progress_callback
        self._trace_sink = trace_sink

    def _worker_count(self) -> int:
        ceiling = 2
        if self._verified_ceiling is not None:
            try:
                ceiling = int(self._verified_ceiling())
            except (TypeError, ValueError, OverflowError):
                ceiling = 1
        return max(1, min(self._requested_workers, max(1, ceiling), 2))

    @staticmethod
    def _coerce_result(candidate: DocumentCandidate, result: Any) -> ExtractionOutcome:
        if isinstance(result, ExtractionOutcome):
            if result.candidate.identity != candidate.identity:
                return ExtractionOutcome.unresolved(
                    candidate, "OUTCOME_IDENTITY_MISMATCH", "OUTCOME_IDENTITY_MISMATCH"
                )
            return result
        return ExtractionOutcome.resolved(candidate, result)

    def _run_remote(self, candidate: DocumentCandidate) -> Any:
        if self._stop_requested():
            return ExtractionOutcome(
                candidate=candidate,
                status="cancelled",
                reason_code="STOP_REQUESTED",
                message="STOP_REQUESTED",
            )
        return self._remote_extractor(candidate)

    def _emit_progress(self, completed: int, total: int) -> None:
        if self._progress_callback is None:
            return
        percent = 100 if total == 0 else min(100, max(0, int(completed * 100 / total)))
        try:
            self._progress_callback(completed, total, percent)
        except Exception:
            return

    def extract(self, candidates: Iterable[DocumentCandidate]) -> list[ExtractionOutcome]:
        ordered = [
            candidate
            for _original_index, candidate in sorted(
                enumerate(tuple(candidates)),
                key=lambda item: (item[1].sequence, item[0]),
            )
        ]
        total = len(ordered)
        self._emit_progress(0, total)
        outcomes: dict[int, ExtractionOutcome] = {}
        unresolved: list[tuple[int, DocumentCandidate]] = []
        completed = 0

        def record(ordinal: int, outcome: ExtractionOutcome) -> None:
            nonlocal completed
            if ordinal in outcomes:
                return
            outcomes[ordinal] = outcome
            completed += 1
            if self._trace_sink is not None:
                try:
                    self._trace_sink(
                        {
                            "document_id": outcome.candidate.identity.document_id,
                            "sequence": outcome.candidate.sequence,
                            "status": outcome.status,
                            "reason_code": outcome.reason_code,
                        }
                    )
                except Exception:
                    pass
            self._emit_progress(completed, total)

        def stopped(candidate: DocumentCandidate) -> ExtractionOutcome:
            return ExtractionOutcome(
                candidate=candidate,
                status="cancelled",
                reason_code="STOP_REQUESTED",
                message="STOP_REQUESTED",
            )

        def propagate_terminal(
            candidate: DocumentCandidate, terminal: ExtractionOutcome
        ) -> ExtractionOutcome:
            return ExtractionOutcome(
                candidate=candidate,
                status=terminal.status,
                reason_code=terminal.reason_code,
                message=terminal.reason_code,
            )

        for ordinal, candidate in enumerate(ordered):
            if self._stop_requested():
                record(ordinal, stopped(candidate))
                continue
            try:
                local_result = self._local_parser(candidate)
            except Exception as exc:
                record(ordinal, _safe_failure(candidate, exc))
                continue
            if isinstance(local_result, ExtractionOutcome) or local_result is not None:
                record(ordinal, self._coerce_result(candidate, local_result))
            else:
                unresolved.append((ordinal, candidate))

        if self._stop_requested():
            for ordinal, candidate in unresolved:
                record(ordinal, stopped(candidate))
            unresolved = []

        breaker: ExtractionOutcome | None = None
        position = 0
        while position < len(unresolved):
            if breaker is not None:
                for ordinal, candidate in unresolved[position:]:
                    record(ordinal, propagate_terminal(candidate, breaker))
                break

            ordinal, candidate = unresolved[position]
            if not candidate.parallel_safe:
                if self._stop_requested():
                    outcome = stopped(candidate)
                else:
                    try:
                        outcome = self._coerce_result(
                            candidate, self._remote_extractor(candidate)
                        )
                    except Exception as exc:
                        outcome = _safe_failure(candidate, exc)
                record(ordinal, outcome)
                if outcome.status in {"quota_exhausted", "auth_failed"}:
                    breaker = outcome
                position += 1
                continue

            segment: list[tuple[int, DocumentCandidate]] = []
            while position < len(unresolved) and unresolved[position][1].parallel_safe:
                segment.append(unresolved[position])
                position += 1

            queued = deque(segment)
            with ThreadPoolExecutor(
                max_workers=self._worker_count(), thread_name_prefix="invoice-extract"
            ) as executor:
                active: dict[Future[Any], tuple[int, DocumentCandidate]] = {}

                def fill_slots() -> None:
                    while queued and len(active) < self._worker_count() and breaker is None:
                        item_ordinal, item_candidate = queued.popleft()
                        if self._stop_requested():
                            record(item_ordinal, stopped(item_candidate))
                            continue
                        try:
                            future = executor.submit(self._run_remote, item_candidate)
                        except Exception as exc:
                            record(item_ordinal, _safe_failure(item_candidate, exc))
                            continue
                        active[future] = (item_ordinal, item_candidate)

                fill_slots()
                while active:
                    finished, _pending = wait(tuple(active), return_when=FIRST_COMPLETED)
                    for future in sorted(
                        finished, key=lambda item: active[item][0]
                    ):
                        item_ordinal, item_candidate = active.pop(future)
                        try:
                            outcome = self._coerce_result(
                                item_candidate, future.result()
                            )
                        except Exception as exc:
                            outcome = _safe_failure(item_candidate, exc)
                        record(item_ordinal, outcome)
                        if outcome.status in {"quota_exhausted", "auth_failed"}:
                            breaker = outcome
                    if breaker is not None:
                        while queued:
                            item_ordinal, item_candidate = queued.popleft()
                            record(
                                item_ordinal,
                                propagate_terminal(item_candidate, breaker),
                            )
                        for future, (item_ordinal, item_candidate) in tuple(active.items()):
                            if future.cancel():
                                active.pop(future)
                                record(
                                    item_ordinal,
                                    propagate_terminal(item_candidate, breaker),
                                )
                    else:
                        fill_slots()

        final: list[ExtractionOutcome] = []
        for ordinal, candidate in enumerate(ordered):
            outcome = outcomes.get(ordinal)
            if outcome is None:
                outcome = ExtractionOutcome.unresolved(
                    candidate, "MISSING_TERMINAL_OUTCOME", "MISSING_TERMINAL_OUTCOME"
                )
                record(ordinal, outcome)
            final.append(outcome)
        return final


class SharedRuntimeRemoteExtractor:
    """Worker-local extractor state over one run-owned GLM runtime."""

    def __init__(
        self,
        *,
        owner_extractor: Any,
        sidecar: dict[str, dict[str, Any]],
        sidecar_lock: Any,
        worker_factory: Callable[[Any], Any],
        custom_rules: str = "",
        since_date: str | None = None,
        before_date: str | None = None,
    ) -> None:
        self.owner_extractor = owner_extractor
        self.sidecar = sidecar
        self.sidecar_lock = sidecar_lock
        self.worker_factory = worker_factory
        self.custom_rules = custom_rules
        self.since_date = since_date
        self.before_date = before_date

    def verified_ceiling(self) -> int:
        profiles = getattr(
            getattr(self.owner_extractor, "glm_runtime", None), "profiles", {}
        ) or {}
        ceilings = [
            int(getattr(profile, "max_concurrency", 1) or 1)
            for profile in profiles.values()
        ]
        return max(1, min(2, max(ceilings, default=1)))

    def __call__(self, candidate: DocumentCandidate) -> ExtractionOutcome:
        with self.sidecar_lock:
            prepared = dict(self.sidecar[candidate.identity.document_id])
        worker = None
        try:
            worker = self.worker_factory(
                getattr(self.owner_extractor, "glm_runtime", None)
            )
            info_json = worker.extract_remote_only(
                prepared["base64_img"],
                custom_rules=self.custom_rules,
                pdf_path=prepared["pdf_path"],
                document_context={
                    **prepared["metadata"],
                    "search_since_date": self.since_date or "",
                    "search_before_date": self.before_date or "",
                },
            )
            if not info_json:
                return ExtractionOutcome(
                    candidate=candidate,
                    status="manual_review",
                    reason_code="EXTRACTOR_ALL_ENGINES_FAILED",
                    message="EXTRACTOR_ALL_ENGINES_FAILED",
                    artifact_path=prepared["pdf_path"],
                )
            return ExtractionOutcome.resolved(
                candidate,
                {
                    "pdf_path": prepared["pdf_path"],
                    "metadata": prepared["metadata"],
                    "info_json": info_json,
                    "extraction_trace": copy.deepcopy(
                        getattr(worker, "last_extraction_trace", {}) or {}
                    ),
                    "extraction_timing": copy.deepcopy(
                        getattr(worker, "last_timing_trace", {}) or {}
                    ),
                },
            )
        finally:
            close_worker = getattr(worker, "close", None)
            if callable(close_worker):
                try:
                    close_worker()
                except Exception:
                    pass
