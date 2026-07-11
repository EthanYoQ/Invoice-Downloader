import copy
import hashlib
import json
import logging
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_api import (
    InvoiceAppAPI,
    _FallbackDocumentTraceStore,
    build_processing_history_key,
)
from email_fetcher import _expand_bwjf_shortlink, normalize_invoice_link_candidate
from pinned_http import PinnedHttpTransport
from url_security import PublicUrlPolicy


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
    local_source = tmp_path / "Original-Business-Name.PDF"
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
    assert Path(local_retained_path).name == "Original-Business-Name.PDF"
    assert Path(local_manual_path).name == "P0_Review_Original-Business-Name.PDF"
    assert Path(retained_path).name.startswith(f"LinkRetention_{url_hash[:16]}_")
    assert Path(manual_path).name.startswith(f"P0_LinkReview_{url_hash[:16]}_")


def test_email_fetcher_shortlink_warning_redacts_url_and_exception(caplog):
    caplog.set_level(logging.WARNING)
    policy = PublicUrlPolicy(
        resolver=lambda host, port: ["93.184.216.34"],
        proxy_endpoint=None,
    )

    class FailingTransport:
        def request(self, *args, **kwargs):
            raise RuntimeError("LOG-EXCEPTION-SECRET")

    with patch(
        "requests.get",
        side_effect=AssertionError("raw requests.get called"),
    ):
        assert _expand_bwjf_shortlink(
            CAPABILITY_URL,
            url_policy=policy,
            pinned_transport=FailingTransport(),
        ) is None

    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "https://fp.bwjf.cn/<redacted>" in rendered
    for forbidden in FORBIDDEN_VALUES:
        assert forbidden not in rendered


def test_shortlink_safe_failure_preserves_original_candidate_for_later_recovery():
    with patch("email_fetcher._expand_bwjf_shortlink", return_value=None):
        assert normalize_invoice_link_candidate(CAPABILITY_URL) == CAPABILITY_URL


def test_email_fetcher_shortlink_uses_pinned_proxy_transport_for_every_redirect():
    final_url = (
        "https://fp.bwjf.cn/download/invoice.pdf"
        "?invoice=INVOICE-NUMBER-SECRET&sign=CAPABILITY-TOKEN"
    )
    policy = PublicUrlPolicy(
        resolver=lambda host, port: ["198.18.0.7"],
        public_resolver=lambda host, port: ["93.184.216.34"],
        proxy_endpoint="http://127.0.0.1:7897",
        proxy_bypass_checker=lambda host: False,
    )

    class RecordingTransport:
        def __init__(self):
            self.calls = []

        def request(self, session, method, target, **kwargs):
            plan = PinnedHttpTransport.build_plan(target)
            self.calls.append((method, target, plan, kwargs))
            if len(self.calls) == 1:
                return SimpleNamespace(
                    status_code=302,
                    headers={"Location": final_url},
                    url=target.url,
                    content=b"",
                )
            return SimpleNamespace(
                status_code=200,
                headers={"Content-Type": "application/pdf"},
                url=target.url,
                content=b"%PDF",
            )

    transport = RecordingTransport()
    with patch("requests.get", side_effect=AssertionError("raw requests.get called")):
        result = _expand_bwjf_shortlink(
            CAPABILITY_URL,
            url_policy=policy,
            pinned_transport=transport,
        )

    assert result == final_url
    assert len(transport.calls) == 2
    for method, target, plan, kwargs in transport.calls:
        assert method == "GET"
        assert target.transport_mode == "proxy"
        assert plan.selected_ip == "93.184.216.34"
        assert plan.proxy_url == "http://127.0.0.1:7897"
        assert plan.proxy_connect_authority == "93.184.216.34:443"
        assert plan.server_hostname == "fp.bwjf.cn"
        assert plan.assert_hostname == "fp.bwjf.cn"
        assert kwargs["read_body"] is False
        assert kwargs["max_response_bytes"] <= 64 * 1024


def test_url_trace_history_packaged_stage_and_ui_persistence_are_secret_free(tmp_path):
    info = _sensitive_info()
    info["is_url"] = True
    raw_filename = "INVOICE-NUMBER-SECRET_CAPABILITY-TOKEN.url"
    runtime_before = copy.deepcopy(info)

    trace_path = tmp_path / "debug_trace.jsonl"
    trace_store = _FallbackDocumentTraceStore(output_path=str(trace_path))
    trace_store.start_document(
        source_filename=raw_filename,
        source_path=CAPABILITY_URL,
        document_id="document-url-7",
        persistence_is_url=True,
    )
    trace_store.set_fields(
        "document-url-7",
        source_message_uid="MAILBOX-UID-SECRET",
        normalized_fields={
            "InvoiceNumber": "INVOICE-NUMBER-SECRET",
            "Purchaser": "BUYER-SECRET",
            "Seller": "SELLER-SECRET",
        },
        source_download_result={
            **info,
            "exception": f"failed at {CAPABILITY_URL}",
        },
    )
    trace_store.flush()

    api = InvoiceAppAPI()
    state_dir = tmp_path / "state"
    history_key = build_processing_history_key(info, raw_filename, CAPABILITY_URL)
    api._commit_output_state(str(state_dir), {history_key}, {})

    packaged_path = tmp_path / "packaged_5p_diag.jsonl"
    api._packaged_diag_enabled = True
    api._packaged_diag_file = lambda: str(packaged_path)
    api._packaged_diag_write(
        "browser_first_exception",
        "_run_processing_loop",
        "exception",
        summary={"file_name": raw_filename, "url_result_count": 0},
        exc=RuntimeError(f"provider failed at {CAPABILITY_URL}"),
    )

    stage_path = tmp_path / "stage_events.jsonl"
    api._run_context = {"enabled": True}
    api._monitoring_path = lambda filename: str(stage_path)
    api._safe_emit_stage_event(
        "url_candidate",
        "failed",
        {
            "reason": f"failed at {CAPABILITY_URL}",
            "source_message_uid": "MAILBOX-UID-SECRET",
            "file_name": raw_filename,
        },
    )

    log_start = len(api.logs)
    api.logs.append(
        {
            "type": "error",
            "msg": (
                f"{raw_filename} MAILBOX-UID-SECRET INVOICE-NUMBER-SECRET "
                f"MAILBOX-SUBJECT-SECRET {CAPABILITY_URL}"
            ),
        }
    )
    api._sanitize_url_candidate_logs(log_start, info)

    assert re.fullmatch(r"url:[0-9a-f]{64}", history_key)
    persisted = "\n".join(
        path.read_text(encoding="utf-8", errors="replace")
        for path in (trace_path, state_dir / ".antigravity_history.json", packaged_path, stage_path)
    )
    rendered_logs = json.dumps(api.logs[log_start:], ensure_ascii=False)
    forbidden = (
        "capability-segment",
        "CAPABILITY-TOKEN",
        "QUERY-SECRET",
        "MAILBOX-UID-SECRET",
        "INVOICE-NUMBER-SECRET",
        "MAILBOX-SUBJECT-SECRET",
        raw_filename,
    )
    for secret in forbidden:
        assert secret not in persisted
        assert secret not in rendered_logs
        assert secret not in history_key
    assert info == runtime_before
