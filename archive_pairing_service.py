from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Callable

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

def reconcile_archive_pairs(
    output_root: str | Path,
    event_sink: Callable[[dict[str, Any]], None] | None = None,
    trace_store: Any | None = None,
    artifact_metadata: dict[str, dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Pair and adjacently rename ride/hotel artifacts after serialized archive."""
    root = Path(output_root)
    counts = {"ride": 0, "hotel": 0}

    def metadata(path: Path) -> dict[str, Any]:
        payload = parse_archived_filename(path.name)
        payload.update(dict((artifact_metadata or {}).get(str(path), {}) or {}))
        payload.update({"path": str(path), "filename": path.name})
        return payload

    def document_id(meta: dict[str, Any]) -> str:
        explicit = str(meta.get("document_id") or "").strip()
        if explicit:
            return explicit
        if trace_store is None:
            return ""
        return str(
            trace_store.get_document_id_by_archive_target(meta.get("path")) or ""
        )

    def record_candidate(
        meta: dict[str, Any], family: str, role: str
    ) -> None:
        identifier = document_id(meta)
        if not identifier or trace_store is None:
            return
        trace_store.set_fields(
            identifier,
            combine_keys={
                "combine_type": family,
                "document_role": role,
                "date": meta.get("date", ""),
                "amount": meta.get("amount", ""),
                "seller": meta.get("seller", ""),
                "source_message_uid": meta.get("source_message_uid", ""),
                "reason_code": f"{family.upper()}_COMBINE_CANDIDATE",
            },
        )

    def record_result(
        meta: dict[str, Any], status: str, reason_code: str, **extra: Any
    ) -> None:
        identifier = document_id(meta)
        if not identifier or trace_store is None:
            return
        trace_store.set_fields(
            identifier,
            combine_result={
                "status": status,
                "reason_code": reason_code,
                **extra,
            },
        )

    @staticmethod
    def same_pairing_group(first: dict[str, Any], second: dict[str, Any]) -> bool:
        for key in (
            "source_message_uid",
            "source_email_id",
            "email_id",
            "provider_group_key",
            "pairing_group_key",
        ):
            left = str(first.get(key) or "").strip()
            right = str(second.get(key) or "").strip()
            if left and left == right:
                return True
        return False

    def pairing_required_unmatched(
        unmatched: tuple[dict, ...], counterparts: list[dict]
    ) -> bool:
        return any(
            bool(meta.get("pairing_required"))
            and any(same_pairing_group(meta, counterpart) for counterpart in counterparts)
            for meta in unmatched
        )

    def reconcile_family(
        family: str,
        invoices: list[dict[str, Any]],
        companions: list[dict[str, Any]],
        assignment: Any,
    ) -> None:
        invoice_role = "invoice"
        companion_role = "itinerary" if family == "ride" else "folio"
        for meta in invoices:
            record_candidate(meta, family, invoice_role)
        for meta in companions:
            record_candidate(meta, family, companion_role)

        ambiguous_ids = {
            identifier
            for ambiguity in assignment.ambiguities
            for identifier in ambiguity.document_ids
        }
        all_meta = invoices + companions
        for meta in all_meta:
            if document_id(meta) in ambiguous_ids:
                record_result(
                    meta,
                    "ambiguous",
                    f"{family.upper()}_COMBINE_AMBIGUOUS_ASSIGNMENT",
                    ambiguity_reason="multiple_optimal_pair_memberships",
                )
        for meta in assignment.unmatched_invoices:
            if document_id(meta) not in ambiguous_ids:
                record_result(
                    meta,
                    "not_matched",
                    f"{family.upper()}_COMBINE_NO_MATCH",
                )
        for meta in assignment.unmatched_companions:
            if document_id(meta) not in ambiguous_ids:
                record_result(
                    meta,
                    "not_matched",
                    f"{family.upper()}_COMBINE_NO_MATCH",
                )

        if assignment.ambiguities:
            raise RuntimeError(f"{family.upper()}_PAIRING_AMBIGUOUS")
        if pairing_required_unmatched(
            assignment.unmatched_invoices, companions
        ) or pairing_required_unmatched(assignment.unmatched_companions, invoices):
            raise RuntimeError(f"{family.upper()}_PAIRING_REQUIRED_UNMATCHED")

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
        ride_invoices = [
            doc
            for doc in ride_docs
            if doc.get("artifact_role") == "ride_invoice"
            or (
                not doc.get("artifact_role")
                and not is_ride_itinerary_filename(doc["filename"])
            )
        ]
        ride_itineraries = [
            doc
            for doc in ride_docs
            if doc.get("artifact_role") == "ride_itinerary"
            or (
                not doc.get("artifact_role")
                and is_ride_itinerary_filename(doc["filename"])
            )
        ]
        assignment = assign_ride_pairs(ride_invoices, ride_itineraries)
        reconcile_family("ride", ride_invoices, ride_itineraries, assignment)
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
            if doc.get("artifact_role") == "hotel_invoice"
            or (
                not doc.get("artifact_role")
                and not is_hotel_folio_filename(doc["filename"])
                and not is_hotel_order_filename(doc["filename"])
            )
        ]
        hotel_folios = [
            doc
            for doc in hotel_docs
            if doc.get("artifact_role") == "hotel_folio"
            or (
                not doc.get("artifact_role")
                and is_hotel_folio_filename(doc["filename"])
            )
        ]
        assignment = assign_hotel_pairs(hotel_invoices, hotel_folios)
        reconcile_family("hotel", hotel_invoices, hotel_folios, assignment)
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
