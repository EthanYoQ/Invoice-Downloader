from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Callable, Iterable, Mapping

from candidate_pipeline import DocumentCandidate


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
        object.__setattr__(self, "trace_context", MappingProxyType(dict(self.trace_context)))

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL_STATUSES

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
    if isinstance(exc, TimeoutError):
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

        for ordinal, candidate in enumerate(ordered):
            if self._stop_requested():
                outcomes[ordinal] = ExtractionOutcome(
                    candidate=candidate,
                    status="cancelled",
                    reason_code="STOP_REQUESTED",
                    message="STOP_REQUESTED",
                )
                continue
            try:
                local_result = self._local_parser(candidate)
            except Exception as exc:
                outcomes[ordinal] = _safe_failure(candidate, exc)
                continue
            if isinstance(local_result, ExtractionOutcome) or local_result is not None:
                outcomes[ordinal] = self._coerce_result(candidate, local_result)
            else:
                unresolved.append((ordinal, candidate))

        safe = [item for item in unresolved if item[1].parallel_safe]
        unsafe = [item for item in unresolved if not item[1].parallel_safe]
        if self._stop_requested():
            safe, cancelled = [], safe
            unsafe, cancelled_unsafe = [], unsafe
            for ordinal, candidate in (*cancelled, *cancelled_unsafe):
                outcomes[ordinal] = ExtractionOutcome(
                    candidate=candidate,
                    status="cancelled",
                    reason_code="STOP_REQUESTED",
                    message="STOP_REQUESTED",
                )

        if safe:
            with ThreadPoolExecutor(
                max_workers=self._worker_count(), thread_name_prefix="invoice-extract"
            ) as executor:
                future_candidates: dict[Future[Any], tuple[int, DocumentCandidate]] = {}
                for ordinal, candidate in safe:
                    if self._stop_requested():
                        outcomes[ordinal] = ExtractionOutcome(
                            candidate=candidate,
                            status="cancelled",
                            reason_code="STOP_REQUESTED",
                            message="STOP_REQUESTED",
                        )
                        continue
                    future_candidates[executor.submit(self._run_remote, candidate)] = (
                        ordinal,
                        candidate,
                    )
                for future in as_completed(future_candidates):
                    ordinal, candidate = future_candidates[future]
                    try:
                        outcomes[ordinal] = self._coerce_result(candidate, future.result())
                    except Exception as exc:
                        outcomes[ordinal] = _safe_failure(candidate, exc)

        for ordinal, candidate in unsafe:
            if self._stop_requested():
                outcomes[ordinal] = ExtractionOutcome(
                    candidate=candidate,
                    status="cancelled",
                    reason_code="STOP_REQUESTED",
                    message="STOP_REQUESTED",
                )
                continue
            try:
                outcomes[ordinal] = self._coerce_result(
                    candidate, self._remote_extractor(candidate)
                )
            except Exception as exc:
                outcomes[ordinal] = _safe_failure(candidate, exc)

        final: list[ExtractionOutcome] = []
        for ordinal, candidate in enumerate(ordered):
            outcome = outcomes.get(ordinal)
            if outcome is None:
                outcome = ExtractionOutcome.unresolved(
                    candidate, "MISSING_TERMINAL_OUTCOME", "MISSING_TERMINAL_OUTCOME"
                )
            final.append(outcome)
            if self._trace_sink is not None:
                try:
                    self._trace_sink(
                        {
                            "document_id": candidate.identity.document_id,
                            "sequence": candidate.sequence,
                            "status": outcome.status,
                            "reason_code": outcome.reason_code,
                        }
                    )
                except Exception:
                    pass
            self._emit_progress(ordinal + 1, total)
        return final
