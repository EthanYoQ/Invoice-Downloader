"""Independent, standard-library-only contracts for finalized truth manifests."""

from __future__ import annotations

import copy
import datetime as dt
import json
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_CHANNELS = {"qq.com": "qq", "163.com": "163"}
_PAIR_ROLES = {
    "invoice": "invoice",
    "hotel_folio": "companion",
    "itinerary": "companion",
}


class TruthContractError(ValueError):
    """A deterministic, machine-classifiable truth contract failure."""

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _date(value: Any, *, code: str) -> dt.date:
    try:
        return dt.date.fromisoformat(_text(value))
    except ValueError as exc:
        raise TruthContractError(code, _text(value)) from exc


def _mail_date(value: Any, *, code: str) -> dt.datetime:
    text = _text(value)
    try:
        return dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise TruthContractError(code, text) from exc


def _amount(value: Any, *, required: bool) -> Decimal | None:
    text = _text(value).replace(",", "")
    if not text and not required:
        return None
    try:
        amount = Decimal(text)
    except InvalidOperation as exc:
        raise TruthContractError("invalid_amount", text) from exc
    if not amount.is_finite() or amount < 0:
        raise TruthContractError("invalid_amount", text)
    try:
        return amount.quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise TruthContractError("invalid_amount", text) from exc


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return copy.deepcopy(value)


def _require_text(row: Mapping[str, Any], field: str, code: str) -> str:
    value = _text(row.get(field))
    if not value:
        raise TruthContractError(code, field)
    return value


@dataclass(frozen=True)
class TruthRow:
    decision: str
    truth_id: str
    source_email_id: str
    source_kind: str
    file_name: str
    source_url: str
    document_role: str
    truth_type: str
    expected_category: str
    invoice_date: dt.date | None
    mail_date_local: dt.datetime
    seller: str
    purchaser: str
    amount: Decimal | None
    invoice_code: str
    invoice_number: str
    artifact_sha256: str
    pair_key: str
    raw: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, decision: str, index: int) -> "TruthRow":
        if not isinstance(value, Mapping):
            raise TruthContractError("invalid_truth_row", f"{decision}[{index}]")
        if decision not in {"included", "excluded", "pending_review"}:
            raise TruthContractError("invalid_truth_decision", decision)

        declared = _text(value.get("truth_status") or value.get("status"))
        if decision == "included" and declared != "included":
            raise TruthContractError("invalid_truth_decision")
        if declared == "suspected":
            raise TruthContractError("invalid_truth_decision")

        truth_id = _text(value.get("truth_id"))
        if decision == "included" and not truth_id:
            raise TruthContractError("missing_truth_id", f"included[{index}]")
        source_email_id = _text(value.get("source_email_id") or value.get("email_id"))
        source_kind = _require_text(value, "source_kind", "missing_source_identity")
        file_name = _text(value.get("file_name"))
        source_url = _text(value.get("source_url"))
        sha256 = _text(value.get("sha256")).lower()
        if not source_email_id or not (file_name or source_url or sha256):
            raise TruthContractError("missing_source_identity", f"{decision}[{index}]")

        mail_date = _mail_date(value.get("mail_date_local"), code="invalid_mail_date")
        if decision == "included":
            role = _require_text(value, "document_role", "missing_document_role")
            truth_type = _require_text(value, "truth_type", "missing_truth_type")
            category = _require_text(value, "expected_category", "missing_expected_category")
            invoice_date = _date(value.get("invoice_date"), code="invalid_invoice_date")
            seller = _require_text(value, "seller", "missing_seller")
            purchaser = _require_text(value, "purchaser", "missing_purchaser")
            amount = _amount(value.get("amount"), required=True)
            if not _SHA256_RE.fullmatch(sha256):
                raise TruthContractError("invalid_artifact_hash", truth_id)
            evidence = value.get("evidence")
            if not isinstance(evidence, list) or not any(
                isinstance(item, Mapping)
                and _text(item.get("sha256")).lower() == sha256
                and isinstance(item.get("bytes"), int)
                and item["bytes"] > 0
                for item in evidence
            ):
                raise TruthContractError("invalid_artifact_hash", f"evidence:{truth_id}")
        else:
            role = _text(value.get("document_role"))
            truth_type = _text(value.get("truth_type"))
            category = _text(value.get("expected_category") or value.get("category"))
            invoice_date = _date(value["invoice_date"], code="invalid_invoice_date") if _text(value.get("invoice_date")) else None
            seller = _text(value.get("seller"))
            purchaser = _text(value.get("purchaser"))
            amount = _amount(value.get("amount"), required=False)
            if sha256 and not _SHA256_RE.fullmatch(sha256):
                raise TruthContractError("invalid_artifact_hash", f"{decision}[{index}]")
            if decision == "excluded" and not _text(value.get("reason") or value.get("reason_code")):
                raise TruthContractError("missing_exclusion_reason", f"excluded[{index}]")

        return cls(
            decision=decision,
            truth_id=truth_id,
            source_email_id=source_email_id,
            source_kind=source_kind,
            file_name=file_name,
            source_url=source_url,
            document_role=role,
            truth_type=truth_type,
            expected_category=category,
            invoice_date=invoice_date,
            mail_date_local=mail_date,
            seller=seller,
            purchaser=purchaser,
            amount=amount,
            invoice_code=_text(value.get("invoice_code")),
            invoice_number=_text(value.get("invoice_number")),
            artifact_sha256=sha256,
            pair_key=_text(value.get("pair_key") or value.get("pair_id")),
            raw=_freeze(value),
        )

    def to_mapping(self) -> dict[str, Any]:
        return _thaw(self.raw)


