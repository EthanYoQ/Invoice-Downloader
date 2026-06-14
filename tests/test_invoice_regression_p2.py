import json
import tempfile
import unittest
from pathlib import Path

import fitz

import app_api
from build_truth_dataset import (
    companion_evidence_paths_for_primary,
    document_role_for,
    expected_category_for,
    parse_cits_pdf,
    parse_foreign_invoice_pdf,
    parse_generic_xml,
    parse_loose_standard_einvoice_pdf,
    parse_pdf_local,
    parse_marriott_folio_pdf,
    parse_ofd_from_metadata,
    parse_train_ticket_pdf,
    row_in_truth_window,
    truth_type_from_fields,
    truth_type_from_seller,
)
from invoice_extractor import InvoiceExtractor
from strict_truth_audit import compare, contains_fuzzy


def write_text_pdf(path: Path, text: str):
    doc = fitz.open()
    page = doc.new_page(width=595, height=842)
    y = 40
    for line in text.splitlines():
        page.insert_text((40, y), line, fontsize=10, fontname="china-s")
        y += 14
    doc.save(path)
    doc.close()


class InvoiceP2RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.extractor = InvoiceExtractor(api_key="", output_dir=str(self.root / "extracted"))

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_non_target_company_invoice_with_unknown_seller_stays_out_of_manual_check(self):
        pdf = self.root / "Invoice-23265242.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")

        success, final_path = self.extractor.route_and_rename_file(
            str(pdf),
            {
                "is_invoice": True,
                "Date": "20260524",
                "Purchaser": "Yong Qi",
                "Seller": "未知",
                "Amount": "49.99",
                "InvoiceNumber": "Invoice #23265242",
                "Type": "非目标公司发票",
            },
        )

        self.assertTrue(success)
        self.assertIn("非目标公司发票", final_path)
        self.assertFalse(self.extractor.last_route_trace["used_manual_check"])

    def test_regular_invoice_with_unknown_seller_still_requires_manual_check(self):
        pdf = self.root / "unknown-seller.pdf"
        pdf.write_bytes(b"%PDF-1.4\n% placeholder\n")

        success, final_path = self.extractor.route_and_rename_file(
            str(pdf),
            {
                "is_invoice": True,
                "Date": "20260524",
                "Purchaser": "辉瑞投资有限公司",
                "Seller": "未知",
                "Amount": "49.99",
                "InvoiceNumber": "Invoice #23265242",
                "Type": "其他",
            },
        )

        self.assertTrue(success)
        self.assertIn("待人工复核", final_path)
        self.assertTrue(self.extractor.last_route_trace["used_manual_check"])

    def test_foreign_invoice_fast_path_extracts_seller_date_and_invoice_number(self):
        pdf = self.root / "Invoice-23265242.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "UNPAID",
                    "IT7 Networks Inc",
                    "130-1959 152 St",
                    "Invoice #23265242",
                    "Invoice Date: Sunday, May 24th, 2026",
                    "Invoiced To",
                    "Yong Qi",
                    "Description",
                    "Total",
                    "$49.99 USD",
                    "Sub Total",
                    "$49.99 USD",
                    "Total",
                    "$49.99 USD",
                ]
            ),
        )

        result = self.extractor._try_extract_foreign_invoice_from_pdf_text(str(pdf))

        self.assertEqual(result["InvoiceNumber"], "23265242")
        self.assertEqual(result["Date"], "20260524")
        self.assertEqual(result["Seller"], "IT7 Networks Inc")
        self.assertEqual(result["Purchaser"], "Yong Qi")
        self.assertEqual(result["Amount"], "49.99")
        self.assertEqual(result["Type"], "其他")

    def test_truth_builder_parses_foreign_invoice_ordinal_date(self):
        pdf = self.root / "Invoice-23265242.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "UNPAID",
                    "IT7 Networks Inc",
                    "Invoice #23265242",
                    "Invoice Date: Sunday, May 24th, 2026",
                    "Invoiced To",
                    "Yong Qi",
                    "Total",
                    "$49.99 USD",
                ]
            ),
        )

        fields = parse_foreign_invoice_pdf(pdf)

        self.assertEqual(fields["invoice_number"], "23265242")
        self.assertEqual(fields["invoice_date"], "2026-05-24")
        self.assertEqual(fields["seller"], "IT7 Networks Inc")

    def test_cits_truth_parser_uses_total_before_tax_detail_cny(self):
        pdf = self.root / "3693356_SCCT00921845.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "INVOICE",
                    "SCCT00921845",
                    "Date:",
                    "17/03/26",
                    "PFIZER-PFIZER INVESTMENT CO. LTD.",
                    "辉瑞投资有限公司",
                    "Online Dom Air",
                    "Origin",
                    "Destination",
                    "Beijing",
                    "SHANGHAI",
                    "Airline",
                    "Flight No",
                    "MU",
                    "5126",
                    "Tax Detail:CN CNY 50.00+YQ CNY 20.00",
                    "CITS - American Express Global Business Travel",
                    "Total:",
                    "CNY",
                    " 1,885.82",
                    "Grand Total:",
                    "One thousand eight hundred and eighty five Chinese Yuan and eighty two Cents Only",
                    "Amount Received: CNY",
                    " 1,885.82",
                ]
            ),
        )

        fields = parse_cits_pdf(pdf)

        self.assertEqual(fields["invoice_number"], "SCCT00921845")
        self.assertEqual(fields["amount"], "1885.82")
        self.assertEqual(fields["truth_type"], "机票")

    def test_truth_builder_train_ticket_uses_departure_date_not_issue_date(self):
        pdf = self.root / "train.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "发票号码:25119110010007003615",
                    "Beijingnan",
                    "G187",
                    "2025年11月21日",
                    "电子发票（铁路电子客票）",
                    "13:38开",
                    "票价:￥223.00",
                    "亓勇",
                    "电子客票号:1001059086112299407762025",
                    "辉瑞投资有限公司",
                    "开票日期:2025年12月24日",
                    "Jinanxi",
                    "济南西站",
                    "北京南站",
                    "购买方名称:",
                ]
            ),
        )

        fields = parse_train_ticket_pdf(pdf)

        self.assertEqual(fields["invoice_date"], "2025-11-21")
        self.assertEqual(fields["invoice_number"], "25119110010007003615")
        self.assertEqual(fields["amount"], "223.00")

    def test_cits_runtime_parser_classifies_air_invoice_as_flight_ticket(self):
        pdf = self.root / "3693356_SCCT00921845.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "INVOICE",
                    "SCCT00921845",
                    "Date:",
                    "17/03/26",
                    "PFIZER-PFIZER INVESTMENT CO. LTD.",
                    "辉瑞投资有限公司",
                    "Online Dom Air",
                    "Origin",
                    "Destination",
                    "Beijing",
                    "SHANGHAI",
                    "Airline",
                    "Flight No",
                    "MU",
                    "5126",
                    "Tax Detail:CN CNY 50.00+YQ CNY 20.00",
                    "CITS - American Express Global Business Travel",
                    "Total:",
                    "CNY",
                    " 1,885.82",
                ]
            ),
        )

        result = self.extractor._try_extract_cits_gbt_from_pdf_text(str(pdf))

        self.assertEqual(result["InvoiceNumber"], "SCCT00921845")
        self.assertEqual(result["Amount"], "1885.82")
        self.assertEqual(result["Type"], "机票")
        self.assertEqual(result["Seller"], "CITS GBT")

    def test_cits_runtime_parser_keeps_hotel_gds_fee_as_other(self):
        pdf = self.root / "3693347_SCCT00921841.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "INVOICE",
                    "SCCT00921841",
                    "Date:",
                    "17/03/26",
                    "PFIZER-PFIZER INVESTMENT CO. LTD.",
                    "辉瑞投资有限公司",
                    "Hotel (GDS) Dom",
                    "Room(s): 1 Room(s) X 1 Night(s)",
                    "BEIJING- HILTON GARDEN INN BEIJING GUOMAO",
                    "CITS - American Express Global Business Travel",
                    "Total:",
                    "CNY",
                    " 21.41",
                    "Grand Total:",
                    "Twenty one Chinese Yuan and forty one Cents Only",
                ]
            ),
        )

        result = self.extractor._try_extract_cits_gbt_from_pdf_text(str(pdf))

        self.assertEqual(result["InvoiceNumber"], "SCCT00921841")
        self.assertEqual(result["Amount"], "21.41")
        self.assertEqual(result["Type"], "其他")

    def test_cits_truth_parser_classifies_hotel_gds_fee_as_travel_service_fee(self):
        pdf = self.root / "3693347_SCCT00921841.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "INVOICE",
                    "SCCT00921841",
                    "Date:",
                    "17/03/26",
                    "PFIZER-PFIZER INVESTMENT CO. LTD.",
                    "辉瑞投资有限公司",
                    "Hotel (GDS) Dom",
                    "Room(s): 1 Room(s) X 1 Night(s)",
                    "BEIJING- HILTON GARDEN INN BEIJING GUOMAO",
                    "CITS - American Express Global Business Travel",
                    "Total:",
                    "CNY",
                    " 21.41",
                ]
            ),
        )

        fields = parse_cits_pdf(pdf)

        self.assertEqual(fields["truth_type"], "差旅服务费")
        self.assertEqual(fields["category"], "差旅服务费")

    def test_cits_runtime_parser_routes_personal_air_itinerary_as_non_target(self):
        pdf = self.root / "谢超锋3月17日行程单 - 机票.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "03月17日国内行程",
                    "CITSGBT",
                    "感谢您选择国旅运通。",
                    "乘客姓名",
                    "谢超锋",
                    "出发:2026年03月17日",
                    "航班号: MU 5126",
                    "PEK",
                    "SHA",
                    "中国东方航空公司",
                    "北京首都机场",
                    "上海虹桥机场",
                    "客票价格:",
                    "1名成人机票总价: CNY 1850.00 (票价:1780.00 + 税:70.00)",
                ]
            ),
        )

        result = self.extractor._try_extract_cits_gbt_from_pdf_text(str(pdf))

        self.assertEqual(result["Type"], "非目标公司发票")
        self.assertEqual(result["Seller"], "CITS GBT")
        self.assertEqual(result["Amount"], "1850.00")
        self.assertEqual(result["Date"], "20260317")

    def test_cits_truth_parser_classifies_hotel_itinerary_as_folio(self):
        pdf = self.root / "谢超锋3月16日行程单 - 酒店.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "03月16日",
                    "03月17日国内行程",
                    "CITSGBT",
                    "感谢您选择国旅运通。",
                    "旅客姓名: 谢超锋",
                    "入住日期:",
                    "> 离店日期:",
                    "03月16日",
                    "03月17日",
                    "北京国贸希尔顿花园酒店（非协议）",
                    "电话: 86-10-56676806",
                    "预定状态:预订成功",
                    "■ 价格及付款信息",
                    "864.55 CNY （最终价格及税费以酒店前台支付的为准）",
                    "总价:",
                    "864.55 CNY",
                    "每晚平均价(每间夜):",
                    "现付",
                    "支付方式:",
                ]
            ),
        )

        fields = parse_cits_pdf(pdf)

        self.assertEqual(fields["amount"], "864.55")
        self.assertEqual(fields["truth_type"], "住宿水单")
        self.assertEqual(fields["category"], "住宿水单")
        self.assertEqual(fields["seller"], "北京国贸希尔顿花园酒店")

    def test_cits_runtime_parser_classifies_hotel_itinerary_as_folio(self):
        pdf = self.root / "谢超锋3月16日行程单 - 酒店.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "03月16日",
                    "03月17日国内行程",
                    "CITSGBT",
                    "感谢您选择国旅运通。",
                    "旅客姓名: 谢超锋",
                    "入住日期:",
                    "> 离店日期:",
                    "03月16日",
                    "03月17日",
                    "北京国贸希尔顿花园酒店（非协议）",
                    "电话: 86-10-56676806",
                    "预定状态:预订成功",
                    "■ 价格及付款信息",
                    "864.55 CNY （最终价格及税费以酒店前台支付的为准）",
                    "总价:",
                    "864.55 CNY",
                    "每晚平均价(每间夜):",
                    "现付",
                    "支付方式:",
                ]
            ),
        )

        result = self.extractor._try_extract_cits_gbt_from_pdf_text(
            str(pdf),
            document_context={
                "mail_date_local": "2026-03-22 22:51:36",
                "subject": "谢超锋转发: [EXTERNAL] 谢超锋3月16日行程单 - 酒店",
                "original_filename": pdf.name,
            },
        )

        self.assertEqual(result["Type"], "住宿水单")
        self.assertEqual(result["Seller"], "北京国贸希尔顿花园酒店")
        self.assertEqual(result["Amount"], "864.55")
        self.assertEqual(result["Date"], "20260316")
        self.assertTrue(result["_is_folio"])

    def test_archive_normalizer_preserves_cits_hotel_folio_over_itinerary_filename(self):
        helper = getattr(app_api, "normalize_document_type_for_archive", None)
        self.assertIsNotNone(helper, "app_api.normalize_document_type_for_archive is required")
        if helper is None:
            return

        info_json = {
            "Type": "住宿水单",
            "Seller": "成都首座万丽酒店",
            "_is_folio": True,
        }
        doc_type, reason_codes = helper(
            info_json,
            "谢超锋3月12日行程单 - 酒店.pdf",
            False,
        )

        self.assertEqual(doc_type, "住宿水单")
        self.assertTrue(info_json["_is_folio"])
        self.assertIn("CLASSIFIED_AS_HOTEL_FOLIO", reason_codes)
        self.assertNotIn("CLASSIFIED_AS_RIDE_ITINERARY", reason_codes)

    def test_archive_normalizer_preserves_cits_personal_air_itinerary_as_non_target(self):
        helper = getattr(app_api, "normalize_document_type_for_archive", None)
        self.assertIsNotNone(helper, "app_api.normalize_document_type_for_archive is required")
        if helper is None:
            return

        info_json = {
            "Type": "非目标公司发票",
            "Seller": "CITS GBT",
            "Purchaser": "个人",
        }
        doc_type, reason_codes = helper(
            info_json,
            "亓勇3月13日行程单 - 机票.pdf",
            False,
        )

        self.assertEqual(doc_type, "非目标公司发票")
        self.assertNotIn("CLASSIFIED_AS_FLIGHT_ITINERARY", reason_codes)

    def test_cwt_classifier_preserves_local_cits_air_invoice_type(self):
        classifier = getattr(app_api, "classify_cwt_document_type", None)
        self.assertIsNotNone(classifier, "app_api.classify_cwt_document_type is required")
        if classifier is None:
            return

        doc_type, reason_codes = classifier(
            {"Type": "机票", "Seller": "CITS GBT", "InvoiceNumber": "SCCT00919573"},
            {"subject": "CITS GBT Invoice SCCT00919573 (首段行程：上海虹桥-成都双流/2026-03-12 )"},
            "3687447_SCCT00919573.pdf",
            local_cits_fast_path=True,
        )

        self.assertEqual(doc_type, "机票")
        self.assertNotIn("CLASSIFIED_AS_CWT_SERVICE_FEE", reason_codes)

    def test_truth_builder_ofd_metadata_uses_shared_seller_type_rules(self):
        fields = parse_ofd_from_metadata(
            self.root / "辉瑞投资有限公司_数电普票_开票金额482.00元_开票日期20260525_26372000002439975871.ofd",
            {"subject": "您收到来自【济南历下小螺号海鲜店】的电子发票【发票号码26372000002439975871】，请查收"},
            "辉瑞投资有限公司",
        )

        self.assertEqual(fields["seller"], "济南历下小螺号海鲜店")
        self.assertEqual(fields["truth_type"], "餐饮")

    def test_truth_builder_pdf_prefers_same_stem_xml_fields(self):
        pdf = self.root / "261120000009_44157226_辉瑞投资有限公司.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票（普通发票）",
                    "发票号码 26112000000944157226",
                    "开票日期 2026年03月11日",
                    "辉瑞投资有限公司",
                    "*餐饮服务*餐饮服务",
                    "￥5483.00",
                    "￥174.00",
                ]
            ),
        )
        (self.root / "261120000009_44157226_辉瑞投资有限公司.xml").write_text(
            "\n".join(
                [
                    "<Invoice>",
                    "<InvoiceNumber>26112000000944157226</InvoiceNumber>",
                    "<IssueTime>2026-03-11</IssueTime>",
                    "<SellerName>北京满锅金涮肉馆</SellerName>",
                    "<BuyerName>辉瑞投资有限公司</BuyerName>",
                    "<TotalTax-includedAmount>174.00</TotalTax-includedAmount>",
                    "</Invoice>",
                ]
            ),
            encoding="utf-8",
        )

        fields, engine = parse_pdf_local(self.extractor, pdf, "辉瑞投资有限公司")

        self.assertEqual(engine, "companion_xml_for_pdf")
        self.assertEqual(fields["seller"], "北京满锅金涮肉馆")
        self.assertEqual(fields["amount"], "174.00")
        self.assertEqual(fields["invoice_number"], "26112000000944157226")

    def test_truth_builder_pdf_evidence_keeps_same_stem_xml(self):
        pdf = self.root / "26372000001413204721-辉瑞投资有限公司.pdf"
        xml = self.root / "26372000001413204721-辉瑞投资有限公司.xml"
        ofd = self.root / "26372000001413204721-辉瑞投资有限公司.ofd"
        pdf.write_bytes(b"%PDF-1.4\n")
        xml.write_text("<Invoice />", encoding="utf-8")
        ofd.write_bytes(b"OFD")

        companions = companion_evidence_paths_for_primary(pdf, "26372000001413204721")

        self.assertEqual([path.name for path in companions], [xml.name, ofd.name])

    def test_truth_builder_loose_pdf_prefers_restaurant_seller_over_item_name(self):
        pdf = self.root / "dzfp_26332000000317238481_辉瑞投资有限公司_20260113001310.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票（普通发票）",
                    "发票号码 26332000000317238481",
                    "开票日期 2026年01月13日",
                    "名称:",
                    "名称:",
                    "项目名称",
                    "辉瑞投资有限公司",
                    "杭州市拱墅区小男孩饮品店",
                    "¥495.00",
                    "*餐饮服务*餐饮服务",
                ]
            ),
        )

        fields = parse_loose_standard_einvoice_pdf(pdf, "辉瑞投资有限公司")

        self.assertEqual(fields["seller"], "杭州市拱墅区小男孩饮品店")
        self.assertEqual(fields["truth_type"], "餐饮")
        self.assertEqual(fields["amount"], "495.00")

    def test_truth_builder_xml_preserves_item_name_for_type_classification(self):
        xml = self.root / "restaurant_service.xml"
        xml.write_text(
            "\n".join(
                [
                    "<EInvoice>",
                    "<EInvoiceData>",
                    "<SellerInformation><SellerName>北京旭茂宇通商贸有限公司</SellerName></SellerInformation>",
                    "<BuyerInformation><BuyerName>辉瑞投资有限公司</BuyerName></BuyerInformation>",
                    "<BasicInformation><TotalTax-includedAmount>369.00</TotalTax-includedAmount><RequestTime>2026-03-31</RequestTime></BasicInformation>",
                    "<IssuItemInformation><ItemName>*餐饮服务*餐饮服务</ItemName></IssuItemInformation>",
                    "</EInvoiceData>",
                    "<TaxSupervisionInfo><InvoiceNumber>26112000001272487786</InvoiceNumber><IssueTime>2026-03-31</IssueTime></TaxSupervisionInfo>",
                    "</EInvoice>",
                ]
            ),
            encoding="utf-8",
        )

        fields = parse_generic_xml(xml)
        truth_type = truth_type_from_fields(
            fields,
            {"subject": "【电子发票】北京旭贸宇通商贸有限公司（发票金额：369.00元）", "file_name": xml.name},
            "辉瑞投资有限公司",
        )

        self.assertEqual(fields["item_name"], "*餐饮服务*餐饮服务")
        self.assertEqual(truth_type, "餐饮")

    def test_truth_type_from_fields_lets_seller_override_generic_other(self):
        truth_type = truth_type_from_fields(
            {"truth_type": "其他", "seller": "济南历下小螺号海鲜店"},
            {"subject": "您收到来自【济南历下小螺号海鲜店】的电子发票"},
            "辉瑞投资有限公司",
        )

        self.assertEqual(truth_type, "餐饮")

    def test_truth_type_from_fields_keeps_12306_delivery_service_fee_as_other(self):
        truth_type = truth_type_from_fields(
            {"truth_type": "其他", "seller": "中国铁路网络有限公司"},
            {"subject": "12306----网络订餐配送费电子发票开具通知", "file_name": "26117000000093349789.pdf"},
            "辉瑞投资有限公司",
        )

        self.assertEqual(truth_type, "其他")

    def test_truth_type_from_seller_recognizes_common_food_brands(self):
        for seller in [
            "上海金拱门食品有限公司",
            "北京麦当劳食品有限公司",
            "北京肯德基有限公司",
            "北京盒马网络科技有限公司",
            "北京满锅金涮肉馆",
            "济南市中康盛鑫立旺烧烤店",
            "成华区钢小月郡肝串串香店",
            "北京陇人家食府",
            "杭州市拱墅区小男孩饮品店",
        ]:
            with self.subTest(seller=seller):
                self.assertEqual(truth_type_from_seller(seller), "餐饮")

    def test_standard_einvoice_parser_handles_wrapped_buyer_seller_and_total_amount(self):
        pdf = self.root / "huazhu.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电⼦发票（增值税专用发票）",
                    "项目名称",
                    "规格型号",
                    "单位",
                    "数量",
                    "单价",
                    "金额",
                    "税率/征收率",
                    "税额",
                    "发票号码：",
                    "开票日期：",
                    "购",
                    "买",
                    "方",
                    "信",
                    "息统一社会信用代码/纳税人识别号：",
                    "名称：",
                    "销",
                    "售",
                    "方",
                    "信",
                    "息",
                    "统一社会信用代码/纳税人识别号：",
                    "名称：",
                    "合 计",
                    "价税合计（大写）",
                    "（小写）",
                    "开票人：",
                    "¥416.18",
                    "¥24.97",
                    "肆佰肆拾壹圆壹角伍分",
                    "¥441.15",
                    "¥",
                    "112154576",
                    "张正来",
                    "26332000004952544376",
                    "2026年06月11日",
                    "辉瑞投资有限公司",
                    "杭州浙凯酒店管理有限公司",
                    "91310000710920127H",
                    "91330114MA8GERFU78",
                    "*生产生活服务*住宿费",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["Seller"], "杭州浙凯酒店管理有限公司")
        self.assertEqual(result["Amount"], "441.15")
        self.assertEqual(result["InvoiceNumber"], "26332000004952544376")
        self.assertEqual(result["Type"], "住宿发票")

    def test_standard_einvoice_parser_handles_jd_parallel_seller_buyer_layout(self):
        pdf = self.root / "jd.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "合",
                    "计",
                    "价税合计(大写)",
                    "(小写)",
                    "发票号码:",
                    "开票日期:",
                    "销",
                    "售",
                    "方",
                    "信",
                    "息",
                    "购",
                    "买",
                    "方",
                    "信",
                    "息",
                    "名 称:",
                    "统一社会信用代码/纳税人识别号:",
                    "电子发票(普通发票)",
                    "名 称:",
                    "统一社会信用代码/纳税人识别号:",
                    "北京京东世纪信息技术有限公司",
                    "辉瑞投资有限公司",
                    "91110302562134916R",
                    "91310000710920127H",
                    "*印刷品*服务设计",
                    "¥207.06",
                    "¥0.00",
                    "贰佰零柒圆零陆分",
                    "¥207.06",
                    "订单号:319122726076",
                    "王梅",
                    "25117000000953853334",
                    "2025年07月19日",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["Seller"], "北京京东世纪信息技术有限公司")
        self.assertEqual(result["InvoiceNumber"], "25117000000953853334")
        self.assertEqual(result["Amount"], "207.06")

    def test_standard_einvoice_parser_handles_mcdonalds_wrapped_party_layout(self):
        pdf = self.root / "mcdonalds.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票（普通发票）",
                    "发票号码：",
                    "开票日期：",
                    "购",
                    "买",
                    "方",
                    "信",
                    "息",
                    "销",
                    "售",
                    "方",
                    "信",
                    "息",
                    "名称：",
                    "名称：",
                    "统一社会信用代码/纳税人识别号：",
                    "统一社会信用代码/纳税人识别号：",
                    "开票人：",
                    "合        计",
                    "价税合计（大写）",
                    "（小写）",
                    "备",
                    "注",
                    "项目名称",
                    "规格型号",
                    "单  位",
                    "数  量",
                    "单  价",
                    "金  额",
                    "税率/征收率",
                    "税  额",
                    "26437000000204293337",
                    "2026年06⽉14⽇",
                    "辉瑞投资有限公司",
                    "湖南⾦拱⻔⻝品有限公司",
                    "91310000710920127H",
                    "91430000616780869M",
                    "温燕莉",
                    "61.56",
                    "¥",
                    "3.69",
                    "¥",
                    "65.25",
                    "¥",
                    "陆拾伍元贰角伍分",
                    "*生产生活服务*餐饮服务",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["Seller"], "湖南金拱门食品有限公司")
        self.assertEqual(result["Amount"], "65.25")
        self.assertEqual(result["Type"], "餐饮")

    def test_standard_einvoice_parser_uses_company_lines_before_tax_ids(self):
        pdf = self.root / "kfc_parallel_names.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票（普通发票）",
                    "发票号码：",
                    "开票日期：",
                    "购买方信息",
                    "销售方信息",
                    "名称：",
                    "名称：",
                    "统一社会信用代码/纳税人识别号：",
                    "统一社会信用代码/纳税人识别号：",
                    "26117000000129854499",
                    "2026年01月19日",
                    "辉瑞投资有限公司",
                    "北京肯德基有限公司",
                    "91310000710920127H",
                    "91110000600007281U",
                    "合计",
                    "45.75",
                    "¥",
                    "2.75",
                    "¥",
                    "价税合计（大写）",
                    "（小写）",
                    "48.50",
                    "¥",
                    "肆拾捌圆伍角整",
                    "BJN394260116;",
                    "*餐饮服务*餐饮服务",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["Seller"], "北京肯德基有限公司")
        self.assertEqual(result["Amount"], "48.50")
        self.assertEqual(result["Type"], "餐饮")

    def test_standard_einvoice_parser_accepts_sparse_seafood_shop_layout(self):
        pdf = self.root / "kpbyd_sparse_seafood_shop.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票(普通发票)",
                    "发票号码:",
                    "开票日期:",
                    "购",
                    "买",
                    "方",
                    "信",
                    "息",
                    "统一社会信用代码/纳税人识别号:",
                    "销",
                    "售",
                    "方",
                    "信",
                    "息",
                    "统一社会信用代码/纳税人识别号:",
                    "名称:",
                    "名称:",
                    "项目名称",
                    "规格型号",
                    "单 位",
                    "数 量",
                    "单 价",
                    "金 额",
                    "税率/征收率",
                    "税 额",
                    "合",
                    "计",
                    "价税合计(大写)",
                    "(小写)",
                    "备",
                    "注",
                    "开票人:",
                    "26372000002439975871",
                    "2026年05月25日",
                    "辉瑞投资有限公司",
                    "91310000710920127H",
                    "济南历下小螺号海鲜店",
                    "92370102MA3M0TLJ0F",
                    "¥454.72",
                    "¥27.28",
                    "肆佰捌拾贰圆整",
                    "¥482.00",
                    "杨朕",
                    "*餐饮服务*餐费",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["Seller"], "济南历下小螺号海鲜店")
        self.assertEqual(result["InvoiceNumber"], "26372000002439975871")
        self.assertEqual(result["Amount"], "482.00")
        self.assertEqual(result["Type"], "餐饮")

    def test_standard_einvoice_parser_ignores_personal_name_before_invoice_number(self):
        pdf = self.root / "nuonuo_person_before_number.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票（普通发票）",
                    "发票号码：",
                    "开票日期：",
                    "购买方信息",
                    "销售方信息",
                    "统一社会信用代码/纳税人识别号：",
                    "名称：",
                    "统一社会信用代码/纳税人识别号：",
                    "名称：",
                    "合计",
                    "价税合计（大写）",
                    "（小写）",
                    "¥390.57",
                    "¥23.43",
                    "肆佰壹拾肆圆整",
                    "¥ 414.00",
                    "陈前",
                    "26312000002898578371",
                    "2026年05月11日",
                    "辉瑞投资有限公司",
                    "上海市静安区行前餐厅(个体工商户)",
                    "91310000710920127H",
                    "92310106MAEX8T0X5G",
                    "*餐饮服务*餐饮服务",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["Seller"], "上海市静安区行前餐厅(个体工商户)")
        self.assertEqual(result["Amount"], "414.00")
        self.assertEqual(result["Type"], "餐饮")

    def test_standard_einvoice_parser_accepts_restaurant_shop_seller(self):
        pdf = self.root / "hotpot_shop_invoice.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票（普通发票）",
                    "发票号码：",
                    "开票日期：",
                    "购买方信息",
                    "销售方信息",
                    "名称：",
                    "名称：",
                    "25372000000301404618",
                    "2025年10月30日",
                    "济南优柯生物技术有限公司",
                    "91370100MA3PRNTBX4",
                    "历城区赛顺火锅店",
                    "92370112MA3LD8RF94",
                    "¥396.04",
                    "¥3.96",
                    "肆佰圆整",
                    "¥400.00",
                    "*餐饮服务*餐费",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "济南优柯生物技术有限公司")
        self.assertEqual(result["Seller"], "历城区赛顺火锅店")
        self.assertEqual(result["Amount"], "400.00")
        self.assertEqual(result["Type"], "餐饮")

    def test_standard_einvoice_parser_accepts_sparse_restaurant_seller_line(self):
        pdf = self.root / "sparse_restaurant_invoice.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票(普通发票)",
                    "发票号码:",
                    "开票日期:",
                    "名称:",
                    "名称:",
                    "项目名称",
                    "规格型号",
                    "单 位",
                    "数 量",
                    "单 价",
                    "金 额",
                    "税率/征收率",
                    "税 额",
                    "价税合计(大写)",
                    "(小写)",
                    "开票人:",
                    "26112000000944157226",
                    "2026年03月11日",
                    "辉瑞投资有限公司",
                    "91310000710920127H",
                    "北京满锅金涮肉馆",
                    "92110101MA00GT5483",
                    "¥172.28",
                    "¥1.72",
                    "壹佰柒拾肆圆整",
                    "¥174.00",
                    "*餐饮服务*餐饮服务",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Purchaser"], "辉瑞投资有限公司")
        self.assertEqual(result["Seller"], "北京满锅金涮肉馆")
        self.assertEqual(result["Amount"], "174.00")
        self.assertEqual(result["Type"], "餐饮")

    def test_standard_einvoice_parser_prefers_price_tax_total_small_amount(self):
        pdf = self.root / "didi_discount_invoice.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "电子发票（普通发票）",
                    "旅客运输服务",
                    "发票号码: 26337000000257791609",
                    "开票日期: 2026年03月11日",
                    "购买方信息",
                    "销售方信息",
                    "名称：辉瑞投资有限公司",
                    "统一社会信用代码/纳税人识别号：91310000710920127H",
                    "名称：杭州滴滴出行科技有限公司",
                    "统一社会信用代码/纳税人识别号：91330110MA2H0BC10Q",
                    "*运输服务*客运服务费",
                    "417.57",
                    "1",
                    "417.57",
                    "3%",
                    "12.53",
                    "*运输服务*客运服务费",
                    "-10.97",
                    "3%",
                    "-0.33",
                    "合计",
                    "406.60",
                    "¥",
                    "12.20",
                    "¥",
                    "价税合计（大写）",
                    "（小写）",
                    "418.80",
                    "¥",
                    "肆佰壹拾捌圆捌角整",
                ]
            ),
        )

        result = self.extractor._try_extract_standard_china_einvoice_from_pdf_text_v2(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Amount"], "418.80")
        self.assertEqual(result["Seller"], "杭州滴滴出行科技有限公司")
        self.assertEqual(result["Type"], "打车")

    def test_hotel_folio_parser_classifies_huazhu_checkout_bill_as_exempt_folio(self):
        pdf = self.root / "folio.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "全季杭州下沙大学城酒店",
                    "结账单",
                    "客人姓名",
                    "：亓勇",
                    "入住日期",
                    "：2026-06-10",
                    "离店日期",
                    "：2026-06-11",
                    "打印日期",
                    "：2026-06-11",
                    "消费合计",
                    "441.15",
                    "付款合计",
                    "441.15",
                ]
            ),
        )

        result = self.extractor._try_extract_ihg_folio_from_pdf_text(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Type"], "住宿水单")
        self.assertEqual(result["Date"], "20260611")
        self.assertEqual(result["Purchaser"], "亓勇")
        self.assertEqual(result["Seller"], "全季杭州下沙大学城酒店")
        self.assertEqual(result["Amount"], "441.15")

    def test_truth_builder_marriott_folio_uses_total_balance_and_departure_date(self):
        pdf = self.root / "marriott_folio.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "Renaissance Beijing Wangfujing Hotel 北京王府井金茂万丽酒店",
                    "Tel 电话： (86 10) 6520 8888 | marriott.com",
                    "Marriott Bonvoy",
                    "住宿服务",
                    "03-02-26",
                    "04-02-26",
                    "1,199.99",
                    "1,199.99",
                    "Total",
                    "2,399.98",
                    "Balance",
                    "CNY2,399.98",
                    "INFORMATION INVOICE PRINTED ON 05-FEB-26 03:32",
                    "Arrival",
                    "Departure",
                    "03-02-26",
                    "05-02-26",
                    "~{[G#:117332096|GA:2026-02-03|GD:2026-02-05|AMT:2,399.98|ST:FOLIO]}",
                    '~{[FOLIO:99200|2058.30,16706|135.84,16500|205.84]}',
                    "Guest Folio",
                    "Qi, Yong  亓勇",
                ]
            ),
        )

        result = parse_marriott_folio_pdf(pdf)

        self.assertEqual(result["truth_type"], "住宿水单")
        self.assertEqual(result["category"], "住宿水单")
        self.assertEqual(result["amount"], "2399.98")
        self.assertEqual(result["invoice_date"], "2026-02-05")

    def test_truth_builder_marriott_folio_uses_total_when_balance_is_zero(self):
        pdf = self.root / "paid_marriott_folio.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "The JW Marriott Hotel Hangzhou  杭州JW万豪酒店",
                    "marriott.com",
                    "Marriott Bonvoy",
                    "住宿服务",
                    "23/04/26",
                    "24/04/26",
                    "Total",
                    "1,219.64",
                    "1,219.64",
                    "Balance",
                    "CNY0.00",
                    "INFORMATION INVOICE PRINTED ON 24-APR-26 10:21",
                    "~{[G#:88685323|GA:2026-04-23|GD:2026-04-24|AMT:0.00|ST:FOLIO]}",
                    "~{[FOLIO:99200|1046.00,29103|5.81,29104|8.80,16706|63.23,16500|95.80]}",
                    "Guest Folio",
                    "Qi, Yong  亓勇",
                ]
            ),
        )

        result = parse_marriott_folio_pdf(pdf)

        self.assertEqual(result["amount"], "1219.64")
        self.assertEqual(result["invoice_date"], "2026-04-24")

    def test_runtime_marriott_folio_uses_total_when_balance_is_zero(self):
        pdf = self.root / "paid_marriott_folio.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "The JW Marriott Hotel Hangzhou  杭州JW万豪酒店",
                    "marriott.com",
                    "Marriott Bonvoy",
                    "住宿服务",
                    "23/04/26",
                    "24/04/26",
                    "Total",
                    "1,219.64",
                    "1,219.64",
                    "Balance",
                    "CNY0.00",
                    "INFORMATION INVOICE PRINTED ON 24-APR-26 10:21",
                    "~{[G#:88685323|GA:2026-04-23|GD:2026-04-24|AMT:0.00|ST:FOLIO]}",
                    "~{[FOLIO:99200|1046.00,29103|5.81,29104|8.80,16706|63.23,16500|95.80]}",
                    "Guest Folio",
                    "Qi, Yong  亓勇",
                ]
            ),
        )

        result = self.extractor._try_extract_generic_hotel_folio_from_pdf_text(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Type"], "住宿水单")
        self.assertEqual(result["Amount"], "1219.64")
        self.assertEqual(result["Date"], "20260424")
        self.assertEqual(result["Seller"], "The JW Marriott Hotel Hangzhou 杭州JW万豪酒店")

    def test_runtime_marriott_folio_uses_mmddyy_table_date_and_charge_amount(self):
        pdf = self.root / "courtyard_marriott.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "Courtyard by Marriott Hangzhou West 杭州西溪万怡酒店",
                    "www.marriott.com",
                    "INFORMATION INVOICE",
                    "Mr YONG QI",
                    "DATE",
                    "日期",
                    "CHARGES",
                    "消费",
                    "CREDITS",
                    "付款",
                    "房费",
                    "01-12-26",
                    "732.25",
                    "732.25",
                    "Balance 余额:",
                    "Arrive:",
                    "入住日期",
                    "Depart:",
                    "离店日期",
                    "01-12-26",
                    "01-13-26",
                    "亓勇",
                ]
            ),
        )

        result = self.extractor._try_extract_generic_hotel_folio_from_pdf_text(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Type"], "住宿水单")
        self.assertEqual(result["Date"], "20260112")
        self.assertEqual(result["Amount"], "732.25")
        self.assertEqual(result["Seller"], "Courtyard by Marriott Hangzhou West 杭州西溪万怡酒店")

    def test_runtime_gaode_itinerary_keeps_gaode_seller_and_total_amount(self):
        pdf = self.root / "gaode_itinerary.pdf"
        write_text_pdf(
            pdf,
            "\n".join(
                [
                    "高德地图—打车——行程单",
                    "AMAP ITINERARY",
                    "申请时间：2026-03-11",
                    "行程时间：2025-12-20 15:10至2026-02-03 21:11",
                    "共计2单行程，合计46.10元",
                    "序号",
                    "服务商",
                    "车型",
                    "上车时间",
                    "城市",
                    "起点",
                    "终点",
                    "金额",
                    "1",
                    "火箭出行",
                    "特快车",
                    "2025-12-20 15:10",
                    "北京市",
                    "北京机电研究所(西1门)",
                    "清河站(2层南进站口)",
                    "22.87元",
                    "2",
                    "火箭出行",
                    "优享型",
                    "2026-02-03 20:55",
                    "北京市",
                    "23.23元",
                ]
            ),
        )

        result = self.extractor._try_extract_ride_itinerary_from_pdf_text(str(pdf))

        self.assertIsNotNone(result)
        self.assertEqual(result["Type"], "打车")
        self.assertTrue(result["_is_itinerary"])
        self.assertEqual(result["Seller"], "高德地图")
        self.assertEqual(result["Date"], "20251220")
        self.assertEqual(result["Amount"], "46.10")

    def test_truth_builder_keeps_supporting_documents_out_of_non_target_bucket(self):
        self.assertEqual(
            expected_category_for("住宿水单", "亓勇", "辉瑞"),
            ("住宿水单", "住宿水单"),
        )
        self.assertEqual(
            expected_category_for("行程单", "亓勇", "辉瑞"),
            ("行程单", "行程单"),
        )

    def test_truth_type_from_fields_classifies_gaode_ride_invoice_as_ride(self):
        fields = {
            "truth_type": "其他",
            "category": "其他",
            "seller": "名称:北京利通出行科技有限公司",
            "invoice_number": "26117000000407045289",
        }
        meta = {
            "subject": "高德打车电子发票",
            "file_name": "【火箭出行-46.10元-2个行程】高德打车电子发票.pdf",
        }

        self.assertEqual(truth_type_from_fields(fields, meta, "辉瑞"), "打车")

    def test_truth_type_from_fields_keeps_didi_invoice_ahead_of_bundle_subject_itinerary(self):
        fields = {
            "truth_type": "其他",
            "category": "其他",
            "seller": "名称:北京滴滴出行科技有限公司",
            "invoice_number": "26117000000858801641",
        }
        meta = {
            "subject": "滴滴出行电子发票及行程报销单",
            "file_name": "滴滴电子发票A.pdf",
        }

        self.assertEqual(truth_type_from_fields(fields, meta, "辉瑞"), "打车")
        self.assertEqual(document_role_for("打车", fields, meta), "invoice")

    def test_truth_builder_uses_non_target_folder_for_non_target_purchasers(self):
        self.assertEqual(
            expected_category_for("其他", "个人", "辉瑞"),
            ("非目标公司发票", "非目标公司发票"),
        )

    def test_truth_builder_filters_existing_source_rows_by_local_mail_date_window(self):
        self.assertTrue(row_in_truth_window({"mail_date_local": "2026-02-05 03:32:39"}, "2026-02-05", "2026-02-06"))
        self.assertFalse(row_in_truth_window({"mail_date_local": "2026-02-04 23:59:59"}, "2026-02-05", "2026-02-06"))
        self.assertFalse(row_in_truth_window({"mail_date_local": "2026-02-06 00:00:00"}, "2026-02-05", "2026-02-06"))

    def test_strict_audit_seller_fuzzy_match_ignores_whitespace(self):
        self.assertTrue(
            contains_fuzzy(
                "The JW Marriott Hotel Hangzhou  杭州JW万豪酒店",
                "The JW Marriott Hotel Hangzhou 杭州JW万豪酒店",
            )
        )

    def test_filename_folio_signal_overrides_generic_hotel_type_before_purchaser_check(self):
        helper = getattr(app_api, "normalize_document_type_for_archive", None)
        self.assertIsNotNone(helper, "app_api.normalize_document_type_for_archive is required")
        if helper is None:
            return

        info_json = {"Type": "住宿", "Seller": "Sheraton Changsha Hotel"}
        doc_type, reason_codes = helper(info_json, "csxsi_folio_ef_sj_gc524340322.pdf", False)

        self.assertEqual(doc_type, "住宿水单")
        self.assertTrue(info_json["_is_folio"])
        self.assertIn("CLASSIFIED_AS_HOTEL_FOLIO", reason_codes)

    def test_strict_audit_uses_final_archive_fields_for_train_departure_date(self):
        run_root = self.root / "train-run"
        output_dir = run_root / "output" / "火车票"
        monitoring_dir = run_root / "monitoring"
        diagnostics_dir = run_root / "diagnostics"
        output_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        diagnostics_dir.mkdir(parents=True)
        train_path = output_dir / "20251124-济南西站-苏州北站-火车票.pdf"
        train_path.write_text("train", encoding="utf-8")
        (monitoring_dir / "artifact_events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "archive",
                    "document_id": "train-doc",
                    "path": str(train_path),
                    "email_id": "2387",
                    "file_name": "25379166812003105592.pdf",
                    "category": "火车票",
                    "final_type": "火车票",
                    "seller": "中国铁路",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (diagnostics_dir / "debug_trace.jsonl").write_text(
            json.dumps(
                {
                    "document_id": "train-doc",
                    "source_filename": "25379166812003105592.pdf",
                    "normalized_fields": {
                        "Date": "20251224",
                        "Amount": "702.00",
                        "Seller": "中国铁路",
                        "Type": "火车票",
                        "InvoiceNumber": "25379166812003105592",
                    },
                    "extractor_raw_result": {
                        "result": {
                            "Departure_Date": "20251124",
                            "Departure_City": "济南西站",
                            "Destination_City": "苏州北站",
                        }
                    },
                    "classification_result": {"category": "火车票", "final_type": "火车票"},
                    "naming_result": {"final_path": str(train_path), "display_type": "火车票"},
                    "archive_target": str(train_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "train",
                    "source_email_id": "2387",
                    "file_name": "25379166812003105592.pdf",
                    "truth_type": "火车票",
                    "document_role": "invoice",
                    "invoice_date": "2025-11-24",
                    "seller": "中国铁路",
                    "amount": "702.00",
                    "invoice_number": "25379166812003105592",
                    "expected_category": "火车票",
                }
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["user_p1_conclusion"]["field_mismatch_rows"], [])

    def test_strict_audit_uses_combined_ride_filename_amount(self):
        run_root = self.root / "ride-combined-run"
        output_dir = run_root / "output" / "打车"
        monitoring_dir = run_root / "monitoring"
        diagnostics_dir = run_root / "diagnostics"
        output_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        diagnostics_dir.mkdir(parents=True)
        invoice_path = output_dir / "0311-滴滴-03-发票_282.03元.pdf"
        invoice_path.write_text("ride", encoding="utf-8")
        (monitoring_dir / "artifact_events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "archive",
                    "document_id": "ride-invoice-doc",
                    "path": str(invoice_path),
                    "email_id": "6899",
                    "file_name": "滴滴电子发票A.pdf",
                    "category": "打车",
                    "final_type": "打车",
                    "seller": "滴滴出行科技有限公司",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (diagnostics_dir / "debug_trace.jsonl").write_text(
            json.dumps(
                {
                    "document_id": "ride-invoice-doc",
                    "source_filename": "滴滴电子发票A.pdf",
                    "normalized_fields": {
                        "Date": "20260311",
                        "Amount": "273.81",
                        "Seller": "滴滴出行科技有限公司",
                        "Type": "打车",
                        "InvoiceNumber": "26127000000147892168",
                    },
                    "classification_result": {"category": "打车", "final_type": "打车"},
                    "naming_result": {"final_path": str(invoice_path), "display_type": "打车"},
                    "combine_result": {
                        "status": "matched",
                        "reason_code": "RIDE_COMBINE_MATCHED",
                        "final_filename": invoice_path.name,
                    },
                    "archive_target": str(invoice_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "ride-invoice",
                    "source_email_id": "6899",
                    "file_name": "滴滴电子发票A.pdf",
                    "truth_type": "打车",
                    "document_role": "invoice",
                    "invoice_date": "2026-03-11",
                    "seller": "滴滴出行",
                    "amount": "282.03",
                    "invoice_number": "26127000000147892168",
                    "expected_category": "打车",
                }
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["user_p1_conclusion"]["field_mismatch_rows"], [])

    def test_strict_audit_accepts_extracted_ride_invoice_amount_after_pair_rename(self):
        run_root = self.root / "ride-combined-run-tax-gap"
        output_dir = run_root / "output" / "打车"
        monitoring_dir = run_root / "monitoring"
        diagnostics_dir = run_root / "diagnostics"
        output_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        diagnostics_dir.mkdir(parents=True)
        invoice_path = output_dir / "0614-滴滴-12-发票_3149.19元.pdf"
        invoice_path.write_text("ride", encoding="utf-8")
        (monitoring_dir / "artifact_events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "archive",
                    "document_id": "ride-invoice-doc",
                    "path": str(invoice_path),
                    "email_id": "7089",
                    "file_name": "滴滴电子发票A.pdf",
                    "category": "打车",
                    "final_type": "打车",
                    "seller": "北京滴滴出行科技有限公司",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (diagnostics_dir / "debug_trace.jsonl").write_text(
            json.dumps(
                {
                    "document_id": "ride-invoice-doc",
                    "source_filename": "滴滴电子发票A.pdf",
                    "normalized_fields": {
                        "Date": "20260614",
                        "Amount": "3057.46",
                        "Seller": "北京滴滴出行科技有限公司",
                        "Type": "打车",
                        "InvoiceNumber": "26117000000858801641",
                    },
                    "classification_result": {"category": "打车", "final_type": "打车"},
                    "naming_result": {"final_path": str(invoice_path), "display_type": "打车"},
                    "combine_result": {
                        "status": "matched",
                        "reason_code": "RIDE_COMBINE_MATCHED",
                        "final_filename": invoice_path.name,
                    },
                    "archive_target": str(invoice_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "ride-invoice",
                    "source_email_id": "7089",
                    "file_name": "滴滴电子发票A.pdf",
                    "truth_type": "打车",
                    "document_role": "invoice",
                    "invoice_date": "2026-06-14",
                    "seller": "北京滴滴出行科技有限公司",
                    "amount": "3057.46",
                    "invoice_number": "26117000000858801641",
                    "expected_category": "打车",
                }
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["user_p1_conclusion"]["field_mismatch_rows"], [])

    def test_strict_audit_keeps_ride_itinerary_trip_date_after_pair_rename(self):
        run_root = self.root / "ride-itinerary-date-run"
        output_dir = run_root / "output" / "打车"
        monitoring_dir = run_root / "monitoring"
        diagnostics_dir = run_root / "diagnostics"
        output_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        diagnostics_dir.mkdir(parents=True)
        itinerary_path = output_dir / "0311-高德-07-行程单_46.10元.pdf"
        itinerary_path.write_text("ride-itinerary", encoding="utf-8")
        (monitoring_dir / "artifact_events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "archive",
                    "document_id": "ride-itinerary-doc",
                    "path": str(itinerary_path),
                    "email_id": "6904",
                    "file_name": "高德打车电子行程单.pdf",
                    "category": "打车",
                    "final_type": "打车",
                    "seller": "高德地图",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (diagnostics_dir / "debug_trace.jsonl").write_text(
            json.dumps(
                {
                    "document_id": "ride-itinerary-doc",
                    "source_filename": "高德打车电子行程单.pdf",
                    "normalized_fields": {
                        "Date": "20251220",
                        "Amount": "46.10",
                        "Seller": "高德地图",
                        "Type": "打车",
                        "InvoiceNumber": "",
                    },
                    "classification_result": {"category": "打车", "final_type": "打车"},
                    "naming_result": {"final_path": str(itinerary_path), "display_type": "打车行程单"},
                    "combine_result": {
                        "status": "matched",
                        "reason_code": "RIDE_COMBINE_MATCHED",
                        "final_filename": itinerary_path.name,
                    },
                    "archive_target": str(itinerary_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "ride-itinerary",
                    "source_email_id": "6904",
                    "file_name": "高德打车电子行程单.pdf",
                    "truth_type": "行程单",
                    "document_role": "itinerary",
                    "invoice_date": "2025-12-20",
                    "seller": "高德地图",
                    "amount": "46.10",
                    "expected_category": "行程单",
                    "sha256": "",
                }
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["user_p1_conclusion"]["field_mismatch_rows"], [])

    def test_strict_audit_keeps_hotel_folio_stay_date_after_pair_rename(self):
        run_root = self.root / "hotel-folio-date-run"
        output_dir = run_root / "output" / "住宿发票"
        monitoring_dir = run_root / "monitoring"
        diagnostics_dir = run_root / "diagnostics"
        output_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        diagnostics_dir.mkdir(parents=True)
        folio_path = output_dir / "20260113-住宿-03-水单_732.25元.pdf"
        folio_path.write_text("hotel-folio", encoding="utf-8")
        (monitoring_dir / "artifact_events.jsonl").write_text(
            json.dumps(
                {
                    "kind": "archive",
                    "document_id": "hotel-folio-doc",
                    "path": str(folio_path),
                    "email_id": "2409",
                    "file_name": "hghcw_folio_franchised22333148.pdf",
                    "category": "住宿发票",
                    "final_type": "住宿水单",
                    "seller": "Courtyard by Marriott Hangzhou West 杭州西溪万怡酒店",
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (diagnostics_dir / "debug_trace.jsonl").write_text(
            json.dumps(
                {
                    "document_id": "hotel-folio-doc",
                    "source_filename": "hghcw_folio_franchised22333148.pdf",
                    "normalized_fields": {
                        "Date": "20260112",
                        "Amount": "732.25",
                        "Seller": "Courtyard by Marriott Hangzhou West 杭州西溪万怡酒店",
                        "Type": "住宿水单",
                        "InvoiceNumber": "",
                    },
                    "classification_result": {"category": "住宿发票", "final_type": "住宿水单"},
                    "naming_result": {"final_path": str(folio_path), "display_type": "住宿水单"},
                    "archive_target": str(folio_path),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "hotel-folio",
                    "source_email_id": "2409",
                    "file_name": "hghcw_folio_franchised22333148.pdf",
                    "truth_type": "住宿水单",
                    "document_role": "hotel_folio",
                    "invoice_date": "2026-01-12",
                    "seller": "Courtyard by Marriott Hangzhou West 杭州西溪万怡酒店",
                    "amount": "732.25",
                    "expected_category": "住宿水单",
                    "invoice_number": "",
                    "sha256": "",
                }
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["user_p1_conclusion"]["field_mismatch_rows"], [])

    def test_strict_audit_reports_required_hotel_pair_when_files_are_not_combined(self):
        run_root = self.root / "run"
        output_dir = run_root / "output" / "住宿发票"
        monitoring_dir = run_root / "monitoring"
        output_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        invoice_path = output_dir / "20260601_住宿发票_1950.00_湖南运达酒店管理有限公司长沙运达喜来登酒店.pdf"
        folio_path = output_dir / "20260601_住宿水单_1950.00_Sheraton Changsha Hotel.pdf"
        invoice_path.write_text("invoice", encoding="utf-8")
        folio_path.write_text("folio", encoding="utf-8")
        events = [
            {
                "kind": "archive",
                "document_id": "invoice-doc",
                "path": str(invoice_path),
                "email_id": "7051",
                "file_name": "7051_downloadFormat_XML.xml",
                "category": "住宿发票",
                "final_type": "住宿发票",
                "seller": "湖南运达酒店管理有限公司长沙运达喜来登酒店",
            },
            {
                "kind": "archive",
                "document_id": "folio-doc",
                "path": str(folio_path),
                "email_id": "7050",
                "file_name": "csxsi_folio_ef_sj_gc524340322.pdf",
                "category": "住宿发票",
                "final_type": "住宿水单",
                "seller": "Sheraton Changsha Hotel",
            },
        ]
        (monitoring_dir / "artifact_events.jsonl").write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
            encoding="utf-8",
        )
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "invoice",
                    "source_email_id": "7051",
                    "file_name": "7051_downloadFormat_XML.xml",
                    "truth_type": "住宿发票",
                    "document_role": "invoice",
                    "invoice_date": "2026-06-01",
                    "seller": "湖南运达酒店管理有限公司长沙运达喜来登酒店",
                    "amount": "1950.00",
                    "expected_category": "住宿发票",
                },
                {
                    "truth_id": "folio",
                    "source_email_id": "7050",
                    "file_name": "csxsi_folio_ef_sj_gc524340322.pdf",
                    "truth_type": "住宿水单",
                    "document_role": "hotel_folio",
                    "invoice_date": "2026-06-01",
                    "seller": "Sheraton Changsha Hotel",
                    "amount": "1950.00",
                    "expected_category": "住宿水单",
                },
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["p0_conclusion"]["count"], 0)
        self.assertEqual(result["user_p1_conclusion"]["count"], 0)
        self.assertIn("p2_conclusion", result)
        self.assertEqual(result["p2_conclusion"]["count"], 1)
        self.assertEqual(result["p2_conclusion"]["bad_rows"][0]["reason"], "required_hotel_pair_not_combined")

    def test_strict_audit_prefers_archived_invoice_number_over_duplicate_retention_sha(self):
        run_root = self.root / "hotel-run-with-duplicate-retention"
        output_dir = run_root / "output" / "住宿发票"
        duplicate_dir = run_root / "output" / "_audit_retention" / "duplicates"
        monitoring_dir = run_root / "monitoring"
        diagnostics_dir = run_root / "diagnostics"
        output_dir.mkdir(parents=True)
        duplicate_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        diagnostics_dir.mkdir(parents=True)
        invoice_path = output_dir / "20260601-住宿-01-发票_1950.00元.pdf"
        folio_path = output_dir / "20260601-住宿-01-水单_1950.00元.pdf"
        duplicate_path = duplicate_dir / "baiwang_1_preview_api_pdf_direct.pdf"
        invoice_path.write_text("archived invoice", encoding="utf-8")
        folio_path.write_text("archived folio", encoding="utf-8")
        duplicate_path.write_text("duplicate invoice", encoding="utf-8")
        events = [
            {
                "kind": "archive",
                "document_id": "invoice-doc",
                "path": str(invoice_path),
                "email_id": "7051",
                "file_name": "email_body_receipt_26432000001239781576.pdf",
                "category": "住宿发票",
                "final_type": "住宿发票",
                "seller": "湖南运达酒店管理有限公司长沙运达喜来登酒店",
            },
            {
                "kind": "archive",
                "document_id": "folio-doc",
                "path": str(folio_path),
                "email_id": "7050",
                "file_name": "csxsi_folio_ef_sj_gc524340322.pdf",
                "category": "住宿发票",
                "final_type": "住宿水单",
                "seller": "Sheraton Changsha Hotel",
            },
        ]
        (monitoring_dir / "artifact_events.jsonl").write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
            encoding="utf-8",
        )
        traces = [
            {
                "document_id": "invoice-doc",
                "source_filename": "email_body_receipt_26432000001239781576.pdf",
                "archive_target": str(invoice_path),
                "normalized_fields": {
                    "InvoiceNumber": "26432000001239781576",
                    "Date": "20260601",
                    "Amount": "1950.00",
                    "Seller": "湖南运达酒店管理有限公司长沙运达喜来登酒店",
                },
                "classification_result": {"category": "住宿发票", "final_type": "住宿发票"},
                "naming_result": {"target_folder": "住宿发票", "display_type": "住宿发票"},
                "combine_result": {"status": "matched"},
            },
            {
                "document_id": "folio-doc",
                "source_filename": "csxsi_folio_ef_sj_gc524340322.pdf",
                "archive_target": str(folio_path),
                "normalized_fields": {
                    "Date": "20260601",
                    "Amount": "1950.00",
                    "Seller": "Sheraton Changsha Hotel",
                },
                "classification_result": {"category": "住宿发票", "final_type": "住宿水单"},
                "naming_result": {"target_folder": "住宿发票", "display_type": "住宿水单"},
                "combine_result": {"status": "matched"},
            },
            {
                "document_id": "duplicate-doc",
                "source_filename": "baiwang_1_preview_api_pdf_direct.pdf",
                "archive_target": str(duplicate_path),
                "normalized_fields": {
                    "InvoiceNumber": "26432000001239781576",
                    "Date": "20260601",
                    "Amount": "1950.00",
                    "Seller": "湖南运达酒店管理有限公司长沙运达喜来登酒店",
                },
                "classification_result": {"category": "duplicates", "final_type": "住宿发票"},
                "naming_result": {"status": "skipped", "reason_code": "BUSINESS_DUPLICATE_SKIPPED"},
                "combine_result": {"status": "not_applicable"},
            },
        ]
        (diagnostics_dir / "debug_trace.jsonl").write_text(
            "\n".join(json.dumps(trace, ensure_ascii=False) for trace in traces),
            encoding="utf-8",
        )
        duplicate_sha = __import__("hashlib").sha256(duplicate_path.read_bytes()).hexdigest()
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "invoice",
                    "source_email_id": "7051",
                    "file_name": "baiwang_1_preview_api_pdf_direct.pdf",
                    "truth_type": "住宿发票",
                    "document_role": "invoice",
                    "invoice_number": "26432000001239781576",
                    "invoice_date": "2026-06-01",
                    "seller": "湖南运达酒店管理有限公司长沙运达喜来登酒店",
                    "amount": "1950.00",
                    "expected_category": "住宿发票",
                    "sha256": duplicate_sha,
                },
                {
                    "truth_id": "folio",
                    "source_email_id": "7050",
                    "file_name": "csxsi_folio_ef_sj_gc524340322.pdf",
                    "truth_type": "住宿水单",
                    "document_role": "hotel_folio",
                    "invoice_date": "2026-06-01",
                    "seller": "Sheraton Changsha Hotel",
                    "amount": "1950.00",
                    "expected_category": "住宿水单",
                },
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["p0_conclusion"]["count"], 0)
        self.assertEqual(result["p2_conclusion"]["count"], 0)
        invoice_match = next(row for row in result["matched_rows"] if row["truth_id"] == "invoice")
        self.assertEqual(Path(invoice_match["matched_path"]).name, invoice_path.name)

    def test_strict_audit_reports_required_ride_pair_when_files_are_not_combined(self):
        run_root = self.root / "ride-run"
        output_dir = run_root / "output" / "打车"
        monitoring_dir = run_root / "monitoring"
        output_dir.mkdir(parents=True)
        monitoring_dir.mkdir(parents=True)
        invoice_path = output_dir / "20260605_打车_100.00_滴滴出行.pdf"
        itinerary_path = output_dir / "20260605_行程单_100.00_滴滴出行.pdf"
        invoice_path.write_text("invoice", encoding="utf-8")
        itinerary_path.write_text("itinerary", encoding="utf-8")
        events = [
            {
                "kind": "archive",
                "document_id": "ride-invoice-doc",
                "path": str(invoice_path),
                "email_id": "8001",
                "file_name": "didi_invoice.pdf",
                "category": "打车",
                "final_type": "打车",
                "seller": "滴滴出行",
            },
            {
                "kind": "archive",
                "document_id": "ride-itinerary-doc",
                "path": str(itinerary_path),
                "email_id": "8001",
                "file_name": "didi_itinerary.pdf",
                "category": "打车",
                "final_type": "行程单",
                "seller": "滴滴出行",
            },
        ]
        (monitoring_dir / "artifact_events.jsonl").write_text(
            "\n".join(json.dumps(event, ensure_ascii=False) for event in events),
            encoding="utf-8",
        )
        manifest = {
            "summary": {"finalized": True},
            "included": [
                {
                    "truth_id": "ride-invoice",
                    "source_email_id": "8001",
                    "file_name": "didi_invoice.pdf",
                    "truth_type": "打车",
                    "document_role": "invoice",
                    "invoice_date": "2026-06-05",
                    "seller": "滴滴出行",
                    "amount": "100.00",
                    "expected_category": "打车",
                },
                {
                    "truth_id": "ride-itinerary",
                    "source_email_id": "8001",
                    "file_name": "didi_itinerary.pdf",
                    "truth_type": "打车行程单",
                    "document_role": "ride_itinerary",
                    "invoice_date": "2026-06-05",
                    "seller": "滴滴出行",
                    "amount": "100.00",
                    "expected_category": "打车",
                },
            ],
        }

        result = compare(manifest, run_root)

        self.assertEqual(result["p0_conclusion"]["count"], 0)
        self.assertEqual(result["user_p1_conclusion"]["count"], 0)
        self.assertEqual(result["p2_conclusion"]["count"], 1)
        self.assertEqual(result["p2_conclusion"]["bad_rows"][0]["reason"], "required_ride_pair_not_combined")


if __name__ == "__main__":
    unittest.main()
