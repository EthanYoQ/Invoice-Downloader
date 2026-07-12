from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json
import threading

import pytest

from app_archive_adapter import AppArchiveAdapter
from archive_pairing_service import reconcile_archive_pairs
from archive_service import ArchiveDecision, ArchiveService
from candidate_pipeline import CandidatePipeline, CandidatePreflight, DocumentCandidate
from document_acceptance import DocumentAcceptanceService
from extraction_pipeline import ExtractionOutcome
from invoice_domain import DocumentIdentity


def _resolved_outcome(path: Path, *, metadata=None, info_json=None, trace=None):
    row = {"filepath": str(path), "email_id": "mail-1", **(metadata or {})}
    candidate = CandidatePipeline().collect([row])[0]
    return ExtractionOutcome.resolved(
        candidate,
        {
            "pdf_path": str(path),
            "metadata": row,
            "info_json": {
                "Date": "20260610",
                "Amount": "10.00",
                "Purchaser": "辉瑞",
                "Seller": "示例销售方",
                "Type": "餐饮",
                "InvoiceCode": "",
                "InvoiceNumber": "",
                "is_invoice": True,
                **(info_json or {}),
            },
            "extraction_trace": trace or {},
        },
    )


@pytest.mark.parametrize(
    ("metadata", "info_json", "preview"),
    [
        (
            {"provider_family": "baiwang"},
            {"Seller": "销售方", "Amount": "10.00"},
            "发票预览 下载PDF文件 下载OFD文件 关于百望",
        ),
        (
            {
                "provider_family": "baiwang",
                "provider_expected_fields": {"seller": "期望销售方", "amount": "20.00"},
            },
            {"Seller": "实际销售方", "Amount": "10.00"},
            "电子发票 发票号码 123",
        ),
        (
            {
                "provider_family": "chinatax_direct_invoice",
                "provider_expected_fields": {"invoice_number": "EXPECTED"},
            },
            {"InvoiceNumber": "ACTUAL"},
            "电子发票 发票号码 ACTUAL",
        ),
        (
            {
                "provider_family": "chinatax_direct_invoice",
                "provider_expected_fields": {"seller": "期望销售方"},
            },
            {"Seller": "实际销售方"},
            "电子发票 销售方名称 实际销售方",
        ),
        (
            {
                "provider_family": "chinatax_direct_invoice",
                "provider_expected_fields": {"invoice_number": "MATCHED"},
            },
            {"InvoiceNumber": "MATCHED"},
            "电子发票 发票号码 MATCHED",
        ),
    ],
)
def test_acceptance_service_matches_legacy_app_api_matrix(
    tmp_path: Path, metadata, info_json, preview
):
    from app_api import InvoiceAppAPI

    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF-1.4\nacceptance")
    api = InvoiceAppAPI()
    api._extract_pdf_preview_text = lambda *_args, **_kwargs: preview
    api._inspect_pdf_health = lambda _path: {"pdf_health_class": "healthy"}
    service = DocumentAcceptanceService(api)
    complete_info = {
        "Date": "20260610",
        "Amount": "10.00",
        "Seller": "示例销售方",
        "InvoiceNumber": "",
        **info_json,
    }

    expected = api._evaluate_document_acceptance(
        metadata,
        complete_info,
        service.normalized_snapshot(complete_info),
        api._inspect_pdf_health(str(source)),
        str(source),
    )

    assert service.evaluate(metadata, complete_info, str(source)) == expected