@dataclass(frozen=True)
class TruthManifest:
    dataset: str
    date_from: dt.date
    date_to: dt.date
    before_exclusive: dt.date
    mailbox: str
    account_domain: str
    account_channel: str
    target_company: str
    finalized: bool
    included: tuple[TruthRow, ...]
    excluded: tuple[TruthRow, ...]
    pending_review: tuple[TruthRow, ...]
    summary: Mapping[str, Any]

    @classmethod
    def from_path(cls, path: str | Path) -> "TruthManifest":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise TruthContractError("invalid_manifest_json", type(exc).__name__) from exc
        return cls.from_mapping(payload)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "TruthManifest":
        if not isinstance(value, Mapping):
            raise TruthContractError("invalid_manifest", "root must be an object")
        summary = value.get("summary")
        if not isinstance(summary, Mapping):
            raise TruthContractError("invalid_manifest", "summary")
        if summary.get("finalized") is not True:
            raise TruthContractError("manifest_not_finalized")
        if summary.get("pending_review_count") != 0:
            raise TruthContractError("pending_review_not_zero")

        date_from = _date(summary.get("date_from"), code="invalid_date_scope")
        date_to = _date(summary.get("date_to"), code="invalid_date_scope")
        before = _date(summary.get("before_exclusive"), code="invalid_date_scope")
        if date_from > date_to or before != date_to + dt.timedelta(days=1):
            raise TruthContractError("invalid_date_scope")
        domain = _text(summary.get("account_domain")).lower()
        channel = _CHANNELS.get(domain)
        declared_channel = _text(summary.get("account_channel") or summary.get("channel")).lower()
        if not channel or (declared_channel and declared_channel != channel):
            raise TruthContractError("unsupported_account_channel", domain or declared_channel)
        mailbox = _require_text(summary, "mailbox", "missing_mailbox")
        target = _text(summary.get("target_company") or summary.get("target_company_id"))
        if not target:
            raise TruthContractError("missing_target_company")

        collections = {}
        for decision in ("included", "excluded", "pending_review"):
            rows = value.get(decision)
            if not isinstance(rows, list):
                raise TruthContractError("invalid_manifest", decision)
            collections[decision] = tuple(
                TruthRow.from_mapping(row, decision=decision, index=index)
                for index, row in enumerate(rows)
            )

        if collections["pending_review"]:
            raise TruthContractError("pending_review_not_zero")
        expected_counts = {
            "included": summary.get("included_count"),
            "excluded": summary.get("excluded_count"),
            "pending_review": summary.get("pending_review_count"),
        }
        for decision, expected in expected_counts.items():
            if expected != len(collections[decision]):
                raise TruthContractError("summary_count_mismatch", decision)

        seen = set()
        for row in collections["included"]:
            if row.truth_id in seen:
                raise TruthContractError("duplicate_truth_id", row.truth_id)
            seen.add(row.truth_id)
        for decision_rows in collections.values():
            for row in decision_rows:
                if not date_from <= row.mail_date_local.date() <= date_to:
                    raise TruthContractError("row_outside_mail_scope", row.truth_id or row.source_email_id)
        cls._validate_pair_evidence(collections["included"])

        return cls(
            dataset=_require_text(summary, "dataset", "missing_dataset"),
            date_from=date_from,
            date_to=date_to,
            before_exclusive=before,
            mailbox=mailbox,
            account_domain=domain,
            account_channel=channel,
            target_company=target,
            finalized=True,
            included=collections["included"],
            excluded=collections["excluded"],
            pending_review=collections["pending_review"],
            summary=_freeze(summary),
        )

    @staticmethod
    def _validate_pair_evidence(rows: tuple[TruthRow, ...]) -> None:
        groups: dict[str, list[TruthRow]] = {}
        for row in rows:
            if row.pair_key:
                groups.setdefault(row.pair_key, []).append(row)
        for pair_key, members in sorted(groups.items()):
            roles = {_PAIR_ROLES.get(row.document_role, row.document_role) for row in members}
            if len(members) != 2 or roles != {"invoice", "companion"}:
                raise TruthContractError("missing_pair_evidence", pair_key)
            if members[0].amount != members[1].amount:
                raise TruthContractError("missing_pair_evidence", pair_key)
            is_hotel = any(row.document_role == "hotel_folio" for row in members)
            if is_hotel and members[0].invoice_date != members[1].invoice_date:
                raise TruthContractError("missing_pair_evidence", pair_key)

    def to_mapping(self) -> dict[str, Any]:
        return {
            "summary": _thaw(self.summary),
            "included": [row.to_mapping() for row in self.included],
            "excluded": [row.to_mapping() for row in self.excluded],
            "pending_review": [row.to_mapping() for row in self.pending_review],
        }
