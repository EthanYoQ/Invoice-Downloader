import re
from email.utils import parsedate_to_datetime

import fitz


CANONICAL_MARKER = "EMAIL_BODY_RECEIPT_CANONICAL"


def _normalize_amount(value):
    match = re.search(r"-?\d+(?:\.\d{1,2})?", str(value or "").replace(",", ""))
    if not match:
        return ""
    return f"{float(match.group(0)):.2f}"


def _normalize_date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(20\d{2})[-/.年]\s*(\d{1,2})[-/.月]\s*(\d{1,2})", text)
    if match:
        return f"{match.group(1)}{int(match.group(2)):02d}{int(match.group(3)):02d}"
    compact = re.sub(r"\D", "", text)
    if len(compact) >= 8 and compact.startswith("20"):
        return compact[:8]
    return ""


def _normalize_token(value):
    return re.sub(r"\s+", "", str(value or "")).strip()


def _date_from_email_header(email_date):
    if not email_date:
        return ""
    try:
        dt = parsedate_to_datetime(str(email_date))
    except Exception:
        return ""
    if not dt:
        return ""
    return f"{dt.year:04d}{dt.month:02d}{dt.day:02d}"


def _type_from_seller(seller):
    text = str(seller or "")
    if any(token in text for token in ("酒店", "宾馆", "住宿", "旅店", "万丽", "喜来登", "万豪")):
        return "住宿发票"
    if any(token in text for token in (
        "餐",
        "饭",
        "菜",
        "茶",
        "饭店",
        "酒家",
        "面馆",
        "火锅",
        "咖啡",
        "食",
        "海鲜",
        "肯德基",
        "麦当劳",
        "金拱",
        "盒马",
        "饮品",
        "烧烤",
        "串串",
        "食府",
        "小吃",
    )):
        return "餐饮"
    return "其他"


def _parse_icloud_receipt(subject, sender, body_text):
    combined = f"{subject}\n{sender}\n{body_text}"
    lowered = combined.lower()
    if "icloud" not in lowered or "文稿" not in combined:
        return {}

    invoice_match = re.search(r"文稿(?:编号)?\s*[:：]?\s*([0-9]{8,20})", combined)
    if not invoice_match:
        return {}

    date_match = re.search(r"(?:日期|收据)\s*[:：]?\s*(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日)", combined)
    if not date_match:
        date_match = re.search(r"(20\d{2}年\s*\d{1,2}月\s*\d{1,2}日)", combined)

    total_match = re.search(r"总计\s*[¥￥]?\s*([0-9]+(?:\.[0-9]{1,2})?)", combined)
    amount = _normalize_amount(total_match.group(1) if total_match else "")
    if not amount:
        amounts = re.findall(r"[¥￥]\s*([0-9]+(?:\.[0-9]{1,2})?)", combined)
        if amounts:
            amount = _normalize_amount(max(amounts, key=lambda item: float(item)))

    seller = "云上艾珀（贵州）技术有限公司" if ("云上艾珀" in combined or "云上贵州" in combined) else "Apple"
    date_value = _normalize_date(date_match.group(1) if date_match else "")
    if not amount or not date_value:
        return {}

    return {
        "is_invoice": True,
        "Date": date_value,
        "Purchaser": "个人",
        "Seller": seller,
        "Amount": amount,
        "InvoiceCode": "",
        "InvoiceNumber": invoice_match.group(1),
        "Type": "其他",
        "category": "其他",
        "Departure_Date": "",
        "Departure_City": "",
        "Destination_City": "",
    }


def _parse_baiwang_body(subject, sender, body_text):
    combined = f"{subject}\n{sender}\n{body_text}"
    if "发票号码" not in combined or "为您开具了电子发票" not in combined:
        return {}
    if "baiwang" not in combined.lower() and "百望" not in combined and "电子发票下载" not in subject:
        return {}

    seller_match = re.search(r"您好[:：]?\s*(.+?)为您开具了电子发票", combined, flags=re.DOTALL)
    if not seller_match:
        seller_match = re.search(r"([^\n]+?)为您开具了电子发票", combined)
    number_match = re.search(r"发票号码\s*[:：]?\s*([0-9]{8,20})", combined)
    amount_match = re.search(r"发票金额\s*[:：]?\s*([0-9]+(?:\.[0-9]{1,2})?)", combined)
    date_match = re.search(r"开票日期\s*[:：]?\s*(20\d{2}[-/.年]\s*\d{1,2}[-/.月]\s*\d{1,2})", combined)
    purchaser_match = re.search(r"购方名称\s*[:：]?\s*([^\s]+)", combined)
    if not (seller_match and number_match and amount_match and date_match):
        return {}

    seller = _normalize_token(seller_match.group(1).splitlines()[-1])
    purchaser = _normalize_token(purchaser_match.group(1) if purchaser_match else "")
    amount = _normalize_amount(amount_match.group(1))
    date_value = _normalize_date(date_match.group(1))
    if not seller or not amount or not date_value:
        return {}
    doc_type = _type_from_seller(seller)

    return {
        "is_invoice": True,
        "Date": date_value,
        "Purchaser": purchaser or "未知购买方",
        "Seller": seller,
        "Amount": amount,
        "InvoiceCode": "",
        "InvoiceNumber": number_match.group(1),
        "Type": doc_type,
        "category": doc_type,
        "Departure_Date": "",
        "Departure_City": "",
        "Destination_City": "",
    }