def test_acceptance_rejection_retains_and_fails_closed_before_route(tmp_path: Path):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"%PDF-1.4\nprovider")
    outcome = _resolved_outcome(
        source,
        metadata={
            "provider_family": "chinatax_direct_invoice",
            "provider_expected_fields": {"invoice_number": "EXPECTED"},
        },
        info_json={"InvoiceNumber": "ACTUAL", "is_invoice": False},
    )
    retained = []

    class API:
        logs = []
        processed_invoices = []
        error_invoices = []
        discovered_categories = set()
        stats = {"invoices": 0, "errors": 0}
        audit_counts = {"manual_check": 0, "retention": 0, "raw_invoices": 0}

        @staticmethod
        def _resolve_active_company():
            return "辉瑞"

        @staticmethod
        def _inspect_pdf_health(_path):
            return {"pdf_health_class": "healthy"}

        @staticmethod
        def _evaluate_document_acceptance(*_args, **_kwargs):
            return {
                "accepted": False,
                "reason_code": "DIRECT_INVOICE_EXPECTED_ENTITY_MISMATCH",
                "bucket": "provider_entity_mismatch",
                "message": "expected versus actual",
            }

        @staticmethod
        def _attachment_diag_metadata(metadata, **kwargs):
            return {
                **dict(metadata),
                "document_id": kwargs.get("document_id"),
                **dict(kwargs.get("extra") or {}),
            }

        @staticmethod
        def _retain_artifact(_root, path, bucket, reason, metadata):
            retained.append((path, bucket, reason, metadata))
            return path

        @staticmethod
        def _safe_emit_artifact_event(*_args, **_kwargs):
            return None

        @staticmethod
        def _safe_emit_stage_event(*_args, **_kwargs):
            return None

        @staticmethod
        def _cwt_cancellation_matching(_root):
            return None

    class Extractor:
        last_route_trace = {}

        @staticmethod
        def route_and_rename_file(*_args, **_kwargs):
            pytest.fail("rejected provider artifact must never reach archive routing")

    trace_store = SimpleNamespace(set_fields=lambda *_args, **_kwargs: None)
    adapter = AppArchiveAdapter(
        api=API(),
        extractor=Extractor(),
        save_path=str(tmp_path),
        business_records={},
        trace_store=trace_store,
        pairing_finalizer=lambda *_args: None,
    )
    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        dedupe_key=adapter.dedupe_key,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    assert report.unresolved_count == 1
    assert report.can_complete is False
    assert report.outcomes[0].outcome.status == "unresolved"
    assert (
        report.outcomes[0].outcome.reason_code
        == "DIRECT_INVOICE_EXPECTED_ENTITY_MISMATCH"
    )
    assert retained[0][1] == "provider_entity_mismatch"
    assert retained[0][3]["document_id"] == outcome.candidate.identity.document_id
    assert retained[0][3]["document_acceptance"]["reason_code"] == (
        "DIRECT_INVOICE_EXPECTED_ENTITY_MISMATCH"
    )
    assert API.stats == {"invoices": 0, "errors": 1}


def test_production_app_acceptance_rejection_never_commits_or_completes(tmp_path: Path):
    from app_api import InvoiceAppAPI, ProcessingLoopFailure

    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"invoice" * 300)
    row = {
        "filepath": str(source),
        "email_id": "mail-1",
        "provider_family": "chinatax_direct_invoice",
        "provider_expected_fields": {"invoice_number": "EXPECTED"},
    }
    api = InvoiceAppAPI()
    api._stop_requested = False
    api._inspect_pdf_health = lambda _path: {"pdf_health_class": "healthy"}
    api._evaluate_document_acceptance = lambda *_args, **_kwargs: {
        "accepted": False,
        "reason_code": "DIRECT_INVOICE_EXPECTED_ENTITY_MISMATCH",
        "bucket": "provider_entity_mismatch",
        "message": "expected versus actual",
        "provider_family": "chinatax_direct_invoice",
    }
    api._commit_output_state = lambda *_args: pytest.fail(
        "acceptance rejection must not commit history or completed state"
    )

    class Extractor:
        glm_runtime = SimpleNamespace(profiles={})
        last_route_trace = {}

        @staticmethod
        def load_processed_records():
            return {}

        @staticmethod
        def probe_local_only(*_args, **_kwargs):
            return SimpleNamespace(
                status="resolved",
                result={
                    "Date": "20260610",
                    "Amount": "10.00",
                    "Purchaser": "辉瑞",
                    "Seller": "商户",
                    "Type": "餐饮",
                    "InvoiceNumber": "ACTUAL",
                    "is_invoice": True,
                },
                engine="local",
                reason_code="LOCAL",
            )

        @staticmethod
        def route_and_rename_file(*_args, **_kwargs):
            pytest.fail("rejected document must not reach route_and_rename_file")

    with pytest.raises(ProcessingLoopFailure, match="PROCESSING_PIPELINE_INCOMPLETE"):
        api._run_processing_loop_with_extractor(
            [row],
            "key",
            str(tmp_path / "output"),
            _extractor=Extractor(),
            _pairing_finalizer=lambda *_args: None,
        )


@pytest.mark.parametrize(
    "document_type",
    [
        "行程单",
        "住宿水单",
        "住宿确认单",
        "航班行程单",
        "差旅服务费",
        "火车票",
        "过路费",
        "定额发票",
    ],
)
def test_supporting_and_registry_exempt_non_invoice_documents_archive(
    tmp_path: Path, document_type: str
):
    source = tmp_path / f"{document_type}.pdf"
    source.write_bytes(b"supporting")
    outcome = _resolved_outcome(
        source,
        info_json={"Type": document_type, "is_invoice": False},
    )
    routed = []

    api = _ArchiveAPI()
    extractor = _ArchiveExtractor(routed)
    adapter = _adapter(tmp_path, api, extractor)
    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        dedupe_key=adapter.dedupe_key,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    assert report.archived_count == 1
    assert report.retained_count == 0
    assert report.can_complete is True
    assert len(routed) == 1


