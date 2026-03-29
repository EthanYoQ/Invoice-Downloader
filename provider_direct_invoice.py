import re
import xml.etree.ElementTree as ET
from urllib.parse import parse_qsl, urlparse


DIRECT_INVOICE_FAMILIES = (
    "chinatax_direct_invoice",
    "bwjf_signed_invoice",
)


def _compact_text(value):
    return re.sub(r"\s+", "", str(value or "")).strip().lower()


def normalize_token(value):
    return re.sub(r"\s+", "", str(value or "")).strip()


def normalize_amount(value):
    match = re.search(r"(\d+(?:\.\d{1,2})?)", str(value or ""))
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
        compact = re.sub(r"\D", "", text)
        if len(compact) >= 8 and compact[:4].startswith("20"):
            return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
        return ""
    return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"


def infer_direct_invoice_family(url):
    parsed = urlparse(str(url or "").strip())
    host = (parsed.hostname or "").lower()
    path = (parsed.path or "").lower()
    if host == "dppt.beijing.chinatax.gov.cn" and "/kpfw/fpjfzz/v1/exportdzfpwjewm" in path:
        return "chinatax_direct_invoice"
    if host == "fp.bwjf.cn" and (path.startswith("/u/") or path.startswith("/downsigninvoice")):
        return "bwjf_signed_invoice"
    return ""


def is_direct_invoice_family_url(url):
    return bool(infer_direct_invoice_family(url))


def collect_direct_invoice_candidate_urls(primary_url, extra_urls=None):
    ordered = []
    seen = set()
    for candidate in [primary_url, *(extra_urls or [])]:
        normalized = str(candidate or "").strip()
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(normalized)
    return ordered


def _query_dict(url):
    return {
        key: value
        for key, value in parse_qsl(urlparse(str(url or "").strip()).query, keep_blank_values=True)
        if key
    }


def extract_direct_invoice_email_fields(body_text="", *, url="", subject=""):
    body = re.sub(r"\s+", " ", str(body_text or "")).strip()
    subject = str(subject or "").strip()
    query = _query_dict(url)
    result = {
        "invoice_number": "",
        "seller": "",
        "invoice_date": "",
        "preferred_kind": "pdf",
    }

    for key in ("Fphm", "fphm"):
        if query.get(key):
            result["invoice_number"] = str(query[key]).strip()
            break
    if not result["invoice_number"]:
        subject_match = re.search(r"发票(?:号码|号碼|号):?\s*([0-9]{8,20})", subject)
        if subject_match:
            result["invoice_number"] = subject_match.group(1)
    if not result["invoice_number"]:
        all_text = f"{subject} {body}"
        candidates = re.findall(r"(?<!\d)(\d{20})(?!\d)", all_text)
        if candidates:
            result["invoice_number"] = candidates[0]

    seller_name = query.get("sellerName") or query.get("sellername") or ""
    if seller_name:
        result["seller"] = normalize_token(seller_name)
    if not result["seller"]:
        for pattern in (
            r"您收到一张【(.+?)】开具的发票",
            r"您收到来自(.+?)的电子发票",
            r"来自【(.+?)】开具的发票",
        ):
            match = re.search(pattern, subject)
            if match:
                result["seller"] = normalize_token(match.group(1))
                break

    for key in ("Kprq", "kprq"):
        if query.get(key):
            result["invoice_date"] = normalize_date(query[key])
            break
    if not result["invoice_date"]:
        for pattern in (
            r"(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})",
            r"(20\d{2}\d{2}\d{2})",
        ):
            match = re.search(pattern, body) or re.search(pattern, subject)
            if match:
                result["invoice_date"] = normalize_date(match.group(1))
                break

    preferred_kind = (query.get("Wjgs") or query.get("wjgs") or query.get("jflx") or "").strip().lower()
    if preferred_kind in {"pdf", "xml", "ofd"}:
        result["preferred_kind"] = preferred_kind

    return {key: value for key, value in result.items() if value}


def build_direct_invoice_group_key(*, family="", email_id="", expected_fields=None, candidate_urls=None):
    family = str(family or "").strip() or "direct_invoice"
    expected_fields = dict(expected_fields or {})
    invoice_number = str(expected_fields.get("invoice_number") or "").strip()
    seller = normalize_token(expected_fields.get("seller") or "")
    if email_id and invoice_number:
        return f"{family}:{email_id}:{invoice_number}"
    if email_id and seller:
        return f"{family}:{email_id}:{seller}"
    if email_id:
        return f"{family}:{email_id}"
    urls = collect_direct_invoice_candidate_urls("", candidate_urls or [])
    if invoice_number:
        return f"{family}:{invoice_number}"
    if urls:
        return f"{family}:{'|'.join(sorted(urls))}"
    return f"{family}:unknown"