def _parse_51fapiao_body(subject, sender, body_text, email_date):
    combined = f"{subject}\n{sender}\n{body_text}"
    if "51fapiao" not in combined.lower() and "51发票" not in combined:
        return {}
    number_match = re.search(r"发票号码[:：]?\s*([0-9]{8,20})", combined)
    seller_match = re.search(r"来自[【\[](.+?)[】\]]", combined)
    amount_match = re.search(r"金额为\s*([0-9]+(?:\.[0-9]{1,2})?)", combined)
    purchaser_match = re.search(r"购方名称[:：]?\s*([^\s\]]+)", combined)
    if not (number_match and seller_match and amount_match):
        return {}
    seller = _normalize_token(seller_match.group(1))
    amount = _normalize_amount(amount_match.group(1))
    date_value = _normalize_date(combined) or _date_from_email_header(email_date)
    if not seller or not amount or not date_value:
        return {}
    doc_type = _type_from_seller(seller)
    return {
        "is_invoice": True,
        "Date": date_value,
        "Purchaser": _normalize_token(purchaser_match.group(1) if purchaser_match else "") or "未知购买方",
        "Seller": seller,
        "Amount": amount,
        "InvoiceCode": "",
        "InvoiceNumber": number_match.group(1),
        "Type": doc_type,
        "category": doc_type,
        "Departure_Date": "",
        "Departure_City": "",
        "Destination_City": "",
    }


def parse_email_body_receipt_fields(subject="", sender="", body_text="", email_date=""):
    body = str(body_text or "")
    if len(body.strip()) < 40:
        return {}
    for parser in (
        lambda: _parse_baiwang_body(subject, sender, body),
        lambda: _parse_51fapiao_body(subject, sender, body, email_date),
        lambda: _parse_icloud_receipt(subject, sender, body),
    ):
        fields = parser()
        if fields:
            return fields
    return {}


def build_email_body_receipt_filename(email_id="", fields=None):
    fields = dict(fields or {})
    invoice_number = re.sub(r"\W+", "", str(fields.get("InvoiceNumber") or "")).strip()
    if invoice_number:
        return f"email_body_receipt_{invoice_number}.pdf"
    email_part = re.sub(r"\W+", "", str(email_id or "")).strip() or "unknown"
    return f"email_body_receipt_{email_part}.pdf"


def render_email_body_receipt_pdf_bytes(fields, body_text="", source_email_id=""):
    fields = dict(fields or {})
    canonical_lines = [
        CANONICAL_MARKER,
        f"来源邮件ID: {source_email_id}",
        f"发票号码: {fields.get('InvoiceNumber', '')}",
        f"发票代码: {fields.get('InvoiceCode', '')}",
        f"开票日期: {fields.get('Date', '')}",
        f"购买方名称: {fields.get('Purchaser', '')}",
        f"销售方名称: {fields.get('Seller', '')}",
        f"价税合计: {fields.get('Amount', '')}",
        f"票据类型: {fields.get('Type', '')}",
        "",
        "原始邮件正文摘录:",
    ]
    body_lines = [line.strip() for line in str(body_text or "").splitlines() if line.strip()]
    doc = fitz.open()
    margin = 48
    line_height = 14
    max_y = 842 - margin
    max_chars = 48
    page = doc.new_page(width=595, height=842)
    y = margin

    def _wrap_line(line):
        value = str(line or "")
        if not value:
            return [""]
        return [value[index:index + max_chars] for index in range(0, len(value), max_chars)]

    for raw_line in canonical_lines + body_lines[:120]:
        for line in _wrap_line(raw_line):
            if y > max_y:
                page = doc.new_page(width=595, height=842)
                y = margin
            page.insert_text((margin, y), line, fontsize=10, fontname="china-s")
            y += line_height
    payload = doc.tobytes()
    doc.close()
    return payload
