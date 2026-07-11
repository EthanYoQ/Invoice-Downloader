from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from extraction_pipeline import ExtractionOutcome


@dataclass(frozen=True)
class ArchivedOutcome:
    outcome: ExtractionOutcome
    archive_path: str = ""
    duplicate: bool = False


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
        return self.unresolved_count == 0 and self.manual_count == 0


class ArchiveService:
    """Serialize archive side effects and make them idempotent by document identity."""

    def __init__(
        self,
        *,
        writer: Callable[[ExtractionOutcome, Path], str],
        event_sink: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._writer = writer
        self._event_sink = event_sink
        self._archived_ids: set[str] = set()

    def delegate_batch(
        self,
        outcomes: Iterable[ExtractionOutcome],
        output_root: str | Path,
        batch_writer: Callable[[list[dict[str, Any]]], Any],
    ) -> Any:
        """Bridge the immutable pipeline to the existing all-at-once archive contract.

        The legacy writer still owns its exact naming, pairing, retention, and event
        behavior. This bridge only restores original sequence and refuses to hide a
        non-resolved candidate before handing control to it.
        """
        del output_root
        ordered = sorted(tuple(outcomes), key=lambda item: item.candidate.sequence)
        legacy_rows: list[dict[str, Any]] = []
        for outcome in ordered:
            if outcome.status != "resolved" or not isinstance(outcome.payload, Mapping):
                raise RuntimeError(
                    f"PIPELINE_BATCH_UNRESOLVED:{outcome.candidate.identity.document_id}:"
                    f"{outcome.reason_code or outcome.status}"
                )
            legacy_rows.append(dict(outcome.payload))
        return batch_writer(legacy_rows)

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
                    archive_path = str(self._writer(outcome, root) or "")
                except Exception as exc:
                    outcome = ExtractionOutcome.unresolved(
                        outcome.candidate,
                        "ARCHIVE_WRITE_FAILED",
                        f"ARCHIVE_WRITE_FAILED:{type(exc).__name__}",
                    )
                    unresolved_count += 1
                else:
                    self._archived_ids.add(document_id)
                    archived_count += 1
            elif outcome.status == "retained":
                self._archived_ids.add(document_id)
                retained_count += 1
            elif outcome.status == "manual_review":
                self._archived_ids.add(document_id)
                manual_count += 1
            else:
                unresolved_count += 1

            archived_outcome = ArchivedOutcome(outcome=outcome, archive_path=archive_path)
            archived.append(archived_outcome)
            if self._event_sink is not None:
                self._event_sink(
                    {
                        "document_id": document_id,
                        "sequence": outcome.candidate.sequence,
                        "status": outcome.status,
                        "archive_path": archive_path,
                    }
                )

        return ArchiveReport(
            outcomes=tuple(archived),
            archived_count=archived_count,
            retained_count=retained_count,
            manual_count=manual_count,
            unresolved_count=unresolved_count,
            duplicate_count=duplicate_count,
        )
