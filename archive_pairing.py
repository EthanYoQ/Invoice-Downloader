from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime as dt
import os


@dataclass(frozen=True)
class PairRename:
    invoice_filename: str
    supporting_filename: str
    pair_label: str = ""


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


def match_ride_pairs(invoices: list[dict], itineraries: list[dict]) -> list[tuple[dict, dict]]:
    matched = []
    used_itineraries = set()
    for invoice in invoices:
        for index, itinerary in enumerate(itineraries):
            if index in used_itineraries:
                continue
            if not ride_amounts_match(invoice.get("amount"), itinerary.get("amount")):
                continue
            matched.append((invoice, itinerary))
            used_itineraries.add(index)
            break
    return matched


def match_hotel_pairs(invoices: list[dict], folios: list[dict]) -> list[tuple[dict, dict]]:
    matched = []
    used_folios = set()
    for invoice in invoices:
        for index, folio in enumerate(folios):
            if index in used_folios:
                continue
            if not hotel_amounts_match(invoice.get("amount"), folio.get("amount")):
                continue
            if not hotel_dates_match(invoice.get("date"), folio.get("date")):
                continue
            matched.append((invoice, folio))
            used_folios.add(index)
            break
    return matched


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
