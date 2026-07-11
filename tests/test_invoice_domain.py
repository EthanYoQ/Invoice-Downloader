from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime, timezone
from decimal import Decimal
import unittest
from zoneinfo import ZoneInfo

import archive_pairing
from app_api import classify_cwt_document_type, normalize_document_type_for_archive
from company_rules import classify_purchaser_relation
from document_types import normalize_document_type
from invoice_domain import (
    ArchivedArtifact,
    DocumentIdentity,
    InvoiceRecord,
    RouteInfo,
    parse_amount,
    parse_local_date,
)


class DomainParserTests(unittest.TestCase):
    def test_parse_amount_accepts_grouping_currency_and_negative_values(self):
        self.assertEqual(parse_amount(" CNY 1,234.50 "), Decimal("1234.50"))
        self.assertEqual(parse_amount("-￥145.00元"), Decimal("-145.00"))
        self.assertEqual(parse_amount(12), Decimal("12"))

    def test_parse_amount_preserves_unknown_as_none_and_rejects_bool(self):
        for value in (None, "", "未知", "0x12", object()):
            with self.subTest(value=value):
                self.assertIsNone(parse_amount(value))
        with self.assertRaises(TypeError):
            parse_amount(True)
        with self.assertRaises(TypeError):
            parse_amount(False)

    def test_parse_local_date_keeps_local_dates_and_normalizes_aware_values(self):
        shanghai = ZoneInfo("Asia/Shanghai")
        self.assertEqual(parse_local_date(date(2026, 6, 1)), date(2026, 6, 1))
        self.assertEqual(parse_local_date(datetime(2026, 6, 1, 23, 0)), date(2026, 6, 1))
        self.assertEqual(
            parse_local_date(datetime(2026, 6, 1, 16, 30, tzinfo=timezone.utc)),
            date(2026, 6, 2),
        )
        timestamp = datetime(2026, 6, 1, 16, 30, tzinfo=timezone.utc).timestamp()
        self.assertEqual(parse_local_date(timestamp), date(2026, 6, 2))
        self.assertEqual(
            parse_local_date(datetime(2026, 6, 2, 0, 30, tzinfo=shanghai)),
            date(2026, 6, 2),
        )

    def test_parse_local_date_supports_legacy_shapes_without_guessing_unknowns(self):
        self.assertEqual(parse_local_date("20260601"), date(2026, 6, 1))
        self.assertEqual(parse_local_date("2026-06-01"), date(2026, 6, 1))
        self.assertEqual(parse_local_date("2026年06月01日"), date(2026, 6, 1))
        self.assertEqual(parse_local_date("开票日期: 2026/06/01"), date(2026, 6, 1))
        for value in (None, "", "未知", "未知日期", "20261340", True):
            with self.subTest(value=value):
                self.assertIsNone(parse_local_date(value))


