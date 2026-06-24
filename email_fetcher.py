import imaplib
import email
import email.utils
from email.header import decode_header
import json
import logging
import os
import re
import datetime
import time
import zipfile
from urllib.parse import parse_qsl, urljoin, urlparse, unquote
from bs4 import BeautifulSoup
from PIL import Image
from io import BytesIO
from email_body_receipts import (
    build_email_body_receipt_filename,
    parse_email_body_receipt_fields,
    render_email_body_receipt_pdf_bytes,
)
from provider_baiwang import (
    build_baiwang_group_key,
    collect_baiwang_candidate_urls,
    extract_baiwang_email_fields as extract_baiwang_email_fields_helper,
    infer_baiwang_download_kind,
    is_baiwang_family_url,
    merge_expected_fields,
)
from provider_direct_invoice import (
    DIRECT_INVOICE_FAMILIES,
    build_direct_invoice_group_key,
    collect_direct_invoice_candidate_urls,
    extract_direct_invoice_email_fields,
    infer_direct_invoice_family,
    is_direct_invoice_family_url,
)
try:
    from pyzbar.pyzbar import decode
except ImportError:
    decode = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    LOCAL_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 四层漏斗过滤进制 - 硬编码常量
TIER1_DOMAINS = ['@12306.cn', '@rails.com.cn', '@didichuxing.com', '@gaode.com', '@marriott.com', '@hworld.com', '@cits.com', '@meituan.com', '@carlsonwagonlit.com', '@mycwt.com', '@citsgbt.com']
KEYWORDS_SUBJECT = ['发票', '行程单', '账单', 'receipt', 'invoice']
KEYWORDS_BODY = ['发票', '报销', '行程', '差旅']
VALID_EXTENSIONS = ['.pdf', '.jpg', '.jpeg', '.png']
INLINE_IMAGE_EXTENSIONS = ['.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp']
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024
SMALL_IMAGE_BYTES = 50 * 1024
TINY_IMAGE_BYTES = 10 * 1024
TRACKING_PIXEL_BYTES = 4 * 1024
TINY_IMAGE_DIMENSION = 32
TRACKING_PIXEL_DIMENSION = 2
DECORATIVE_NAME_KEYWORDS = [
    'logo', 'banner', 'icon', 'footer', 'header', 'signature', 'avatar',
    'wechat', 'weixin', 'qrcode_logo', 'thumb', 'sprite',
]
MARKETING_LINK_KEYWORDS = [
    'unsubscribe', 'optout', 'preference', 'privacy', 'track', 'tracking',
    'pixel', 'open', 'facebook', 'instagram', 'linkedin', 'twitter', 'weibo',
    'youtube', 'banner', 'logo',
]
DOCUMENT_LINK_HINTS = [
    '发票', '下载', 'pdf', '行程单', '账单', 'receipt', 'download', 'click',
    '点击查看', '获取', '打开链接', 'invoice', 'fapiao', 'ofd', 'xml',
    'chinatax', 'baiwang', 'nuonuo',
]
INVOICEISH_ATTACHMENT_KEYWORDS = (
    "invoice",
    "receipt",
    "fapiao",
    "发票",
    "账单",
    "行程单",
    "水单",
    "tax",
    "ofd",
    "xml",
)


def _contains_keyword_casefold(text, keywords):
    normalized = str(text or "").casefold()
    return any(str(keyword or "").casefold() in normalized for keyword in keywords)


def _classify_email_tier(sender, subject, body_text):
    _, sender_addr = email.utils.parseaddr(str(sender or ""))
    sender_domain = sender_addr.split("@")[-1].lower() if "@" in sender_addr else ""
    sender_domain = f"@{sender_domain}" if sender_domain else ""

    if any(domain in sender_domain for domain in TIER1_DOMAINS):
        return 1
    if _contains_keyword_casefold(subject, KEYWORDS_SUBJECT):
        return 2
    if _contains_keyword_casefold(body_text, KEYWORDS_BODY):
        return 3
    return 4
BAIWANG_STRONG_URL_TOKENS = (
    "downloadpdf",
    "downloadofd",
    "downloadxml",
    "previewinvoice",
    "previewinvoiceall",
    "wjgs=pdf",
    "wjgs=ofd",
    "wjgs=xml",
    "pdfurl=",
)
HTTP_URL_SCHEMES = {'http', 'https'}
KNOWN_BWJF_NAVIGATION_HOST = 'www.bwjf.cn'
KNOWN_BWJF_NAVIGATION_PATHS = {'/', '/productIntroduction', '/resourceCenter'}
KNOWN_APPLE_UTILITY_EXACT_PATHS = {
    ('account.apple.com', '/choose-your-country/'),
    ('www.apple.com', '/legal/internet-services/icloud/ww/'),
    ('www.apple.com', '/legal/privacy/szh/'),
    ('www.apple.com', '/support/icloud/ww'),
    ('www.icloud.com.cn', '/find'),
}
KNOWN_MEDIA_MARKETING_PATTERNS = {
    ('film.qq.com', '/act/jump.html'),
    ('v.qq.com', '/x/cover/'),
}
KNOWN_NUONUO_HOME_RULES = {
    ('www.nuonuo.com', '/nuonuo/web/aboutone/index/index.html', ''): 'A_KNOWN_NUONUO_PRODUCT_HOME_PAGE',
    ('fp.nuonuo.com', '/', '/'): 'A_KNOWN_NUONUO_PRODUCT_HOME_PAGE',
    ('ntf.nuonuo.com', '/', '/home'): 'A_KNOWN_NUONUO_PRODUCT_HOME_PAGE',
    ('bmjc.nuonuo.com', '/Contents/smartCode/web/index.html', '/index'): 'A_KNOWN_NUONUO_PRODUCT_HOME_PAGE',
    ('nst.nuonuo.com', '/', '/'): 'A_KNOWN_NUONUO_PRODUCT_HOME_PAGE',
    ('baoxiao.nuonuo.com', '/', ''): 'A_KNOWN_NUONUO_PRODUCT_HOME_PAGE',
}
KNOWN_URL_NOISE_EXACT_RULES = {
    ('support.apple.com', '/HT207594'): 'A_KNOWN_APPLE_UTILITY_PAGE',
    ('apple.com', '/support/icloud/ww'): 'A_KNOWN_APPLE_UTILITY_PAGE',
    ('www.apple.com', '/cn/privacy/'): 'A_KNOWN_APPLE_UTILITY_PAGE',
    ('accounts.google.com', '/AccountDisavow'): 'A_KNOWN_ACCOUNT_UTILITY_PAGE',
    ('myaccount.google.com', '/notifications'): 'A_KNOWN_ACCOUNT_UTILITY_PAGE',
    ('wx.mail.qq.com', '/list/readtemplate'): 'A_KNOWN_QQMAIL_PROXY_PAGE',
    ('wow.liepin.com', '/t1009287/index.html'): 'A_KNOWN_REDIRECT_NOISE_PAGE',
    ('support.battlenet.com.cn', '/'): 'A_KNOWN_GAME_HELP_LEGAL_PAGE',
    ('store.steampowered.com', '/'): 'A_KNOWN_GAME_HELP_LEGAL_PAGE',
    ('www.steampowered.com', '/getsteam'): 'A_KNOWN_GAME_HELP_LEGAL_PAGE',
}
KNOWN_URL_NOISE_PREFIX_RULES = {
    'click.mail.all.com': (
        ('path', '/u/', 'A_KNOWN_REDIRECT_NOISE_PAGE'),
    ),
    'store.steampowered.com': (
        ('normalized_path', '/account/', 'A_KNOWN_GAME_HELP_LEGAL_PAGE'),
        ('normalized_path', '/points/', 'A_KNOWN_GAME_HELP_LEGAL_PAGE'),
    ),
}
SHADOW_NOISE_PATH_PATTERNS = (
    '/account/',
    '/support/',
    '/help/',
    '/legal/',
    '/privacy/',
    '/tos',
    '/find',
    '/transactions/',
    '/act/jump.html',
    '/x/cover/',
    '/w/article/',
)
DEFINITE_NOISE_HOST_RULES = {
    "www.dmit.io": "B_DEFINITE_NOISE_DMIT_ACCOUNT_PAGE",
    "bandwagonhost.com": "B_DEFINITE_NOISE_BANDWAGON_ACCOUNT_PAGE",
    "bwhstatus.com": "B_DEFINITE_NOISE_BANDWAGON_STATUS_PAGE",
    "ping.pe": "B_DEFINITE_NOISE_BANDWAGON_STATUS_PAGE",
    "port.ping.pe": "B_DEFINITE_NOISE_BANDWAGON_STATUS_PAGE",
    "www.linkedin.com": "B_DEFINITE_NOISE_SOCIAL_PAGE",
    "facebook.com": "B_DEFINITE_NOISE_SOCIAL_PAGE",
    "instagram.com": "B_DEFINITE_NOISE_SOCIAL_PAGE",
    "twitter.com": "B_DEFINITE_NOISE_SOCIAL_PAGE",
    "x.com": "B_DEFINITE_NOISE_SOCIAL_PAGE",
    "mastodon.social": "B_DEFINITE_NOISE_SOCIAL_PAGE",
    "reddit.com": "B_DEFINITE_NOISE_SOCIAL_PAGE",
    "help.apple.com": "B_DEFINITE_NOISE_APPLE_UTILITY_PAGE",
    "www.securitytools.net": "B_DEFINITE_NOISE_SECURITY_UTILITY_PAGE",
    "account.proton.me": "B_DEFINITE_NOISE_PROTON_ACCOUNT_PAGE",
    "mail.proton.me": "B_DEFINITE_NOISE_PROTON_ACCOUNT_PAGE",
    "calendar.proton.me": "B_DEFINITE_NOISE_PROTON_ACCOUNT_PAGE",
    "wallet.proton.me": "B_DEFINITE_NOISE_PROTON_ACCOUNT_PAGE",
    "account.protonvpn.com": "B_DEFINITE_NOISE_PROTON_ACCOUNT_PAGE",
    "protonvpn.zendesk.com": "B_DEFINITE_NOISE_PROTON_SUPPORT_PAGE",
}
DEFINITE_NOISE_HOST_SUFFIX_RULES = {
    ".awstrack.me": "B_DEFINITE_NOISE_TRACKING_HOST",
}
DEFINITE_NOISE_EXACT_URL_RULES = {
    "https://t.me/bwhofficial2": "B_DEFINITE_NOISE_BANDWAGON_STATUS_PAGE",
    "http://3.cn/2-dc3zvv": "B_DEFINITE_NOISE_JD_SURVEY_REDIRECT",
}
DEFINITE_NOISE_HOST_PATH_PREFIX_RULES = {
    "bwh81.net": (
        ("/ipchange.php", "B_DEFINITE_NOISE_BANDWAGON_STATUS_PAGE"),
    ),
    "proton.me": (
        ("/blog", "B_DEFINITE_NOISE_PROTON_BLOG_PAGE"),
        ("/legal/", "B_DEFINITE_NOISE_PROTON_LEGAL_PAGE"),
        ("/support/", "B_DEFINITE_NOISE_PROTON_SUPPORT_PAGE"),
    ),
    "protonvpn.com": (
        ("/blog", "B_DEFINITE_NOISE_PROTON_BLOG_PAGE"),
        ("/support", "B_DEFINITE_NOISE_PROTON_SUPPORT_PAGE"),
        ("/support-form", "B_DEFINITE_NOISE_PROTON_SUPPORT_PAGE"),
    ),
    "tr.jd.com": (
        ("/jump/transfer", "B_DEFINITE_NOISE_JD_SURVEY_REDIRECT"),
    ),
    "i-mkt.jd.com": (
        ("/subscribe/index", "B_DEFINITE_NOISE_JD_SUBSCRIPTION_PAGE"),
    ),
}
BILLING_HINT_KEYWORDS = (
    "invoice",
    "receipt",
    "fapiao",
    "发票",
    "账单",
    "bill",
    "payment",
    "付款",
    "支付",
)

