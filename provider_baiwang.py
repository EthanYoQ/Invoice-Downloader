import re
import xml.etree.ElementTree as ET
from urllib.parse import urlparse


BAIWANG_HOST_TOKENS = (
    "baiwang.com",
    "efapiao.com",
)

BAIWANG_URL_TOKENS = (
    "u.baiwang.com",
    "previewinvoiceall",
    "previewinvoice",
    "smkp-vue",
    "maillink",
    "downloadpdf",
    "downloadofd",
    "downloadxml",
)

BAIWANG_WRAPPER_MARKERS = (
    "发票预览",
    "下载pdf",
    "下载 pdf",
    "下载ofd",
    "下载 ofd",
    "下载xml",
    "下载 xml",
    "关于百望",
    "previewinvoice",
    "downloadpdf",
    "downloadofd",
    "downloadxml",
)

BAIWANG_STRUCTURED_MARKERS = (
    "发票号码",
    "发票代码",
    "购买方名称",
    "销售方名称",
    "价税合计",
    "开票日期",
    "发票金额",
    "invoice_number",
    "seller",
    "buyer",
)


def compact_text(value):
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def normalize_token(value):
    return re.sub(r"\s+", "", str(value or "")).strip()


def normalize_amount(value):
    match = re.search(r"(\d+(?:\.\d{2})?)", str(value or ""))
    if not match:
        return ""
    amount = match.group(1)
    if "." not in amount:
        return f"{amount}.00"
    integer, decimal = amount.split(".", 1)
    return f"{integer}.{decimal[:2].ljust(2, '0')}"


def normalize_date(value):
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if not match:
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def is_baiwang_family_url(url, *, sender_addr="", subject=""):
    lower_url = str(url or "").strip().lower()
    host = urlparse(lower_url).netloc
    lower_sender = str(sender_addr or "").lower()
    lower_subject = compact_text(subject)
    if any(token in host for token in BAIWANG_HOST_TOKENS):
        return True
    if any(token in lower_url for token in BAIWANG_URL_TOKENS):
        return True
    if "baiwang" in lower_sender:
        return True
    if "电子发票下载" in lower_subject or "发票下载" in lower_subject:
        return True
    return False


def collect_baiwang_candidate_urls(primary_url, extra_urls=None):
    ordered = []
    seen = set()
    for candidate in [primary_url, *(extra_urls or [])]:
        normalized = str(candidate or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def build_baiwang_group_key(*, email_id="", candidate_urls=None):
    email_id = str(email_id or "").strip()
    if email_id:
        return f"baiwang:{email_id}"
    urls = collect_baiwang_candidate_urls("", candidate_urls or [])
    if not urls:
        return "baiwang:unknown"
    return f"baiwang:{'|'.join(sorted(urls))}"


def extract_baiwang_email_fields(body_text):
    body = re.sub(r"\s+", " ", str(body_text or "")).strip()
    if not body:
        return {}

    result = {
        "seller": "",
        "purchaser": "",
        "amount": "",
        "invoice_date": "",
        "invoice_number": "",
        "invoice_code": "",
    }

    seller_patterns = [
        r"(?:用户，您好[:：]?\s*|您好[:：]?\s*)(.+?)为您开具了电子发票",
        r"(.+?)为您开具了电子发票",
        r"来自[【\\[](.+?)[】\\]]开具的发票",
        r"seller[:：]?\s*([^\s]+)",
    ]
    for pattern in seller_patterns:
        seller_match = re.search(pattern, body, flags=re.IGNORECASE)
        if seller_match:
            result["seller"] = normalize_token(seller_match.group(1))
            break

    purchaser_patterns = [
        r"购买方名称[:：]?\s*([^\s]+)",
        r"buyer[:：]?\s*([^\s]+)",
    ]
    for pattern in purchaser_patterns:
        purchaser_match = re.search(pattern, body, flags=re.IGNORECASE)
        if purchaser_match:
            result["purchaser"] = normalize_token(purchaser_match.group(1))
            break

    amount_patterns = [
        r"(?:发票金额|价税合计|amount)[:：]?\s*([0-9]+\.[0-9]{2})",
    ]
    for pattern in amount_patterns:
        amount_match = re.search(pattern, body, flags=re.IGNORECASE)
        if amount_match:
            result["amount"] = normalize_amount(amount_match.group(1))
            break
    if not result["amount"]:
        all_amounts = re.findall(r"([0-9]+\.[0-9]{2})", body)
        if all_amounts:
            result["amount"] = normalize_amount(max(all_amounts, key=lambda value: float(value)))

    date_patterns = [
        r"(?:开票日期|issue\s*time|issue\s*date|request\s*time|date)[:：]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})",
    ]
    for pattern in date_patterns:
        date_match = re.search(pattern, body, flags=re.IGNORECASE)
        if date_match:
            result["invoice_date"] = normalize_date(date_match.group(1))
            break
    if not result["invoice_date"]:
        date_candidates = re.findall(r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})", body)
        if date_candidates:
            result["invoice_date"] = normalize_date(date_candidates[0])

    number_patterns = [
        r"(?:发票号码|invoice\s*number)[:：]?\s*([0-9]{8,})",
    ]
    for pattern in number_patterns:
        number_match = re.search(pattern, body, flags=re.IGNORECASE)
        if number_match:
            result["invoice_number"] = number_match.group(1)
            break
    if not result["invoice_number"]:
        digit_candidates = re.findall(r"(?<!\d)(\d{20})(?!\d)", body)
        if digit_candidates:
            result["invoice_number"] = digit_candidates[0]

    code_patterns = [
        r"(?:发票代码|invoice\s*code)[:：]?\s*([0-9]{8,})",
    ]
    for pattern in code_patterns:
        code_match = re.search(pattern, body, flags=re.IGNORECASE)
        if code_match:
            result["invoice_code"] = code_match.group(1)
            break

    return {key: value for key, value in result.items() if value}


