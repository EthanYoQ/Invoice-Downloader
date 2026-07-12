from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import re
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from document_types import DocumentType, normalize_document_type


_SHANGHAI = ZoneInfo("Asia/Shanghai")
_UNKNOWN_TEXT = {
    "",
    "unknown",
    "unknowndate",
    "未知",
    "未知日期",
    "未知金额",
    "暂无",
}


@dataclass(frozen=True)
class _FrozenLegacyValue:
    kind: str
    value: Any


def _freeze_legacy(value: Any, active_containers: set[int] | None = None) -> _FrozenLegacyValue:
    if isinstance(value, bytearray):
        return _FrozenLegacyValue("bytearray", bytes(value))
    if isinstance(value, (type(None), bool, int, float, str, bytes, Decimal, date, datetime)):
        return _FrozenLegacyValue("scalar", value)

    container_kind = None
    if isinstance(value, Mapping):
        container_kind = "mapping"
    elif isinstance(value, list):
        container_kind = "list"
    elif isinstance(value, tuple):
        container_kind = "tuple"
    elif isinstance(value, set):
        container_kind = "set"
    elif isinstance(value, frozenset):
        container_kind = "frozenset"
    if container_kind is None:
        raise TypeError(f"Unsupported legacy value type: {type(value).__name__}")

    active = active_containers if active_containers is not None else set()
    container_id = id(value)
    if container_id in active:
        raise TypeError("Cyclic legacy values are not supported")
    active.add(container_id)
    try:
        if container_kind == "mapping":
            frozen = tuple(
                (_freeze_legacy(key, active), _freeze_legacy(item, active))
                for key, item in value.items()
            )
        else:
            frozen = tuple(_freeze_legacy(item, active) for item in value)
    finally:
        active.remove(container_id)
    return _FrozenLegacyValue(container_kind, frozen)


def _thaw_legacy(value: _FrozenLegacyValue) -> Any:
    if value.kind == "mapping":
        return {_thaw_legacy(key): _thaw_legacy(item) for key, item in value.value}
    if value.kind == "list":
        return [_thaw_legacy(item) for item in value.value]
    if value.kind == "tuple":
        return tuple(_thaw_legacy(item) for item in value.value)
    if value.kind == "set":
        return {_thaw_legacy(item) for item in value.value}
    if value.kind == "frozenset":
        return frozenset(_thaw_legacy(item) for item in value.value)
    if value.kind == "bytearray":
        return bytearray(value.value)
    return value.value


def parse_amount(value: Any) -> Decimal | None:
    """Parse a monetary value without introducing binary floating-point state."""
    if isinstance(value, bool):
        raise TypeError("Boolean values are not valid amounts")
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, (int, float)):
        text = str(value)
    elif isinstance(value, str):
        text = value
    else:
        return None

    normalized = str(text).strip()
    if normalized.lower().replace(" ", "") in _UNKNOWN_TEXT:
        return None
    normalized = re.sub(r"(?i)\b(?:cny|rmb)\b", "", normalized)
    normalized = normalized.replace(",", "").replace("¥", "").replace("￥", "").replace("元", "")
    normalized = re.sub(r"\s+", "", normalized)
    if not normalized:
        return None
    has_opening_parenthesis = normalized.startswith("(")
    has_closing_parenthesis = normalized.endswith(")")
    if has_opening_parenthesis or has_closing_parenthesis:
        if not (has_opening_parenthesis and has_closing_parenthesis):
            return None
        inner = normalized[1:-1]
        if not inner or "(" in inner or ")" in inner or inner.startswith(("+", "-")):
            return None
        normalized = f"-{inner}"
    elif "(" in normalized or ")" in normalized:
        return None
    try:
        amount = Decimal(normalized)
    except InvalidOperation:
        return None
    return amount if amount.is_finite() else None


