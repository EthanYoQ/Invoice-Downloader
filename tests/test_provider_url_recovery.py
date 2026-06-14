import json
import tempfile
import unittest
from pathlib import Path
from urllib.parse import urlparse

from app_api import InvoiceAppAPI, build_processing_history_key
import pdf_converter
from email_fetcher import _build_link_candidate_decision
from pdf_converter import PDFConverter
from provider_direct_invoice import infer_direct_invoice_family


class FakeResponse:
    def __init__(self, url, content=b"", headers=None, status_code=200, json_data=None):
        self.url = url
        self.content = content
        self.headers = headers or {}
        self.status_code = status_code
        self._json_data = json_data
        self.text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)

    def close(self):
        return None


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        parsed = urlparse(url)
        if "sdapi.fpyun.com.cn" in parsed.netloc:
            return FakeResponse(
                "https://fp.baiwang.com/format/d",
                b"%PDF-1.5\nfpyun pdf",
                {"Content-Type": "application/pdf;charset=utf-8"},
            )
        if "files.pdd-fapiao.com" in parsed.netloc and "/pdf/" in parsed.path:
            return FakeResponse(url, b"%PDF-1.5\npdd pdf", {"Content-Type": "application/pdf"})
        if "eicore-invoice-" in parsed.netloc and parsed.path.endswith(".pdf"):
            return FakeResponse(url, b"%PDF-1.5\njd pdf", {"Content-Type": "application/pdf"})
        if "etd.kpbyd.com" in parsed.netloc and "fileCode=" in parsed.query and parsed.query.endswith("_pdf"):
            return FakeResponse(url, b"%PDF-1.5\nkpbyd pdf", {"Content-Type": "application/pdf"})
        if url == "https://nnfp.jss.com.cn/71ykyWlR=C-18aNx":
            return FakeResponse(
                "https://nnfp.jss.com.cn/scan-invoice/printQrcode?paramList=91430103MABWFD0J9B!!!26060100373102829862!false&aliView=true&shortLinkSource=1&wxApplet=0",
                b"<html></html>",
                {"Content-Type": "text/html; charset=utf-8"},
            )
        if "nuonuo.pdf" in url:
            return FakeResponse(url, b"%PDF-1.5\nnuonuo pdf", {"Content-Type": "application/pdf"})
        if "nuonuo.xml" in url:
            return FakeResponse(url, NUONUO_XML.encode("utf-8"), {"Content-Type": "application/xml"})
        if "baiwang.com/bwmg/mix/bw/downloadFormat" in url and "formatType=PDF" in url:
            return FakeResponse(url, b"%PDF-1.5\nbaiwang pdf", {"Content-Type": "text/pdf;charset=utf8"})
        if "baiwang.com/bwmg/mix/bw/downloadFormat" in url and "formatType=XML" in url:
            return FakeResponse(url, BAIWANG_XML.encode("utf-8"), {"Content-Type": "text/xml;charset=utf8"})
        if "baiwang.com/bwmg/mix/bw/downloadFormat" in url and "formatType=OFD" in url:
            return FakeResponse(url, b"PK\x03\x04ofd", {"Content-Type": "text/ofd;charset=utf8"})
        return FakeResponse(url, b"<html></html>", {"Content-Type": "text/html"})

    def post(self, url, data=None, headers=None, **kwargs):
        if "getIvcDetailShow.do" in url:
            return FakeResponse(
                url,
                json.dumps({
                    "status": "0000",
                    "data": {
                        "invoiceSimpleVo": {
                            "fphm": "26432000001233579481",
                            "saleName": "长沙楼上餐饮管理有限公司",
                            "buyername": "辉瑞投资有限公司",
                            "orderTotal": 399.40,
                            "invoiceDate": "2026-06-01 00:37:12",
                            "url": "https://inv.jss.com.cn/nuonuo.pdf",
                            "xmlUrl": "https://storage.nuonuo.com/nuonuo.xml",
                        }
                    },
                }).encode("utf-8"),
                {"Content-Type": "application/json;charset=utf-8"},
            )
        if "previewInvoiceQd" in url:
            return FakeResponse(
                url,
                json.dumps({"success": True, "total": "1", "data": [{"invoiceNo": "26432000001239781576"}]}).encode("utf-8"),
                {"Content-Type": "application/json"},
                json_data={"success": True, "total": "1", "data": [{"invoiceNo": "26432000001239781576"}]},
            )
        return FakeResponse(url, b"{}", {"Content-Type": "application/json"})


