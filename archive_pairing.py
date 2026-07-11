from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime as dt
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
import re

from pairing_engine import PairingAmbiguity, PairingDocument, pair_documents


@dataclass(frozen=True)
class PairRename:
    invoice_filename: str
    supporting_filename: str
    pair_label: str = ""


@dataclass(frozen=True)
class ArchivePairingResult:
    pairs: tuple[tuple[dict, dict], ...]
    unmatched_invoices: tuple[dict, ...]
    unmatched_companions: tuple[dict, ...]
    ambiguities: tuple[PairingAmbiguity, ...]


def parse_archived_filename(filename: str) -> dict:
    """Parse the current archive naming shape into pairing metadata."""
    name, ext = os.path.splitext(filename)
    parts = name.split("_")
    if len(parts) >= 4:
        return {
            "date": parts[0],
            "type": parts[1],
            "amount": parts[2],
            "seller": "_".join(parts[3:]),
            "ext": ext,
        }
    return {
        "date": parts[0] if parts else "",
        "type": "",
        "amount": "",
        "seller": "",
        "ext": ext,
    }


def is_ride_itinerary_filename(filename: str) -> bool:
    return any(token in filename for token in ("行程单", "行程报销单", "报销单"))


def is_hotel_order_filename(filename: str) -> bool:
    return any(token in filename for token in ("确认单", "行程单"))


def is_hotel_folio_filename(filename: str) -> bool:
    lowered = filename.lower()
    return any(token in lowered for token in ("水单", "folio", "账单", "明细"))


