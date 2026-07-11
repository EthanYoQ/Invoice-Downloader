from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
from typing import Any, Callable, Iterable

from archive_pairing import (
    assign_hotel_pairs,
    assign_ride_pairs,
    build_hotel_pair_renames,
    build_ride_pair_renames,
    is_hotel_folio_filename,
    is_hotel_order_filename,
    is_ride_itinerary_filename,
    parse_archived_filename,
)
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


class AppArchiveAdapter:
    """Product rule adapter used by ArchiveService's serialized owner loop."""

    def __init__(
        self,
        *,
        api: Any,
        extractor: Any,
        save_path: str,
        business_records: dict[str, Any],
        trace_store: Any,
        pairing_finalizer: Callable[[ArchiveReport, Path], None] | None = None,
    ) -> None:
        self.api = api
        self.extractor = extractor
        self.save_path = save_path
        self.business_records = business_records
        self.trace_store = trace_store
        self.pairing_finalizer = pairing_finalizer

    def normalize(self, outcome: ExtractionOutcome) -> dict[str, Any]:
        import time

        from app_api import normalize_document_type_for_archive
        from company_rules import classify_purchaser_relation
        from document_types import get_archive_folder, is_exempt_type

        payload = outcome.to_legacy_payload()
        info_json = dict(payload.get("info_json") or {})
        metadata = dict(payload.get("metadata") or {})
        file_name = os.path.basename(
            payload.get("pdf_path") or outcome.candidate.source_path
        )
        doc_type, _reason_codes = normalize_document_type_for_archive(
            info_json, file_name
        )
        info_json["Type"] = doc_type
        if not bool(info_json.get("is_invoice", True)):
            payload["archive_status"] = "retained"
        elif not is_exempt_type(doc_type):
            relation = classify_purchaser_relation(
                str(info_json.get("Purchaser") or ""),
                self.api._resolve_active_company(),
            )
            if relation == "non_target":
                info_json["Type"] = "非目标公司发票"
                message = (
                    "购买方与目标公司不匹配，已分流到非目标公司发票 "
                    "[CLASSIFIED_AS_NON_TARGET_COMPANY]"
                    if outcome.candidate.identity.source_kind == "url"
                    else (
                        "购买方与目标公司不匹配 "
                        f"({str(info_json.get('Purchaser') or '')})，已分流到非目标公司发票 "
                        "[CLASSIFIED_AS_NON_TARGET_COMPANY]"
                    )
                )
                try:
                    self.api.logs.append(
                        {
                            "time": time.strftime("[%H:%M:%S]"),
                            "type": "拦截:",
                            "color": "text-yellow-400",
                            "msg": message,
                        }
                    )
                except Exception:
                    pass
            elif relation == "unknown":
                payload["archive_status"] = "manual_review"
        if int(metadata.get("tier", 0) or 0) == 3:
            payload["archive_status"] = "manual_review"
        if info_json.get("_cwt_cancellation"):
            payload["archive_status"] = "manual_review"
        payload["info_json"] = info_json
        payload["category"] = get_archive_folder(info_json.get("Type", "其他"))
        return payload

    @staticmethod
    def classify(payload: dict[str, Any]) -> str:
        return str(payload.get("category") or "其他")

    @staticmethod
    def dedupe_key(outcome: ExtractionOutcome, payload: dict[str, Any]) -> str:
        from app_api import build_business_record_key

        info_json = payload.get("info_json") or {}
        code = str(info_json.get("InvoiceCode") or "").strip()
        number = str(info_json.get("InvoiceNumber") or "").strip()
        if code or number:
            return build_business_record_key(
                code, number, info_json, outcome.candidate.source_filename
            )
        return outcome.candidate.identity.document_id

    def archive_operation(
        self,
        outcome: ExtractionOutcome,
        payload: dict[str, Any] | None,
        category: str,
        root: Path,
    ) -> ArchiveDecision:
        del category, root
        from app_api import record_business_success
        from document_types import MANUAL_REVIEW_FOLDER, get_archive_folder

        metadata = (
            dict(payload.get("metadata") or {})
            if isinstance(payload, dict)
            else outcome.candidate.to_legacy()
        )
        source_path = (
            str(payload.get("pdf_path") or outcome.candidate.source_path)
            if isinstance(payload, dict)
            else outcome.candidate.source_path
        )
        if outcome.status != "resolved":
            if outcome.status == "manual_review":
                path = self.api._send_to_manual_check(
                    self.save_path,
                    source_path,
                    outcome.reason_code or "PIPELINE_MANUAL_REVIEW",
                    metadata=metadata,
                    is_url=outcome.candidate.identity.source_kind == "url",
                )
                return ArchiveDecision(path=path, status="manual_review")
            path = self.api._retain_artifact(
                self.save_path,
                source_path,
                f"pipeline_{outcome.status}",
                outcome.reason_code or outcome.status,
                metadata,
            )
            if outcome.status == "quota_exhausted":
                self.api._mark_quota_exhausted("GLM API 额度不足")
            return ArchiveDecision(path=path, status="unresolved")

        archive_status = str(payload.get("archive_status") or "")
        if archive_status == "manual_review":
            path = self.api._send_to_manual_check(
                self.save_path,
                source_path,
                "PIPELINE_MANUAL_REVIEW",
                metadata=metadata,
                is_url=outcome.candidate.identity.source_kind == "url",
            )
            return ArchiveDecision(path=path, status="manual_review")
        if archive_status == "retained":
            path = self.api._retain_artifact(
                self.save_path,
                source_path,
                "pipeline_retained",
                "PIPELINE_RETAINED",
                metadata,
            )
            return ArchiveDecision(path=path, status="retained")

        info_json = payload["info_json"]
        mapped_folder = get_archive_folder(info_json.get("Type", ""))
        custom_rules = (
            {info_json.get("Type", ""): mapped_folder}
            if mapped_folder != info_json.get("Type", "")
            else None
        )
        success, path = self.extractor.route_and_rename_file(
            source_path, info_json, custom_rules=custom_rules
        )
        if not success:
            retained = self.api._retain_artifact(
                self.save_path,
                source_path,
                "archive_failures",
                "ARCHIVE_STAGE_FAILED",
                metadata,
            )
            return ArchiveDecision(path=retained, status="unresolved")
        naming_trace = getattr(self.extractor, "last_route_trace", {}) or {}
        if MANUAL_REVIEW_FOLDER in str(path) or naming_trace.get("used_manual_check"):
            return ArchiveDecision(path=path, status="manual_review")
        code = str(info_json.get("InvoiceCode") or "").strip()
        number = str(info_json.get("InvoiceNumber") or "").strip()
        if code or number:
            record_business_success(
                self.business_records,
                code,
                number,
                info_json,
                os.path.basename(path),
            )
        self.trace_store.set_fields(
            outcome.candidate.identity.document_id,
            normalized_fields=info_json,
            classification_result={"status": "classified", "category": mapped_folder},
            naming_result=dict(naming_trace) or {"status": "archived"},
            archive_target=path,
        )
        self.api._safe_emit_artifact_event(
            "archived",
            path,
            document_id=outcome.candidate.identity.document_id,
            category=mapped_folder,
        )
        self.api.stats["invoices"] = int(self.api.stats.get("invoices", 0)) + 1
        return ArchiveDecision(path=path, status="archived")

    def finalize(self, report: ArchiveReport, root: Path) -> None:
        if self.pairing_finalizer is not None:
            self.pairing_finalizer(report, root)
            return
        reconcile_archive_pairs(
            root,
            event_sink=lambda event: self.api._safe_emit_stage_event(
                "archive_pairing", "result", event
            ),
            trace_store=self.trace_store,
        )
        self.api._cwt_cancellation_matching(str(root))


