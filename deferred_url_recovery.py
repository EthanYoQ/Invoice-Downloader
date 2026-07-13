from __future__ import annotations

from collections import OrderedDict, deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import Callable, Iterable

from candidate_pipeline import DocumentCandidate
from extraction_pipeline import ExtractionOutcome, normalize_url_terminal_outcome


DEFAULT_URL_RECOVERY_MAX_WORKERS = 10
DEFAULT_STRONG_PROVIDER_MAX_WORKERS = 4


@dataclass(frozen=True)
class _RecoveryGroup:
    key: tuple[str, str]
    candidates: tuple[DocumentCandidate, ...]
    strong_provider: bool

    @property
    def first_sequence(self) -> int:
        return self.candidates[0].sequence


class DeferredUrlRecoveryScheduler:
    """Recover URL groups concurrently while keeping each provider group serial."""

    def __init__(
        self,
        max_workers: int = DEFAULT_URL_RECOVERY_MAX_WORKERS,
        max_strong_workers: int = DEFAULT_STRONG_PROVIDER_MAX_WORKERS,
        stop_requested: Callable[[], bool] | None = None,
        progress_callback: Callable[[int, int, int], None] | None = None,
    ) -> None:
        self._max_workers = max(1, int(max_workers))
        self._max_strong_workers = min(
            self._max_workers, max(1, int(max_strong_workers))
        )
        self._stop_requested = stop_requested or (lambda: False)
        self._progress_callback = progress_callback

    @staticmethod
    def _is_strong_provider(candidate: DocumentCandidate) -> bool:
        return bool(str(candidate.metadata.get("provider_family") or "").strip())

    @classmethod
    def _group_candidates(
        cls, candidates: Iterable[DocumentCandidate]
    ) -> list[_RecoveryGroup]:
        ordered = sorted(tuple(candidates), key=lambda candidate: candidate.sequence)
        grouped: OrderedDict[tuple[str, str], list[DocumentCandidate]] = OrderedDict()
        for candidate in ordered:
            provider_group = str(candidate.identity.provider_group_key or "").strip()
            key = (
                ("provider", provider_group)
                if provider_group
                else ("document", candidate.identity.document_id)
            )
            grouped.setdefault(key, []).append(candidate)

        groups = [
            _RecoveryGroup(
                key=key,
                candidates=tuple(group),
                strong_provider=any(cls._is_strong_provider(item) for item in group),
            )
            for key, group in grouped.items()
        ]
        return sorted(
            groups,
            key=lambda group: (
                0 if group.strong_provider else 1,
                group.first_sequence,
                group.key,
            ),
        )

    @staticmethod
    def _callback_failure(
        candidate: DocumentCandidate, reason_code: str
    ) -> ExtractionOutcome:
        return normalize_url_terminal_outcome(
            candidate,
            ExtractionOutcome(
                candidate=candidate,
                status="unresolved",
                reason_code=reason_code,
                message=reason_code,
                artifact_path=candidate.source_path,
            ),
        )

    @staticmethod
    def _cancelled(candidate: DocumentCandidate) -> ExtractionOutcome:
        return ExtractionOutcome(
            candidate=candidate,
            status="cancelled",
            reason_code="STOP_REQUESTED",
            message="STOP_REQUESTED",
            artifact_path=candidate.source_path,
        )

    def _recover_group(
        self,
        group: _RecoveryGroup,
        recover_one: Callable[[DocumentCandidate], ExtractionOutcome],
    ) -> list[ExtractionOutcome]:
        outcomes = []
        for candidate in group.candidates:
            if self._stop_requested():
                outcomes.append(self._cancelled(candidate))
                continue
            try:
                outcome = recover_one(candidate)
            except Exception:
                outcome = self._callback_failure(
                    candidate, "URL_RECOVERY_CALLBACK_FAILED"
                )
            if not isinstance(outcome, ExtractionOutcome):
                outcome = self._callback_failure(
                    candidate, "URL_RECOVERY_CALLBACK_INVALID_OUTCOME"
                )
            elif outcome.candidate.identity != candidate.identity:
                outcome = self._callback_failure(
                    candidate, "OUTCOME_IDENTITY_MISMATCH"
                )
            outcomes.append(outcome)
        return outcomes

    def _emit_progress(self, completed: int, total: int) -> None:
        if self._progress_callback is None:
            return
        percent = 100 if total == 0 else min(100, max(0, int(completed * 100 / total)))
        try:
            self._progress_callback(completed, total, percent)
        except Exception:
            return

    def recover(
        self,
        candidates: Iterable[DocumentCandidate],
        recover_one: Callable[[DocumentCandidate], ExtractionOutcome],
        *,
        progress_offset: int = 0,
        progress_total: int | None = None,
        emit_progress: bool = True,
    ) -> list[ExtractionOutcome]:
        groups = self._group_candidates(candidates)
        candidate_count = sum(len(group.candidates) for group in groups)
        progress_offset = max(0, int(progress_offset))
        minimum_total = progress_offset + candidate_count
        overall_total = (
            minimum_total
            if progress_total is None
            else max(minimum_total, int(progress_total))
        )
        if not groups:
            return []

        queued = deque(groups)
        outcomes: list[ExtractionOutcome] = []
        completed = 0

        def record(group_outcomes: Iterable[ExtractionOutcome]) -> None:
            nonlocal completed
            for outcome in group_outcomes:
                outcomes.append(outcome)
                completed += 1
                if emit_progress:
                    self._emit_progress(progress_offset + completed, overall_total)

        def cancel_queued() -> None:
            while queued:
                group = queued.popleft()
                record(self._cancelled(candidate) for candidate in group.candidates)

        with ThreadPoolExecutor(
            max_workers=self._max_workers, thread_name_prefix="url-recovery"
        ) as executor:
            active: dict[Future[list[ExtractionOutcome]], _RecoveryGroup] = {}

            def fill_slots() -> None:
                if self._stop_requested():
                    cancel_queued()
                    return
                while queued and len(active) < self._max_workers:
                    active_strong = sum(
                        int(group.strong_provider) for group in active.values()
                    )
                    group = None
                    if active_strong < self._max_strong_workers:
                        group = queued.popleft()
                    else:
                        for index, candidate_group in enumerate(queued):
                            if not candidate_group.strong_provider:
                                group = candidate_group
                                del queued[index]
                                break
                    if group is None:
                        break
                    if self._stop_requested():
                        record(self._cancelled(item) for item in group.candidates)
                        cancel_queued()
                        return
                    active[executor.submit(self._recover_group, group, recover_one)] = group

            fill_slots()
            while active:
                finished, _pending = wait(tuple(active), return_when=FIRST_COMPLETED)
                for future in sorted(
                    finished, key=lambda item: active[item].first_sequence
                ):
                    group = active.pop(future)
                    try:
                        group_outcomes = future.result()
                    except Exception:
                        group_outcomes = [
                            self._callback_failure(
                                candidate, "URL_RECOVERY_GROUP_FAILED"
                            )
                            for candidate in group.candidates
                        ]
                    record(group_outcomes)
                fill_slots()

        return sorted(outcomes, key=lambda outcome: outcome.candidate.sequence)