def test_ordinary_non_invoice_is_retained_as_terminal_non_business_output(tmp_path: Path):
    source = tmp_path / "ordinary.pdf"
    source.write_bytes(b"ordinary")
    outcome = _resolved_outcome(
        source,
        info_json={"Type": "餐饮", "is_invoice": False, "rejection_reason": "not a receipt"},
    )
    routed = []

    api = _ArchiveAPI()
    adapter = _adapter(tmp_path, api, _ArchiveExtractor(routed))
    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        dedupe_key=adapter.dedupe_key,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    assert report.retained_count == 1
    assert report.can_complete is True
    assert routed == []
    assert api.error_invoices[0]["artifact_kind"] == "retention"


@pytest.mark.parametrize(
    ("filename", "metadata", "info_json", "expected_type", "reason_code"),
    [
        (
            "CWT-行程单 - 酒店.pdf",
            {"sender": "notification@citsgbt.com"},
            {"Type": "其他", "Seller": "国旅运通"},
            "住宿确认单",
            "CLASSIFIED_AS_CWT_HOTEL_BY_FILENAME",
        ),
        (
            "CWT-flight-itinerary.pdf",
            {"sender": "notification@citsgbt.com"},
            {"Type": "其他", "Seller": "国旅运通"},
            "航班行程单",
            "CLASSIFIED_AS_CWT_FLIGHT_BY_FILENAME",
        ),
        (
            "SCCT-service.pdf",
            {"subject": "SCCT invoice"},
            {"Type": "其他", "Seller": "GBT Travel Services"},
            "差旅服务费",
            "CLASSIFIED_AS_CWT_SERVICE_FEE",
        ),
    ],
)
def test_cwt_classification_uses_canonical_classifier_and_preserves_reason_codes(
    tmp_path: Path, filename, metadata, info_json, expected_type, reason_code
):
    source = tmp_path / filename
    source.write_bytes(b"cwt")
    outcome = _resolved_outcome(source, metadata=metadata, info_json=info_json)
    adapter = _adapter(tmp_path, _ArchiveAPI(), _ArchiveExtractor([]))

    payload = adapter.normalize(outcome)

    assert payload["info_json"]["Type"] == expected_type
    assert reason_code in payload["classification_reason_codes"]


def test_cwt_local_fast_path_signal_preserves_type_and_reason(tmp_path: Path):
    source = tmp_path / "CWT-local.pdf"
    source.write_bytes(b"cwt")
    outcome = _resolved_outcome(
        source,
        metadata={"sender": "notification@citsgbt.com"},
        info_json={"Type": "住宿水单", "is_invoice": False},
        trace={
            "engine": "local_cits_gbt_pdf",
            "reason_code": "LOCAL_CITS_GBT_PDF_FAST_PATH",
        },
    )

    payload = _adapter(tmp_path, _ArchiveAPI(), _ArchiveExtractor([])).normalize(outcome)

    assert payload["info_json"]["Type"] == "住宿水单"
    assert "PRESERVED_LOCAL_CITS_GBT_TYPE" in payload["classification_reason_codes"]


def test_cwt_cancellation_registers_before_final_matching(tmp_path: Path):
    source = tmp_path / "酒店预定取消知会-张三-20260610入住-上海.pdf"
    source.write_bytes(b"cwt-cancel")
    outcome = _resolved_outcome(
        source,
        metadata={"sender": "notification@citsgbt.com"},
        info_json={"Type": "其他", "Seller": "国旅运通", "is_invoice": False},
    )
    api = _ArchiveAPI()
    adapter = _adapter(tmp_path, api, _ArchiveExtractor([]))
    payload = adapter.normalize(outcome)

    decision = adapter.archive_operation(outcome, payload, payload["category"], tmp_path)

    assert payload["info_json"]["_cwt_cancellation"] is True
    assert "CWT_HOTEL_CANCELLATION" in payload["classification_reason_codes"]
    assert decision.status == "manual_review"
    assert api._cwt_cancellation_registry == [
        {"file_name": source.name, "manual_check_path": str(source)}
    ]


def test_cwt_cancellation_matching_moves_related_hotel_confirmation(tmp_path: Path):
    from app_api import InvoiceAppAPI
    from document_types import MANUAL_REVIEW_FOLDER

    hotel_dir = tmp_path / "住宿发票"
    hotel_dir.mkdir()
    confirmation = hotel_dir / "20260610_住宿确认单_0.00_张三酒店.pdf"
    confirmation.write_bytes(b"confirmation")
    api = InvoiceAppAPI()
    api._cwt_cancellation_registry = [
        {"file_name": "酒店预定取消知会-张三-20260610入住-上海.pdf"}
    ]

    api._cwt_cancellation_matching(str(tmp_path))

    moved = tmp_path / MANUAL_REVIEW_FOLDER / f"P0_CancelMatch_{confirmation.name}"
    assert moved.exists()
    assert not confirmation.exists()
    assert Path(f"{moved}.json").exists()


