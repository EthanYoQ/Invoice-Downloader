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


def _line_value(text: str, labels: tuple[str, ...]) -> str:
    names = "|".join(re.escape(label) for label in labels)
    match = re.search(
        rf"(?im)^\s*(?:{names})\s*[:：]?\s*([^\r\n]+)",
        text,
    )
    return match.group(1).strip() if match else ""


def _fields_from_text(text: str) -> dict[str, str]:
    return {
        "invoice_number": _line_value(
            text, ("Invoice Number", "Invoice No", "发票号码", "发票号")
        ),
        "invoice_code": _line_value(text, ("Invoice Code", "发票代码")),
        "invoice_date": _line_value(
            text, ("Invoice Date", "Issue Date", "开票日期", "日期")
        ),
        "amount": _line_value(
            text, ("Total Amount", "Amount", "价税合计", "合计金额", "金额")
        ),
        "seller": _line_value(text, ("Seller", "Seller Name", "销售方", "销方名称")),
        "document_role": _line_value(
            text, ("Document Role", "Document Type", "单据类型", "类型")
        ),
        "departure": _line_value(text, ("Departure", "From", "出发地")),
        "destination": _line_value(text, ("Destination", "To", "目的地")),
    }


def _parse_pdf(path: Path) -> tuple[dict[str, str] | None, str]:
    try:
        import fitz

        document = fitz.open(path)
        try:
            if document.page_count <= 0:
                return None, "FINAL_FORMAT_INVALID"
            text = "\n".join(page.get_text("text") for page in document)
        finally:
            document.close()
    except Exception:
        return None, "FINAL_FORMAT_INVALID"
    if not text.strip():
        return None, "FINAL_CONTENT_BLANK"
    fields = _fields_from_text(text)
    if not any(_text(value) for value in fields.values()):
        return None, "FINAL_CONTENT_UNPARSEABLE"
    return fields, ""


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
        fields, error = _parse_pdf(path)
    elif suffix == ".xml":
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
