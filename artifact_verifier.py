"""Independent final-artifact verification for strict batch admission."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import re
from pathlib import Path
from typing import Any, Mapping
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class ArtifactVerification:
    passed: bool
    manual_required: bool
    verification_mode: str
    matched_fields: tuple[str, ...] = ()
    reason_code: str = ""


@dataclass(frozen=True)
class _PdfFieldHit:
    page_index: int
    bbox: tuple[float, float, float, float]


def _text(value: Any) -> str:
    return str(value or "").strip()


def _compact(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", _text(value)).casefold()


def _name(value: Any) -> str:
    return re.sub(r"[\W_]+", "", _text(value), flags=re.UNICODE).casefold()


def _date(value: Any) -> str:
    match = re.search(r"(20\d{2})\D?(\d{1,2})\D?(\d{1,2})", _text(value))
    if not match:
        return ""
    try:
        return f"{int(match.group(1)):04d}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    except ValueError:
        return ""


def _amount(value: Any) -> Decimal | None:
    match = re.search(r"-?\d[\d,]*(?:\.\d+)?", _text(value))
    if not match:
        return None
    try:
        return Decimal(match.group(0).replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _sequence(value: Any) -> str:
    return "".join(character.casefold() for character in _text(value) if character.isalnum())


def _spatially_contiguous(words: list[Any]) -> bool:
    for previous, current in zip(words, words[1:]):
        previous_height = max(1.0, float(previous[3]) - float(previous[1]))
        current_height = max(1.0, float(current[3]) - float(current[1]))
        scale = max(previous_height, current_height)
        previous_center = (float(previous[1]) + float(previous[3])) / 2
        current_center = (float(current[1]) + float(current[3])) / 2
        if abs(current_center - previous_center) <= scale:
            horizontal_gap = float(current[0]) - float(previous[2])
            if horizontal_gap < -scale or horizontal_gap > scale * 4:
                return False
        elif float(current[1]) - float(previous[3]) > scale * 1.5:
            return False
    return True


def _word_sequence_hits(page: Any, expected: Any) -> tuple[_PdfFieldHit, ...]:
    needle = _sequence(expected)
    if not needle:
        return ()
    words = page.get_text("words", sort=True)
    indexed: list[tuple[int, int, Any]] = []
    chunks: list[str] = []
    offset = 0
    for word in words:
        normalized = _sequence(word[4])
        if not normalized:
            continue
        chunks.append(normalized)
        indexed.append((offset, offset + len(normalized), word))
        offset += len(normalized)
    joined = "".join(chunks)
    hits: list[_PdfFieldHit] = []
    start = joined.find(needle)
    while start >= 0:
        end = start + len(needle)
        if not (
            (start and joined[start - 1].isdigit() and needle[0].isdigit())
            or (end < len(joined) and joined[end].isdigit() and needle[-1].isdigit())
        ):
            selected = [word for left, right, word in indexed if right > start and left < end]
            if selected and _spatially_contiguous(selected):
                import fitz

                bbox = fitz.Rect(selected[0][:4])
                for word in selected[1:]:
                    bbox |= fitz.Rect(word[:4])
                hits.append(
                    _PdfFieldHit(
                        page_index=int(page.number),
                        bbox=(bbox.x0, bbox.y0, bbox.x1, bbox.y1),
                    )
                )
        start = joined.find(needle, start + 1)
    return tuple(hits)


def _find_hits(document: Any, expected: Any) -> tuple[_PdfFieldHit, ...]:
    hits: list[_PdfFieldHit] = []
    for page in document:
        hits.extend(_word_sequence_hits(page, expected))
    return tuple(hits)


def _render_metrics(page: Any, *, bbox: tuple[float, float, float, float] | None = None) -> tuple[int, int, int]:
    import fitz

    clip = page.rect if bbox is None else fitz.Rect(bbox)
    if clip.is_empty or clip.is_infinite or not page.rect.contains(clip):
        return 0, 255, 255
    pixmap = page.get_pixmap(
        matrix=fitz.Matrix(2, 2),
        colorspace=fitz.csGRAY,
        alpha=False,
        clip=clip,
    )
    samples = pixmap.samples
    if not samples:
        return 0, 255, 255
    return sum(value < 245 for value in samples), min(samples), max(samples)


def _document_is_visibly_nonblank(document: Any) -> bool:
    nonwhite = 0
    pixels = 0
    darkest = 255
    lightest = 0
    for page in document:
        page_nonwhite, page_min, page_max = _render_metrics(page)
        nonwhite += page_nonwhite
        pixels += int(page.rect.width * 2) * int(page.rect.height * 2)
        darkest = min(darkest, page_min)
        lightest = max(lightest, page_max)
    return (
        pixels > 0
        and nonwhite >= max(64, int(pixels * 0.0005))
        and lightest - darkest >= 16
    )


def _hit_is_visible(document: Any, hit: _PdfFieldHit) -> bool:
    if hit.page_index < 0 or hit.page_index >= document.page_count:
        return False
    page = document[hit.page_index]
    nonwhite, darkest, lightest = _render_metrics(page, bbox=hit.bbox)
    import fitz

    rect = fitz.Rect(hit.bbox)
    pixels = max(1, int(rect.width * 2) * int(rect.height * 2))
    return nonwhite >= max(8, int(pixels * 0.01)) and lightest - darkest >= 16


def _visible_hit(document: Any, expected: Any) -> _PdfFieldHit | None:
    return next(
        (hit for hit in _find_hits(document, expected) if _hit_is_visible(document, hit)),
        None,
    )


def _verify_pdf(path: Path, truth: Mapping[str, Any]) -> ArtifactVerification:
    try:
        import fitz

        document = fitz.open(path)
    except Exception:
        return _failure("FINAL_FORMAT_INVALID")
    try:
        if document.page_count <= 0:
            return _failure("FINAL_FORMAT_INVALID")
        if not _document_is_visibly_nonblank(document):
            return _failure("FINAL_VISUAL_BLANK")

        expected_number = _text(truth.get("invoice_number"))
        expected_code = _text(truth.get("invoice_code"))
        matched: list[str] = []
        if expected_number or expected_code:
            if expected_number:
                if _visible_hit(document, expected_number) is None:
                    return _failure("FINAL_FIELD_NOT_VISIBLE")
                matched.append("invoice_number")
            if expected_code:
                if _visible_hit(document, expected_code) is None:
                    return _failure("FINAL_FIELD_NOT_VISIBLE")
                matched.append("invoice_code")
        else:
            expected_date = _date(truth.get("invoice_date"))
            expected_amount = _amount(truth.get("amount"))
            if not expected_date or _visible_hit(document, expected_date) is None:
                return _failure("FINAL_QUORUM_MISMATCH")
            amount_text = format(expected_amount, "f") if expected_amount is not None else ""
            if not amount_text or _visible_hit(document, amount_text) is None:
                return _failure("FINAL_QUORUM_MISMATCH")
            matched.extend(("invoice_date", "amount"))

            supporting = False
            seller = _text(truth.get("seller"))
            if seller and _visible_hit(document, seller) is not None:
                matched.append("seller")
                supporting = True
            role = _text(truth.get("document_role"))
            if role and _visible_hit(document, role) is not None:
                matched.append("document_role")
                supporting = True
            route = truth.get("route") if isinstance(truth.get("route"), Mapping) else {}
            departure = _text(route.get("departure") or truth.get("departure"))
            destination = _text(route.get("destination") or truth.get("destination"))
            if (
                departure
                and destination
                and _visible_hit(document, departure) is not None
                and _visible_hit(document, destination) is not None
            ):
                matched.append("route")
                supporting = True
            if not supporting:
                return _failure("FINAL_QUORUM_MISMATCH")

        return ArtifactVerification(
            passed=True,
            manual_required=False,
            verification_mode="transformed_content_identity",
            matched_fields=tuple(matched),
        )
    except Exception:
        return _failure("FINAL_CONTENT_UNPARSEABLE")
    finally:
        document.close()


def _parse_xml(path: Path) -> tuple[dict[str, str] | None, str]:
    try:
        root = ET.parse(path).getroot()
    except (ET.ParseError, OSError):
        return None, "FINAL_FORMAT_INVALID"
    values: dict[str, str] = {}
    for element in root.iter():
        tag = element.tag.split("}", 1)[-1].casefold()
        value = _text(element.text)
        if value and tag not in values:
            values[tag] = value

    def first(*aliases: str) -> str:
        return next((values[name.casefold()] for name in aliases if name.casefold() in values), "")

    fields = {
        "invoice_number": first("InvoiceNumber", "InvoiceNo", "Fphm"),
        "invoice_code": first("InvoiceCode", "Fpdm"),
        "invoice_date": first("InvoiceDate", "IssueDate", "Kprq"),
        "amount": first("TotalAmount", "Amount", "Jshj", "TotalTaxIncludedAmount"),
        "seller": first("SellerName", "Seller", "Xfmc"),
        "document_role": first("DocumentRole", "DocumentType", "Type"),
        "departure": first("Departure", "From", "DepartureCity"),
        "destination": first("Destination", "To", "DestinationCity"),
    }
    if not any(fields.values()):
        return None, "FINAL_CONTENT_UNPARSEABLE"
    return fields, ""


def _failure(reason: str) -> ArtifactVerification:
    return ArtifactVerification(
        passed=False,
        manual_required=True,
        verification_mode="transformed_content_identity",
        reason_code=reason,
    )


def verify_final_artifact(
    truth: Mapping[str, Any],
    output_path: str | Path,
    *,
    output_sha256: str,
    source_chain_sha256s: list[str] | tuple[str, ...],
) -> ArtifactVerification:
    truth_hash = _text(truth.get("sha256")).lower()
    output_hash = _text(output_sha256).lower()
    source_hashes = {_text(value).lower() for value in source_chain_sha256s}
    if truth_hash and output_hash == truth_hash:
        return ArtifactVerification(
            passed=True,
            manual_required=False,
            verification_mode="unchanged_sha256",
            matched_fields=("sha256",),
        )
    if not truth_hash or truth_hash not in source_hashes:
        return _failure("SOURCE_LINEAGE_MISSING")

    path = Path(output_path)
    suffix = path.suffix.casefold()
    if suffix == ".pdf":
        return _verify_pdf(path, truth)
    if suffix == ".xml":
        fields, error = _parse_xml(path)
    else:
        return _failure("FINAL_FORMAT_UNSUPPORTED")
    if fields is None:
        return _failure(error or "FINAL_CONTENT_UNPARSEABLE")

    matched: list[str] = []
    expected_number = _compact(truth.get("invoice_number"))
    expected_code = _compact(truth.get("invoice_code"))
    if expected_number or expected_code:
        if expected_number:
            if _compact(fields.get("invoice_number")) != expected_number:
                return _failure("FINAL_IDENTITY_MISMATCH")
            matched.append("invoice_number")
        if expected_code:
            if _compact(fields.get("invoice_code")) != expected_code:
                return _failure("FINAL_IDENTITY_MISMATCH")
            matched.append("invoice_code")
    else:
        expected_date = _date(truth.get("invoice_date"))
        expected_amount = _amount(truth.get("amount"))
        if not expected_date or _date(fields.get("invoice_date")) != expected_date:
            return _failure("FINAL_QUORUM_MISMATCH")
        if expected_amount is None or _amount(fields.get("amount")) != expected_amount:
            return _failure("FINAL_QUORUM_MISMATCH")
        matched.extend(("invoice_date", "amount"))
        supporting = False
        expected_seller = _name(truth.get("seller"))
        if expected_seller and _name(fields.get("seller")) == expected_seller:
            matched.append("seller")
            supporting = True
        expected_role = _name(truth.get("document_role"))
        if expected_role and _name(fields.get("document_role")) == expected_role:
            matched.append("document_role")
            supporting = True
        route = truth.get("route") if isinstance(truth.get("route"), Mapping) else {}
        expected_departure = _name(route.get("departure") or truth.get("departure"))
        expected_destination = _name(route.get("destination") or truth.get("destination"))
        if (
            expected_departure
            and expected_destination
            and _name(fields.get("departure")) == expected_departure
            and _name(fields.get("destination")) == expected_destination
        ):
            matched.append("route")
            supporting = True
        if not supporting:
            return _failure("FINAL_QUORUM_MISMATCH")

    return ArtifactVerification(
        passed=True,
        manual_required=False,
        verification_mode="transformed_content_identity",
        matched_fields=tuple(matched),
    )