class FakeRequests:
    @staticmethod
    def Session():
        return FakeSession()


NUONUO_XML = """<?xml version="1.0" encoding="utf-8"?>
<EInvoice><SellerName>长沙楼上餐饮管理有限公司</SellerName><BuyerName>辉瑞投资有限公司</BuyerName>
<TotalTax-includedAmount>399.40</TotalTax-includedAmount><InvoiceNumber>26432000001233579481</InvoiceNumber>
<IssueTime>2026-06-01</IssueTime></EInvoice>"""

BAIWANG_XML = """<?xml version="1.0" encoding="utf-8"?>
<EInvoice><SellerName>湖南运达酒店管理有限公司长沙运达喜来登酒店</SellerName><BuyerName>辉瑞投资有限公司</BuyerName>
<TotalTax-includedAmount>1950.00</TotalTax-includedAmount><InvoiceNumber>26432000001239781576</InvoiceNumber>
<IssueTime>2026-06-01</IssueTime></EInvoice>"""


class ProviderUrlRecoveryTests(unittest.TestCase):
    def test_direct_invoice_acceptance_normalizes_seller_parentheses(self):
        api = InvoiceAppAPI()
        api._extract_pdf_preview_text = lambda *args, **kwargs: ""

        result = api._evaluate_document_acceptance(
            {
                "provider_family": "pdd_direct_invoice",
                "provider_expected_fields": {
                    "invoice_number": "25322000000555648119",
                    "seller": "姑苏区平阊园苏式面馆（个体工商户）",
                },
            },
            {},
            {
                "InvoiceNumber": "25322000000555648119",
                "Seller": "姑苏区平阊园苏式面馆(个体工商户)",
            },
            {"pdf_health_class": "ok"},
            "unused.pdf",
        )

        self.assertTrue(result["accepted"], result)

    def test_fpyun_and_nuonuo_links_are_provider_candidates_not_controlled_run_non_provider_urls(self):
        cases = [
            (
                "https://sdapi.fpyun.com.cn/invoice/qd/download/getInvoiceFile?fptqm=LQ26T4BJR950&type=1",
                "下载pdf文件(推荐)",
                "【发票云】尊敬的【辉瑞投资投资有限公司】客户,您收到1张来自【杭州联郡餐饮管理有限公司】为您开具的电子发票【取票码:LQ26T4BJR950】【发票号码:26337000000517112500】",
                "fpyun_direct_invoice",
            ),
            (
                "https://nnfp.jss.com.cn/71ykyWlR=C-18aNx",
                "下载发票",
                "您收到一张【长沙楼上餐饮管理有限公司】开具的发票【发票号码：26432000001233579481】",
                "nuonuo_scan_invoice",
            ),
            (
                "https://files.pdd-fapiao.com/invoice/92320508MADJX0LR2E/pdf/2025/11/25/25322000000555648119_a798.pdf",
                "下载PDF",
                "您收到来自姑苏区平阊园苏式面馆（个体工商户）的电子发票【发票号25322000000555648119】",
                "pdd_direct_invoice",
            ),
            (
                "https://eicore-invoice-25.s3.cn-north-1.jdcloud-oss.com/digital-invoice/digital_25117000000953853334.pdf?AWSAccessKeyId=JDC_8007&Expires=2702626081&Signature=x",
                "发票PDF",
                "您的京东订单电子发票已开具",
                "jdcloud_direct_invoice",
            ),
            (
                "https://etd.kpbyd.com/hub/files/download?code=abc&fileCode=shandong_0_26372000002439975871_20260525_8LQ3a3vnsNoEfsb_pdf",
                "下载发票",
                "您收到来自【济南历下小螺号海鲜店】的电子发票【发票号码26372000002439975871】",
                "kpbyd_direct_invoice",
            ),
        ]
        for url, anchor, subject, expected_family in cases:
            with self.subTest(url=url):
                decision = _build_link_candidate_decision(
                    url,
                    anchor,
                    tier=2,
                    sender_addr="",
                    subject=subject,
                    body_text="",
                )
                self.assertEqual(decision["candidate_action"], "main_chain")
                self.assertEqual(decision["provider_family"], expected_family)
                self.assertEqual(infer_direct_invoice_family(url), expected_family)

    def test_direct_file_invoice_recovery_downloads_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        cases = [
            (
                "https://files.pdd-fapiao.com/invoice/92320508MADJX0LR2E/pdf/2025/11/25/25322000000555648119_a798.pdf",
                "pdd_direct_invoice",
            ),
            (
                "https://eicore-invoice-25.s3.cn-north-1.jdcloud-oss.com/digital-invoice/digital_25117000000953853334.pdf?AWSAccessKeyId=JDC_8007&Expires=2702626081&Signature=x",
                "jdcloud_direct_invoice",
            ),
            (
                "https://etd.kpbyd.com/hub/files/download?code=abc&fileCode=shandong_0_26372000002439975871_20260525_8LQ3a3vnsNoEfsb_pdf",
                "kpbyd_direct_invoice",
            ),
        ]
        try:
            for url, family in cases:
                with self.subTest(url=url):
                    result = converter._recover_direct_invoice_group(
                        [url],
                        "电子发票",
                        "email-id",
                        str(Path(converter.staging_dir) / family),
                        {"provider_family": family, "provider_expected_fields": {}},
                    )
                    self.assertEqual(result["status"], "downloaded")
                    self.assertTrue(result["pdf_path"].endswith(".pdf"))
        finally:
            pdf_converter.requests = original_requests

    def test_url_history_key_distinguishes_body_identified_provider_invoices(self):
        first = build_processing_history_key(
            {
                "is_url": True,
                "email_id": "2446",
                "subject": "电子发票下载",
                "tier": 2,
                "source_url": "https://www.baiwang.com",
                "provider_family": "baiwang",
                "provider_expected_fields": {"invoice_number": "26112000000474524341"},
            },
            "www.baiwang.com",
            "https://www.baiwang.com",
        )
        second = build_processing_history_key(
            {
                "is_url": True,
                "email_id": "7001",
                "subject": "电子发票下载",
                "tier": 2,
                "source_url": "https://www.baiwang.com",
                "provider_family": "baiwang",
                "provider_expected_fields": {"invoice_number": "26332000003359187226"},
            },
            "www.baiwang.com",
            "https://www.baiwang.com",
        )

        self.assertNotEqual(first, second)
        self.assertIn("2446", first)
        self.assertIn("26112000000474524341", first)

    def test_direct_invoice_recovery_downloads_fpyun_pdf_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        try:
            result = converter._recover_direct_invoice_group(
                ["https://sdapi.fpyun.com.cn/invoice/qd/download/getInvoiceFile?fptqm=LQ26T4BJR950&type=1"],
                "【发票云】发票号码:26337000000517112500",
                "7063",
                str(Path(converter.staging_dir) / "7063"),
                {"provider_family": "fpyun_direct_invoice", "provider_expected_fields": {"invoice_number": "26337000000517112500"}},
            )
        finally:
            pdf_converter.requests = original_requests
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["pdf_path"].endswith(".pdf"))

    def test_baiwang_preview_invoice_downloads_pdf_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        try:
            result = converter._recover_baiwang_group(
                ["https://pis.baiwang.com/smkp-vue/previewInvoiceAllEle?param=A79B8219096507C9"],
                "电子发票下载",
                "7051",
                str(Path(converter.staging_dir) / "7051"),
                {"provider_family": "baiwang", "provider_expected_fields": {"invoice_number": "26432000001239781576"}},
            )
        finally:
            pdf_converter.requests = original_requests
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["pdf_path"].endswith(".pdf"))

    def test_nuonuo_shortlink_recovers_invoice_pdf_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        try:
            result = converter._recover_direct_invoice_group(
                ["https://nnfp.jss.com.cn/71ykyWlR=C-18aNx"],
                "您收到一张【长沙楼上餐饮管理有限公司】开具的发票【发票号码：26432000001233579481】",
                "7048",
                str(Path(converter.staging_dir) / "7048"),
                {"provider_family": "nuonuo_scan_invoice", "provider_expected_fields": {"invoice_number": "26432000001233579481"}},
            )
        finally:
            pdf_converter.requests = original_requests
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["pdf_path"].endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()