def test_candidate_carries_exact_legacy_attachment_and_url_history_keys(tmp_path: Path):
    from app_api import build_processing_history_key

    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"history-content")
    attachment_row = {
        "filepath": str(source),
        "email_id": "mail-1",
        "subject": "attachment",
        "tier": 2,
    }
    url_row = {
        "filepath": "https://example.com/invoice?token=secret",
        "source_url": "https://example.com/invoice?token=secret",
        "is_url": True,
        "email_id": "mail-2",
        "provider_family": "baiwang",
        "provider_expected_fields": {"invoice_number": "123"},
    }

    attachment, url = CandidatePipeline().collect([attachment_row, url_row])

    assert attachment.compatibility_history_key == build_processing_history_key(
        attachment_row, source.name, str(source)
    )
    assert url.compatibility_history_key == build_processing_history_key(
        url_row, "invoice", url_row["filepath"]
    )
    assert "secret" not in url.compatibility_history_key


def test_direct_candidate_construction_derives_nonempty_legacy_history_key(
    tmp_path: Path,
):
    from app_api import build_processing_history_key

    source = tmp_path / "direct.pdf"
    source.write_bytes(b"direct-history-content")
    candidate = DocumentCandidate(
        identity=DocumentIdentity("direct-document", "attachment"),
        sequence=0,
        source_path=str(source),
        source_filename=source.name,
        metadata={"filepath": str(source), "email_id": "mail-direct"},
    )

    assert candidate.compatibility_history_key == build_processing_history_key(
        candidate.to_legacy(), source.name, str(source)
    )


@pytest.mark.parametrize("kind", ["attachment", "url"])
def test_legacy_history_key_skips_before_local_or_model_work(tmp_path: Path, kind: str):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"history-content")
    row = (
        {"filepath": str(source), "email_id": "mail-1"}
        if kind == "attachment"
        else {
            "filepath": "https://example.com/invoice?id=secret",
            "source_url": "https://example.com/invoice?id=secret",
            "is_url": True,
            "email_id": "mail-1",
            "provider_family": "baiwang",
        }
    )
    candidate = CandidatePipeline().collect([row])[0]

    class Extractor:
        @staticmethod
        def probe_local_only(*_args, **_kwargs):
            pytest.fail("legacy history must skip before local probe")

    preflight = CandidatePreflight(
        api=SimpleNamespace(),
        extractor=Extractor(),
        working_history={candidate.compatibility_history_key},
        sidecar={},
        sidecar_lock=threading.Lock(),
        converter_factory=lambda: pytest.fail("legacy history must skip recovery"),
    )

    outcome = preflight(candidate)

    assert outcome.status == "duplicate"
    assert outcome.reason_code == "HISTORY_DUPLICATE_SKIP"


def test_successful_app_commit_persists_canonical_and_legacy_history_keys(
    monkeypatch, tmp_path: Path
):
    from app_api import InvoiceAppAPI

    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"invoice" * 300)
    row = {"filepath": str(source), "email_id": "mail-1", "subject": "invoice"}
    candidate = CandidatePipeline().collect([row])[0]
    api = InvoiceAppAPI()
    api._stop_requested = False
    committed = []
    api._commit_output_state = lambda _state, history, _records: committed.append(set(history))
    monkeypatch.setattr(
        "document_acceptance.DocumentAcceptanceService.evaluate",
        lambda *_args, **_kwargs: {"accepted": True},
    )

    class Extractor:
        glm_runtime = SimpleNamespace(profiles={})

        @staticmethod
        def load_processed_records():
            return {}

        @staticmethod
        def probe_local_only(*_args, **_kwargs):
            return SimpleNamespace(
                status="resolved",
                result={
                    "Date": "20260610",
                    "Amount": "10.00",
                    "Purchaser": "辉瑞",
                    "Seller": "商户",
                    "Type": "餐饮",
                    "is_invoice": True,
                },
                engine="local",
                reason_code="LOCAL",
            )

    report = api._run_processing_loop_with_extractor(
        [row],
        "key",
        str(tmp_path / "output"),
        _extractor=Extractor(),
        _archive_operation=lambda *_args: ArchiveDecision(path=str(source)),
        _pairing_finalizer=lambda *_args: None,
    )

    assert report.can_complete is True
    assert committed == [
        {candidate.identity.document_id, candidate.compatibility_history_key}
    ]


def test_completed_legacy_history_file_is_loaded_for_rollback_compatibility(tmp_path: Path):
    from app_api import InvoiceAppAPI

    state_dir = tmp_path / "state"
    state_dir.mkdir()
    (state_dir / ".antigravity_history.json").write_text(
        json.dumps(["att:legacy-sha", "url:v2:legacy"]), encoding="utf-8"
    )
    (state_dir / "run_state.json").write_text(
        json.dumps({"status": "completed"}), encoding="utf-8"
    )
    api = InvoiceAppAPI()

    assert api._load_committed_history(str(state_dir)) == {
        "att:legacy-sha",
        "url:v2:legacy",
    }