EMAIL_FETCH_LOOP_PAUSE_SECONDS = 0.05

def decode_str(s):
    """解码邮件头的字符串"""
    if not s:
        return ""
    decoded_parts = decode_header(s)
    result = ""
    for content, charset in decoded_parts:
        if isinstance(content, bytes):
            charset = charset or 'utf-8'
            try:
                result += content.decode(charset, errors='replace')
            except LookupError:
                result += content.decode('utf-8', errors='replace')
        else:
            result += content
    return result


def infer_link_type(url, anchor_text=""):
    """粗略判断链接对应的票据格式，未知时返回 unknown。"""
    lower_url = url.lower()
    lower_text = anchor_text.lower()

    if "wjgs=pdf" in lower_url or ".pdf" in lower_url or "pdf" in lower_text:
        return "pdf"
    if "wjgs=ofd" in lower_url or ".ofd" in lower_url or "ofd" in lower_text:
        return "ofd"
    if "wjgs=xml" in lower_url or ".xml" in lower_url or "xml" in lower_text:
        return "xml"
    return "unknown"


def build_link_group_key(url):
    """
    为同一张票据的多格式下载链接生成归并键。
    对已知税票链接按核心参数归并；未知链接按 URL 本身保留。
    """
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    path = parsed.path
    query_pairs = parse_qsl(parsed.query, keep_blank_values=True)
    query = dict(query_pairs)

    if any(key.lower() == "wjgs" for key, _ in query_pairs):
        filtered_pairs = [
            (key, value)
            for key, value in query_pairs
            if key.lower() not in {"wjgs", "czsj"}
        ]
        return (
            host,
            path,
            tuple(sorted(filtered_pairs)),
        )

    if "chinatax" in host and "exportdzfpwjewm" in path.lower():
        return (
            host,
            path,
            query.get("Fphm", ""),
            query.get("Kprq", ""),
            query.get("Jym", ""),
        )

    return url.strip()


def _looks_like_invoice_url(url):
    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in HTTP_URL_SCHEMES:
        return False

    lower_url = url.lower()
    return any(token in lower_url for token in ('pdfurl=', 'downsigninvoice', 'fphm=', 'jflx=', 'sign='))


def _extract_nested_invoice_url(url):
    query_pairs = parse_qsl(urlparse(url.strip()).query, keep_blank_values=True)
    for key, value in query_pairs:
        if key.lower() != "pdfurl" or not value:
            continue

        canonical_url = unquote(value).strip()
        if canonical_url and urlparse(canonical_url).scheme.lower() in HTTP_URL_SCHEMES:
            return canonical_url
    return None


def _match_shadow_noise_page_type(url):
    path = urlparse(url.strip()).path.lower()
    for pattern in SHADOW_NOISE_PATH_PATTERNS:
        if pattern in path:
            return True, pattern
    return False, ""


def _expand_bwjf_shortlink(url, timeout=5):
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    path_parts = [part for part in parsed.path.split('/') if part]
    if host != "fp.bwjf.cn" or len(path_parts) != 2 or path_parts[0].lower() != "u" or not path_parts[1]:
        return None

    try:
        import requests
    except ImportError:
        logging.warning("requests unavailable, skipping bwjf shortlink expansion")
        return None

    try:
        response = requests.get(
            url.strip(),
            allow_redirects=False,
            timeout=timeout,
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                )
            },
        )
        response.close()
    except Exception as exc:
        logging.warning(f"Failed to expand bwjf shortlink {url}: {exc}")
        return None

    if response.status_code not in {301, 302, 303, 307, 308}:
        return None

    location = response.headers.get("Location", "").strip()
    if not location:
        return None

    resolved_location = urljoin(url.strip(), location)
    nested_invoice_url = _extract_nested_invoice_url(resolved_location)
    if nested_invoice_url:
        return nested_invoice_url

    if _looks_like_invoice_url(resolved_location):
        return resolved_location

    return None


def normalize_invoice_link_candidate(url):
    stripped_url = url.strip()
    nested_invoice_url = _extract_nested_invoice_url(stripped_url)
    if nested_invoice_url:
        return nested_invoice_url

    expanded_url = _expand_bwjf_shortlink(stripped_url)
    if expanded_url:
        return expanded_url

    return stripped_url


def _is_known_bwjf_navigation_link(url):
    parsed = urlparse(url.strip())
    if parsed.netloc.lower() != KNOWN_BWJF_NAVIGATION_HOST:
        return False

    if _looks_like_invoice_url(url):
        return False

    normalized_path = parsed.path.rstrip('/') or '/'
    return normalized_path in KNOWN_BWJF_NAVIGATION_PATHS


def _should_drop_baiwang_wrapper_url(url, *, sender_addr="", raw_attachment_exts=None):
    parsed = urlparse(url.strip())
    if parsed.netloc.lower() != "bwfp.baiwang.com":
        return False

    local_part, _, domain = sender_addr.lower().partition("@")
    if domain != "vip.baiwang.com" or not local_part.startswith("yun"):
        return False

    ext_sets = list((raw_attachment_exts or {}).values())
    has_pdf = any(".pdf" in ext_set for ext_set in ext_sets)
    has_companion = any(".xml" in ext_set or ".ofd" in ext_set for ext_set in ext_sets)
    return has_pdf and has_companion


def extract_baiwang_email_fields(body_text):
    return extract_baiwang_email_fields_helper(body_text)


def _detect_provider_family(url, *, sender_addr="", subject=""):
    direct_invoice_family = infer_direct_invoice_family(url)
    if direct_invoice_family:
        return direct_invoice_family
    if is_baiwang_family_url(url, sender_addr=sender_addr, subject=subject):
        return "baiwang"
    host = urlparse(url.strip()).netloc.lower()
    sender_addr = sender_addr.lower()
    subject = str(subject or "").lower()
    if "baiwang" in host or host.endswith("efapiao.com") or "baiwang" in sender_addr or "电子发票下载" in subject:
        return "baiwang"
    return ""


def _build_provider_expected_fields(url, *, sender_addr="", subject="", body_text=""):
    provider_family = _detect_provider_family(url, sender_addr=sender_addr, subject=subject)
    if provider_family == "baiwang":
        return provider_family, extract_baiwang_email_fields(body_text)
    if provider_family in DIRECT_INVOICE_FAMILIES:
        return provider_family, extract_direct_invoice_email_fields(body_text, url=url, subject=subject)
    return provider_family, {}


def _merge_provider_expected_fields(*field_maps):
    merged = merge_expected_fields(*field_maps)
    for field_map in field_maps:
        preferred_kind = str(dict(field_map or {}).get("preferred_kind") or "").strip().lower()
        if preferred_kind and "preferred_kind" not in merged:
            merged["preferred_kind"] = preferred_kind
    return {key: value for key, value in merged.items() if value}


def _collect_provider_group_urls(provider_family, candidate_urls):
    if provider_family == "baiwang":
        return collect_baiwang_candidate_urls("", candidate_urls)
    if provider_family in DIRECT_INVOICE_FAMILIES:
        return collect_direct_invoice_candidate_urls("", candidate_urls)
    return [str(candidate or "").strip() for candidate in candidate_urls if str(candidate or "").strip()]


def _build_provider_group_key(provider_family, *, email_id="", candidate_urls=None, expected_fields=None):
    if provider_family == "baiwang":
        return build_baiwang_group_key(email_id=email_id, candidate_urls=candidate_urls)
    if provider_family in DIRECT_INVOICE_FAMILIES:
        return build_direct_invoice_group_key(
            family=provider_family,
            email_id=email_id,
            expected_fields=expected_fields,
            candidate_urls=candidate_urls,
        )
    return ""


def _match_definite_noise_rule(url, anchor_text="", *, sender_addr="", subject=""):
    if is_baiwang_family_url(url, sender_addr=sender_addr, subject=subject):
        return None
    if is_direct_invoice_family_url(url):
        return None

    if _looks_like_invoice_url(url):
        return None

    normalized_url = str(url or "").strip().lower()
    exact_url_reason = DEFINITE_NOISE_EXACT_URL_RULES.get(normalized_url)
    if exact_url_reason:
        return exact_url_reason

    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    normalized_path = (parsed.path or "/").rstrip("/") or "/"
    combined_hint = f"{str(anchor_text or '').lower()} {str(subject or '').lower()}"
    has_billing_hint = any(keyword in combined_hint for keyword in BILLING_HINT_KEYWORDS)
    if has_billing_hint:
        return None

    exact_reason = DEFINITE_NOISE_HOST_RULES.get(host)
    if exact_reason:
        return exact_reason

    for suffix, reason_code in DEFINITE_NOISE_HOST_SUFFIX_RULES.items():
        if host.endswith(suffix):
            return reason_code

    for prefix, reason_code in DEFINITE_NOISE_HOST_PATH_PREFIX_RULES.get(host, ()):
        if normalized_path.startswith(prefix):
            return reason_code

    if host == "protonvpn.com" and normalized_path.startswith("/support/"):
        return "B_DEFINITE_NOISE_PROTON_SUPPORT_PAGE"

    if host == "proton.me" and normalized_path in {"/", "/mail", "/calendar", "/drive"}:
        return "B_DEFINITE_NOISE_PROTON_ACCOUNT_PAGE"

    return None


def _has_link_hint(text, keywords):
    sample = str(text or "").lower()
    return any(keyword in sample for keyword in keywords)


def _combined_link_hint_text(url, anchor_text="", *, sender_addr="", subject="", body_text=""):
    return " ".join([
        str(url or "").lower(),
        str(anchor_text or "").lower(),
        str(sender_addr or "").lower(),
        str(subject or "").lower(),
        str(body_text or "").lower(),
    ])


def _augment_link_decision(decision, url, anchor_text, *, sender_addr="", subject="", body_text=""):
    provider_family, provider_expected_fields = _build_provider_expected_fields(
        url,
        sender_addr=sender_addr,
        subject=subject,
        body_text=body_text,
    )
    parsed = urlparse(url.strip())
    decision.update({
        "source_url": url.strip(),
        "anchor_text": str(anchor_text or "").strip(),
        "url_host": parsed.netloc.lower(),
        "url_path": parsed.path or "/",
        "provider_family": provider_family,
        "provider_expected_fields": provider_expected_fields,
    })
    return decision