def _float_amount(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def ride_amounts_match(invoice_amount, itinerary_amount) -> bool:
    inv_amt = _float_amount(invoice_amount)
    itn_amt = _float_amount(itinerary_amount)
    if inv_amt is None or itn_amt is None:
        return False
    return (
        abs(inv_amt - itn_amt) < 0.01
        or abs(inv_amt * 1.03 - itn_amt) < 0.50
        or abs(itn_amt * 1.03 - inv_amt) < 0.50
    )


def hotel_amounts_match(invoice_amount, folio_amount) -> bool:
    inv_amt = _float_amount(invoice_amount)
    fol_amt = _float_amount(folio_amount)
    return inv_amt is not None and fol_amt is not None and abs(inv_amt - fol_amt) <= 0.01


def hotel_dates_match(invoice_date: str, folio_date: str, tolerance_days: int = 3) -> bool:
    try:
        inv_d = dt.strptime(str(invoice_date or ""), "%Y%m%d").date()
        fol_d = dt.strptime(str(folio_date or ""), "%Y%m%d").date()
    except (TypeError, ValueError):
        return True
    return abs((inv_d - fol_d).days) <= tolerance_days


def _decimal_amount(value) -> Decimal | None:
    cleaned = str(value or "").replace(",", "").replace("¥", "").replace("￥", "").replace("元", "").strip()
    if not cleaned:
        return None
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _business_date(value) -> date | None:
    cleaned = re.sub(r"[^0-9]", "", str(value or ""))
    if len(cleaned) != 8:
        return None
    try:
        return dt.strptime(cleaned, "%Y%m%d").date()
    except ValueError:
        return None


def _provider(meta: dict) -> str:
    explicit = str(meta.get("provider") or "").strip().lower()
    if explicit:
        return explicit
    combined = " ".join(
        str(meta.get(key) or "") for key in ("provider_family", "seller", "filename", "path")
    ).lower()
    if any(token in combined for token in ("高德", "gaode", "amap", "约车", "盛智")):
        return "gaode"
    if any(token in combined for token in ("滴滴", "didi")):
        return "didi"
    return ""


def _merchant_tokens(meta: dict) -> frozenset[str]:
    text = " ".join(str(meta.get(key) or "") for key in ("seller", "merchant"))
    return frozenset(token.lower() for token in re.findall(r"[A-Za-z0-9]+|[\u4e00-\u9fff]+", text) if token)


def _archive_document_id(meta: dict, role: str) -> str:
    for key in ("document_id", "id", "path", "filename"):
        value = str(meta.get(key) or "").strip()
        if value:
            return value
    amount = _decimal_amount(meta.get("amount"))
    business_date = _business_date(meta.get("date"))
    payload = {
        "role": role,
        "amount": format(amount, "f") if amount is not None else "",
        "business_date": business_date.isoformat() if business_date else "",
        "provider": _provider(meta),
        "merchant_tokens": sorted(_merchant_tokens(meta)),
        "source_message_uid": str(
            meta.get("source_message_uid") or meta.get("source_email_id") or meta.get("email_id") or ""
        ).strip(),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return f"canonical:{hashlib.sha256(canonical.encode('utf-8')).hexdigest()}"


def _pairing_document(meta: dict, role: str) -> PairingDocument:
    return PairingDocument(
        id=_archive_document_id(meta, role),
        role=role,
        amount=_decimal_amount(meta.get("amount")),
        business_date=_business_date(meta.get("date")),
        provider=_provider(meta),
        merchant_tokens=_merchant_tokens(meta),
        source_message_uid=str(
            meta.get("source_message_uid") or meta.get("source_email_id") or meta.get("email_id") or ""
        ).strip(),
        path=str(meta.get("path") or ""),
    )


def _assign_archive_pairs(family: str, invoices: list[dict], companions: list[dict]) -> ArchivePairingResult:
    invoice_role = "ride_invoice" if family == "ride" else "hotel_invoice"
    companion_role = "ride_itinerary" if family == "ride" else "hotel_folio"
    invoice_documents = tuple(_pairing_document(meta, invoice_role) for meta in invoices)
    companion_documents = tuple(_pairing_document(meta, companion_role) for meta in companions)
    invoice_by_id = {document.id: meta for document, meta in zip(invoice_documents, invoices)}
    companion_by_id = {document.id: meta for document, meta in zip(companion_documents, companions)}
    result = pair_documents(family, invoice_documents, companion_documents)
    return ArchivePairingResult(
        pairs=tuple((invoice_by_id[invoice.id], companion_by_id[companion.id]) for invoice, companion in result.pairs),
        unmatched_invoices=tuple(invoice_by_id[document.id] for document in result.unmatched_invoices),
        unmatched_companions=tuple(companion_by_id[document.id] for document in result.unmatched_companions),
        ambiguities=result.ambiguities,
    )


def assign_ride_pairs(invoices: list[dict], itineraries: list[dict]) -> ArchivePairingResult:
    return _assign_archive_pairs("ride", invoices, itineraries)


def assign_hotel_pairs(invoices: list[dict], folios: list[dict]) -> ArchivePairingResult:
    return _assign_archive_pairs("hotel", invoices, folios)


def match_ride_pairs(invoices: list[dict], itineraries: list[dict]) -> list[tuple[dict, dict]]:
    return list(assign_ride_pairs(invoices, itineraries).pairs)


def match_hotel_pairs(invoices: list[dict], folios: list[dict]) -> list[tuple[dict, dict]]:
    return list(assign_hotel_pairs(invoices, folios).pairs)


def _format_amount(value) -> str:
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return str(value or "")


def _ride_platform(invoice_filename: str, itinerary_filename: str) -> str:
    combined = f"{invoice_filename} {itinerary_filename}"
    if any(token in combined for token in ("高德", "约车", "盛智")):
        return "高德"
    return "滴滴"


def build_ride_pair_renames(invoice: dict, itinerary: dict, pair_index: int) -> PairRename:
    mmdd = invoice.get("date", "")[4:8] if len(invoice.get("date", "")) >= 8 else invoice.get("date", "")
    base_amount = _format_amount(itinerary.get("amount"))
    platform = _ride_platform(invoice.get("filename", ""), itinerary.get("filename", ""))
    return PairRename(
        invoice_filename=f"{mmdd}-{platform}-{pair_index:02d}-发票_{base_amount}元{invoice.get('ext', '')}",
        supporting_filename=f"{mmdd}-{platform}-{pair_index:02d}-行程单_{base_amount}元{itinerary.get('ext', '')}",
        pair_label=platform,
    )


def build_hotel_pair_renames(invoice: dict, folio: dict, pair_index: int) -> PairRename:
    base_date = invoice.get("date", "")
    base_amount = _format_amount(invoice.get("amount"))
    return PairRename(
        invoice_filename=f"{base_date}-住宿-{pair_index:02d}-发票_{base_amount}元{invoice.get('ext', '')}",
        supporting_filename=f"{base_date}-住宿-{pair_index:02d}-水单_{base_amount}元{folio.get('ext', '')}",
        pair_label="住宿",
    )