class InvoiceRecordTests(unittest.TestCase):
    def setUp(self):
        self.identity = DocumentIdentity(
            document_id="doc-1",
            source_message_uid="7051",
            source_filename="invoice.pdf",
            source_locator="C:/staging/invoice.pdf",
            source_kind="attachment",
        )

    def test_domain_values_are_frozen(self):
        route = RouteInfo(departure_date=date(2026, 6, 1), departure_city="长沙", destination_city="杭州")
        record = InvoiceRecord(
            identity=self.identity,
            is_invoice=True,
            invoice_date=date(2026, 6, 1),
            purchaser="辉瑞投资有限公司",
            seller="测试酒店",
            amount=Decimal("441.15"),
            invoice_code="044001",
            invoice_number="12345678",
            document_type="住宿发票",
            category="住宿发票",
            route=route,
        )
        artifact = ArchivedArtifact(
            identity=self.identity,
            role="hotel_invoice",
            path="C:/output/invoice.pdf",
            filename="invoice.pdf",
            document_type="住宿发票",
            amount=Decimal("441.15"),
            business_date=date(2026, 6, 1),
            seller="测试酒店",
        )

        for value in (self.identity, route, record, artifact):
            with self.subTest(value=type(value).__name__), self.assertRaises(FrozenInstanceError):
                value.identity = self.identity

    def test_from_legacy_exposes_typed_values_and_round_trips_every_key(self):
        legacy = {
            "is_invoice": True,
            "Date": "2026-06-01",
            "Purchaser": "辉瑞投资有限公司",
            "Seller": "湖南运达酒店管理有限公司长沙运达喜来登酒店",
            "Amount": "1,950.00",
            "InvoiceCode": "044001",
            "InvoiceNumber": "26432000001239781576",
            "Type": "酒店住宿发票",
            "category": "住宿发票",
            "Departure_Date": "",
            "Departure_City": "",
            "Destination_City": "",
            "_is_folio": False,
            "_is_itinerary": False,
            "_cwt_cancellation": False,
            "rejection_reason": "",
            "provider_context": {"provider": "baiwang", "attempts": [1, 2]},
            "custom_unknown": ["keep", {"nested": True}],
        }

        record = InvoiceRecord.from_legacy(legacy, self.identity)

        self.assertEqual(record.invoice_date, date(2026, 6, 1))
        self.assertEqual(record.amount, Decimal("1950.00"))
        self.assertEqual(record.document_type, "住宿发票")
        self.assertEqual(record.invoice_code, "044001")
        self.assertEqual(record.invoice_number, "26432000001239781576")
        self.assertEqual(record.to_legacy(), legacy)

        restored = record.to_legacy()
        restored["provider_context"]["attempts"].append(3)
        self.assertEqual(record.to_legacy(), legacy)

    def test_legacy_unknown_markers_become_none_without_being_lost(self):
        legacy = {
            "Date": "未知日期",
            "Amount": "未知",
            "Type": "未知分类",
            "Seller": "未知开票方",
            "InvoiceCode": "",
            "InvoiceNumber": "",
        }

        record = InvoiceRecord.from_legacy(legacy, self.identity)

        self.assertIsNone(record.invoice_date)
        self.assertIsNone(record.amount)
        self.assertEqual(record.document_type, "其他")
        self.assertEqual(record.to_legacy(), legacy)

    def test_direct_record_serializes_to_the_existing_legacy_contract(self):
        record = InvoiceRecord(
            identity=self.identity,
            is_invoice=False,
            invoice_date=date(2026, 6, 5),
            purchaser="个人",
            seller="滴滴出行",
            amount=Decimal("100.00"),
            invoice_code="",
            invoice_number="",
            document_type="行程单",
            category="打车",
            route=RouteInfo(),
            flags=frozenset({"itinerary"}),
        )

        self.assertEqual(
            record.to_legacy(),
            {
                "is_invoice": False,
                "Date": "20260605",
                "Purchaser": "个人",
                "Seller": "滴滴出行",
                "Amount": "100.00",
                "InvoiceCode": "",
                "InvoiceNumber": "",
                "Type": "行程单",
                "category": "打车",
                "Departure_Date": "",
                "Departure_City": "",
                "Destination_City": "",
                "_is_itinerary": True,
            },
        )

    def test_bool_amount_is_rejected_at_legacy_boundary(self):
        with self.assertRaises(TypeError):
            InvoiceRecord.from_legacy({"Amount": True}, self.identity)

    def test_extractor_boundary_round_trips_through_invoice_record(self):
        from invoice_extractor import InvoiceExtractor

        payload = {
            "Date": "20260601",
            "Amount": "441.15",
            "Seller": "全季杭州下沙大学城酒店",
            "Purchaser": "辉瑞投资有限公司",
            "Type": "住宿发票",
            "InvoiceCode": "",
            "InvoiceNumber": "12345678",
            "provider_trace": {"engine": "local", "attempts": [1]},
        }
        extractor = InvoiceExtractor(api_key="", output_dir=".")

        result = extractor._adapt_extraction_result(
            payload,
            pdf_path="C:/staging/invoice.pdf",
            document_context={"email_id": "7051", "original_filename": "invoice.pdf"},
        )

        self.assertEqual(result, payload)

    def test_pairing_boundary_rejects_bool_amount(self):
        with self.assertRaises(TypeError):
            archive_pairing._pairing_document({"amount": True}, "hotel_invoice")