def _match_known_noise_rule(url):
    parsed = urlparse(url.strip())
    host = parsed.netloc.lower()
    path = parsed.path or '/'
    normalized_path = path.rstrip('/') or '/'
    fragment = (parsed.fragment or '').strip()
    normalized_fragment = f"/{fragment.lstrip('/')}" if fragment else ''

    if _looks_like_invoice_url(url):
        return None

    if host == 'local-airchina.iemailforce.com' and normalized_path.startswith('/x/c'):
        return 'A_KNOWN_REDIRECT_NOISE_PAGE'

    if host == 'jump.liepin.com' and normalized_path == '/pc/mailclick':
        return 'A_KNOWN_REDIRECT_NOISE_PAGE'

    if (host, path) in KNOWN_APPLE_UTILITY_EXACT_PATHS:
        return 'A_KNOWN_APPLE_UTILITY_PAGE'

    if host == 'fmipmail.icloud.com.cn' and normalized_path.startswith('/fmipservice/mail/fmip'):
        return 'A_KNOWN_APPLE_UTILITY_PAGE'

    for rule_host, rule_path in KNOWN_MEDIA_MARKETING_PATTERNS:
        if host != rule_host:
            continue
        if rule_path.endswith('/') and normalized_path.startswith(rule_path.rstrip('/')):
            return 'A_KNOWN_MEDIA_MARKETING_PAGE'
        if normalized_path == rule_path:
            return 'A_KNOWN_MEDIA_MARKETING_PAGE'

    if host == 'help.steampowered.com':
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    if host == 'store.steampowered.com' and (
        normalized_path.startswith('/steam_refunds') or normalized_path.startswith('/about')
    ):
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    if host == 'www.valvesoftware.com' and normalized_path == '/en':
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    if host == 'battlenet.com.cn' and normalized_path == '/':
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    if host == 'support.battlenet.com.cn' and normalized_path.startswith('/w/article'):
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    if host == 'account.battlenet.com.cn' and normalized_path.startswith('/transactions'):
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    if host == 'legal.battlenet.com.cn' and normalized_path in {'/tos', '/privacy'}:
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    if host == 'shop.battlenet.com.cn' and normalized_path == '/':
        return 'A_KNOWN_GAME_HELP_LEGAL_PAGE'

    nuonuo_reason = KNOWN_NUONUO_HOME_RULES.get((host, path, normalized_fragment))
    if nuonuo_reason:
        return nuonuo_reason

    explicit_reason = KNOWN_URL_NOISE_EXACT_RULES.get((host, path))
    if explicit_reason:
        return explicit_reason

    for prefix_kind, prefix_value, reason_code in KNOWN_URL_NOISE_PREFIX_RULES.get(host, ()):
        candidate_path = path if prefix_kind == 'path' else normalized_path
        if candidate_path.startswith(prefix_value):
            return reason_code

    return None


def prioritize_invoice_links(link_candidates):
    """
    仅去除精确重复链接，保留同组多格式候选，避免多链接被压成单候选。
    """
    seen = set()
    selected = []
    for url, anchor_text in link_candidates:
        dedupe_key = (url.strip(), anchor_text.strip().lower())
        if dedupe_key in seen:
            continue
        seen.add(dedupe_key)
        selected.append((url, anchor_text))
    return selected


def _normalize_prefilter_signals(strong_signals, weak_signals, extreme_signal=None):
    strong = sorted(set(strong_signals))
    weak = sorted(set(weak_signals))
    return {
        "extreme_negative_signal": extreme_signal,
        "strong_negative_signals": strong,
        "weak_negative_signals": weak,
    }


def _make_candidate_decision(bucket, action, reason_code, source_kind, strong_signals=None, weak_signals=None, extreme_signal=None, **extra):
    decision = {
        "candidate_bucket": bucket,
        "candidate_action": action,
        "prefilter_reason_code": reason_code,
        "source_kind": source_kind,
        "prefilter_signals": _normalize_prefilter_signals(strong_signals or [], weak_signals or [], extreme_signal),
    }
    decision.update(extra)
    return decision


def _apply_shadow_noise_metadata(decision, shadow_noise_page_type, shadow_noise_reason):
    prefilter_signals = dict(decision.get("prefilter_signals", {}))
    prefilter_signals["shadow_noise_page_type"] = bool(shadow_noise_page_type)
    prefilter_signals["shadow_noise_reason"] = shadow_noise_reason or ""
    decision["prefilter_signals"] = prefilter_signals
    decision["shadow_noise_page_type"] = bool(shadow_noise_page_type)
    decision["shadow_noise_reason"] = shadow_noise_reason or ""
    return decision


def _detect_image_characteristics(payload):
    try:
        img = Image.open(BytesIO(payload))
        width, height = img.size
        return {"width": width, "height": height}
    except Exception:
        return None


def _has_qr_code(payload):
    if decode is None:
        return None

    try:
        img = Image.open(BytesIO(payload))
        decoded_objs = decode(img)
        return bool(decoded_objs)
    except Exception:
        return None


def _is_decorative_filename(filename):
    lower_name = os.path.basename(filename).lower()
    return any(keyword in lower_name for keyword in DECORATIVE_NAME_KEYWORDS)


def _has_invoiceish_attachment_name(filename):
    lower_name = os.path.basename(str(filename or "")).lower()
    return any(keyword in lower_name for keyword in INVOICEISH_ATTACHMENT_KEYWORDS)


def _has_strong_baiwang_provider_signal(url, anchor_text="", *, sender_addr="", subject="", body_text=""):
    lower_url = str(url or "").lower()
    lower_anchor = str(anchor_text or "").lower()
    lower_sender = str(sender_addr or "").lower()
    lower_subject = str(subject or "").lower()
    lower_body = str(body_text or "").lower()

    if infer_baiwang_download_kind(url=url):
        return True

    if any(token in lower_url for token in BAIWANG_STRONG_URL_TOKENS):
        return True

    combined = " ".join([lower_anchor, lower_subject, lower_body])
    if any(token in combined for token in ("发票", "下载", "invoice", "download", "pdf", "ofd", "xml")):
        return any(marker in lower_url for marker in ("previewinvoice", "downloadpdf", "downloadxml", "downloadofd", "wjgs=pdf", "wjgs=ofd", "wjgs=xml", "pdfurl="))

    return "baiwang" in lower_sender and any(token in lower_url for token in ("previewinvoice", "downloadpdf", "downloadxml", "downloadofd"))


def _build_attachment_candidate_decision(filename, payload, *, tier, content_type="", content_disposition="", zip_context=None):
    ext = os.path.splitext(filename)[1].lower()
    size_bytes = len(payload) if payload else 0
    is_image = ext in INLINE_IMAGE_EXTENSIONS
    is_inline = "inline" in (content_disposition or "").lower() and content_type.startswith("image/")
    strong_signals = []
    weak_signals = []
    extreme_signal = None
    image_meta = _detect_image_characteristics(payload) if payload and is_image else None

    if is_inline:
        strong_signals.append("inline_image")
    if is_image and size_bytes < SMALL_IMAGE_BYTES:
        weak_signals.append("small_image_under_50kb")
    if is_image and size_bytes < TINY_IMAGE_BYTES:
        strong_signals.append("tiny_image_under_10kb")
    if _is_decorative_filename(filename):
        strong_signals.append("decorative_filename")
    if image_meta:
        width = image_meta["width"]
        height = image_meta["height"]
        if width <= TRACKING_PIXEL_DIMENSION and height <= TRACKING_PIXEL_DIMENSION and size_bytes <= TRACKING_PIXEL_BYTES:
            extreme_signal = "tracking_pixel"
        elif width <= TINY_IMAGE_DIMENSION and height <= TINY_IMAGE_DIMENSION:
            strong_signals.append("tiny_image_dimensions")

    if extreme_signal or len(set(strong_signals)) >= 2:
        reason_code = "A_EXTREME_NEGATIVE_SIGNAL" if extreme_signal else "A_TWO_STRONG_NEGATIVE_SIGNALS"
        return _make_candidate_decision(
            "A",
            "drop",
            reason_code,
            zip_context or "attachment",
            strong_signals=strong_signals,
            weak_signals=weak_signals,
            extreme_signal=extreme_signal,
            image_meta=image_meta,
        )

    if size_bytes > MAX_ATTACHMENT_BYTES:
        return _make_candidate_decision(
            "B",
            "retain_only",
            "B_ATTACHMENT_OVER_5MB_RETAIN",
            zip_context or "attachment",
            strong_signals=strong_signals,
            weak_signals=weak_signals + ["attachment_over_5mb"],
            image_meta=image_meta,
        )

    if zip_context == "zip_container_failed":
        return _make_candidate_decision(
            "B",
            "retain_only",
            "B_ZIP_UNPACK_FAILED_RETAIN",
            zip_context,
            strong_signals=strong_signals,
            weak_signals=weak_signals + ["zip_unpack_failed"],
            image_meta=image_meta,
        )

    if zip_context == "zip_container_filtered":
        return _make_candidate_decision(
            "B",
            "retain_only",
            "B_ZIP_FILTERED_TO_OUTER_CONTAINER",
            zip_context,
            strong_signals=strong_signals,
            weak_signals=weak_signals + ["zip_members_filtered"],
            image_meta=image_meta,
        )

    if is_image and tier == 4:
        has_qr = _has_qr_code(payload)
        weak_with_qr = list(weak_signals)
        if has_qr is False:
            weak_with_qr.append("missing_qr")
            if not _has_invoiceish_attachment_name(filename):
                return _make_candidate_decision(
                    "B",
                    "retain_only",
                    "B_TIER4_IMAGE_NO_QR_RETAIN",
                    zip_context or "attachment",
                    strong_signals=strong_signals,
                    weak_signals=weak_with_qr,
                    image_meta=image_meta,
                )
            return _make_candidate_decision(
                "B",
                "retain_only",
                "B_TIER4_IMAGE_NO_QR_RETAIN",
                zip_context or "attachment",
                strong_signals=strong_signals,
                weak_signals=weak_with_qr,
                image_meta=image_meta,
            )
        if has_qr is None:
            weak_with_qr.append("qr_detection_unavailable")
            if not _has_invoiceish_attachment_name(filename):
                return _make_candidate_decision(
                    "B",
                    "retain_only",
                    "B_TIER4_IMAGE_QR_UNKNOWN_RETAIN",
                    zip_context or "attachment",
                    strong_signals=strong_signals,
                    weak_signals=weak_with_qr,
                    image_meta=image_meta,
                )
            return _make_candidate_decision(
                "B",
                "retain_only",
                "B_TIER4_IMAGE_QR_UNKNOWN_RETAIN",
                zip_context or "attachment",
                strong_signals=strong_signals,
                weak_signals=weak_with_qr,
                image_meta=image_meta,
            )

    if is_image and weak_signals:
        if not _has_invoiceish_attachment_name(filename):
            return _make_candidate_decision(
                "B",
                "retain_only",
                "B_LOW_CONFIDENCE_IMAGE_RETAIN",
                zip_context or "attachment",
                strong_signals=strong_signals,
                weak_signals=weak_signals,
                image_meta=image_meta,
            )
        return _make_candidate_decision(
            "B",
            "retain_only",
            "B_LOW_CONFIDENCE_IMAGE_RETAIN",
            zip_context or "attachment",
            strong_signals=strong_signals,
            weak_signals=weak_signals,
            image_meta=image_meta,
        )

    return _make_candidate_decision(
        "B",
        "main_chain",
        "B_ATTACHMENT_MAIN_CHAIN",
        zip_context or "attachment",
        strong_signals=strong_signals,
        weak_signals=weak_signals,
        image_meta=image_meta,
    )


