import hashlib
import json
import math
import re

from url_security import PublicUrlPolicy


_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_.:-]{0,160}$")
_SAFE_TOKEN_FIELDS = frozenset(
    {
        "kind",
        "source_kind",
        "status",
        "status_code",
        "reason",
        "reason_code",
        "prefilter_reason_code",
        "provider",
        "provider_family",
        "provider_recovery_status",
        "candidate_bucket",
        "candidate_action",
        "download_mode",
        "failure_stage",
        "matched_on",
        "category",
        "attachment_ext",
        "mime_content_type",
        "pdf_health_class",
    }
)
_SAFE_COUNT_FIELDS = frozenset(
    {
        "count",
        "candidate_index",
        "payload_size",
        "size_bytes",
        "page_count",
        "emails",
        "archived",
        "manual_check",
        "retention",
        "raw_invoices",
        "errors",
    }
)
_SAFE_BOOL_FIELDS = frozenset(
    {
        "wrapper_detected",
        "expected_match",
        "starts_with_pdf_magic",
        "sibling_pdf_present",
        "sibling_ofd_present",
        "sibling_xml_present",
        "provider_unzipped_pair_suspected",
        "is_url",
    }
)
_SAFE_CONTAINERS = frozenset(
    {
        "timing_ms",
        "timing",
        "pdf_health",
        "tier",
        "url_runtime_result",
        "provider_recovery",
        "source_download_result",
    }
)
_SAFE_LIST_FIELDS = frozenset(
    {
        "attempts",
        "captured_artifacts",
        "captured_network",
        "provider_candidates",
    }
)
_HASH_FIELD_NAMES = {
    "document_id": "document_hash",
    "email_id": "email_hash",
    "source_email_id": "email_hash",
    "sender": "sender_hash",
    "subject": "subject_hash",
    "file_name": "file_hash",
    "original_filename": "file_hash",
    "filepath": "source_path_hash",
    "path": "path_hash",
    "pdf_path": "pdf_path_hash",
    "original_path": "source_path_hash",
    "retained_path": "retained_path_hash",
    "review_path": "review_path_hash",
    "archive_target": "archive_target_hash",
    "source_url": "source_hash",
    "resolved_url": "resolved_hash",
    "url": "url_hash",
    "urls": "urls_hash",
    "provider_candidate_urls": "candidate_urls_hash",
    "anchor_text": "anchor_hash",
    "provider_group_key": "provider_group_hash",
    "attachment_pair_key": "attachment_pair_hash",
    "provider_expected_fields": "expected_fields_hash",
    "provider_recovered_fields": "recovered_fields_hash",
    "expected_fields": "expected_fields_hash",
    "selected_fields": "recovered_fields_hash",
    "fields": "fields_hash",
    "zip_context": "zip_context_hash",
    "prefilter_signals": "prefilter_signals_hash",
}
_DROP_FIELDS = frozenset(
    {
        "authorization",
        "proxy-authorization",
        "cookie",
        "set-cookie",
        "token",
        "query",
        "body",
        "raw_body",
        "body_excerpt",
        "page_title",
        "message",
        "exception",
        "error",
        "buyer",
        "seller",
        "invoice_number",
    }
)


def _canonical_text(value):
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return str(value or "")


def stable_hash(value):
    return hashlib.sha256(_canonical_text(value).encode("utf-8")).hexdigest()


def sanitize_url_for_log(url):
    return PublicUrlPolicy.sanitize(str(url or ""))


def _safe_scalar(key, value):
    if key in _SAFE_BOOL_FIELDS and isinstance(value, bool):
        return value
    if key in _SAFE_COUNT_FIELDS or key.endswith("_count"):
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, float) and math.isfinite(value):
            return value
        return None
    if key.endswith("_ms") or key.endswith("_seconds"):
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value if math.isfinite(float(value)) else None
        return None
    if key in _SAFE_TOKEN_FIELDS:
        if isinstance(value, (int, bool)):
            return value
        text = str(value or "")
        return text if _TOKEN_PATTERN.fullmatch(text) else None
    return None


def sanitize_persistence_payload(value):
    if not isinstance(value, dict):
        return {}

    sanitized = {}
    for raw_key, item in value.items():
        key = str(raw_key or "").lower()
        if key in _DROP_FIELDS:
            continue
        hash_name = _HASH_FIELD_NAMES.get(key)
        if hash_name is not None and item not in (None, "", [], {}):
            sanitized[hash_name] = stable_hash(item)
            continue
        safe_scalar = _safe_scalar(key, item)
        if safe_scalar is not None:
            sanitized[key] = safe_scalar
            continue
        if key in _SAFE_CONTAINERS and isinstance(item, dict):
            nested = sanitize_persistence_payload(item)
            if nested:
                sanitized[key] = nested
            continue
        if key in _SAFE_LIST_FIELDS and isinstance(item, (list, tuple)):
            sanitized[f"{key}_count"] = len(item)
    return sanitized


def build_url_evidence(url, reason_code=""):
    evidence = {
        "kind": "url_evidence",
        "source_hash": stable_hash(url),
        "source_authority": sanitize_url_for_log(url),
    }
    if reason_code and _TOKEN_PATTERN.fullmatch(str(reason_code)):
        evidence["reason_code"] = str(reason_code)
    return evidence
