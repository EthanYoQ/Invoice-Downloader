import tempfile
import unittest
from pathlib import Path

import fitz

from email_body_receipts import (
    parse_email_body_receipt_fields,
    render_email_body_receipt_pdf_bytes,
)
from invoice_extractor import InvoiceExtractor


ICLOUD_BODY = """
收据
日期
2026年01月21日
订单号
MTFKT9WLF9
文稿编号
778080227734
发票
点按此处
iCloud+
¥21.00
合计
¥19.81
增值税为 6%
¥1.19
总计
¥21.00
在中国大陆,iCloud 由云上艾珀(贵州)技术有限公司(云上贵州)运营。
"""


BAIWANG_BODY = """
尊敬的 辉瑞投资有限公司 用户,您好:
王府井饭店管理有限公司北京金茂万丽酒店为您开具了电子发票
发票金额
2399.98
开票日期
2026-02-05
购方名称
辉瑞投资有限公司
发票号码
26112000000474524341
"""


FIFTY_ONE_FAPIAO_BODY = """
[ 此电子发票由51发票平台交付,邮件由系统自动发送,请勿直接回复 ]
尊敬的顾客,您好!
您的数电发票已开具成功,请点击下列网址下载此发票。
点击以下网址,下载此发票
https://a.51fapiao.cn/v/c6fwq3g2r25x8
"""


class EmailBodyReceiptTests(unittest.TestCase):
    def test_parse_icloud_receipt_fields_from_email_body(self):
        fields = parse_email_body_receipt_fields(
            subject="iCloud+ 的发票",
            sender="云上贵州 <no_reply@iCloud.gzdata.com.cn>",
            body_text=ICLOUD_BODY,
        )

        self.assertEqual(fields["InvoiceNumber"], "778080227734")
        self.assertEqual(fields["Date"], "20260121")
        self.assertEqual(fields["Seller"], "云上艾珀（贵州）技术有限公司")
        self.assertEqual(fields["Amount"], "21.00")
        self.assertEqual(fields["Purchaser"], "个人")
        self.assertEqual(fields["Type"], "其他")

    def test_parse_baiwang_invoice_fields_from_email_body(self):
        fields = parse_email_body_receipt_fields(
            subject="电子发票下载",
            sender="系统服务 <yun1@vip.baiwang.com>",
            body_text=BAIWANG_BODY,
        )

        self.assertEqual(fields["InvoiceNumber"], "26112000000474524341")
        self.assertEqual(fields["Date"], "20260205")
        self.assertEqual(fields["Seller"], "王府井饭店管理有限公司北京金茂万丽酒店")
        self.assertEqual(fields["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(fields["Amount"], "2399.98")
        self.assertEqual(fields["Type"], "住宿发票")

    def test_parse_51fapiao_invoice_fields_from_subject_and_body(self):
        fields = parse_email_body_receipt_fields(
            subject="【电子发票】您收到一张来自【北京芳草欣科贸有限公司新湖南菜酒家】价税合计金额为244的电子发票[购方名称:辉瑞投资有限公司 发票号码:26112000001193791786]",
            sender="51发票 <no-reply@51fapiao.cn>",
            body_text=FIFTY_ONE_FAPIAO_BODY,
            email_date="Fri, 27 Mar 2026 11:39:44 +0800",
        )

        self.assertEqual(fields["InvoiceNumber"], "26112000001193791786")
        self.assertEqual(fields["Date"], "20260327")
        self.assertEqual(fields["Seller"], "北京芳草欣科贸有限公司新湖南菜酒家")
        self.assertEqual(fields["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(fields["Amount"], "244.00")
        self.assertEqual(fields["Type"], "餐饮")

    def test_parse_51fapiao_kfc_invoice_classifies_as_food(self):
        fields = parse_email_body_receipt_fields(
            subject="【电子发票】您收到一张来自【北京肯德基有限公司】价税合计金额为48.50的电子发票[购方名称:辉瑞投资有限公司 发票号码:26117000000129854499]",
            sender="51发票 <dzfp@51fapiao.cloud>",
            body_text=FIFTY_ONE_FAPIAO_BODY,
            email_date="Mon, 19 Jan 2026 12:25:44 +0800",
        )

        self.assertEqual(fields["Seller"], "北京肯德基有限公司")
        self.assertEqual(fields["Amount"], "48.50")
        self.assertEqual(fields["Type"], "餐饮")

    def test_rendered_body_receipt_pdf_uses_local_extractor_fast_path(self):
        fields = parse_email_body_receipt_fields(
            subject="iCloud+ 的发票",
            sender="云上贵州 <no_reply@iCloud.gzdata.com.cn>",
            body_text=ICLOUD_BODY,
        )
        pdf_bytes = render_email_body_receipt_pdf_bytes(fields, ICLOUD_BODY, source_email_id="2439")
        target = Path(tempfile.mkdtemp()) / "email_body_receipt.pdf"
        target.write_bytes(pdf_bytes)

        extractor = InvoiceExtractor()
        parsed = extractor._try_extract_email_body_receipt_from_pdf_text(str(target))

        self.assertEqual(parsed["InvoiceNumber"], "778080227734")
        self.assertEqual(parsed["InvoiceCode"], "")
        self.assertEqual(parsed["Seller"], "云上艾珀（贵州）技术有限公司")
        self.assertEqual(parsed["Amount"], "21.00")
        self.assertEqual(parsed["Type"], "其他")

    def test_rendered_body_receipt_pdf_keeps_canonical_fields_when_body_has_many_lines(self):
        fields = parse_email_body_receipt_fields(
            subject="iCloud+ 的发票",
            sender="云上贵州 <no_reply@iCloud.gzdata.com.cn>",
            body_text=ICLOUD_BODY,
        )
        verbose_body = "\n".join([line for line in ICLOUD_BODY.splitlines() if line.strip()] * 3)
        target = Path(tempfile.mkdtemp()) / "email_body_receipt.pdf"
        target.write_bytes(render_email_body_receipt_pdf_bytes(fields, verbose_body, source_email_id="2439"))

        extracted_text = "\n".join(page.get_text("text") for page in fitz.open(target))

        self.assertIn("EMAIL_BODY_RECEIPT_CANONICAL", extracted_text)
        self.assertIn("778080227734", extracted_text)
        self.assertIn("云上艾珀", extracted_text)

    def test_extract_info_accepts_small_canonical_body_receipt_pdf_before_size_gate(self):
        fields = parse_email_body_receipt_fields(
            subject="iCloud+ 的发票",
            sender="云上贵州 <no_reply@iCloud.gzdata.com.cn>",
            body_text=ICLOUD_BODY,
        )
        target = Path(tempfile.mkdtemp()) / "email_body_receipt.pdf"
        target.write_bytes(render_email_body_receipt_pdf_bytes(fields, ICLOUD_BODY, source_email_id="2439"))

        extractor = InvoiceExtractor()
        parsed = extractor.extract_info_via_llm([], pdf_path=str(target))

        self.assertEqual(parsed["InvoiceNumber"], "778080227734")
        self.assertEqual(extractor.last_extraction_trace["engine"], "local_email_body_receipt_pdf")
