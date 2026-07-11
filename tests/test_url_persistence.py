import hashlib
import json
import logging
from pathlib import Path
from unittest.mock import patch

from app_api import InvoiceAppAPI
from email_fetcher import _expand_bwjf_shortlink


CAPABILITY_URL = (
    "https://fp.bwjf.cn/u/capability-segment"
    "?token=CAPABILITY-TOKEN&query=QUERY-SECRET"
)
FORBIDDEN_VALUES = (
    "capability-segment",
    "CAPABILITY-TOKEN",
    "QUERY-SECRET",
    "AUTHORIZATION-SECRET",
    "COOKIE-SECRET",
    "MAILBOX-SUBJECT-SECRET",
    "SENDER-SECRET",
    "ANCHOR-TEXT-SECRET",
    "BODY-EXCERPT-SECRET",
    "BUYER-SECRET",
    "SELLER-SECRET",
    "INVOICE-NUMBER-SECRET",
    "LOCAL-ABSOLUTE-SECRET",
    "LOG-EXCEPTION-SECRET",
)


def _assert_forbidden_absent(value):
    rendered = json.dumps(value, ensure_ascii=False, default=str)
    for forbidden in FORBIDDEN_VALUES:
        assert forbidden not in rendered


def _disk_evidence(root):
    evidence = []
    for path in sorted(Path(root).rglob("*")):
        evidence.append(str(path.relative_to(root)))
        if path.is_file():
            evidence.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(evidence)


def _sensitive_info():
    fields = {
        "buyer": "BUYER-SECRET",
        "seller": "SELLER-SECRET",
        "invoice_number": "INVOICE-NUMBER-SECRET",
    }
    return {
        "email_id": "mailbox-item",
        "sender": "SENDER-SECRET@example.test",
        "subject": "MAILBOX-SUBJECT-SECRET",
        "filepath": r"C:\LOCAL-ABSOLUTE-SECRET\invoice.pdf",
        "file_name": "MAILBOX-SUBJECT-SECRET.pdf",
        "source_kind": "url",
        "candidate_index": 7,
        "prefilter_reason_code": "URL_RUNTIME_FAILED",
        "source_url": CAPABILITY_URL,
        "resolved_url": CAPABILITY_URL,
        "anchor_text": "ANCHOR-TEXT-SECRET",
        "url_path": "/capability-segment",
        "provider_family": "bwjf_signed_invoice",
        "provider_expected_fields": fields,
        "provider_recovered_fields": fields,
        "body_excerpt": "BODY-EXCERPT-SECRET",
        "authorization": "Bearer AUTHORIZATION-SECRET",
        "cookie": "session=COOKIE-SECRET",
    }


def test_appapi_diag_retention_and_manual_url_evidence_are_privacy_safe(tmp_path):
    api = InvoiceAppAPI()
    output_dir = tmp_path / "output"
    info = _sensitive_info()
    runtime_result = {
        **info,
        "status": "failed",
        "reason_code": "URL_RUNTIME_FAILED",
        "timing_ms": {"total_ms": 12.5},
        "captured_network": [{"kind": "network_seen", "url": CAPABILITY_URL}],
    }

    metadata = api._attachment_diag_metadata(
        info,
        file_name=info["file_name"],
        document_id="document-7",
        extra={"url_runtime_result": runtime_result},
    )
    safe_trace = api._sanitize_url_persistence_payload(runtime_result)
    retained_path = api._retain_artifact(
        str(output_dir),
        CAPABILITY_URL,
        "url_runtime_failed",
        "URL_RUNTIME_FAILED",
        metadata,
    )
    manual_path = api._send_to_manual_check(
        str(output_dir),
        CAPABILITY_URL,
        "URL_RUNTIME_FAILED",
        metadata=metadata,
        is_url=True,
    )
    local_source = tmp_path / "MAILBOX-SUBJECT-SECRET_INVOICE-NUMBER-SECRET.pdf"
    local_source.write_bytes(b"%PDF-1.4\nbenign evidence")
    local_retained_path = api._retain_artifact(
        str(output_dir),
        str(local_source),
        "processing_errors",
        "PROCESSING_FAILED",
        {**metadata, "source_kind": "attachment"},
    )
    local_manual_path = api._send_to_manual_check(
        str(output_dir),
        str(local_source),
        "MANUAL_REVIEW",
        metadata={**metadata, "file_name": local_source.name},
    )

    _assert_forbidden_absent(metadata)
    _assert_forbidden_absent(safe_trace)
    disk_evidence = _disk_evidence(output_dir)
    for forbidden in (*FORBIDDEN_VALUES, str(tmp_path)):
        assert forbidden not in disk_evidence

    url_hash = hashlib.sha256(CAPABILITY_URL.encode("utf-8")).hexdigest()
    assert url_hash in json.dumps(metadata, ensure_ascii=False)
    assert url_hash in disk_evidence
    assert "URL_RUNTIME_FAILED" in disk_evidence
    assert Path(retained_path).is_file()
    assert Path(manual_path).is_file()
    assert Path(local_retained_path).is_file()
    assert Path(local_manual_path).is_file()


def test_email_fetcher_shortlink_warning_redacts_url_and_exception(caplog):
    caplog.set_level(logging.WARNING)

    with patch(
        "requests.get",
        side_effect=RuntimeError("LOG-EXCEPTION-SECRET"),
    ):
        assert _expand_bwjf_shortlink(CAPABILITY_URL) is None

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://fp.bwjf.cn/<redacted>" in rendered
    for forbidden in FORBIDDEN_VALUES:
        assert forbidden not in rendered