def test_archive_success_restores_frontend_row_category_stats_and_event_shape(tmp_path: Path):
    source = tmp_path / "invoice.pdf"
    source.write_bytes(b"invoice")
    outcome = _resolved_outcome(
        source,
        info_json={
            "Date": "20260610",
            "Amount": "88.50",
            "Seller": "成功商户",
            "Type": "餐饮",
        },
    )
    api = _ArchiveAPI()
    adapter = _adapter(tmp_path, api, _ArchiveExtractor([]))

    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        dedupe_key=adapter.dedupe_key,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    assert report.can_complete is True
    assert len(api.processed_invoices) == 1
    row = api.processed_invoices[0]
    assert set(row) == {"id", "date", "amount", "category", "merchant", "path"}
    assert row | {"id": "ignored"} == {
        "id": "ignored",
        "date": "20260610",
        "amount": "¥ 88.50",
        "category": "餐饮",
        "merchant": "成功商户",
        "path": str(source),
    }
    assert api.discovered_categories == {"餐饮"}
    assert api.stats == {"invoices": 1, "errors": 0}
    assert api.artifact_events == [
        (
            "archived",
            str(source),
            {
                "document_id": outcome.candidate.identity.document_id,
                "category": "餐饮",
            },
        )
    ]


@pytest.mark.parametrize(
    ("candidate_action", "status", "expected_category", "expected_status"),
    [
        ("retain_only", "retained", "预过滤保全", "已保全待判断"),
        ("manual_review", "manual_review", "人工复核", "待人工复核"),
    ],
)
def test_prefilter_terminal_outcomes_restore_frontend_error_rows_and_counts(
    tmp_path: Path, candidate_action, status, expected_category, expected_status
):
    source = tmp_path / f"{status}.pdf"
    source.write_bytes(b"terminal")
    row = {
        "filepath": str(source),
        "email_id": "mail-1",
        "candidate_action": candidate_action,
        "prefilter_reason_code": "PREFILTER_REASON",
    }
    candidate = CandidatePipeline().collect([row])[0]
    outcome = ExtractionOutcome(
        candidate=candidate,
        status=status,
        reason_code="PREFILTER_REASON",
        artifact_path=str(source),
    )
    api = _ArchiveAPI()
    adapter = _adapter(tmp_path, api, _ArchiveExtractor([]))

    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    assert report.can_complete is True
    assert api.stats == {"invoices": 0, "errors": 1}
    assert len(api.error_invoices) == 1
    error = api.error_invoices[0]
    assert error["category"] == expected_category
    assert error["status"] == expected_status
    assert error["reason"] == "PREFILTER_REASON"
    assert error["path"] == str(source)
    assert {"id", "date", "amount", "category", "merchant", "path", "name", "sColor", "status", "reason", "rColor"} <= set(error)


def test_tier3_manual_review_restores_classified_frontend_state(tmp_path: Path):
    source = tmp_path / "tier3.pdf"
    source.write_bytes(b"tier3")
    outcome = _resolved_outcome(
        source,
        metadata={"tier": 3},
        info_json={"Date": "20260610", "Amount": "66.00", "Seller": "边缘商户", "Type": "餐饮"},
    )
    api = _ArchiveAPI()
    adapter = _adapter(tmp_path, api, _ArchiveExtractor([]))

    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    assert report.manual_count == 1
    assert report.can_complete is True
    assert api.discovered_categories == {"餐饮"}
    assert api.stats == {"invoices": 0, "errors": 1}
    assert api.error_invoices[0]["date"] == "20260610"
    assert api.error_invoices[0]["amount"] == "¥ 66.00"
    assert api.error_invoices[0]["category"] == "餐饮"
    assert api.error_invoices[0]["merchant"] == "边缘商户"
    assert api.error_invoices[0]["status"] == "待人工复核"


def test_router_manual_check_restores_classified_frontend_state(tmp_path: Path):
    source = tmp_path / "low-confidence.pdf"
    source.write_bytes(b"low-confidence")
    manual_path = tmp_path / "Manual_Check" / "P0_Review_low-confidence.pdf"
    manual_path.parent.mkdir()
    manual_path.write_bytes(b"review")
    outcome = _resolved_outcome(
        source,
        info_json={"Date": "20260610", "Amount": "12.00", "Seller": "待复核商户", "Type": "餐饮"},
    )
    api = _ArchiveAPI()

    class ManualExtractor:
        last_route_trace = {
            "used_manual_check": True,
            "reason_code": "ROUTE_TO_MANUAL_CHECK",
        }

        @staticmethod
        def route_and_rename_file(*_args, **_kwargs):
            return True, str(manual_path)

    adapter = _adapter(tmp_path, api, ManualExtractor())
    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    assert report.manual_count == 1
    assert report.can_complete is True
    assert api.stats == {"invoices": 0, "errors": 1}
    assert api.error_invoices[0]["path"] == str(manual_path)
    assert api.error_invoices[0]["reason"] == "ROUTE_TO_MANUAL_CHECK"