def infer_direct_download_kind(url="", content_type="", content_disposition="", filename=""):
    combined = " ".join(
        [
            str(url or "").lower(),
            str(content_type or "").lower(),
            str(content_disposition or "").lower(),
            str(filename or "").lower(),
        ]
    )
    if "application/pdf" in combined or ".pdf" in combined or "wjgs=pdf" in combined or "jflx=pdf" in combined:
        return "pdf"
    if "xml" in combined or "wjgs=xml" in combined or "jflx=xml" in combined:
        return "xml"
    if "ofd" in combined or "wjgs=ofd" in combined or "jflx=ofd" in combined:
        return "ofd"
    return ""


def _xml_first_text(root, names):
    normalized_names = {name.lower() for name in names}
    for node in root.iter():
        local_name = node.tag.split("}")[-1].lower()
        if local_name in normalized_names and (node.text or "").strip():
            return node.text.strip()
    return ""


def parse_direct_invoice_xml_fields(xml_bytes):
    if not xml_bytes:
        return {}

    xml_text = ""
    for encoding in ("utf-8", "utf-8-sig", "gb18030", "latin-1"):
        try:
            xml_text = xml_bytes.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if not xml_text:
        return {}

    root = None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        root = None

    result = {
        "invoice_number": "",
        "invoice_code": "",
        "seller": "",
        "purchaser": "",
        "amount": "",
        "invoice_date": "",
    }

    if root is not None:
        result["invoice_number"] = normalize_token(
            _xml_first_text(root, {"fphm", "invoice_no", "invoiceno", "invoice_number"})
        )
        result["invoice_code"] = normalize_token(
            _xml_first_text(root, {"fpdm", "invoice_code", "invoicecode"})
        )
        result["seller"] = normalize_token(
            _xml_first_text(root, {"xfmc", "sellername", "seller_name", "seller"})
        )
        result["purchaser"] = normalize_token(
            _xml_first_text(root, {"gmfmc", "buyername", "buyer_name", "purchaser"})
        )
        result["amount"] = normalize_amount(
            _xml_first_text(root, {"jshj", "amount", "total_amount", "价税合计"})
        )
        result["invoice_date"] = normalize_date(
            _xml_first_text(root, {"kprq", "invoice_date", "issuedate", "issue_date"})
        )

    if not result["invoice_number"]:
        match = re.search(r"(?<!\d)(\d{20})(?!\d)", xml_text)
        if match:
            result["invoice_number"] = match.group(1)
    if not result["seller"]:
        for pattern in (r"<xfmc>(.*?)</xfmc>", r"<sellerName>(.*?)</sellerName>"):
            match = re.search(pattern, xml_text, flags=re.IGNORECASE | re.DOTALL)
            if match:
                result["seller"] = normalize_token(match.group(1))
                break
    if not result["amount"]:
        amounts = re.findall(r"([0-9]+\.[0-9]{2})", xml_text)
        if amounts:
            result["amount"] = normalize_amount(max(amounts, key=lambda value: float(value)))
    if not result["invoice_date"]:
        date_match = re.search(r"(20\d{2}[-/.年]?\d{1,2}[-/.月]?\d{1,2})", xml_text)
        if date_match:
            result["invoice_date"] = normalize_date(date_match.group(1))

    return {key: value for key, value in result.items() if value}


def extract_direct_invoice_fields_from_pdf_text(text):
    content = str(text or "")
    if not content:
        return {}

    result = {
        "invoice_number": "",
        "invoice_code": "",
        "seller": "",
        "purchaser": "",
        "amount": "",
        "invoice_date": "",
    }

    patterns = [
        ("invoice_number", r"发票号码[:：]?\s*([0-9]{8,20})"),
        ("invoice_code", r"发票代码[:：]?\s*([0-9]{8,20})"),
        ("invoice_date", r"开票日期[:：]?\s*(20\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2})"),
        ("purchaser", r"购买方名称[:：]?\s*([^\s]+)"),
        ("seller", r"销售方名称[:：]?\s*([^\s]+)"),
    ]
    for key, pattern in patterns:
        match = re.search(pattern, content)
        if not match:
            continue
        value = match.group(1)
        if key == "invoice_date":
            result[key] = normalize_date(value)
        else:
            result[key] = normalize_token(value)

    amount_patterns = [
        r"价税合计(?:\([^)]+\))?[:：]?\s*[¥￥]?\s*([0-9]+\.[0-9]{2})",
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
            result["amount"] = normalize_amount(max(amounts, key=lambda value: float(value)))

    return {key: value for key, value in result.items() if value}