def reconcile_archive_pairs(
    output_root: str | Path,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    trace_store: Any | None = None,
) -> dict[str, int]:
    """Pair and adjacently rename ride/hotel artifacts after serialized archive."""
    root = Path(output_root)
    counts = {"ride": 0, "hotel": 0}

    def metadata(path: Path) -> dict[str, Any]:
        payload = parse_archived_filename(path.name)
        payload.update({"path": str(path), "filename": path.name})
        return payload

    def validate_targets(*moves: tuple[dict[str, Any], Path]) -> None:
        for source, target in moves:
            if Path(source["path"]) != target and target.exists():
                raise RuntimeError("ARCHIVE_PAIR_TARGET_COLLISION")

    def move_pair(
        family: str,
        pair_index: int,
        first: tuple[dict[str, Any], Path, str],
        second: tuple[dict[str, Any], Path, str],
    ) -> None:
        validate_targets((first[0], first[1]), (second[0], second[1]))
        for source, target, role in (first, second):
            source_path = str(source["path"])
            os.replace(source_path, target)
            if trace_store is None:
                continue
            document_id = trace_store.get_document_id_by_archive_target(source_path)
            trace_store.move_archive_target(source_path, str(target))
            if document_id:
                counterpart = second[1] if source is first[0] else first[1]
                trace_store.set_fields(
                    document_id,
                    combine_keys={
                        "combine_type": family,
                        "document_role": role,
                        "reason_code": f"{family.upper()}_COMBINE_CANDIDATE",
                    },
                    combine_result={
                        "status": "matched",
                        "reason_code": f"{family.upper()}_COMBINE_MATCHED",
                        "paired_with": counterpart.name,
                        "pair_index": pair_index,
                        "final_filename": target.name,
                    },
                    archive_target=str(target),
                )

    def emit(event: dict[str, Any]) -> None:
        if event_sink is None:
            return
        try:
            event_sink(event)
        except Exception:
            pass

    ride_dir = root / "打车"
    if ride_dir.is_dir():
        ride_docs = [metadata(path) for path in sorted(ride_dir.iterdir()) if path.is_file()]
        ride_invoices = [doc for doc in ride_docs if not is_ride_itinerary_filename(doc["filename"])]
        ride_itineraries = [doc for doc in ride_docs if is_ride_itinerary_filename(doc["filename"])]
        assignment = assign_ride_pairs(ride_invoices, ride_itineraries)
        if assignment.ambiguities:
            raise RuntimeError("RIDE_PAIRING_AMBIGUOUS")
        for index, (invoice, itinerary) in enumerate(assignment.pairs, start=1):
            rename = build_ride_pair_renames(invoice, itinerary, index)
            invoice_target = ride_dir / rename.invoice_filename
            itinerary_target = ride_dir / rename.supporting_filename
            move_pair(
                "ride",
                index,
                (invoice, invoice_target, "invoice"),
                (itinerary, itinerary_target, "itinerary"),
            )
            counts["ride"] += 1
            emit({"family": "ride", "pair_index": index, "status": "paired"})

    hotel_dir = root / "住宿发票"
    if hotel_dir.is_dir():
        hotel_docs = [metadata(path) for path in sorted(hotel_dir.iterdir()) if path.is_file()]
        hotel_invoices = [
            doc
            for doc in hotel_docs
            if not is_hotel_folio_filename(doc["filename"])
            and not is_hotel_order_filename(doc["filename"])
        ]
        hotel_folios = [doc for doc in hotel_docs if is_hotel_folio_filename(doc["filename"])]
        assignment = assign_hotel_pairs(hotel_invoices, hotel_folios)
        if assignment.ambiguities:
            raise RuntimeError("HOTEL_PAIRING_AMBIGUOUS")
        for index, (invoice, folio) in enumerate(assignment.pairs, start=1):
            rename = build_hotel_pair_renames(invoice, folio, index)
            invoice_target = hotel_dir / rename.invoice_filename
            folio_target = hotel_dir / rename.supporting_filename
            move_pair(
                "hotel",
                index,
                (invoice, invoice_target, "invoice"),
                (folio, folio_target, "folio"),
            )
            counts["hotel"] += 1
            emit({"family": "hotel", "pair_index": index, "status": "paired"})
    return counts