def test_cwt_trusted_classification_is_exempt_from_tier3_manual_review(tmp_path: Path):
    source = tmp_path / "CWT-flight.pdf"
    source.write_bytes(b"cwt")
    outcome = _resolved_outcome(
        source,
        metadata={"tier": 3, "sender": "notification@citsgbt.com"},
        info_json={"Type": "其他", "Seller": "国旅运通", "is_invoice": False},
    )

    payload = _adapter(tmp_path, _ArchiveAPI(), _ArchiveExtractor([])).normalize(outcome)

    assert payload["info_json"]["Type"] == "航班行程单"
    assert payload.get("archive_status") != "manual_review"


@pytest.mark.parametrize(
    ("family", "folder", "invoice_role", "companion_role"),
    [
        ("hotel", "住宿发票", "hotel_invoice", "hotel_folio"),
        ("ride", "打车", "ride_invoice", "ride_itinerary"),
    ],
)
def test_pairing_uses_archived_outcome_metadata_not_filename_parsing(
    tmp_path: Path, family, folder, invoice_role, companion_role
):
    target_dir = tmp_path / folder
    target_dir.mkdir()
    invoice = target_dir / "opaque-a.pdf"
    companion = target_dir / "opaque-b.pdf"
    invoice.write_bytes(b"invoice")
    companion.write_bytes(b"companion")
    trace = _TraceStore(
        {str(invoice): "invoice-id", str(companion): "companion-id"}
    )
    artifact_metadata = {
        str(invoice): {
            "document_id": "invoice-id",
            "artifact_role": invoice_role,
            "source_message_uid": "same-mail",
            "seller": "同一商户",
            "date": "20260610",
            "amount": "100.00",
            "pairing_required": True,
        },
        str(companion): {
            "document_id": "companion-id",
            "artifact_role": companion_role,
            "source_message_uid": "same-mail",
            "seller": "同一商户",
            "date": "20260610",
            "amount": "100.00",
            "pairing_required": True,
        },
    }

    counts = reconcile_archive_pairs(
        tmp_path,
        trace_store=trace,
        artifact_metadata=artifact_metadata,
    )

    assert counts[family] == 1
    assert trace.fields["invoice-id"]["combine_result"]["status"] == "matched"
    assert trace.fields["companion-id"]["combine_result"]["status"] == "matched"


@pytest.mark.parametrize(
    ("family", "folder", "invoice_role", "companion_role"),
    [
        ("hotel", "住宿发票", "hotel_invoice", "hotel_folio"),
        ("ride", "打车", "ride_invoice", "ride_itinerary"),
    ],
)
def test_pairing_required_unmatched_same_group_fails_archive_report_p2_gate(
    tmp_path: Path, family, folder, invoice_role, companion_role
):
    target_dir = tmp_path / folder
    target_dir.mkdir()
    invoice = target_dir / "opaque-a.pdf"
    companion = target_dir / "opaque-b.pdf"
    invoice.write_bytes(b"invoice")
    companion.write_bytes(b"companion")
    trace = _TraceStore(
        {str(invoice): "invoice-id", str(companion): "companion-id"}
    )
    artifact_metadata = {
        str(invoice): {
            "document_id": "invoice-id",
            "artifact_role": invoice_role,
            "source_message_uid": "same-mail",
            "date": "20260610",
            "amount": "100.00",
            "pairing_required": True,
        },
        str(companion): {
            "document_id": "companion-id",
            "artifact_role": companion_role,
            "source_message_uid": "same-mail",
            "date": "20260610",
            "amount": "999.00",
            "pairing_required": True,
        },
    }
    service = ArchiveService(
        writer=lambda *_args: "",
        finalizer=lambda _report, root: reconcile_archive_pairs(
            root,
            trace_store=trace,
            artifact_metadata=artifact_metadata,
        ),
    )

    report = service.archive([], tmp_path)

    assert report.can_complete is False
    assert report.unresolved_count == 1
    assert trace.fields["invoice-id"]["combine_result"]["status"] == "not_matched"
    assert trace.fields["companion-id"]["combine_result"]["status"] == "not_matched"
    assert trace.fields["invoice-id"]["combine_result"]["reason_code"].startswith(
        family.upper()
    )


