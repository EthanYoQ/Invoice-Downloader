import json
import re
import sys
import types
import unittest
from pathlib import Path

import document_types
import email_fetcher
from app_api import InvoiceAppAPI
from invoice_extractor import InvoiceExtractor


ROOT = Path(__file__).resolve().parents[1]


class DocumentTypeContractTests(unittest.TestCase):
    def test_llm_type_constraint_matches_document_type_registry(self):
        parser_types = set(InvoiceExtractor._valid_types())
        expected_types = set(document_types.DOCUMENT_TYPES)
        self.assertEqual(parser_types, expected_types)
        self.assertIn("住宿发票", parser_types)
        self.assertIn("住宿水单", parser_types)

    def test_unknown_lodging_type_falls_back_to_registered_lodging_invoice(self):
        parser = InvoiceExtractor(api_key="", output_dir=str(ROOT / "tmp" / "contract-extractor"))
        result = parser._normalize_type_from_text("酒店住宿发票")
        self.assertEqual(result, "住宿发票")


class ArchivePairingContractTests(unittest.TestCase):
    def test_ride_pairing_preserves_existing_adjacent_filename_contract(self):
        from archive_pairing import build_ride_pair_renames, parse_archived_filename

        invoice = parse_archived_filename("20260605_打车_100.00_滴滴出行.pdf")
        itinerary = parse_archived_filename("20260605_行程单_100.00_滴滴出行.pdf")

        pair = build_ride_pair_renames(invoice, itinerary, 1)

        self.assertEqual(pair.invoice_filename, "0605-滴滴-01-发票_100.00元.pdf")
        self.assertEqual(pair.supporting_filename, "0605-滴滴-01-行程单_100.00元.pdf")
        self.assertEqual(pair.pair_label, "滴滴")

    def test_hotel_pairing_preserves_existing_adjacent_filename_contract(self):
        from archive_pairing import build_hotel_pair_renames, parse_archived_filename

        invoice = parse_archived_filename("20260601_住宿发票_1950.00_湖南运达酒店管理有限公司长沙运达喜来登酒店.pdf")
        folio = parse_archived_filename("20260601_住宿水单_1950.00_Sheraton Changsha Hotel.pdf")

        pair = build_hotel_pair_renames(invoice, folio, 1)

        self.assertEqual(pair.invoice_filename, "20260601-住宿-01-发票_1950.00元.pdf")
        self.assertEqual(pair.supporting_filename, "20260601-住宿-01-水单_1950.00元.pdf")
        self.assertEqual(pair.pair_label, "住宿")


class WindowControlContractTests(unittest.TestCase):
    def test_backend_exposes_maximize_window_api(self):
        calls = []

        class FakeWindow:
            def maximize(self):
                calls.append("maximize")

        fake_webview = types.SimpleNamespace(windows=[FakeWindow()])
        original = sys.modules.get("webview")
        sys.modules["webview"] = fake_webview
        try:
            result = InvoiceAppAPI().maximize_window()
        finally:
            if original is None:
                sys.modules.pop("webview", None)
            else:
                sys.modules["webview"] = original

        self.assertEqual(calls, ["maximize"])
        self.assertTrue(result["success"])

    def test_frontend_renders_three_window_controls(self):
        source = (ROOT / "templates" / "index_app.js").read_text(encoding="utf-8")
        self.assertIn('callApi("minimize_window")', source)
        self.assertIn('callApi("maximize_window")', source)
        self.assertIn('callApi("close_window")', source)
        self.assertIn("window-traffic-button--maximize", source)


class ProviderWrapperContractTests(unittest.TestCase):
    def test_baiwang_email_field_wrapper_delegates_to_provider_parser(self):
        body = "您好：王府井饭店管理有限公司北京金茂万丽酒店为您开具了电子发票 购买方名称 辉瑞投资有限公司 价税合计 1950.00 开票日期 2026年06月01日 发票号码 26432000001239781576"

        result = email_fetcher.extract_baiwang_email_fields(body)

        self.assertEqual(result["seller"], "王府井饭店管理有限公司北京金茂万丽酒店")
        self.assertEqual(result["purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["amount"], "1950.00")
        self.assertEqual(result["invoice_date"], "2026-06-01")
        self.assertEqual(result["invoice_number"], "26432000001239781576")


if __name__ == "__main__":
    unittest.main()
