"""
DeepSeek text-extraction engine for invoice field extraction.

Consumes OCR text (from local_ocr.py or GLM-OCR) and produces structured invoice fields
via the DSH LLM (DeepSeek). The output is adapted to the existing _adapt_result path
through InvoiceRecord.from_legacy().
"""

import json
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

# System prompt for DeepSeek extraction
EXTRACTION_SYSTEM_PROMPT = """You are an invoice field extractor for Chinese invoices. Extract the following fields from OCR text and return ONLY valid JSON, no markdown, no explanation:

Required fields:
- is_invoice: boolean (true if this is an invoice, false otherwise)
- Date: string, invoice date in YYYY-MM-DD format
- Purchaser: string, buyer company name in original Chinese
- Seller: string, seller company name in original Chinese
- Amount: string, total amount including tax as a decimal number string (e.g. "9.10")
- InvoiceNumber: string, invoice number
- InvoiceCode: string, invoice code (may be empty)
- Type: string, one of: 餐饮, 火车票, 打车, 住宿发票, 住宿水单, 机票, 行程单, 差旅服务费, 其他, 非目标公司发票

Optional fields (include only if clearly present):
- Departure_Date: string, departure date in YYYY-MM-DD format (for train/flight tickets)
- Departure_City: string, departure city (for train/flight tickets)
- Destination_City: string, destination city (for train/flight tickets)

Rules:
- Preserve original Chinese names for Purchaser and Seller — do NOT translate
- Amount must be the total including tax (价税合计)
- Date must be the invoice issue date (开票日期), not departure/travel date
- If a field is not found, use empty string ""
- Return ONLY the JSON object, no other text"""

# JSON Schema for validation
EXTRACTION_SCHEMA = {
    "type": "object",
    "required": ["is_invoice", "Date", "Purchaser", "Seller", "Amount", "InvoiceNumber", "Type"],
    "properties": {
        "is_invoice": {"type": "boolean"},
        "Date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "Purchaser": {"type": "string"},
        "Seller": {"type": "string"},
        "Amount": {"type": "string"},
        "InvoiceNumber": {"type": "string"},
        "InvoiceCode": {"type": "string"},
        "Type": {"type": "string", "enum": ["餐饮", "火车票", "打车", "住宿发票", "住宿水单", "机票", "行程单", "差旅服务费", "其他", "非目标公司发票"]},
        "Departure_Date": {"type": "string"},
        "Departure_City": {"type": "string"},
        "Destination_City": {"type": "string"},
    },
}

# Config field for retry policy (can be overridden via cordis.yml config)
DEFAULT_MAX_RETRIES = 2


def validate_extraction(payload: dict) -> list[str]:
    """Validate extraction output against schema. Returns list of errors (empty if valid)."""
    errors = []
    if not isinstance(payload, dict):
        return ["payload is not a dict"]

    for field_name in EXTRACTION_SCHEMA["required"]:
        if field_name not in payload:
            errors.append(f"missing required field: {field_name}")

    if "Date" in payload and payload["Date"]:
        import re
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", payload["Date"]):
            errors.append(f"Date format invalid: {payload['Date']}")

    if "Type" in payload and payload["Type"]:
        valid_types = EXTRACTION_SCHEMA["properties"]["Type"]["enum"]
        if payload["Type"] not in valid_types:
            errors.append(f"Type not in enum: {payload['Type']}")

    return errors


def build_extraction_prompt(ocr_text: str, document_type_hint: str = None) -> str:
    """Build the user prompt for DeepSeek extraction."""
    prompt = f"Extract invoice fields from this OCR text:\n\n{ocr_text}"
    if document_type_hint:
        prompt += f"\n\nDocument type hint: {document_type_hint}"
    return prompt


def parse_extraction_response(response_text: str) -> dict:
    """Parse DeepSeek response text into a dict. Handles markdown-wrapped JSON."""
    text = response_text.strip()
    # Remove markdown code block if present
    if text.startswith("```"):
        lines = text.split("\n")
        # Remove first and last lines (```json and ```)
        if lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines)
    return json.loads(text)


class DeepSeekExtractor:
    """
    DeepSeek-based invoice field extractor.

    Uses the DSH LLM (via IPC extraction.request) to extract structured fields
    from OCR text. The output is validated against EXTRACTION_SCHEMA and
    adapted to the legacy _adapt_result path.
    """

    def __init__(self, extraction_client=None):
        """
        Args:
            extraction_client: ExtractionClient from ipc.protocol for DSH LLM calls.
                              If None, extraction requests will fail.
        """
        self._client = extraction_client

    def extract(self, ocr_text: str, document_type_hint: str = None) -> dict:
        """
        Extract invoice fields from OCR text.

        Returns a dict matching the legacy _adapt_result shape:
        {is_invoice, Date, Purchaser, Seller, Amount, InvoiceNumber, InvoiceCode, Type, ...}

        Raises RuntimeError if extraction fails after retries.
        """
        if not self._client:
            raise RuntimeError("no extraction client configured")

        prompt = build_extraction_prompt(ocr_text, document_type_hint)
        last_errors = []

        for attempt in range(DEFAULT_MAX_RETRIES + 1):
            try:
                # Send extraction request via IPC
                result = self._client.extract(prompt, document_type_hint)

                # Parse if result is a string (raw model output)
                if isinstance(result, str):
                    result = parse_extraction_response(result)

                # Validate
                errors = validate_extraction(result)
                if not errors:
                    logger.info(f"extraction succeeded on attempt {attempt + 1}")
                    return result

                last_errors = errors
                logger.warning(f"extraction validation failed on attempt {attempt + 1}: {errors}")

                # On retry, include validation errors in prompt
                if attempt < DEFAULT_MAX_RETRIES:
                    prompt = build_extraction_prompt(ocr_text, document_type_hint)
                    prompt += f"\n\nPrevious attempt had errors: {'; '.join(errors)}. Please fix and return valid JSON."

            except json.JSONDecodeError as e:
                last_errors = [f"JSON parse error: {e}"]
                logger.warning(f"extraction JSON parse failed on attempt {attempt + 1}: {e}")
            except Exception as e:
                last_errors = [f"extraction error: {e}"]
                logger.warning(f"extraction failed on attempt {attempt + 1}: {e}")

        raise RuntimeError(f"extraction failed after {DEFAULT_MAX_RETRIES + 1} attempts: {'; '.join(last_errors)}")

    def extract_and_adapt(self, ocr_text: str, pdf_path: str = None, document_context: dict = None) -> dict:
        """
        Extract fields and adapt through the existing _adapt_result path.

        This is the main entry point that matches the existing engine's interface.
        """
        result = self.extract(ocr_text, document_type_hint=document_context.get("type_hint") if document_context else None)

        # Import here to avoid circular dependency
        from invoice_domain import DocumentIdentity, InvoiceRecord

        context = document_context or {}
        source_locator = os.path.abspath(pdf_path) if pdf_path else ""
        source_filename = str(context.get("original_filename") or (os.path.basename(pdf_path) if pdf_path else ""))
        identity = DocumentIdentity(
            document_id=str(context.get("document_id") or source_locator or source_filename or "deepseek-extraction"),
            source_message_uid=str(context.get("email_id") or context.get("source_email_id") or ""),
            source_filename=source_filename,
            source_locator=source_locator,
            source_kind=str(context.get("source_kind") or "deepseek_extraction"),
            provider_group_key=str(context.get("provider_group_key") or ""),
        )
        return InvoiceRecord.from_legacy(result, identity).to_legacy()