def test_pairing_ambiguity_updates_every_trace_before_fail_closed(tmp_path: Path):
    hotel = tmp_path / "住宿发票"
    hotel.mkdir()
    paths = [hotel / f"opaque-{index}.pdf" for index in range(4)]
    for path in paths:
        path.write_bytes(b"artifact")
    ids = [f"doc-{index}" for index in range(4)]
    trace = _TraceStore(dict(zip(map(str, paths), ids)))
    artifact_metadata = {}
    for index, path in enumerate(paths):
        artifact_metadata[str(path)] = {
            "document_id": ids[index],
            "artifact_role": "hotel_invoice" if index < 2 else "hotel_folio",
            "source_message_uid": "same-mail",
            "date": "20260610",
            "amount": "100.00",
            "pairing_required": True,
        }

    report = ArchiveService(
        writer=lambda *_args: "",
        finalizer=lambda _report, root: reconcile_archive_pairs(
            root,
            trace_store=trace,
            artifact_metadata=artifact_metadata,
        ),
    ).archive([], tmp_path)

    assert report.can_complete is False
    assert report.unresolved_count == 1
    assert {
        trace.fields[document_id]["combine_result"]["status"]
        for document_id in ids
    } == {"ambiguous"}


class _TraceStore:
    def __init__(self, archive_ids=None):
        self.fields = {}
        self.archive_ids = dict(archive_ids or {})

    def set_fields(self, document_id, **fields):
        self.fields.setdefault(document_id, {}).update(fields)

    def record_failure_event(self, *_args, **_kwargs):
        return None

    def get_document_id_by_archive_target(self, path):
        return self.archive_ids.get(str(path))

    def move_archive_target(self, source, target):
        document_id = self.archive_ids.pop(str(source), None)
        if document_id:
            self.archive_ids[str(target)] = document_id


class _ArchiveAPI:
    def __init__(self):
        self.logs = []
        self.processed_invoices = []
        self.error_invoices = []
        self.discovered_categories = set()
        self.stats = {"invoices": 0, "errors": 0}
        self.audit_counts = {"manual_check": 0, "retention": 0, "raw_invoices": 0}
        self._cwt_cancellation_registry = []
        self.artifact_events = []

    @staticmethod
    def _resolve_active_company():
        return "辉瑞"

    @staticmethod
    def _inspect_pdf_health(_path):
        return {"pdf_health_class": "healthy"}

    @staticmethod
    def _retain_artifact(_root, path, _bucket, _reason, _metadata):
        return path

    @staticmethod
    def _send_to_manual_check(_root, path, _reason, **_kwargs):
        return path

    def _safe_emit_artifact_event(self, event, path, **kwargs):
        self.artifact_events.append((event, path, kwargs))

    @staticmethod
    def _safe_emit_stage_event(*_args, **_kwargs):
        return None

    @staticmethod
    def _cwt_cancellation_matching(_root):
        return None


class _ArchiveExtractor:
    last_route_trace = {"status": "archived"}

    def __init__(self, routed):
        self.routed = routed

    def route_and_rename_file(self, path, info_json, custom_rules=None):
        self.routed.append((path, dict(info_json), custom_rules))
        return True, path


def _adapter(tmp_path, api, extractor, trace_store=None):
    acceptance = SimpleNamespace(
        evaluate=lambda *_args, **_kwargs: {"accepted": True},
        normalized_snapshot=DocumentAcceptanceService.normalized_snapshot,
    )
    return AppArchiveAdapter(
        api=api,
        extractor=extractor,
        save_path=str(tmp_path),
        business_records={},
        trace_store=trace_store or _TraceStore(),
        acceptance_service=acceptance,
        pairing_finalizer=lambda *_args: None,
    )


def test_production_adapter_writes_normalized_snapshot_before_standard_route(
    tmp_path: Path,
):
    from invoice_extractor import InvoiceExtractor

    source = tmp_path / "raw.pdf"
    source.write_bytes(b"%PDF-1.4\nnormalized route")
    outcome = _resolved_outcome(
        source,
        info_json={
            "Date": "2026-06-10",
            "Amount": "¥ 1,000.00",
            "Purchaser": "辉瑞投资有限公司",
            "Seller": "标准商户",
            "InvoiceCode": "  CODE  ",
            "InvoiceNumber": "  NUMBER  ",
            "Type": "餐饮",
        },
    )
    extractor = InvoiceExtractor.__new__(InvoiceExtractor)
    extractor.output_dir = str(tmp_path / "output")
    extractor.last_route_trace = {}
    trace_store = _TraceStore()
    adapter = _adapter(tmp_path, _ArchiveAPI(), extractor, trace_store)

    report = ArchiveService(
        normalizer=adapter.normalize,
        classifier=adapter.classify,
        archive_operation=adapter.archive_operation,
        dedupe_key=adapter.dedupe_key,
        finalizer=adapter.finalize,
    ).archive([outcome], tmp_path)

    archived = report.outcomes[0]
    assert archived.outcome.status == "resolved"
    assert Path(archived.archive_path).name == "20260610_餐饮_1000.00_标准商户.pdf"
    assert adapter.pairing_metadata[str(archived.archive_path)]["date"] == "20260610"
    assert adapter.pairing_metadata[str(archived.archive_path)]["amount"] == "1000.00"
    normalized = trace_store.fields[outcome.candidate.identity.document_id][
        "normalized_fields"
    ]
    assert {field: normalized[field] for field in (
        "Date",
        "Amount",
        "Purchaser",
        "Seller",
        "InvoiceCode",
        "InvoiceNumber",
    )} == {
        "Date": "20260610",
        "Amount": "1000.00",
        "Purchaser": "辉瑞投资有限公司",
        "Seller": "标准商户",
        "InvoiceCode": "CODE",
        "InvoiceNumber": "NUMBER",
    }