def merge_expected_fields(*field_maps):
    merged = {
        "seller": "",
        "purchaser": "",
        "amount": "",
        "invoice_date": "",
        "invoice_number": "",
        "invoice_code": "",
    }
    for field_map in field_maps:
        for key, value in dict(field_map or {}).items():
            if value and not merged.get(key):
                merged[key] = value
    return {key: value for key, value in merged.items() if value}


def looks_like_baiwang_wrapper_text(text):
    compact = compact_text(text)
    if not compact:
        return False
    marker_hits = sum(1 for marker in BAIWANG_WRAPPER_MARKERS if compact_text(marker) in compact)
    return marker_hits >= 2 and not has_structured_invoice_anchor(text)


def has_structured_invoice_anchor(text):
    compact = compact_text(text)
    if not compact:
        return False
    return any(compact_text(marker) in compact for marker in BAIWANG_STRUCTURED_MARKERS)


def infer_baiwang_download_kind(url="", content_type="", content_disposition="", filename=""):
    combined = " ".join(
        [
            str(url or "").lower(),
            str(content_type or "").lower(),
            str(content_disposition or "").lower(),
            str(filename or "").lower(),
        ]
    )
    if (
        "application/pdf" in combined
        or "text/pdf" in combined
        or ".pdf" in combined
        or "wjgs=pdf" in combined
        or "formattype=pdf" in combined
    ):
        return "pdf"
    if "xml" in combined or "wjgs=xml" in combined or "formattype=xml" in combined:
        return "xml"
    if "ofd" in combined or "wjgs=ofd" in combined or "formattype=ofd" in combined:
        return "ofd"
    return ""


def extract_fields_from_pdf_text(text):
    content = str(text or "")
    if not content:
        return {}

    result = {
        "seller": "",
        "purchaser": "",
        "amount": "",
        "invoice_date": "",
        "invoice_number": "",
        "invoice_code": "",
    }

    patterns = [
        ("invoice_number", r"发票号码[:：]?\s*([0-9]{8,})"),
        ("invoice_code", r"发票代码[:：]?\s*([0-9]{8,})"),
        ("invoice_date", r"开票日期[:：]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})"),
        ("purchaser", r"购买方名称[:：]?\s*([^\s]+)"),
        ("seller", r"销售方名称[:：]?\s*([^\s]+)"),
    ]
    for key, pattern in patterns:
        match = re.search(pattern, content)
        if not match:
            continue
        if key == "invoice_date":
            result[key] = normalize_date(match.group(1))
        else:
            result[key] = normalize_token(match.group(1))

    amount_patterns = [
        r"价税合计(?:（小写）|\(小写\))?[:：]?\s*[¥￥]?\s*([0-9]+\.[0-9]{2})",
        r"发票金额[:：]?\s*[¥￥]?\s*([0-9]+\.[0-9]{2})",
    ]
    for pattern in amount_patterns:
        match = re.search(pattern, content)
        if match:
            result["amount"] = normalize_amount(match.group(1))
            break
    if not result["amount"]:
        amounts = re.findall(r"([0-9]+\.[0-9]{2})", content)
        if amounts:
            result["amount"] = normalize_amount(amounts[-1])

    return {key: value for key, value in result.items() if value}


def parse_baiwang_xml_fields(xml_bytes):
    if not xml_bytes:
        return {}

    xml_text = ""
    for encoding in ["utf-8", "utf-8-sig", "gb18030", "latin-1"]:
        try:
            xml_text = xml_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not xml_text:
        return {}

    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return {}

    def first_text(names):
        for name in names:
            for node in root.iter():
                if node.tag.split("}", 1)[-1] != name:
                    continue
                text = normalize_token("".join(node.itertext()))
                if text:
                    return text
        return ""

    return {
        key: value
        for key, value in {
            "seller": first_text(["SellerName"]),
            "purchaser": first_text(["BuyerName"]),
            "amount": normalize_amount(first_text(["TotalTax-includedAmount", "TotaltaxIncludedAmount"])),
            "invoice_date": normalize_date(first_text(["IssueTime", "IssueDate", "RequestTime"])),
            "invoice_code": first_text(["InvoiceCode"]),
            "invoice_number": first_text(["InvoiceNumber", "EIid"]),
        }.items()
        if value
    }


def match_baiwang_expected_fields(expected_fields, actual_fields):
    expected = dict(expected_fields or {})
    actual = dict(actual_fields or {})
    if not any(str(value or "").strip() for value in expected.values()):
        return True, "no_expected_fields"

    expected_number = str(expected.get("invoice_number") or "").strip()
    actual_number = str(actual.get("invoice_number") or "").strip()
    if expected_number:
        return expected_number == actual_number and bool(actual_number), "invoice_number"

    expected_seller = compact_text(expected.get("seller"))
    expected_amount = normalize_amount(expected.get("amount"))
    expected_date = normalize_date(expected.get("invoice_date"))

    actual_seller = compact_text(actual.get("seller"))
    actual_amount = normalize_amount(actual.get("amount"))
    actual_date = normalize_date(actual.get("invoice_date"))

    seller_match = not expected_seller or expected_seller in actual_seller or actual_seller in expected_seller
    amount_match = not expected_amount or expected_amount == actual_amount
    date_match = not expected_date or expected_date == actual_date
    return seller_match and amount_match and date_match, "seller_amount_date"
