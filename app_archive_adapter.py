from __future__ import annotations

from pathlib import Path
import os
from typing import Any, Callable

from archive_pairing_service import reconcile_archive_pairs
from archive_service import ArchiveDecision, ArchiveReport
from extraction_pipeline import ExtractionOutcome

class AppArchiveAdapter:
    """Product rule adapter used by ArchiveService's serialized owner loop."""

    def __init__(
        self,
        *,
        api: Any,
        extractor: Any,
        save_path: str,
        business_records: dict[str, Any],
        trace_store: Any,
        acceptance_service: Any | None = None,
        pairing_finalizer: Callable[[ArchiveReport, Path], None] | None = None,
    ) -> None:
        self.api = api
        self.extractor = extractor
        self.save_path = save_path
        self.business_records = business_records
        self.trace_store = trace_store
        if acceptance_service is None:
            from document_acceptance import DocumentAcceptanceService

            acceptance_service = DocumentAcceptanceService(api)
        self.acceptance_service = acceptance_service
        self.pairing_finalizer = pairing_finalizer
        self.pairing_metadata: dict[str, dict[str, Any]] = {}

    def _append_success_state(
        self,
        outcome: ExtractionOutcome,
        info_json: dict[str, Any],
        category: str,
        path: str,
    ) -> None:
        import time

        self.api.discovered_categories.add(category)
        self.api.processed_invoices.append(
            {
                "id": f"inv_{time.time()}_{outcome.candidate.sequence}",
                "date": info_json.get("Date", "---"),
                "amount": f"¥ {info_json.get('Amount', '0.00')}",
                "category": category,
                "merchant": info_json.get("Seller", "未知开票方"),
                "path": path,
            }
        )
        self.api.stats["invoices"] = int(self.api.stats.get("invoices", 0)) + 1

    def _append_prefilter_error_state(
        self,
        outcome: ExtractionOutcome,
        path: str,
    ) -> None:
        import time

        is_manual = outcome.status == "manual_review"
        self.api.stats["errors"] = int(self.api.stats.get("errors", 0)) + 1
        self.api.error_invoices.append(
            {
                "id": f"inv_prefilter_{time.time()}_{outcome.candidate.sequence}",
                "date": "---",
                "amount": "---",
                "category": "人工复核" if is_manual else "预过滤保全",
                "merchant": "低置信度候选" if is_manual else "高/中置信度候选",
                "path": path,
                "name": os.path.basename(path),
                "sColor": "bg-yellow-500" if is_manual else "bg-blue-500",
                "status": "待人工复核" if is_manual else "已保全待判断",
                "reason": outcome.reason_code
                or ("P0_C_MANUAL_REVIEW" if is_manual else "P0_B_RETENTION"),
                "rColor": (
                    "text-yellow-600 border-yellow-200 bg-yellow-50"
                    if is_manual
                    else "text-blue-600 border-blue-200 bg-blue-50"
                ),
            }
        )

    def _append_classified_manual_state(
        self,
        outcome: ExtractionOutcome,
        info_json: dict[str, Any],
        category: str,
        path: str,
        reason_code: str,
    ) -> None:
        import time

        self.api.stats["errors"] = int(self.api.stats.get("errors", 0)) + 1
        self.api.error_invoices.append(
            {
                "id": f"inv_{time.time()}_{outcome.candidate.sequence}",
                "date": info_json.get("Date", "---"),
                "amount": f"¥ {info_json.get('Amount', '0.00')}",
                "category": category,
                "merchant": info_json.get("Seller", "未知开票方"),
                "path": path,
                "name": os.path.basename(path),
                "artifact_kind": "manual_check",
                "sColor": "bg-yellow-500",
                "status": "待人工复核",
                "reason": reason_code,
                "rColor": "text-yellow-600 border-yellow-200 bg-yellow-50",
            }
        )

    def normalize(self, outcome: ExtractionOutcome) -> dict[str, Any]:
        import time

        from app_api import normalize_document_type_for_archive
        from company_rules import classify_purchaser_relation
        from document_types import (
            apply_strong_train_evidence_override,
            classify_cwt_document_type,
            get_archive_folder,
            is_exempt_type,
        )

        payload = outcome.to_legacy_payload()
        info_json = dict(payload.get("info_json") or {})
        metadata = dict(payload.get("metadata") or {})
        file_name = os.path.basename(
            payload.get("pdf_path") or outcome.candidate.source_path
        )
        pdf_path = str(payload.get("pdf_path") or outcome.candidate.source_path)
        pdf_health = self.api._inspect_pdf_health(pdf_path)
        acceptance = self.acceptance_service.evaluate(
            metadata,
            info_json,
            pdf_path,
            pdf_health=pdf_health,
        )
        payload["document_acceptance"] = acceptance
        payload["acceptance_pdf_health"] = pdf_health
        self.trace_store.set_fields(
            outcome.candidate.identity.document_id,
            document_acceptance=acceptance,
        )
        acceptance_rejected = not acceptance.get("accepted", True)
        if acceptance_rejected:
            payload["archive_status"] = "acceptance_rejected"
        else:
            from document_acceptance import DocumentAcceptanceService

            normalized_snapshot = self.acceptance_service.normalized_snapshot(info_json)
            DocumentAcceptanceService.write_canonical_fields(
                info_json, normalized_snapshot
            )
        original_type = str(info_json.get("Type") or "")
        seller = str(info_json.get("Seller") or "")
        is_cwt = (
            "citsgbt.com" in str(metadata.get("sender") or "").lower()
            or any(
                keyword in seller
                for keyword in (
                    "国旅运通",
                    "CWT",
                    "Carlson Wagonlit",
                    "citsgbt",
                    "GBT Travel",
                )
            )
            or any(
                keyword in str(metadata.get("subject") or "").lower()
                for keyword in ("citsgbt", "国旅运通", "cwt", "cits gbt", "scct")
            )
            or any(
                keyword in file_name.lower()
                for keyword in ("citsgbt", "国旅运通", "cwt", "scct")
            )
        )
        extraction_trace = dict(payload.get("extraction_trace") or {})
        if is_cwt:
            doc_type, reason_codes = classify_cwt_document_type(
                info_json,
                metadata,
                file_name,
                local_cits_fast_path=(
                    extraction_trace.get("engine") == "local_cits_gbt_pdf"
                    or extraction_trace.get("reason_code")
                    == "LOCAL_CITS_GBT_PDF_FAST_PATH"
                ),
            )
        else:
            doc_type, reason_codes = normalize_document_type_for_archive(
                info_json, file_name
            )
            preview_reader = getattr(self.api, "_extract_pdf_preview_text", None)
            doc_type, train_reason_codes = apply_strong_train_evidence_override(
                doc_type,
                original_type,
                seller,
                info_json,
                metadata,
                file_name,
                preview_loader=(
                    lambda: preview_reader(pdf_path, max_pages=1)
                    if callable(preview_reader)
                    else ""
                ),
            )
            reason_codes.extend(train_reason_codes)
            if (
                not train_reason_codes
                and original_type in {"住宿确认单", "航班行程单", "差旅服务费"}
            ):
                doc_type = original_type
                reason_codes.append("PRESERVED_REGISTERED_SUPPORTING_TYPE")
        info_json["Type"] = doc_type
        is_exempt = is_exempt_type(doc_type)
        payload["classification_reason_codes"] = reason_codes
        payload["is_exempt"] = is_exempt
        if (
            not acceptance_rejected
            and not bool(info_json.get("is_invoice", True))
            and not is_exempt
        ):
            payload["archive_status"] = "retained"
        elif not acceptance_rejected and not is_exempt:
            relation = classify_purchaser_relation(
                str(info_json.get("Purchaser") or ""),
                self.api._resolve_active_company(),
            )
            if relation == "non_target":
                info_json["Type"] = "非目标公司发票"
                message = (
                    "购买方与目标公司不匹配，已分流到非目标公司发票 "
                    "[CLASSIFIED_AS_NON_TARGET_COMPANY]"
                    if outcome.candidate.identity.source_kind == "url"
                    else (
                        "购买方与目标公司不匹配 "
                        f"({str(info_json.get('Purchaser') or '')})，已分流到非目标公司发票 "
                        "[CLASSIFIED_AS_NON_TARGET_COMPANY]"
                    )
                )
                try:
                    self.api.logs.append(
                        {
                            "time": time.strftime("[%H:%M:%S]"),
                            "type": "拦截:",
                            "color": "text-yellow-400",
                            "msg": message,
                        }
                    )
                except Exception:
                    pass
            elif relation == "unknown":
                payload["archive_status"] = "manual_review"
                payload["manual_reason_code"] = "COMPANY_PURCHASER_UNKNOWN"
        cwt_classified = any(
            reason.startswith("CLASSIFIED_AS_CWT_") or reason.startswith("CWT_")
            for reason in reason_codes
        )
        if (
            not acceptance_rejected
            and int(metadata.get("tier", 0) or 0) == 3
            and not cwt_classified
        ):
            payload["archive_status"] = "manual_review"
            payload["manual_reason_code"] = "TIER3_MANUAL_REVIEW"
        if not acceptance_rejected and info_json.get("_cwt_cancellation"):
            payload["archive_status"] = "manual_review"
        payload["info_json"] = info_json
        payload["category"] = get_archive_folder(info_json.get("Type", "其他"))
        if (
            payload.get("archive_status") not in {"retained", "acceptance_rejected"}
            and payload.get("manual_reason_code") != "COMPANY_PURCHASER_UNKNOWN"
        ):
            self.api.discovered_categories.add(payload["category"])
        return payload

    @staticmethod
    def classify(payload: dict[str, Any]) -> str:
        return str(payload.get("category") or "其他")

    @staticmethod
    def dedupe_key(outcome: ExtractionOutcome, payload: dict[str, Any]) -> str:
        from app_api import build_business_record_key

        info_json = payload.get("info_json") or {}
        code = str(info_json.get("InvoiceCode") or "").strip()
        number = str(info_json.get("InvoiceNumber") or "").strip()
        if code or number:
            return build_business_record_key(
                code, number, info_json, outcome.candidate.source_filename
            )
        return outcome.candidate.identity.document_id

    def archive_operation(
        self,
        outcome: ExtractionOutcome,
        payload: dict[str, Any] | None,
        category: str,
        root: Path,
    ) -> ArchiveDecision:
        del category, root
        from app_api import record_business_success
        from document_types import MANUAL_REVIEW_FOLDER, get_archive_folder

        metadata = (
            dict(payload.get("metadata") or {})
            if isinstance(payload, dict)
            else outcome.candidate.to_legacy()
        )
        source_path = (
            str(payload.get("pdf_path") or outcome.candidate.source_path)
            if isinstance(payload, dict)
            else outcome.candidate.source_path
        )
        if outcome.status != "resolved":
            if outcome.status == "manual_review":
                path = self.api._send_to_manual_check(
                    self.save_path,
                    source_path,
                    outcome.reason_code or "PIPELINE_MANUAL_REVIEW",
                    metadata=metadata,
                    is_url=outcome.candidate.identity.source_kind == "url",
                )
                self._append_prefilter_error_state(outcome, path)
                return ArchiveDecision(path=path, status="manual_review")
            path = self.api._retain_artifact(
                self.save_path,
                source_path,
                f"pipeline_{outcome.status}",
                outcome.reason_code or outcome.status,
                metadata,
            )
            if outcome.status == "retained":
                self._append_prefilter_error_state(outcome, path)
            if outcome.status == "quota_exhausted":
                self.api._mark_quota_exhausted("GLM API 额度不足")
            return ArchiveDecision(path=path, status="unresolved")

        archive_status = str(payload.get("archive_status") or "")
        if archive_status == "acceptance_rejected":
            acceptance = dict(payload.get("document_acceptance") or {})
            snapshot = self.acceptance_service.normalized_snapshot(
                dict(payload.get("info_json") or {})
            )
            diagnostic_metadata = self.api._attachment_diag_metadata(
                metadata,
                file_name=os.path.basename(source_path),
                document_id=outcome.candidate.identity.document_id,
                extra={
                    "tier": metadata.get("tier", 0),
                    "pdf_health": dict(payload.get("acceptance_pdf_health") or {}),
                    "normalized_snapshot": snapshot,
                    "document_acceptance": acceptance,
                },
            )
            path = self.api._retain_artifact(
                self.save_path,
                source_path,
                acceptance.get("bucket") or "provider_guard_rejected",
                acceptance.get("message")
                or "Downloaded result rejected by document acceptance gate.",
                diagnostic_metadata,
            )
            self.api.stats["errors"] = int(self.api.stats.get("errors", 0)) + 1
            try:
                import time

                self.api.logs.append(
                    {
                        "time": time.strftime("[%H:%M:%S]"),
                        "type": "保全:",
                        "color": "text-yellow-400",
                        "msg": f"下载结果被文档验收闸门拦截: {os.path.basename(path)}",
                    }
                )
            except Exception:
                pass
            self.trace_store.set_fields(
                outcome.candidate.identity.document_id,
                normalized_fields=snapshot,
                classification_result={
                    "status": "rejected",
                    "reason_code": acceptance.get("reason_code"),
                    "provider_family": acceptance.get("provider_family", ""),
                },
                naming_result={
                    "status": "skipped",
                    "reason_code": acceptance.get("reason_code"),
                },
                combine_keys={
                    "status": "not_applicable",
                    "reason_code": "COMBINE_NOT_APPLICABLE",
                },
                combine_result={
                    "status": "not_applicable",
                    "reason_code": "COMBINE_NOT_APPLICABLE",
                },
                archive_target=path,
            )
            record_failure = getattr(self.trace_store, "record_failure_event", None)
            if callable(record_failure):
                record_failure(
                    outcome.candidate.identity.document_id,
                    acceptance.get("reason_code") or "DOCUMENT_ACCEPTANCE_REJECTED",
                    "document_acceptance",
                    acceptance.get("message")
                    or "Downloaded result rejected by document acceptance gate.",
                    severity="failure",
                )
            return ArchiveDecision(
                path=path,
                status="unresolved",
                reason_code=acceptance.get("reason_code")
                or "DOCUMENT_ACCEPTANCE_REJECTED",
            )
        if bool((payload.get("info_json") or {}).get("_cwt_cancellation")):
            path = self.api._send_to_manual_check(
                self.save_path,
                source_path,
                "CWT_HOTEL_CANCELLATION",
                metadata={
                    "subject": metadata.get("subject", ""),
                    "file_name": os.path.basename(source_path),
                },
                is_url=outcome.candidate.identity.source_kind == "url",
            )
            registry = getattr(self.api, "_cwt_cancellation_registry", None)
            if registry is None:
                registry = []
                self.api._cwt_cancellation_registry = registry
            registry.append(
                {
                    "file_name": os.path.basename(source_path),
                    "manual_check_path": path,
                }
            )
            self.trace_store.set_fields(
                outcome.candidate.identity.document_id,
                archive_target=path,
                naming_result={
                    "status": "manual_check",
                    "reason_code": "CWT_HOTEL_CANCELLATION",
                },
            )
            return ArchiveDecision(
                path=path,
                status="manual_review",
                reason_code="CWT_HOTEL_CANCELLATION",
            )
        if archive_status == "manual_review":
            reason_code = str(
                payload.get("manual_reason_code") or "PIPELINE_MANUAL_REVIEW"
            )
            path = self.api._send_to_manual_check(
                self.save_path,
                source_path,
                reason_code,
                metadata=metadata,
                is_url=outcome.candidate.identity.source_kind == "url",
            )
            self._append_classified_manual_state(
                outcome,
                dict(payload.get("info_json") or {}),
                str(payload.get("category") or "待人工复核"),
                path,
                reason_code,
            )
            return ArchiveDecision(
                path=path,
                status="manual_review",
                reason_code=reason_code,
            )
        if archive_status == "retained":
            info_json = dict(payload.get("info_json") or {})
            path = self.api._retain_artifact(
                self.save_path,
                source_path,
                "model_rejected",
                "模型判定为非票据，转人工复核保全",
                {
                    "subject": metadata.get("subject", ""),
                    "tier": metadata.get("tier", 0),
                    "file_name": os.path.basename(source_path),
                    "rejection_reason": info_json.get("rejection_reason", ""),
                    "model_type": info_json.get("Type", ""),
                },
            )
            self.api.stats["errors"] = int(self.api.stats.get("errors", 0)) + 1
            self.api.error_invoices.append(
                {
                    "id": f"inv_{__import__('time').time()}_{outcome.candidate.sequence}",
                    "date": info_json.get("Date", "---"),
                    "amount": f"¥ {info_json.get('Amount', '0.00')}",
                    "category": "保留记录",
                    "merchant": info_json.get("Seller", "未知开票方"),
                    "path": path,
                    "name": os.path.basename(path),
                    "artifact_kind": "retention",
                    "sColor": "bg-yellow-500",
                    "status": "已保全待确认",
                    "reason": info_json.get(
                        "rejection_reason", "模型判定为非票据，原件已保全"
                    ),
                    "rColor": "text-yellow-600 border-yellow-200 bg-yellow-50",
                }
            )
            return ArchiveDecision(path=path, status="retained")

        info_json = payload["info_json"]
        mapped_folder = get_archive_folder(info_json.get("Type", ""))
        custom_rules = (
            {info_json.get("Type", ""): mapped_folder}
            if mapped_folder != info_json.get("Type", "")
            else None
        )
        success, path = self.extractor.route_and_rename_file(
            source_path, info_json, custom_rules=custom_rules
        )
        if not success:
            retained = self.api._retain_artifact(
                self.save_path,
                source_path,
                "archive_failures",
                "ARCHIVE_STAGE_FAILED",
                metadata,
            )
            return ArchiveDecision(path=retained, status="unresolved")
        naming_trace = getattr(self.extractor, "last_route_trace", {}) or {}
        if MANUAL_REVIEW_FOLDER in str(path) or naming_trace.get("used_manual_check"):
            reason_code = str(
                naming_trace.get("reason_code") or "ROUTE_TO_MANUAL_CHECK"
            )
            self._append_classified_manual_state(
                outcome,
                info_json,
                mapped_folder,
                path,
                reason_code,
            )
            return ArchiveDecision(
                path=path,
                status="manual_review",
                reason_code=reason_code,
            )
        code = str(info_json.get("InvoiceCode") or "").strip()
        number = str(info_json.get("InvoiceNumber") or "").strip()
        if code or number:
            record_business_success(
                self.business_records,
                code,
                number,
                info_json,
                os.path.basename(path),
            )
        from app_api import canonical_artifact_role

        artifact_role = canonical_artifact_role(info_json, os.path.basename(path))
        if info_json.get("Type") == "住宿确认单":
            artifact_role = "hotel_order"
        elif info_json.get("Type") == "航班行程单":
            artifact_role = "flight_itinerary"
        self.pairing_metadata[str(path)] = {
            "document_id": outcome.candidate.identity.document_id,
            "source_message_uid": outcome.candidate.identity.source_message_uid,
            "email_id": outcome.candidate.identity.source_message_uid,
            "source_kind": outcome.candidate.identity.source_kind,
            "provider_group_key": outcome.candidate.identity.provider_group_key,
            "provider_family": metadata.get("provider_family", ""),
            "seller": info_json.get("Seller", ""),
            "date": info_json.get("Date", ""),
            "amount": info_json.get("Amount", ""),
            "artifact_role": artifact_role,
            "pairing_required": artifact_role
            in {"ride_invoice", "ride_itinerary", "hotel_invoice", "hotel_folio"},
        }
        self.trace_store.set_fields(
            outcome.candidate.identity.document_id,
            normalized_fields=info_json,
            classification_result={"status": "classified", "category": mapped_folder},
            naming_result=dict(naming_trace) or {"status": "archived"},
            archive_target=path,
        )
        self.api._safe_emit_artifact_event(
            "archived",
            path,
            document_id=outcome.candidate.identity.document_id,
            category=mapped_folder,
        )
        self._append_success_state(outcome, info_json, mapped_folder, path)
        return ArchiveDecision(path=path, status="archived")

    def finalize(self, report: ArchiveReport, root: Path) -> dict[str, str] | None:
        if self.pairing_finalizer is not None:
            return self.pairing_finalizer(report, root)
        reconcile_archive_pairs(
            root,
            event_sink=lambda event: self.api._safe_emit_stage_event(
                "archive_pairing", "result", event
            ),
            trace_store=self.trace_store,
            artifact_metadata=self.pairing_metadata,
        )
        self.api._cwt_cancellation_matching(str(root))
        iterator = getattr(self.trace_store, "iter_records", None)
        if not callable(iterator):
            return None
        return {
            str(row.get("document_id")): str(row.get("archive_target"))
            for row in iterator()
            if row.get("document_id") and row.get("archive_target")
        }
