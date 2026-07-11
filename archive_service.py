from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable

from extraction_pipeline import ExtractionOutcome


@dataclass(frozen=True)
class ArchivedOutcome:
    outcome: ExtractionOutcome
    archive_path: str = ""
    duplicate: bool = False


@dataclass(frozen=True)
class ArchiveDecision:
    path: str = ""
    status: str = "archived"
    reason_code: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"archived", "retained", "manual_review", "unresolved"}:
            raise ValueError(f"Unsupported archive decision status: {self.status}")


@dataclass(frozen=True)
class ArchiveReport:
    outcomes: tuple[ArchivedOutcome, ...]
    archived_count: int
    retained_count: int
    manual_count: int
    unresolved_count: int
    duplicate_count: int

    @property
    def terminal_count(self) -> int:
        return len(self.outcomes)

    @property
    def can_complete(self) -> bool:
        return (
            self.unresolved_count == 0
            and self.manual_count == 0
            and self.retained_count == 0
        )


class ArchiveService:
    """Serialize archive side effects and make them idempotent by document identity."""

    def __init__(
        self,
        *,
        writer: Callable[[ExtractionOutcome, Path], str] | None = None,
        normalizer: Callable[[ExtractionOutcome], Any] | None = None,
        classifier: Callable[[Any], str] | None = None,
        archive_operation: Callable[[ExtractionOutcome, Any, str, Path], str]
        | None = None,
        dedupe_key: Callable[[ExtractionOutcome, Any], str] | None = None,
        existing_dedupe_keys: Iterable[str] = (),
        finalizer: Callable[[ArchiveReport, Path], None] | None = None,
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        if writer is None and archive_operation is None:
            raise TypeError("ArchiveService requires writer or archive_operation")
        self._writer = writer
        self._normalizer = normalizer or (lambda outcome: outcome.to_legacy_payload())
        self._classifier = classifier or (lambda _payload: "")
        self._archive_operation = archive_operation
        self._dedupe_key = dedupe_key or (
            lambda outcome, _payload: outcome.candidate.identity.document_id
        )
        self._finalizer = finalizer
        self._event_sink = event_sink
        self._archived_ids: set[str] = set()
        self._archive_keys: set[str] = {str(key) for key in existing_dedupe_keys}

    def archive(
        self, outcomes: Iterable[ExtractionOutcome], output_root: str | Path
    ) -> ArchiveReport:
        root = Path(output_root)
        ordered = sorted(tuple(outcomes), key=lambda item: item.candidate.sequence)
        archived: list[ArchivedOutcome] = []
        archived_count = retained_count = manual_count = unresolved_count = duplicate_count = 0
        seen_this_call: set[str] = set()

        for outcome in ordered:
            document_id = outcome.candidate.identity.document_id
            if document_id in self._archived_ids or document_id in seen_this_call:
                duplicate_count += 1
                archived.append(ArchivedOutcome(outcome=outcome, duplicate=True))
                continue
            seen_this_call.add(document_id)

            archive_path = outcome.artifact_path
            if outcome.status == "resolved":
                try:
                    normalized = self._normalizer(outcome)
                    category = self._classifier(normalized)
                    archive_key = str(self._dedupe_key(outcome, normalized) or document_id)
                    if archive_key in self._archive_keys:
                        duplicate_count += 1
                        archived.append(ArchivedOutcome(outcome=outcome, duplicate=True))
                        continue
                    if self._archive_operation is not None:
                        operation_result = self._archive_operation(
                            outcome, normalized, category, root
                        )
                    else:
                        operation_result = self._writer(outcome, root)
                except Exception as exc:
                    outcome = ExtractionOutcome.unresolved(
                        outcome.candidate,
                        "ARCHIVE_WRITE_FAILED",
                        f"ARCHIVE_WRITE_FAILED:{type(exc).__name__}",
                    )
                    unresolved_count += 1
                else:
                    decision = (
                        operation_result
                        if isinstance(operation_result, ArchiveDecision)
                        else ArchiveDecision(path=str(operation_result or ""))
                    )
                    archive_path = decision.path
                    if decision.status != "archived":
                        outcome = ExtractionOutcome(
                            candidate=outcome.candidate,
                            status=decision.status,
                            reason_code=decision.reason_code
                            or f"ARCHIVE_{decision.status.upper()}",
                            message=decision.reason_code
                            or f"ARCHIVE_{decision.status.upper()}",
                            artifact_path=archive_path,
                            trace_context=outcome.trace_context,
                        )
                    self._archived_ids.add(document_id)
                    self._archive_keys.add(archive_key)
                    if decision.status == "archived":
                        archived_count += 1
                    elif decision.status == "retained":
                        retained_count += 1
                    elif decision.status == "manual_review":
                        manual_count += 1
                    else:
                        unresolved_count += 1
            elif outcome.status == "retained":
                self._archived_ids.add(document_id)
                if self._archive_operation is not None:
                    try:
                        decision = self._archive_operation(outcome, None, "", root)
                        archive_path = (
                            decision.path
                            if isinstance(decision, ArchiveDecision)
                            else str(decision or archive_path)
                        )
                    except Exception:
                        unresolved_count += 1
                retained_count += 1
            elif outcome.status == "manual_review":
                self._archived_ids.add(document_id)
                if self._archive_operation is not None:
                    try:
                        decision = self._archive_operation(outcome, None, "", root)
                        archive_path = (
                            decision.path
                            if isinstance(decision, ArchiveDecision)
                            else str(decision or archive_path)
                        )
                    except Exception:
                        unresolved_count += 1
                manual_count += 1
            elif outcome.status == "duplicate":
                duplicate_count += 1
            else:
                if self._archive_operation is not None:
                    try:
                        decision = self._archive_operation(outcome, None, "", root)
                        archive_path = (
                            decision.path
                            if isinstance(decision, ArchiveDecision)
                            else str(decision or archive_path)
                        )
                    except Exception:
                        pass
                unresolved_count += 1

            archived_outcome = ArchivedOutcome(outcome=outcome, archive_path=archive_path)
            archived.append(archived_outcome)
            if self._event_sink is not None:
                try:
                    self._event_sink(
                        {
                            "document_id": document_id,
                            "sequence": outcome.candidate.sequence,
                            "status": outcome.status,
                            "archive_path": archive_path,
                        }
                    )
                except Exception:
                    pass

        report = ArchiveReport(
            outcomes=tuple(archived),
            archived_count=archived_count,
            retained_count=retained_count,
            manual_count=manual_count,
            unresolved_count=unresolved_count,
            duplicate_count=duplicate_count,
        )
        if self._finalizer is not None:
            try:
                self._finalizer(report, root)
            except Exception:
                report = ArchiveReport(
                    outcomes=report.outcomes,
                    archived_count=report.archived_count,
                    retained_count=report.retained_count,
                    manual_count=report.manual_count,
                    unresolved_count=report.unresolved_count + 1,
                    duplicate_count=report.duplicate_count,
                )
        return report