class CanonicalRuleParityTests(unittest.TestCase):
    def test_document_type_normalization_preserves_current_vocabulary(self):
        fixtures = {
            "高铁电子客票": "火车票",
            "航班行程单": "航班行程单",
            "酒店Folio": "住宿水单",
            "酒店住宿发票": "住宿发票",
            "滴滴行程报销单": "行程单",
            "高德打车": "打车",
            "差旅服务费发票": "差旅服务费",
            "完全未知": "其他",
        }
        for raw, expected in fixtures.items():
            with self.subTest(raw=raw):
                self.assertEqual(normalize_document_type(raw), expected)

    def test_company_classification_preserves_current_fixtures(self):
        fixtures = (
            ("辉瑞投资有限公司", "辉瑞", "target"),
            ("辉瑞投资有限公司", "generic", "target"),
            ("个人", "辉瑞", "non_target"),
            ("暂无抬头", "辉瑞", "unknown"),
            ("", "辉瑞", "unknown"),
            ("其他公司", "辉瑞", "non_target"),
        )
        for purchaser, company, expected in fixtures:
            with self.subTest(purchaser=purchaser, company=company):
                self.assertEqual(classify_purchaser_relation(purchaser, company), expected)

    def test_archive_normalizer_parity_includes_mutations_and_reason_codes(self):
        fixtures = (
            (
                {"Type": "住宿", "Seller": "Sheraton Changsha Hotel"},
                "csxsi_folio_ef_sj_gc524340322.pdf",
                ("住宿水单", ["CLASSIFIED_AS_HOTEL_FOLIO"]),
                {"_is_folio": True},
            ),
            (
                {"Type": "行程单", "Seller": "滴滴出行"},
                "滴滴行程报销单.pdf",
                ("打车", ["CLASSIFIED_AS_RIDE_ITINERARY"]),
                {"_is_itinerary": True},
            ),
            (
                {"Type": "非目标公司发票", "Seller": "CITS GBT"},
                "行程单 - 机票.pdf",
                ("非目标公司发票", ["PRESERVED_NON_TARGET_COMPANY"]),
                {},
            ),
        )
        for original, filename, expected, mutations in fixtures:
            with self.subTest(filename=filename):
                payload = dict(original)
                self.assertEqual(normalize_document_type_for_archive(payload, filename), expected)
                for key, value in mutations.items():
                    self.assertEqual(payload[key], value)

    def test_cwt_classifier_parity_includes_mutations_and_reason_codes(self):
        cases = (
            (
                {"Type": "机票", "Seller": "CITS GBT"},
                {"subject": "CITS GBT Invoice"},
                "SCCT00919573.pdf",
                True,
                ("机票", ["PRESERVED_LOCAL_CITS_GBT_TYPE"]),
                {},
            ),
            (
                {"Type": "其他", "Seller": "CITS GBT"},
                {"subject": "酒店预订"},
                "酒店取消.pdf",
                False,
                ("住宿确认单", ["CWT_HOTEL_CANCELLATION"]),
                {"_cwt_cancellation": True},
            ),
            (
                {"Type": "其他", "Seller": "GBT Travel Services"},
                {"subject": "SCCT service"},
                "invoice.pdf",
                False,
                ("差旅服务费", ["CLASSIFIED_AS_CWT_SERVICE_FEE"]),
                {},
            ),
        )
        for payload, info, filename, fast_path, expected, mutations in cases:
            with self.subTest(filename=filename):
                actual_payload = dict(payload)
                self.assertEqual(
                    classify_cwt_document_type(actual_payload, info, filename, fast_path),
                    expected,
                )
                for key, value in mutations.items():
                    self.assertEqual(actual_payload[key], value)


if __name__ == "__main__":
    unittest.main()