@pytest.mark.parametrize("model_type", ["机票", "航班行程单"])
def test_strong_train_evidence_overrides_model_air_type_in_adapter(
    tmp_path: Path, model_type: str
):
    from app_api import InvoiceAppAPI

    source = tmp_path / "ticket.pdf"
    source.write_bytes(b"%PDF-1.4\ntrain")
    api = InvoiceAppAPI()
    api._inspect_pdf_health = lambda _path: {"pdf_health_class": "healthy"}
    api._extract_pdf_preview_text = lambda *_args, **_kwargs: "铁路电子客票 12306"
    outcome = _resolved_outcome(
        source,
        metadata={"subject": "电子客票"},
        info_json={
            "Type": model_type,
            "Seller": "中国铁路",
            "Departure_City": "北京",
            "Destination_City": "上海",
        },
    )

    payload = _adapter(tmp_path, api, _ArchiveExtractor([])).normalize(outcome)

    assert payload["info_json"]["Type"] == "火车票"
    assert "CLASSIFIED_AS_TRAIN_BY_STRONG_EVIDENCE" in payload[
        "classification_reason_codes"
    ]


@pytest.mark.parametrize(
    ("metadata", "info_json", "expected_type"),
    [
        (
            {"subject": "航空电子客票"},
            {
                "Type": "机票",
                "Seller": "中国东方航空股份有限公司",
                "Departure_City": "北京",
                "Destination_City": "上海",
            },
            "机票",
        ),
        (
            {"sender": "notification@citsgbt.com", "subject": "CWT flight"},
            {
                "Type": "机票",
                "Seller": "中国铁路",
                "Departure_City": "北京",
                "Destination_City": "上海",
            },
            "航班行程单",
        ),
    ],
)
def test_strong_train_override_does_not_capture_airline_or_cwt(
    tmp_path: Path, metadata: dict, info_json: dict, expected_type: str
):
    from app_api import InvoiceAppAPI

    source = tmp_path / "travel.pdf"
    source.write_bytes(b"%PDF-1.4\ntravel")
    api = InvoiceAppAPI()
    api._inspect_pdf_health = lambda _path: {"pdf_health_class": "healthy"}
    api._extract_pdf_preview_text = lambda *_args, **_kwargs: "航空电子客票"
    outcome = _resolved_outcome(source, metadata=metadata, info_json=info_json)

    payload = _adapter(tmp_path, api, _ArchiveExtractor([])).normalize(outcome)

    assert payload["info_json"]["Type"] == expected_type
    assert "CLASSIFIED_AS_TRAIN_BY_STRONG_EVIDENCE" not in payload[
        "classification_reason_codes"
    ]


@pytest.mark.parametrize(
    ("seller", "subject", "preview", "expected"),
    [
        ("中国铁路", "电子客票", "", True),
        ("其他承运人", "12306 电子客票", "", True),
        ("其他承运人", "电子客票", "铁路电子客票", True),
        ("中国东方航空", "航空电子客票", "航空电子客票", False),
    ],
)
def test_app_api_train_compatibility_delegate_matches_shared_rule(
    seller: str, subject: str, preview: str, expected: bool
):
    from app_api import InvoiceAppAPI
    from document_types import looks_like_train_ticket

    info_json = {
        "Departure_City": "北京",
        "Destination_City": "上海",
    }
    info = {"subject": subject}
    api = InvoiceAppAPI()
    api._extract_pdf_preview_text = lambda *_args, **_kwargs: preview

    shared = looks_like_train_ticket(
        "机票",
        seller,
        info_json,
        info,
        "ticket.pdf",
        preview_loader=lambda: preview,
    )

    assert shared is expected
    assert api._looks_like_train_ticket(
        "机票", seller, info_json, info, "ticket.pdf", pdf_path="ticket.pdf"
    ) is expected