def parse_local_date(value: Any) -> date | None:
    """Parse invoice dates while treating naive values as already local business time."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, datetime):
        if value.tzinfo is not None:
            value = value.astimezone(_SHANGHAI)
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(value, tz=timezone.utc).astimezone(_SHANGHAI).date()
        except (OverflowError, OSError, ValueError):
            return None
    if not isinstance(value, str):
        return None

    text = value.strip()
    if text.lower().replace(" ", "") in _UNKNOWN_TEXT:
        return None
    match = re.fullmatch(r"(\d{4})\s*(?:[-/.年]?\s*)(\d{1,2})\s*(?:[-/.月]?\s*)(\d{1,2})\s*日?", text)
    if not match:
        digits = re.sub(r"\D", "", text)
        match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})", digits)
        if not match:
            return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _format_decimal(value: Decimal | None) -> str:
    return "" if value is None else format(value, "f")


def _format_date(value: date | None) -> str:
    return "" if value is None else value.strftime("%Y%m%d")


@dataclass(frozen=True)
class DocumentIdentity:
    document_id: str
    source_message_uid: str = ""
    source_filename: str = ""
    source_locator: str = ""
    source_kind: str = ""
    provider_group_key: str = ""


@dataclass(frozen=True)
class RouteInfo:
    departure_date: date | None = None
    departure_city: str = ""
    destination_city: str = ""

    def __post_init__(self) -> None:
        if self.departure_date is not None and (
            not isinstance(self.departure_date, date) or isinstance(self.departure_date, datetime)
        ):
            raise TypeError("RouteInfo.departure_date must be date or None")


@dataclass(frozen=True)
class InvoiceRecord:
    identity: DocumentIdentity
    is_invoice: bool = True
    invoice_date: date | None = None
    purchaser: str = ""
    seller: str = ""
    amount: Decimal | None = None
    invoice_code: str = ""
    invoice_number: str = ""
    document_type: DocumentType = "其他"
    category: str = "其他"
    route: RouteInfo = field(default_factory=RouteInfo)
    flags: frozenset[str] = field(default_factory=frozenset)
    _legacy_payload: _FrozenLegacyValue | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.amount is not None and not isinstance(self.amount, Decimal):
            raise TypeError("InvoiceRecord.amount must be Decimal or None")
        if self.invoice_date is not None and (
            not isinstance(self.invoice_date, date) or isinstance(self.invoice_date, datetime)
        ):
            raise TypeError("InvoiceRecord.invoice_date must be date or None")
        object.__setattr__(self, "document_type", normalize_document_type(self.document_type))
        object.__setattr__(self, "flags", frozenset(self.flags))

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any], identity: DocumentIdentity) -> "InvoiceRecord":
        if not isinstance(payload, Mapping):
            raise TypeError("Legacy invoice payload must be a mapping")
        flags = set()
        if payload.get("_is_itinerary"):
            flags.add("itinerary")
        if payload.get("_is_folio"):
            flags.add("folio")
        if payload.get("_cwt_cancellation"):
            flags.add("cwt_cancellation")
        return cls(
            identity=identity,
            is_invoice=bool(payload.get("is_invoice", True)),
            invoice_date=parse_local_date(payload.get("Date")),
            purchaser=str(payload.get("Purchaser") or ""),
            seller=str(payload.get("Seller") or ""),
            amount=parse_amount(payload.get("Amount")),
            invoice_code=str(payload.get("InvoiceCode") or ""),
            invoice_number=str(payload.get("InvoiceNumber") or ""),
            document_type=normalize_document_type(payload.get("Type", "")),
            category=str(payload.get("category") or payload.get("Type") or "其他"),
            route=RouteInfo(
                departure_date=parse_local_date(payload.get("Departure_Date")),
                departure_city=str(payload.get("Departure_City") or ""),
                destination_city=str(payload.get("Destination_City") or ""),
            ),
            flags=frozenset(flags),
            _legacy_payload=_freeze_legacy(dict(payload)),
        )

    def to_legacy(self) -> dict[str, Any]:
        if self._legacy_payload is not None:
            return _thaw_legacy(self._legacy_payload)
        payload: dict[str, Any] = {
            "is_invoice": self.is_invoice,
            "Date": _format_date(self.invoice_date),
            "Purchaser": self.purchaser,
            "Seller": self.seller,
            "Amount": _format_decimal(self.amount),
            "InvoiceCode": self.invoice_code,
            "InvoiceNumber": self.invoice_number,
            "Type": self.document_type,
            "category": self.category,
            "Departure_Date": _format_date(self.route.departure_date),
            "Departure_City": self.route.departure_city,
            "Destination_City": self.route.destination_city,
        }
        if "itinerary" in self.flags:
            payload["_is_itinerary"] = True
        if "folio" in self.flags:
            payload["_is_folio"] = True
        if "cwt_cancellation" in self.flags:
            payload["_cwt_cancellation"] = True
        return payload


@dataclass(frozen=True)
class ArchivedArtifact:
    identity: DocumentIdentity
    role: str
    path: str = ""
    filename: str = ""
    document_type: DocumentType = "其他"
    amount: Decimal | None = None
    business_date: date | None = None
    seller: str = ""
    provider: str = ""
    merchant_tokens: frozenset[str] = field(default_factory=frozenset)
    extension: str = ""
    _legacy_payload: _FrozenLegacyValue | None = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.amount is not None and not isinstance(self.amount, Decimal):
            raise TypeError("ArchivedArtifact.amount must be Decimal or None")
        if self.business_date is not None and (
            not isinstance(self.business_date, date) or isinstance(self.business_date, datetime)
        ):
            raise TypeError("ArchivedArtifact.business_date must be date or None")
        object.__setattr__(self, "document_type", normalize_document_type(self.document_type))
        object.__setattr__(self, "merchant_tokens", frozenset(self.merchant_tokens))

    @classmethod
    def from_legacy(
        cls,
        payload: Mapping[str, Any],
        identity: DocumentIdentity,
        role: str,
        *,
        provider: str = "",
        merchant_tokens: frozenset[str] = frozenset(),
    ) -> "ArchivedArtifact":
        return cls(
            identity=identity,
            role=role,
            path=str(payload.get("path") or ""),
            filename=str(payload.get("filename") or ""),
            document_type=normalize_document_type(payload.get("type") or payload.get("Type") or ""),
            amount=parse_amount(payload.get("amount") if "amount" in payload else payload.get("Amount")),
            business_date=parse_local_date(payload.get("date") if "date" in payload else payload.get("Date")),
            seller=str(payload.get("seller") or payload.get("Seller") or ""),
            provider=provider,
            merchant_tokens=merchant_tokens,
            extension=str(payload.get("ext") or ""),
            _legacy_payload=_freeze_legacy(dict(payload)),
        )

    def to_legacy(self) -> dict[str, Any]:
        if self._legacy_payload is not None:
            return _thaw_legacy(self._legacy_payload)
        return {
            "path": self.path,
            "filename": self.filename,
            "type": self.document_type,
            "amount": _format_decimal(self.amount),
            "date": _format_date(self.business_date),
            "seller": self.seller,
            "provider": self.provider,
            "ext": self.extension,
        }
