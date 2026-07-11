from __future__ import annotations

from typing import Any


class DocumentAcceptanceService:
    """Run the established provider/entity acceptance gate before archive."""

    def __init__(self, api: Any) -> None:
        self.api = api

    @staticmethod
    def normalized_snapshot(fields: dict[str, Any] | None) -> dict[str, Any] | None:
        if not fields or not isinstance(fields, dict):
            return None
        raw_date = str(fields.get("Date", ""))
        clean_date = (
            raw_date.replace("/", "")
            .replace("-", "")
            .replace("年", "")
            .replace("月", "")
            .replace("日", "")
            .strip()
        )
        raw_amount = str(fields.get("Amount", ""))
        clean_amount = (
            raw_amount.replace(",", "")
            .replace("¥", "")
            .replace("￥", "")
            .replace("元", "")
            .replace(" ", "")
            .strip()
        )
        return {
            "Date": clean_date or "未知",
            "Amount": clean_amount or "未知",
            "Purchaser": str(fields.get("Purchaser", "")),
            "Seller": str(fields.get("Seller", "")),
            "Type": str(fields.get("Type", "")),
            "InvoiceCode": str(fields.get("InvoiceCode", "")).strip(),
            "InvoiceNumber": str(fields.get("InvoiceNumber", "")).strip(),
            "is_invoice": fields.get("is_invoice", True),
        }

    def evaluate(
        self,
        metadata: dict[str, Any],
        info_json: dict[str, Any],
        pdf_path: str,
        *,
        pdf_health: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        snapshot = self.normalized_snapshot(info_json)
        if pdf_health is None:
            pdf_health = self.api._inspect_pdf_health(pdf_path)
        return self.api._evaluate_document_acceptance(
            metadata,
            info_json,
            snapshot,
            pdf_health,
            pdf_path,
        )