def _build_link_candidate_decision(url, anchor_text="", *, tier, sender_addr="", subject="", body_text=""):
    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    lower_url = url.lower()
    lower_text = anchor_text.lower()
    shadow_noise_page_type, shadow_noise_reason = _match_shadow_noise_page_type(url)
    strong_signals = []
    weak_signals = []
    extreme_signal = None

    if scheme not in HTTP_URL_SCHEMES:
        extreme_signal = f"non_http_scheme_{scheme or 'unknown'}"

    provider_family, _provider_expected_fields = _build_provider_expected_fields(
        url,
        sender_addr=sender_addr,
        subject=subject,
        body_text=body_text,
    )

    if _is_known_bwjf_navigation_link(url):
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "A",
            "drop",
            "A_KNOWN_BWJF_NAVIGATION_LINK",
            "url",
            strong_signals=["known_bwjf_navigation_link"],
            weak_signals=weak_signals,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    known_noise_rule = _match_known_noise_rule(url)
    if known_noise_rule:
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "A",
            "drop",
            known_noise_rule,
            "url",
            strong_signals=["known_noise_template"],
            weak_signals=weak_signals,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    definite_noise_rule = _match_definite_noise_rule(
        url,
        anchor_text,
        sender_addr=sender_addr,
        subject=subject,
    )
    if definite_noise_rule:
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "B",
            "retain_only",
            definite_noise_rule,
            "url",
            strong_signals=["definite_noise_url_family"],
            weak_signals=weak_signals,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    if any(keyword in lower_url or keyword in lower_text for keyword in MARKETING_LINK_KEYWORDS):
        strong_signals.append("marketing_or_tracking_link")

    path_ext = os.path.splitext(parsed.path)[1].lower()
    if path_ext in {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.css', '.js'}:
        strong_signals.append("decorative_asset_link")

    combined_hint = _combined_link_hint_text(
        url,
        anchor_text,
        sender_addr=sender_addr,
        subject=subject,
        body_text=body_text,
    )
    has_document_hint = _has_link_hint(combined_hint, DOCUMENT_LINK_HINTS)
    has_billing_hint = _has_link_hint(combined_hint, BILLING_HINT_KEYWORDS)
    if not has_document_hint:
        weak_signals.append("missing_link_keywords")
    if not has_billing_hint:
        weak_signals.append("missing_billing_hints")

    if extreme_signal or len(set(strong_signals)) >= 2:
        reason_code = "A_EXTREME_NEGATIVE_SIGNAL" if extreme_signal else "A_TWO_STRONG_NEGATIVE_SIGNALS"
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "A",
            "drop",
            reason_code,
            "url",
            strong_signals=strong_signals,
            weak_signals=weak_signals,
            extreme_signal=extreme_signal,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    if provider_family in DIRECT_INVOICE_FAMILIES:
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "B",
            "main_chain",
            "B_DIRECT_INVOICE_PROVIDER_RECOVERY",
            "url",
            strong_signals=[f"{provider_family}_provider_family"],
            weak_signals=weak_signals,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    if provider_family == "baiwang":
        if _has_strong_baiwang_provider_signal(
            url,
            anchor_text,
            sender_addr=sender_addr,
            subject=subject,
            body_text=body_text,
        ):
            return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
                "B",
                "main_chain",
                "B_BAIWANG_PROVIDER_RECOVERY",
                "url",
                strong_signals=["baiwang_provider_family"],
                weak_signals=weak_signals,
                link_group_key=build_link_group_key(url),
            ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "B",
            "main_chain",
            "B_BAIWANG_PROVIDER_RECOVERY",
            "url",
            strong_signals=["baiwang_provider_family"],
            weak_signals=weak_signals + ["missing_strong_baiwang_download_signal"],
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    if has_document_hint:
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "B",
            "main_chain",
            "B_LINK_DOCUMENT_HINT",
            "url",
            strong_signals=strong_signals,
            weak_signals=weak_signals,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    if has_billing_hint:
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "B",
            "main_chain",
            "B_LINK_BILLING_HINT",
            "url",
            strong_signals=strong_signals,
            weak_signals=weak_signals,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    if tier in {1, 2, 3}:
        return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
            "B",
            "main_chain",
            "B_LINK_TIER_CONFIDENCE",
            "url",
            strong_signals=strong_signals,
            weak_signals=weak_signals,
            link_group_key=build_link_group_key(url),
        ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

    return _augment_link_decision(_apply_shadow_noise_metadata(_make_candidate_decision(
        "B",
        "retain_only",
        "B_NON_PROVIDER_LOW_CONFIDENCE_URL_RETAINED",
        "url",
        strong_signals=strong_signals,
        weak_signals=weak_signals,
        link_group_key=build_link_group_key(url),
    ), shadow_noise_page_type, shadow_noise_reason), url, anchor_text, sender_addr=sender_addr, subject=subject, body_text=body_text)

class EmailFetcher:
    def __init__(self, email_address, auth_code, imap_server="imap.qq.com", imap_port=993, staging_dir="staging", monitoring_dir=None, progress_callback=None):
        self.email_address = email_address
        self.auth_code = auth_code
        self.imap_server = imap_server
        self.imap_port = imap_port
        self.staging_dir = os.path.abspath(staging_dir)
        self.monitoring_dir = os.path.abspath(monitoring_dir) if monitoring_dir else ""
        self.progress_callback = progress_callback
        self.mail = None
        os.makedirs(self.staging_dir, exist_ok=True)

    def _emit_progress(self, message):
        if callable(self.progress_callback):
            try:
                self.progress_callback(str(message or ""))
            except Exception:
                pass

    def _monitoring_path(self, filename):
        if not self.monitoring_dir:
            return ""
        os.makedirs(self.monitoring_dir, exist_ok=True)
        return os.path.join(self.monitoring_dir, filename)

    def _append_jsonl_best_effort(self, path, payload):
        if not path:
            return
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload, ensure_ascii=False, default=str))
                fh.write("\n")
        except Exception as exc:
            logging.warning(f"Failed to append monitoring event {path}: {exc}")

    def _emit_input_inventory_event(self, payload):
        self._append_jsonl_best_effort(
            self._monitoring_path("input_attachment_inventory.jsonl"),
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                **payload,
            },
        )

    def _emit_extract_attachments_diagnostic(self, payload):
        self._append_jsonl_best_effort(
            self._monitoring_path("extract_attachments_diagnostics.jsonl"),
            {
                "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                **payload,
            },
        )

    @staticmethod
    def _attachment_pair_key(filename):
        name = os.path.basename(str(filename or ""))
        stem, _ = os.path.splitext(name)
        return stem.strip()

    @staticmethod
    def _safe_email_id(e_id):
        if isinstance(e_id, bytes):
            return e_id.decode(errors="ignore")
        return str(e_id or "")

    @staticmethod
    def _extract_fetch_bytes(msg_data):
        if not isinstance(msg_data, (list, tuple)):
            return b"", False

        has_tuple_payload = False
        for response_part in msg_data:
            if not isinstance(response_part, tuple):
                continue
            has_tuple_payload = True
            if len(response_part) < 2:
                continue
            candidate = response_part[1]
            if isinstance(candidate, bytearray):
                candidate = bytes(candidate)
            if isinstance(candidate, bytes) and candidate:
                return candidate, has_tuple_payload

        return b"", has_tuple_payload

    @staticmethod
    def _extract_fetch_sequence_id(response_header):
        if isinstance(response_header, bytes):
            header_text = response_header.decode("ascii", errors="ignore")
        else:
            header_text = str(response_header or "")
        match = re.match(r"\s*(\d+)\b", header_text)
        if not match:
            return b""
        return match.group(1).encode("ascii")

    @staticmethod
    def _to_local_naive(dt_value):
        if dt_value is None:
            return None
        if dt_value.tzinfo is not None:
            try:
                dt_value = dt_value.astimezone(LOCAL_TZ)
            except Exception:
                pass
        return dt_value.replace(tzinfo=None)

    @staticmethod
    def _extract_fetch_internaldate(response_header):
        if isinstance(response_header, bytes):
            header_text = response_header.decode("ascii", errors="ignore")
        else:
            header_text = str(response_header or "")
        match = re.search(r'INTERNALDATE\s+"([^"]+)"', header_text, flags=re.IGNORECASE)
        if not match:
            return None
        try:
            internal_dt = email.utils.parsedate_to_datetime(match.group(1))
        except Exception:
            return None
        return EmailFetcher._to_local_naive(internal_dt)

    def _fetch_message_bytes(self, e_id, fetch_command, mode_label):
        attempt = {
            "mode": mode_label,
            "status": "",
            "has_tuple_payload": False,
            "raw_bytes_len": 0,
            "error": "",
        }
        raw_bytes = b""

        try:
            status, msg_data = self.mail.fetch(e_id, fetch_command)
            attempt["status"] = status
            raw_bytes, has_tuple_payload = self._extract_fetch_bytes(msg_data)
            attempt["has_tuple_payload"] = bool(has_tuple_payload)
            attempt["raw_bytes_len"] = len(raw_bytes)
            if status != "OK":
                attempt["error"] = f"fetch_status_{status}"
        except Exception as exc:
            attempt["status"] = "EXCEPTION"
            attempt["error"] = str(exc)

        return raw_bytes, attempt

    def connect(self):
        try:
            logging.info(f"Connecting to IMAP server: {self.imap_server}:{self.imap_port}")
            self.mail = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
            self.mail.login(self.email_address, self.auth_code)
            # 163 邮箱要求登录后发送 RFC 2971 ID 命令
            from email_channel import resolve_channel
            channel = resolve_channel(self.email_address)
            if channel.get("requires_id_cmd"):
                self._send_imap_id_command()
            logging.info("Successfully connected and logged in.")
            return True
        except Exception as e:
            logging.error(f"Failed to connect or log in: {e}")
            return False

    def _send_imap_id_command(self):
        """发送 RFC 2971 ID 命令（163/Netease IMAP 登录后必需）。"""
        try:
            tag = self.mail._new_tag()
            self.mail.send(tag + b' ID ("name" "InvoiceFlowAI" "version" "1.0")\r\n')
            while True:
                line = self.mail.readline()
                if line.startswith(tag):
                    break
        except Exception as e:
            logging.warning(f"RFC 2971 ID command failed (non-fatal): {e}")

    def disconnect(self):
        if self.mail:
            try:
                self.mail.logout()
                logging.info("Logged out from IMAP server.")
            except Exception as e:
                logging.error(f"Error during logout: {e}")

    def fetch_emails_by_date(self, since_date, before_date=None, mailbox="INBOX"):
        if not self.mail:
            logging.error("Not connected to IMAP server.")
            return []

        self.mail.select(mailbox, readonly=True)

        if isinstance(since_date, datetime.date):
            since_date_str = since_date.strftime("%d-%b-%Y")
        else:
            try:
                dt = datetime.datetime.strptime(since_date, "%Y-%m-%d")
                since_date_str = dt.strftime("%d-%b-%Y")
            except:
                since_date_str = since_date
            
        search_criteria_parts = [f'(SINCE "{since_date_str}")']
        
        if before_date:
            if isinstance(before_date, datetime.date):
                before_date_str = before_date.strftime("%d-%b-%Y")
            else:
                try:
                    dt = datetime.datetime.strptime(before_date, "%Y-%m-%d")
                    before_date_str = dt.strftime("%d-%b-%Y")
                except:
                    before_date_str = before_date
            search_criteria_parts.append(f'(BEFORE "{before_date_str}")')

        search_criteria = " ".join(search_criteria_parts)
        logging.info(f"Searching emails with criteria: {search_criteria}")
        self._emit_progress(f"正在 IMAP 搜索：{search_criteria}")
        
        status, messages = self.mail.search(None, search_criteria)
        
        if status != "OK" or not messages[0]:
            logging.info("No emails found or search failed.")
            self._emit_progress("IMAP 搜索完成，命中 0 封邮件。")
            return []

        email_ids = messages[0].split()
        logging.info(f"IMAP returned {len(email_ids)} emails. Applying local Python date filter...")
        self._emit_progress(f"IMAP 搜索完成，命中 {len(email_ids)} 封邮件；正在进行本地日期过滤。")
        
        # 本地时间二次强制过滤 (防御 IMAP SINCE/BEFORE 失效)
        if isinstance(since_date, datetime.date) and not isinstance(since_date, datetime.datetime):
            since_dt = datetime.datetime.combine(since_date, datetime.datetime.min.time())
        elif isinstance(since_date, str):
            since_dt = datetime.datetime.strptime(since_date, "%Y-%m-%d")
        else:
            since_dt = since_date
            
        before_dt = None
        if before_date:
            if isinstance(before_date, datetime.date) and not isinstance(before_date, datetime.datetime):
                before_dt = datetime.datetime.combine(before_date, datetime.datetime.min.time())
            elif isinstance(before_date, str):
                before_dt = datetime.datetime.strptime(before_date, "%Y-%m-%d")
            else:
                before_dt = before_date

        valid_email_ids = []
        total_ids = len(email_ids)
        batch_size = 100
        for batch_start in range(0, total_ids, batch_size):
            chunk = email_ids[batch_start:batch_start + batch_size]
            processed_ids = set()
            sequence_set = b",".join(chunk)
            try:
                status, msg_data = self.mail.fetch(sequence_set, '(INTERNALDATE BODY[HEADER.FIELDS (DATE)])')
                if status != "OK" or not msg_data:
                    valid_email_ids.extend(chunk)
                    logging.warning(f"Failed to batch fetch email dates for local filter: fetch_status_{status}")
                else:
                    fallback_index = 0
                    for response_part in msg_data:
                        if not isinstance(response_part, tuple) or len(response_part) < 2:
                            continue

                        response_id = self._extract_fetch_sequence_id(response_part[0])
                        if not response_id:
                            while fallback_index < len(chunk) and chunk[fallback_index] in processed_ids:
                                fallback_index += 1
                            response_id = chunk[fallback_index] if fallback_index < len(chunk) else b""
                            fallback_index += 1

                        if not response_id:
                            continue

                        processed_ids.add(response_id)
                        dt_naive = None
                        parse_error = None
                        try:
                            msg = email.message_from_bytes(response_part[1])
                            date_str = msg.get("Date")
                            if date_str:
                                try:
                                    dt = email.utils.parsedate_to_datetime(date_str)
                                    dt_naive = self._to_local_naive(dt)
                                except Exception as e:
                                    parse_error = e
                            if dt_naive is None:
                                dt_naive = self._extract_fetch_internaldate(response_part[0])
                            if dt_naive is None:
                                if parse_error:
                                    logging.warning(f"Failed to parse email date for local filter: {parse_error}")
                                valid_email_ids.append(response_id)
                                continue
                            if dt_naive < since_dt:
                                continue
                            if before_dt and dt_naive >= before_dt:
                                continue
                            valid_email_ids.append(response_id)
                        except Exception as e:
                            # 解析失败仍保留，防错杀
                            logging.warning(f"Failed to parse email date for local filter: {e}")
                            valid_email_ids.append(response_id)

                    for e_id in chunk:
                        if e_id not in processed_ids:
                            valid_email_ids.append(e_id)
            except Exception as e:
                logging.warning(f"Failed to batch fetch email dates for local filter: {e}")
                valid_email_ids.extend(chunk)

            processed_count = min(batch_start + len(chunk), total_ids)
            logging.info(
                f"Local date filter progress: {processed_count}/{total_ids}, retained {len(valid_email_ids)} emails."
            )
            self._emit_progress(
                f"正在进行本地日期过滤：{processed_count}/{total_ids}，当前保留 {len(valid_email_ids)} 封邮件。"
            )

        logging.info(f"Local filter completed. {len(valid_email_ids)} emails passed.")
        self._emit_progress(f"本地日期过滤完成，最终保留 {len(valid_email_ids)} 封邮件。")
        return valid_email_ids

    def _legacy_extract_attachments_pre_release_prep(self, email_ids, mailbox="INBOX"):
        """1.3 Extract direct file attachments via 4-tier funnel filtering"""
        if not self.mail:
            return []
            
        self.mail.select(mailbox, readonly=True)
        results = []

        def stage_candidate_file(base_dir, filename, payload):
            os.makedirs(base_dir, exist_ok=True)
            filepath = os.path.join(base_dir, filename)
            counter = 1
            while os.path.exists(filepath):
                name, ext = os.path.splitext(filename)
                filepath = os.path.join(base_dir, f"{name}_{counter}{ext}")
                counter += 1

            with open(filepath, "wb") as f:
                f.write(payload)
            return filepath
        
        chunk_size = 50
        for i in range(0, len(email_ids), chunk_size):
            chunk = email_ids[i:i+chunk_size]
            logging.info(f"Processing chunk {i//chunk_size + 1}/{max(1, (len(email_ids)+chunk_size-1)//chunk_size)}")
            
            for e_id in chunk:
                try:
                    status, msg_data = self.mail.fetch(e_id, '(RFC822)')
                    if status != "OK":
                        continue
                    
                    for response_part in msg_data:
                        if isinstance(response_part, tuple):
                            msg = email.message_from_bytes(response_part[1])
                            subject = decode_str(msg["Subject"])
                            sender = decode_str(msg.get("From", ""))
                            _, sender_addr = email.utils.parseaddr(sender)
                            sender_domain_value = sender_addr.split("@")[-1].lower() if "@" in sender_addr else ""
                            email_id_str = e_id.decode(errors="ignore")
                            
                            safe_subject = re.sub(r'[\\/:*?"<>|]', '_', subject).strip(" .")
                            safe_subject = safe_subject[:50].strip(" .")
                            email_folder_name = f"{email_id_str}_{safe_subject or 'email'}".strip(" .")
                            email_staging_path = os.path.join(self.staging_dir, email_folder_name)
                            
                            body_text = ""
                            attachments_found = []
                            links_found = []
                            
                            # 第一遍：收集正文文本、链接和所有附件
                            for part in msg.walk():
                                content_type = part.get_content_type()
                                content_disposition = str(part.get("Content-Disposition"))
                                
                                if content_type == "text/plain" and "attachment" not in content_disposition:
                                    try:
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            charset = part.get_content_charset() or 'utf-8'
                                            body_text += payload.decode(charset, errors='ignore')
                                    except Exception:
                                        pass
                                elif content_type == "text/html" and "attachment" not in content_disposition:
                                    try:
                                        payload = part.get_payload(decode=True)
                                        if payload:
                                            charset = part.get_content_charset() or 'utf-8'
                                            html_content = payload.decode(charset, errors='ignore')
                                            soup = BeautifulSoup(html_content, 'html.parser')
                                            body_text += soup.get_text()
                                            
                                            for a in soup.find_all('a', href=True):
                                                url = a['href']
                                                text = a.get_text().strip().lower()
                                                if url:
                                                    normalized_url = normalize_invoice_link_candidate(url)
                                                    if normalized_url != url.strip():
                                                        logging.info(f"Normalized embedded invoice link: {url} -> {normalized_url}")
                                                    links_found.append((normalized_url, text))
                                    except Exception:
                                        pass
                                
                                filename = part.get_filename()
                                if filename:
                                    filename = decode_str(filename)
                                    payload = part.get_payload(decode=True)
                                    if payload:
                                        attachments_found.append({
                                            "filename": filename,
                                            "payload": payload,
                                            "content_type": content_type,
                                            "content_disposition": content_disposition,
                                            "email_id": email_id_str,
                                            "sender": sender,
                                            "subject": subject,
                                            "payload_size": len(payload),
                                            "zip_context": "direct_attachment",
                                        })

                            # 判定层级
                            tier = _classify_email_tier(sender, subject, body_text)

                            # --- ZIP / RAR 解包与整理队列 ---
                            process_queue = list(attachments_found)
                            processed_attachments = []
                            
                            while process_queue:
                                attachment_info = process_queue.pop(0)
                                filename = attachment_info["filename"]
                                payload = attachment_info["payload"]
                                ext = os.path.splitext(filename)[1].lower()
                                
                                if ext == '.zip':
                                    appended_members = 0
                                    try:
                                        with zipfile.ZipFile(BytesIO(payload)) as zf:
                                            for zname in zf.namelist():
                                                zext = os.path.splitext(zname)[1].lower()
                                                if zext in VALID_EXTENSIONS or zext in {'.zip', '.ofd', '.xml'}:
                                                    zdata = zf.read(zname)
                                                    process_queue.append({
                                                        "filename": os.path.basename(zname),
                                                        "payload": zdata,
                                                        "content_type": attachment_info.get("content_type", "application/zip"),
                                                        "content_disposition": attachment_info.get("content_disposition", ""),
                                                        "email_id": attachment_info.get("email_id", email_id_str),
                                                        "sender": attachment_info.get("sender", sender),
                                                        "subject": attachment_info.get("subject", subject),
                                                        "payload_size": len(zdata),
                                                        "zip_context": "zip_member_extracted",
                                                        "parent_zip_filename": filename,
                                                    })
                                                    appended_members += 1
                                    except Exception:
                                        processed_attachments.append({
                                            "filename": filename,
                                            "payload": payload,
                                            "ext": ext,
                                            "decision": _build_attachment_candidate_decision(
                                                filename,
                                                payload,
                                                tier=tier,
                                                content_type=attachment_info.get("content_type", ""),
                                                content_disposition=attachment_info.get("content_disposition", ""),
                                                zip_context="zip_container_failed",
                                            ),
                                            "email_id": attachment_info.get("email_id", email_id_str),
                                            "sender": attachment_info.get("sender", sender),
                                            "subject": attachment_info.get("subject", subject),
                                            "payload_size": len(payload),
                                            "content_type": attachment_info.get("content_type", ""),
                                            "content_disposition": attachment_info.get("content_disposition", ""),
                                            "zip_context": "zip_container_failed",
                                        })
                                        continue

                                    if appended_members == 0:
                                        processed_attachments.append({
                                            "filename": filename,
                                            "payload": payload,
                                            "ext": ext,
                                            "decision": _build_attachment_candidate_decision(
                                                filename,
                                                payload,
                                                tier=tier,
                                                content_type=attachment_info.get("content_type", ""),
                                                content_disposition=attachment_info.get("content_disposition", ""),
                                                zip_context="zip_container_filtered",
                                            ),
                                            "email_id": attachment_info.get("email_id", email_id_str),
                                            "sender": attachment_info.get("sender", sender),
                                            "subject": attachment_info.get("subject", subject),
                                            "payload_size": len(payload),
                                            "content_type": attachment_info.get("content_type", ""),
                                            "content_disposition": attachment_info.get("content_disposition", ""),
                                            "zip_context": "zip_container_filtered",
                                        })
                                    else:
                                        self._emit_input_inventory_event({
                                            "email_id": attachment_info.get("email_id", email_id_str),
                                            "sender": attachment_info.get("sender", sender),
                                            "subject": attachment_info.get("subject", subject),
                                            "original_filename": filename,
                                            "attachment_ext": ext,
                                            "payload_size": len(payload),
                                            "mime_content_type": attachment_info.get("content_type", ""),
                                            "content_disposition": attachment_info.get("content_disposition", ""),
                                            "attachment_pair_key": self._attachment_pair_key(filename),
                                            "sibling_pdf_present": False,
                                            "sibling_ofd_present": False,
                                            "sibling_xml_present": False,
                                            "provider_unzipped_pair_suspected": False,
                                            "zip_context": "direct_attachment",
                                            "candidate_action": "expanded",
                                            "inventory_status": "zip_container_expanded",
                                            "entered_main_chain": False,
                                            "zip_member_count": appended_members,
                                        })
                                    continue
                                
                                processed_attachments.append({
                                    "filename": filename,
                                    "payload": payload,
                                    "ext": ext,
                                    "decision": _build_attachment_candidate_decision(
                                        filename,
                                        payload,
                                        tier=tier,
                                        content_type=attachment_info.get("content_type", ""),
                                        content_disposition=attachment_info.get("content_disposition", ""),
                                        zip_context=attachment_info.get("zip_context"),
                                    ),
                                    "email_id": attachment_info.get("email_id", email_id_str),
                                    "sender": attachment_info.get("sender", sender),
                                    "subject": attachment_info.get("subject", subject),
                                    "payload_size": len(payload),
                                    "content_type": attachment_info.get("content_type", ""),
                                    "content_disposition": attachment_info.get("content_disposition", ""),
                                    "zip_context": attachment_info.get("zip_context", "direct_attachment"),
                                })

                            raw_attachment_exts = {}
                            for raw_attachment in attachments_found:
                                pair_key = self._attachment_pair_key(raw_attachment.get("filename"))
                                raw_attachment_exts.setdefault(pair_key, set()).add(
                                    os.path.splitext(raw_attachment.get("filename", ""))[1].lower()
                                )

                            processed_pair_exts = {}
                            for processed_attachment in processed_attachments:
                                pair_key = self._attachment_pair_key(processed_attachment.get("filename"))
                                processed_pair_exts.setdefault(pair_key, set()).add(processed_attachment.get("ext", ""))

                            # 处理文件类
                            for attachment_info in processed_attachments:
                                filename = attachment_info["filename"]
                                payload = attachment_info["payload"]
                                ext = attachment_info["ext"]
                                decision = attachment_info["decision"]
                                pair_key = self._attachment_pair_key(filename)
                                sibling_exts = processed_pair_exts.get(pair_key, set())
                                raw_exts = raw_attachment_exts.get(pair_key, set())
                                provider_unzipped_pair_suspected = (
                                    sender_domain_value == "rails.com.cn"
                                    and ".pdf" in raw_exts
                                    and ".ofd" in raw_exts
                                    and ".zip" not in raw_exts
                                )
                                inventory_payload = {
                                    "email_id": attachment_info.get("email_id", email_id_str),
                                    "sender": attachment_info.get("sender", sender),
                                    "subject": attachment_info.get("subject", subject),
                                    "original_filename": filename,
                                    "attachment_ext": ext,
                                    "payload_size": attachment_info.get("payload_size", len(payload)),
                                    "mime_content_type": attachment_info.get("content_type", ""),
                                    "content_disposition": attachment_info.get("content_disposition", ""),
                                    "attachment_pair_key": pair_key,
                                    "sibling_pdf_present": ".pdf" in sibling_exts or ".pdf" in raw_exts,
                                    "sibling_ofd_present": ".ofd" in sibling_exts or ".ofd" in raw_exts,
                                    "sibling_xml_present": ".xml" in sibling_exts or ".xml" in raw_exts,
                                    "provider_unzipped_pair_suspected": provider_unzipped_pair_suspected,
                                    "zip_context": attachment_info.get("zip_context", "direct_attachment"),
                                    "candidate_action": decision.get("candidate_action"),
                                    "candidate_bucket": decision.get("candidate_bucket"),
                                    "prefilter_reason_code": decision.get("prefilter_reason_code"),
                                }

                                if decision["candidate_action"] == "drop":
                                    self._emit_input_inventory_event({
                                        **inventory_payload,
                                        "inventory_status": "dropped_prefilter",
                                        "entered_main_chain": False,
                                    })
                                    logging.info(
                                        f"Dropped A-layer candidate: {filename} ({decision['prefilter_reason_code']})"
                                    )
                                    continue

                                if ext not in VALID_EXTENSIONS and ext != '.zip':
                                    self._emit_input_inventory_event({
                                        **inventory_payload,
                                        "inventory_status": "skipped_unsupported_extension",
                                        "entered_main_chain": False,
                                    })
                                    continue

                                filepath = stage_candidate_file(email_staging_path, filename, payload)
                                results.append({
                                    "filepath": filepath,
                                    "tier": tier,
                                    "subject": subject,
                                    "is_url": False,
                                    "candidate_bucket": decision["candidate_bucket"],
                                    "candidate_action": decision["candidate_action"],
                                    "source_kind": decision["source_kind"],
                                    "prefilter_reason_code": decision["prefilter_reason_code"],
                                    "prefilter_signals": decision["prefilter_signals"],
                                    "email_id": attachment_info.get("email_id", email_id_str),
                                    "sender": attachment_info.get("sender", sender),
                                    "original_filename": filename,
                                    "attachment_ext": ext,
                                    "payload_size": attachment_info.get("payload_size", len(payload)),
                                    "mime_content_type": attachment_info.get("content_type", ""),
                                    "content_disposition": attachment_info.get("content_disposition", ""),
                                    "attachment_pair_key": pair_key,
                                    "sibling_pdf_present": ".pdf" in sibling_exts or ".pdf" in raw_exts,
                                    "sibling_ofd_present": ".ofd" in sibling_exts or ".ofd" in raw_exts,
                                    "sibling_xml_present": ".xml" in sibling_exts or ".xml" in raw_exts,
                                    "provider_unzipped_pair_suspected": provider_unzipped_pair_suspected,
                                    "zip_context": attachment_info.get("zip_context", "direct_attachment"),
                                })
                                self._emit_input_inventory_event({
                                    **inventory_payload,
                                    "inventory_status": "staged_for_processing",
                                    "entered_main_chain": True,
                                    "staged_path": filepath,
                                })
                                logging.info(
                                    f"Queued {decision['candidate_bucket']}/{decision['candidate_action']} attachment: "
                                    f"{os.path.basename(filepath)}"
                                )
                                    
                            # 处理链接类
                            for link, anchor_text in prioritize_invoice_links(links_found):
                                decision = _build_link_candidate_decision(link, anchor_text, tier=tier)
                                if decision["candidate_action"] == "drop":
                                    logging.info(
                                        f"Dropped A-layer URL candidate: {link} ({decision['prefilter_reason_code']})"
                                    )
                                    continue

                                results.append({
                                    "filepath": link,
                                    "tier": tier, # 继承邮件的 Tier
                                    "subject": subject,
                                    "sender": sender,
                                    "email_id": email_id_str,
                                    "is_url": True,
                                    "candidate_bucket": decision["candidate_bucket"],
                                    "candidate_action": decision["candidate_action"],
                                    "source_kind": decision["source_kind"],
                                    "prefilter_reason_code": decision["prefilter_reason_code"],
                                    "prefilter_signals": decision["prefilter_signals"],
                                })
                                logging.info(f"Discovered embedded invoice link: {link}")

                except Exception as e:
                    logging.error(f"Error processing email {e_id.decode()}: {e}")
            
            time.sleep(EMAIL_FETCH_LOOP_PAUSE_SECONDS)

        return results

    def extract_attachments(self, email_ids, mailbox="INBOX"):
        """1.3 Extract direct file attachments via 4-tier funnel filtering"""
        if not self.mail:
            return []

        self.mail.select(mailbox, readonly=True)
        results = []

        def stage_candidate_file(base_dir, filename, payload):
            os.makedirs(base_dir, exist_ok=True)
            filepath = os.path.join(base_dir, filename)
            counter = 1
            while os.path.exists(filepath):
                name, ext = os.path.splitext(filename)
                filepath = os.path.join(base_dir, f"{name}_{counter}{ext}")
                counter += 1

            with open(filepath, "wb") as f:
                f.write(payload)
            return filepath

        def record_staging_result(email_diag, attachment_indices, filename, content_type, content_disposition, payload_size, staged, error_message=""):
            matched = False
            for diag_index in attachment_indices.get(filename, []):
                if email_diag["attachments"][diag_index].get("staged"):
                    continue
                email_diag["attachments"][diag_index]["staged"] = staged
                email_diag["attachments"][diag_index]["staging_error"] = error_message
                matched = True
                break

            if not matched:
                email_diag["attachments"].append({
                    "filename": filename,
                    "content_type": content_type,
                    "content_disposition": content_disposition,
                    "payload_bytes_len": int(payload_size or 0),
                    "staged": staged,
                    "staging_error": error_message,
                })

        chunk_size = 50
        for i in range(0, len(email_ids), chunk_size):
            chunk = email_ids[i:i + chunk_size]
            logging.info(f"Processing chunk {i//chunk_size + 1}/{max(1, (len(email_ids)+chunk_size-1)//chunk_size)}")

            for e_id in chunk:
                email_id_str = self._safe_email_id(e_id)
                email_diag = {
                    "email_id": email_id_str,
                    "sender": "",
                    "subject": "",
                    "fetch_attempts": [],
                    "selected_fetch_mode": "",
                    "fetch_has_usable_bytes": False,
                    "mime_parse_success": False,
                    "mime_structure_usable": False,
                    "mime_part_count": 0,
                    "attachment_detected": False,
                    "attachments": [],
                    "staging_write_count": 0,
                    "staging_write_failures": 0,
                    "entered_main_chain": False,
                    "terminal_status": "uninitialized",
                }

                try:
                    selected_msg = None
                    parts = []
                    last_parsed_msg = None
                    last_terminal_status = "fetch_no_usable_bytes"

                    fetch_plan = [
                        ("RFC822", "(RFC822)"),
                        ("RFC822_RETRY", "(RFC822)"),
                        ("BODY.PEEK[]", "(BODY.PEEK[])"),
                    ]

                    for attempt_index, (mode_label, fetch_command) in enumerate(fetch_plan):
                        if mode_label == "BODY.PEEK[]" and last_terminal_status not in {
                            "fetch_no_usable_bytes",
                            "mime_parse_failed",
                            "mime_structure_unusable",
                        }:
                            break

                        raw_message_bytes, fetch_attempt = self._fetch_message_bytes(e_id, fetch_command, mode_label)
                        email_diag["fetch_attempts"].append(fetch_attempt)
                        if fetch_attempt["raw_bytes_len"] > 0:
                            email_diag["fetch_has_usable_bytes"] = True

                        if not raw_message_bytes:
                            last_terminal_status = "fetch_no_usable_bytes"
                            if attempt_index < 2:
                                continue
                            break

                        try:
                            msg_candidate = email.message_from_bytes(raw_message_bytes)
                            last_parsed_msg = msg_candidate
                            email_diag["mime_parse_success"] = True
                            email_diag["subject"] = decode_str(msg_candidate.get("Subject"))
                            email_diag["sender"] = decode_str(msg_candidate.get("From", ""))
                        except Exception as exc:
                            last_terminal_status = "mime_parse_failed"
                            email_diag["fetch_attempts"][-1]["error"] = str(exc)
                            if attempt_index < 2:
                                continue
                            break

                        try:
                            parts_candidate = list(msg_candidate.walk())
                        except Exception as exc:
                            parts_candidate = []
                            last_terminal_status = "mime_structure_unusable"
                            email_diag["fetch_attempts"][-1]["error"] = str(exc)
                            if attempt_index < 2:
                                continue
                            break

                        email_diag["mime_part_count"] = len(parts_candidate)
                        if not parts_candidate:
                            last_terminal_status = "mime_structure_unusable"
                            if attempt_index < 2:
                                continue
                            break

                        selected_msg = msg_candidate
                        parts = parts_candidate
                        email_diag["selected_fetch_mode"] = mode_label
                        email_diag["mime_structure_usable"] = True
                        last_terminal_status = "ready_for_attachment_scan"
                        break

                    if selected_msg is None:
                        if last_parsed_msg is not None:
                            email_diag["subject"] = email_diag["subject"] or decode_str(last_parsed_msg.get("Subject"))
                            email_diag["sender"] = email_diag["sender"] or decode_str(last_parsed_msg.get("From", ""))
                        email_diag["terminal_status"] = last_terminal_status
                        continue

                    msg = selected_msg
                    subject = email_diag["subject"]
                    sender = email_diag["sender"]
                    _, sender_addr = email.utils.parseaddr(sender)
                    sender_domain_value = sender_addr.split("@")[-1].lower() if "@" in sender_addr else ""

                    safe_subject = re.sub(r'[\\/:*?"<>|]', '_', subject).strip(" .")
                    safe_subject = safe_subject[:50].strip(" .")
                    email_folder_name = f"{email_id_str}_{safe_subject or 'email'}".strip(" .")
                    email_staging_path = os.path.join(self.staging_dir, email_folder_name)

                    body_text = ""
                    attachments_found = []
                    links_found = []
                    raw_attachment_indices = {}

                    for part in parts:
                        content_type = part.get_content_type()
                        content_disposition = str(part.get("Content-Disposition"))

                        if content_type == "text/plain" and "attachment" not in content_disposition:
                            try:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    charset = part.get_content_charset() or 'utf-8'
                                    body_text += payload.decode(charset, errors='ignore')
                            except Exception:
                                pass
                        elif content_type == "text/html" and "attachment" not in content_disposition:
                            try:
                                payload = part.get_payload(decode=True)
                                if payload:
                                    charset = part.get_content_charset() or 'utf-8'
                                    html_content = payload.decode(charset, errors='ignore')
                                    soup = BeautifulSoup(html_content, 'html.parser')
                                    body_text += soup.get_text()
                                    for a in soup.find_all('a', href=True):
                                        url = a['href']
                                        text = a.get_text().strip().lower()
                                        if url:
                                            normalized_url = normalize_invoice_link_candidate(url)
                                            if normalized_url != url.strip():
                                                logging.info(f"Normalized embedded invoice link: {url} -> {normalized_url}")
                                            links_found.append((normalized_url, text))
                            except Exception:
                                pass

                        filename = part.get_filename()
                        if not filename:
                            continue

                        filename = decode_str(filename)
                        payload = part.get_payload(decode=True)
                        email_diag["attachments"].append({
                            "filename": filename,
                            "content_type": content_type,
                            "content_disposition": content_disposition,
                            "payload_bytes_len": len(payload) if payload else 0,
                            "staged": False,
                            "staging_error": "",
                        })
                        raw_attachment_indices.setdefault(filename, []).append(len(email_diag["attachments"]) - 1)

                        if payload:
                            attachments_found.append({
                                "filename": filename,
                                "payload": payload,
                                "content_type": content_type,
                                "content_disposition": content_disposition,
                                "email_id": email_id_str,
                                "sender": sender,
                                "subject": subject,
                                "payload_size": len(payload),
                                "zip_context": "direct_attachment",
                            })

                    email_diag["attachment_detected"] = bool(email_diag["attachments"])

                    tier = _classify_email_tier(sender, subject, body_text)

                    process_queue = list(attachments_found)
                    processed_attachments = []

                    body_receipt_fields = parse_email_body_receipt_fields(
                        subject=subject,
                        sender=sender,
                        body_text=body_text,
                        email_date=msg.get("Date", ""),
                    )
                    if body_receipt_fields:
                        body_receipt_filename = build_email_body_receipt_filename(email_id_str, body_receipt_fields)
                        body_receipt_payload = render_email_body_receipt_pdf_bytes(
                            body_receipt_fields,
                            body_text,
                            source_email_id=email_id_str,
                        )
                        try:
                            body_receipt_path = stage_candidate_file(
                                email_staging_path,
                                body_receipt_filename,
                                body_receipt_payload,
                            )
                            email_diag["entered_main_chain"] = True
                            email_diag["staging_write_count"] += 1
                            results.append({
                                "filepath": body_receipt_path,
                                "tier": tier,
                                "subject": subject,
                                "is_url": False,
                                "candidate_bucket": "B",
                                "candidate_action": "main_chain",
                                "source_kind": "email_body_receipt",
                                "prefilter_reason_code": "B_EMAIL_BODY_RECEIPT_MAIN_CHAIN",
                                "prefilter_signals": _normalize_prefilter_signals(["email_body_receipt_fields"], []),
                                "email_id": email_id_str,
                                "sender": sender,
                                "original_filename": body_receipt_filename,
                                "attachment_ext": ".pdf",
                                "payload_size": len(body_receipt_payload),
                                "mime_content_type": "application/pdf",
                                "content_disposition": "generated-email-body-receipt",
                                "attachment_pair_key": os.path.splitext(body_receipt_filename)[0],
                                "sibling_pdf_present": True,
                                "sibling_ofd_present": False,
                                "sibling_xml_present": False,
                                "provider_unzipped_pair_suspected": False,
                                "zip_context": "email_body_receipt",
                            })
                            self._emit_input_inventory_event({
                                "email_id": email_id_str,
                                "sender": sender,
                                "subject": subject,
                                "original_filename": body_receipt_filename,
                                "attachment_ext": ".pdf",
                                "payload_size": len(body_receipt_payload),
                                "mime_content_type": "application/pdf",
                                "content_disposition": "generated-email-body-receipt",
                                "attachment_pair_key": os.path.splitext(body_receipt_filename)[0],
                                "sibling_pdf_present": True,
                                "sibling_ofd_present": False,
                                "sibling_xml_present": False,
                                "provider_unzipped_pair_suspected": False,
                                "zip_context": "email_body_receipt",
                                "candidate_action": "main_chain",
                                "candidate_bucket": "B",
                                "prefilter_reason_code": "B_EMAIL_BODY_RECEIPT_MAIN_CHAIN",
                                "inventory_status": "staged_for_processing",
                                "entered_main_chain": True,
                                "staged_path": body_receipt_path,
                                "source_kind": "email_body_receipt",
                                "body_receipt_fields": body_receipt_fields,
                            })
                        except Exception as stage_exc:
                            email_diag["staging_write_failures"] += 1
                            logging.error(f"Failed to stage email body receipt {body_receipt_filename}: {stage_exc}")

                    while process_queue:
                        attachment_info = process_queue.pop(0)
                        filename = attachment_info["filename"]
                        payload = attachment_info["payload"]
                        ext = os.path.splitext(filename)[1].lower()

                        if ext == '.zip':
                            appended_members = 0
                            try:
                                with zipfile.ZipFile(BytesIO(payload)) as zf:
                                    for zname in zf.namelist():
                                        zext = os.path.splitext(zname)[1].lower()
                                        if zext in VALID_EXTENSIONS or zext in {'.zip', '.ofd', '.xml'}:
                                            zdata = zf.read(zname)
                                            process_queue.append({
                                                "filename": os.path.basename(zname),
                                                "payload": zdata,
                                                "content_type": attachment_info.get("content_type", "application/zip"),
                                                "content_disposition": attachment_info.get("content_disposition", ""),
                                                "email_id": attachment_info.get("email_id", email_id_str),
                                                "sender": attachment_info.get("sender", sender),
                                                "subject": attachment_info.get("subject", subject),
                                                "payload_size": len(zdata),
                                                "zip_context": "zip_member_extracted",
                                                "parent_zip_filename": filename,
                                            })
                                            appended_members += 1
                            except Exception:
                                processed_attachments.append({
                                    "filename": filename,
                                    "payload": payload,
                                    "ext": ext,
                                    "decision": _build_attachment_candidate_decision(
                                        filename,
                                        payload,
                                        tier=tier,
                                        content_type=attachment_info.get("content_type", ""),
                                        content_disposition=attachment_info.get("content_disposition", ""),
                                        zip_context="zip_container_failed",
                                    ),
                                    "email_id": attachment_info.get("email_id", email_id_str),
                                    "sender": attachment_info.get("sender", sender),
                                    "subject": attachment_info.get("subject", subject),
                                    "payload_size": len(payload),
                                    "content_type": attachment_info.get("content_type", ""),
                                    "content_disposition": attachment_info.get("content_disposition", ""),
                                    "zip_context": "zip_container_failed",
                                })
                                continue

                            if appended_members == 0:
                                processed_attachments.append({
                                    "filename": filename,
                                    "payload": payload,
                                    "ext": ext,
                                    "decision": _build_attachment_candidate_decision(
                                        filename,
                                        payload,
                                        tier=tier,
                                        content_type=attachment_info.get("content_type", ""),
                                        content_disposition=attachment_info.get("content_disposition", ""),
                                        zip_context="zip_container_filtered",
                                    ),
                                    "email_id": attachment_info.get("email_id", email_id_str),
                                    "sender": attachment_info.get("sender", sender),
                                    "subject": attachment_info.get("subject", subject),
                                    "payload_size": len(payload),
                                    "content_type": attachment_info.get("content_type", ""),
                                    "content_disposition": attachment_info.get("content_disposition", ""),
                                    "zip_context": "zip_container_filtered",
                                })
                            else:
                                self._emit_input_inventory_event({
                                    "email_id": attachment_info.get("email_id", email_id_str),
                                    "sender": attachment_info.get("sender", sender),
                                    "subject": attachment_info.get("subject", subject),
                                    "original_filename": filename,
                                    "attachment_ext": ext,
                                    "payload_size": len(payload),
                                    "mime_content_type": attachment_info.get("content_type", ""),
                                    "content_disposition": attachment_info.get("content_disposition", ""),
                                    "attachment_pair_key": self._attachment_pair_key(filename),
                                    "sibling_pdf_present": False,
                                    "sibling_ofd_present": False,
                                    "sibling_xml_present": False,
                                    "provider_unzipped_pair_suspected": False,
                                    "zip_context": "direct_attachment",
                                    "candidate_action": "expanded",
                                    "inventory_status": "zip_container_expanded",
                                    "entered_main_chain": False,
                                    "zip_member_count": appended_members,
                                })
                            continue

                        processed_attachments.append({
                            "filename": filename,
                            "payload": payload,
                            "ext": ext,
                            "decision": _build_attachment_candidate_decision(
                                filename,
                                payload,
                                tier=tier,
                                content_type=attachment_info.get("content_type", ""),
                                content_disposition=attachment_info.get("content_disposition", ""),
                                zip_context=attachment_info.get("zip_context"),
                            ),
                            "email_id": attachment_info.get("email_id", email_id_str),
                            "sender": attachment_info.get("sender", sender),
                            "subject": attachment_info.get("subject", subject),
                            "payload_size": len(payload),
                            "content_type": attachment_info.get("content_type", ""),
                            "content_disposition": attachment_info.get("content_disposition", ""),
                            "zip_context": attachment_info.get("zip_context", "direct_attachment"),
                        })

                    raw_attachment_exts = {}
                    for raw_attachment in attachments_found:
                        pair_key = self._attachment_pair_key(raw_attachment.get("filename"))
                        raw_attachment_exts.setdefault(pair_key, set()).add(
                            os.path.splitext(raw_attachment.get("filename", ""))[1].lower()
                        )

                    processed_pair_exts = {}
                    for processed_attachment in processed_attachments:
                        pair_key = self._attachment_pair_key(processed_attachment.get("filename"))
                        processed_pair_exts.setdefault(pair_key, set()).add(processed_attachment.get("ext", ""))

                    for attachment_info in processed_attachments:
                        filename = attachment_info["filename"]
                        payload = attachment_info["payload"]
                        ext = attachment_info["ext"]
                        decision = attachment_info["decision"]
                        pair_key = self._attachment_pair_key(filename)
                        sibling_exts = processed_pair_exts.get(pair_key, set())
                        raw_exts = raw_attachment_exts.get(pair_key, set())
                        provider_unzipped_pair_suspected = (
                            sender_domain_value == "rails.com.cn"
                            and ".pdf" in raw_exts
                            and ".ofd" in raw_exts
                            and ".zip" not in raw_exts
                        )
                        inventory_payload = {
                            "email_id": attachment_info.get("email_id", email_id_str),
                            "sender": attachment_info.get("sender", sender),
                            "subject": attachment_info.get("subject", subject),
                            "original_filename": filename,
                            "attachment_ext": ext,
                            "payload_size": attachment_info.get("payload_size", len(payload)),
                            "mime_content_type": attachment_info.get("content_type", ""),
                            "content_disposition": attachment_info.get("content_disposition", ""),
                            "attachment_pair_key": pair_key,
                            "sibling_pdf_present": ".pdf" in sibling_exts or ".pdf" in raw_exts,
                            "sibling_ofd_present": ".ofd" in sibling_exts or ".ofd" in raw_exts,
                            "sibling_xml_present": ".xml" in sibling_exts or ".xml" in raw_exts,
                            "provider_unzipped_pair_suspected": provider_unzipped_pair_suspected,
                            "zip_context": attachment_info.get("zip_context", "direct_attachment"),
                            "candidate_action": decision.get("candidate_action"),
                            "candidate_bucket": decision.get("candidate_bucket"),
                            "prefilter_reason_code": decision.get("prefilter_reason_code"),
                        }

                        if decision["candidate_action"] == "drop":
                            self._emit_input_inventory_event({
                                **inventory_payload,
                                "inventory_status": "dropped_prefilter",
                                "entered_main_chain": False,
                            })
                            logging.info(f"Dropped A-layer candidate: {filename} ({decision['prefilter_reason_code']})")
                            continue

                        if ext not in VALID_EXTENSIONS and ext != '.zip':
                            self._emit_input_inventory_event({
                                **inventory_payload,
                                "inventory_status": "skipped_unsupported_extension",
                                "entered_main_chain": False,
                            })
                            continue

                        try:
                            filepath = stage_candidate_file(email_staging_path, filename, payload)
                        except Exception as stage_exc:
                            email_diag["staging_write_failures"] += 1
                            record_staging_result(
                                email_diag,
                                raw_attachment_indices,
                                filename,
                                attachment_info.get("content_type", ""),
                                attachment_info.get("content_disposition", ""),
                                attachment_info.get("payload_size", len(payload)),
                                False,
                                str(stage_exc),
                            )
                            self._emit_input_inventory_event({
                                **inventory_payload,
                                "inventory_status": "staging_write_failed",
                                "entered_main_chain": False,
                                "staging_error": str(stage_exc),
                            })
                            logging.error(f"Failed to stage attachment {filename}: {stage_exc}")
                            continue

                        email_diag["staging_write_count"] += 1
                        email_diag["entered_main_chain"] = True
                        record_staging_result(
                            email_diag,
                            raw_attachment_indices,
                            filename,
                            attachment_info.get("content_type", ""),
                            attachment_info.get("content_disposition", ""),
                            attachment_info.get("payload_size", len(payload)),
                            True,
                            "",
                        )
                        results.append({
                            "filepath": filepath,
                            "tier": tier,
                            "subject": subject,
                            "is_url": False,
                            "candidate_bucket": decision["candidate_bucket"],
                            "candidate_action": decision["candidate_action"],
                            "source_kind": decision["source_kind"],
                            "prefilter_reason_code": decision["prefilter_reason_code"],
                            "prefilter_signals": decision["prefilter_signals"],
                            "email_id": attachment_info.get("email_id", email_id_str),
                            "sender": attachment_info.get("sender", sender),
                            "original_filename": filename,
                            "attachment_ext": ext,
                            "payload_size": attachment_info.get("payload_size", len(payload)),
                            "mime_content_type": attachment_info.get("content_type", ""),
                            "content_disposition": attachment_info.get("content_disposition", ""),
                            "attachment_pair_key": pair_key,
                            "sibling_pdf_present": ".pdf" in sibling_exts or ".pdf" in raw_exts,
                            "sibling_ofd_present": ".ofd" in sibling_exts or ".ofd" in raw_exts,
                            "sibling_xml_present": ".xml" in sibling_exts or ".xml" in raw_exts,
                            "provider_unzipped_pair_suspected": provider_unzipped_pair_suspected,
                            "zip_context": attachment_info.get("zip_context", "direct_attachment"),
                        })
                        self._emit_input_inventory_event({
                            **inventory_payload,
                            "inventory_status": "staged_for_processing",
                            "entered_main_chain": True,
                            "staged_path": filepath,
                        })
                        logging.info(
                            f"Queued {decision['candidate_bucket']}/{decision['candidate_action']} attachment: "
                            f"{os.path.basename(filepath)}"
                        )

                    kept_url_candidates = []
                    provider_groups = {}
                    emitted_provider_groups = set()
                    emitted_fallback_groups = set()
                    for link, anchor_text in prioritize_invoice_links(links_found):
                        if _should_drop_baiwang_wrapper_url(
                            link,
                            sender_addr=sender_addr,
                            raw_attachment_exts=raw_attachment_exts,
                        ):
                            decision = _augment_link_decision(
                                _make_candidate_decision(
                                    "A",
                                    "drop",
                                    "A_BAIWANG_REDUNDANT_URL_WITH_ATTACHMENT",
                                    "url",
                                    strong_signals=["baiwang_wrapper_url_with_attachment_bundle"],
                                    link_group_key=build_link_group_key(link),
                                ),
                                link,
                                anchor_text,
                                sender_addr=sender_addr,
                                subject=subject,
                                body_text=body_text,
                            )
                        else:
                            decision = _build_link_candidate_decision(
                                link,
                                anchor_text,
                                tier=tier,
                                sender_addr=sender_addr,
                                subject=subject,
                                body_text=body_text,
                            )
                        if decision["candidate_action"] == "drop":
                            logging.info(f"Dropped A-layer URL candidate: {link} ({decision['prefilter_reason_code']})")
                            continue

                        provider_family = decision.get("provider_family", "")
                        if provider_family == "baiwang" or provider_family in DIRECT_INVOICE_FAMILIES:
                            group_key = _build_provider_group_key(
                                provider_family,
                                email_id=email_id_str,
                                candidate_urls=[decision.get("source_url", link)],
                                expected_fields=decision.get("provider_expected_fields", {}),
                            )
                            group_state = provider_groups.setdefault(
                                group_key,
                                {
                                    "provider_family": provider_family,
                                    "candidate_urls": [],
                                    "expected_fields": {},
                                },
                            )
                            group_state["candidate_urls"] = _collect_provider_group_urls(
                                provider_family,
                                [*group_state["candidate_urls"], decision.get("source_url", link)],
                            )
                            group_state["expected_fields"] = _merge_provider_expected_fields(
                                group_state["expected_fields"],
                                decision.get("provider_expected_fields", {}),
                            )
                            decision = dict(decision)
                            decision["provider_group_key"] = group_key

                        kept_url_candidates.append((link, anchor_text, decision))

                    for link, anchor_text, decision in kept_url_candidates:
                        provider_group_key = decision.get("provider_group_key", "")
                        if provider_group_key:
                            if provider_group_key in emitted_provider_groups:
                                logging.info(f"Skipped duplicate provider-group URL candidate: {link} ({provider_group_key})")
                                continue
                            emitted_provider_groups.add(provider_group_key)
                        else:
                            fallback_group_key = (
                                decision.get("link_group_key", build_link_group_key(link)),
                                decision.get("candidate_action", ""),
                                decision.get("provider_family", ""),
                            )
                            if fallback_group_key in emitted_fallback_groups:
                                logging.info(f"Skipped duplicate URL candidate: {link}")
                                continue
                            emitted_fallback_groups.add(fallback_group_key)

                        email_diag["entered_main_chain"] = True
                        result_payload = {
                            "filepath": link,
                            "tier": tier,
                            "subject": subject,
                            "sender": sender,
                            "email_id": email_id_str,
                            "is_url": True,
                            "candidate_bucket": decision["candidate_bucket"],
                            "candidate_action": decision["candidate_action"],
                            "source_kind": decision["source_kind"],
                            "prefilter_reason_code": decision["prefilter_reason_code"],
                            "prefilter_signals": decision["prefilter_signals"],
                            "source_url": decision.get("source_url", link),
                            "anchor_text": decision.get("anchor_text", anchor_text),
                            "url_host": decision.get("url_host", ""),
                            "url_path": decision.get("url_path", ""),
                            "provider_family": decision.get("provider_family", ""),
                            "provider_expected_fields": decision.get("provider_expected_fields", {}),
                        }
                        provider_group_state = provider_groups.get(provider_group_key, {})
                        if provider_group_key:
                            result_payload.update({
                                "provider_group_key": provider_group_key,
                                "provider_candidate_urls": provider_group_state.get("candidate_urls", []),
                                "provider_expected_fields": _merge_provider_expected_fields(
                                    provider_group_state.get("expected_fields", {}),
                                    decision.get("provider_expected_fields", {}),
                                ),
                            })
                        results.append(result_payload)
                        logging.info(f"Discovered embedded invoice link: {link}")

                    if email_diag["entered_main_chain"]:
                        email_diag["terminal_status"] = "entered_main_chain"
                    elif email_diag["attachment_detected"]:
                        email_diag["terminal_status"] = "attachments_found_but_not_staged"
                    else:
                        email_diag["terminal_status"] = "no_attachment_parts_detected"

                except Exception as exc:
                    email_diag["terminal_status"] = "processing_exception"
                    logging.error(f"Error processing email {email_id_str}: {exc}")
                finally:
                    sanitized_attachments = []
                    for attachment_diag in email_diag["attachments"]:
                        sanitized_attachments.append({
                            "filename": attachment_diag.get("filename", ""),
                            "content_type": attachment_diag.get("content_type", ""),
                            "content_disposition": attachment_diag.get("content_disposition", ""),
                            "payload_bytes_len": int(attachment_diag.get("payload_bytes_len", 0) or 0),
                            "staged": bool(attachment_diag.get("staged")),
                            "staging_error": attachment_diag.get("staging_error", ""),
                        })
                    email_diag["attachments"] = sanitized_attachments
                    if email_diag["terminal_status"] == "uninitialized":
                        email_diag["terminal_status"] = "processing_exception"
                    self._emit_extract_attachments_diagnostic(email_diag)

            time.sleep(EMAIL_FETCH_LOOP_PAUSE_SECONDS)

        return results
