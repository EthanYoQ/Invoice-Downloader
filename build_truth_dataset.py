import argparse
import csv
import datetime as dt
import email
import email.utils
import hashlib
import json
import os
import re
import shutil
import sys
import unicodedata
import zipfile
from io import BytesIO
from pathlib import Path
from urllib.parse import unquote, urlparse


DOC_EXTS = {".pdf", ".xml", ".ofd", ".jpg", ".jpeg", ".png"}
ZIP_MEMBER_EXTS = DOC_EXTS | {".zip"}
DIRECT_DOWNLOAD_EXTS = {".pdf", ".xml", ".ofd", ".zip"}
INVOICE_HINTS = (
    "发票",
    "行程单",
    "水单",
    "账单",
    "报销",
    "invoice",
    "receipt",
    "folio",
    "itinerary",
    "bill",
)
FOOD_SELLER_TOKENS = (
    "餐",
    "饭",
    "菜",
    "茶",
    "咖啡",
    "火锅",
    "海鲜",
    "金拱",
    "麦当劳",
    "肯德基",
    "盒马",
    "饮品",
    "烧烤",
    "串串",
    "食府",
    "涮肉",
    "小吃",
    "面馆",
    "饭店",
    "餐厅",
    "餐馆",
)


def repo_root() -> Path:
    return Path(__file__).resolve().parent


ROOT = repo_root()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bs4 import BeautifulSoup
import requests
from email_fetcher import (  # noqa: E402
    EmailFetcher,
    KEYWORDS_BODY,
    KEYWORDS_SUBJECT,
    TIER1_DOMAINS,
    _build_link_candidate_decision,
    _build_provider_group_key,
    _collect_provider_group_urls,
    _merge_provider_expected_fields,
    _should_drop_baiwang_wrapper_url,
    decode_str,
    normalize_invoice_link_candidate,
    prioritize_invoice_links,
)
from invoice_extractor import InvoiceExtractor, normalize_ocr_compat_text  # noqa: E402
from pdf_converter import PDFConverter  # noqa: E402
from provider_baiwang import parse_baiwang_xml_fields  # noqa: E402
from provider_direct_invoice import (  # noqa: E402
    DIRECT_INVOICE_FAMILIES,
    parse_direct_invoice_xml_fields,
)
from user_settings import UserSettingsStore  # noqa: E402


try:
    from zoneinfo import ZoneInfo

    LOCAL_TZ = ZoneInfo("Asia/Shanghai")
except Exception:
    LOCAL_TZ = dt.timezone(dt.timedelta(hours=8))


def write_json(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def append_jsonl(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False) + "\n")


def safe_name(value: str, fallback="item") -> str:
    text = re.sub(r'[\\/:*?"<>|]', "_", str(value or "")).strip(" .")
    text = re.sub(r"\s+", " ", text)
    return (text[:80].strip(" .") or fallback)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_amount(value) -> str:
    if value in (None, ""):
        return ""
    text = str(value).replace(",", "")
    m = re.search(r"-?\d+(?:\.\d+)?", text)
    if not m:
        return ""
    return f"{float(m.group(0)):.2f}"


def norm_date(value) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    m = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    compact = re.sub(r"\D", "", text)
    if len(compact) >= 8 and compact[:4].startswith("20"):
        return f"{compact[:4]}-{compact[4:6]}-{compact[6:8]}"
    return ""


def compact_date(value) -> str:
    return re.sub(r"\D", "", norm_date(value))


def is_valid_iso_date(value) -> bool:
    text = norm_date(value)
    if not text:
        return False
    try:
        dt.datetime.strptime(text, "%Y-%m-%d")
        return True
    except ValueError:
        return False


def has_invoice_hint(*values) -> bool:
    haystack = " ".join(str(v or "") for v in values).lower()
    return any(h.lower() in haystack for h in INVOICE_HINTS)


def parse_mail_date(message) -> str:
    value = message.get("Date", "")
    if not value:
        return ""
    try:
        parsed = email.utils.parsedate_to_datetime(value)
        if parsed.tzinfo:
            parsed = parsed.astimezone(LOCAL_TZ)
        return parsed.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return ""


def fetch_internaldate_local(fetcher: EmailFetcher, email_id) -> str:
    try:
        status, data = fetcher.mail.fetch(email_id, "(INTERNALDATE)")
    except Exception:
        return ""
    if status != "OK" or not data:
        return ""
    for part in data:
        parsed = EmailFetcher._extract_fetch_internaldate(part)
        if parsed:
            return parsed.strftime("%Y-%m-%d %H:%M:%S")
    return ""


def decode_payload(part) -> str:
    payload = part.get_payload(decode=True)
    if not payload:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except Exception:
        return payload.decode("utf-8", errors="replace")


def unique_path(directory: Path, filename: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / safe_name(filename, "attachment")
    stem = target.stem
    suffix = target.suffix
    counter = 1
    while target.exists():
        target = directory / f"{stem}_{counter}{suffix}"
        counter += 1
    return target


def save_payload(directory: Path, filename: str, payload: bytes) -> Path:
    target = unique_path(directory, filename)
    target.write_bytes(payload)
    return target


def extract_zip_members(zip_payload: bytes, directory: Path, parent_name: str, depth=0):
    if depth > 2:
        return []
    rows = []
    try:
        with zipfile.ZipFile(BytesIO(zip_payload)) as zf:
            for member in zf.namelist():
                if member.endswith("/"):
                    continue
                ext = Path(member).suffix.lower()
                if ext not in ZIP_MEMBER_EXTS:
                    continue
                data = zf.read(member)
                member_name = f"{Path(parent_name).stem}__{Path(member).name}"
                if ext == ".zip":
                    rows.extend(extract_zip_members(data, directory, member_name, depth + 1))
                    continue
                saved = save_payload(directory, member_name, data)
                rows.append(saved)
    except Exception:
        return rows
    return rows


def infer_url_ext(url: str, content_type="", content_disposition="") -> str:
    parsed = urlparse(str(url or ""))
    path_ext = Path(unquote(parsed.path or "")).suffix.lower()
    combined = " ".join([str(content_type or ""), str(content_disposition or ""), str(url or "")]).lower()
    if path_ext in DIRECT_DOWNLOAD_EXTS:
        return path_ext
    if "application/pdf" in combined or ".pdf" in combined:
        return ".pdf"
    if "xml" in combined:
        return ".xml"
    if "ofd" in combined:
        return ".ofd"
    if "zip" in combined:
        return ".zip"
    return ""


def filename_from_response(url: str, headers: dict, default_prefix: str) -> str:
    disposition = headers.get("Content-Disposition") or headers.get("content-disposition") or ""
    for pattern in (r"filename\*=UTF-8''([^;]+)", r'filename="?([^";]+)"?'):
        match = re.search(pattern, disposition, flags=re.IGNORECASE)
        if match:
            return safe_name(unquote(match.group(1)), f"{default_prefix}.bin")
    parsed_name = Path(unquote(urlparse(str(url or "")).path or "")).name
    if parsed_name and "." in parsed_name:
        return safe_name(parsed_name, f"{default_prefix}.bin")
    return f"{default_prefix}.bin"


def direct_download_url(url: str, output_dir: Path, *, email_id: str, index: int):
    """Download direct PDF/XML/OFD/ZIP URLs without Chromium."""
    if infer_url_ext(url) not in DIRECT_DOWNLOAD_EXTS:
        return []
    output_dir.mkdir(parents=True, exist_ok=True)
    response = requests.get(
        url,
        timeout=45,
        headers={
            "User-Agent": "Mozilla/5.0 InvoiceFlowAI truth builder",
            "Accept": "application/pdf,application/xml,text/xml,application/octet-stream,*/*",
        },
    )
    response.raise_for_status()
    ext = infer_url_ext(response.url, response.headers.get("Content-Type", ""), response.headers.get("Content-Disposition", ""))
    if ext not in DIRECT_DOWNLOAD_EXTS:
        return []
    filename = filename_from_response(response.url, response.headers, f"{email_id}_direct_{index}{ext}")
    if Path(filename).suffix.lower() not in DIRECT_DOWNLOAD_EXTS:
        filename = f"{Path(filename).stem}{ext}"
    saved = save_payload(output_dir, filename, response.content)
    saved_paths = [saved]
    if saved.suffix.lower() == ".zip":
        saved_paths.extend(extract_zip_members(response.content, output_dir, saved.name))
    return saved_paths


def extract_email_body_and_links(message):
    body_text = ""
    link_candidates = []
    for part in message.walk():
        content_type = part.get_content_type()
        disposition = str(part.get("Content-Disposition") or "").lower()
        if "attachment" in disposition:
            continue
        if content_type == "text/plain":
            body_text += "\n" + decode_payload(part)
        elif content_type == "text/html":
            html = decode_payload(part)
            soup = BeautifulSoup(html, "html.parser")
            body_text += "\n" + soup.get_text("\n")
            for anchor in soup.find_all("a", href=True):
                url = normalize_invoice_link_candidate(anchor.get("href") or "")
                text = anchor.get_text(" ", strip=True)
                if url:
                    link_candidates.append((url, text))

    for url in re.findall(r"https?://[^\s<>\"']+", body_text):
        link_candidates.append((normalize_invoice_link_candidate(url), ""))

    deduped = []
    seen = set()
    for url, text in link_candidates:
        if not url or url in seen:
            continue
        seen.add(url)
        deduped.append((url, text))
    return body_text.strip(), deduped


def compute_tier(sender: str, subject: str, body_text: str) -> int:
    sender_domain = sender.split("@")[-1] if "@" in sender else ""
    sender_domain = f"@{sender_domain}".lower()
    if any(d in sender_domain for d in TIER1_DOMAINS):
        return 1
    if any(k in subject for k in KEYWORDS_SUBJECT):
        return 2
    if any(k in body_text for k in KEYWORDS_BODY):
        return 3
    return 4


def collect_raw_evidence(args, source_root: Path):
    settings = UserSettingsStore().load()
    email_address = settings["email"]
    auth_code = settings["auth_code"]
    fetcher = EmailFetcher(
        email_address,
        auth_code,
        staging_dir=str(source_root / "raw_documents"),
        monitoring_dir=str(source_root / "monitoring"),
    )
    raw_root = source_root / "raw_documents"
    link_root = source_root / "link_evidence"
    evidence_index = []
    email_rows = []
    url_rows = []
    link_download_rows = []

    if not fetcher.connect():
        raise RuntimeError("IMAP connection failed")

    try:
        email_ids = fetcher.fetch_emails_by_date(args.date_from, args.before_exclusive, mailbox=args.mailbox)
        converter = PDFConverter(staging_dir=str(link_root))
        emitted_provider_groups = set()
        emitted_fallback_groups = set()
        for ordinal, e_id in enumerate(email_ids, start=1):
            email_id = fetcher._safe_email_id(e_id)
            raw_bytes = b""
            fetch_attempts = []
            for mode_label, fetch_command in [
                ("RFC822", "(RFC822)"),
                ("RFC822_RETRY", "(RFC822)"),
                ("BODY.PEEK[]", "(BODY.PEEK[])"),
            ]:
                raw_bytes, attempt = fetcher._fetch_message_bytes(e_id, fetch_command, mode_label)
                fetch_attempts.append(attempt)
                if raw_bytes:
                    break
            if not raw_bytes:
                email_rows.append({
                    "email_id": email_id,
                    "ordinal": ordinal,
                    "fetch_failed": True,
                    "fetch_attempts": fetch_attempts,
                })
                continue

            message = email.message_from_bytes(raw_bytes)
            subject = decode_str(message.get("Subject", ""))
            sender = decode_str(message.get("From", ""))
            _, sender_addr = email.utils.parseaddr(sender)
            mail_date_local = parse_mail_date(message) or fetch_internaldate_local(fetcher, e_id)
            body_text, links_found = extract_email_body_and_links(message)
            tier = compute_tier(sender, subject, body_text)
            folder = raw_root / f"{email_id}_{safe_name(subject, 'email')}"
            attachments = []
            raw_attachment_exts = {}

            for part in message.walk():
                filename = part.get_filename()
                if not filename:
                    continue
                filename = decode_str(filename)
                payload = part.get_payload(decode=True)
                if not payload:
                    continue
                ext = Path(filename).suffix.lower()
                pair_key = Path(filename).stem.lower()
                raw_attachment_exts.setdefault(pair_key, set()).add(ext)
                if ext not in DOC_EXTS and ext != ".zip":
                    attachments.append({
                        "filename": filename,
                        "ext": ext,
                        "bytes": len(payload),
                        "saved": False,
                        "reason": "unsupported_extension",
                    })
                    continue
                saved_paths = []
                if ext == ".zip":
                    zip_path = save_payload(folder, filename, payload)
                    saved_paths.append(zip_path)
                    saved_paths.extend(extract_zip_members(payload, folder, filename))
                else:
                    saved_paths.append(save_payload(folder, filename, payload))
                for saved in saved_paths:
                    saved_ext = saved.suffix.lower()
                    row = {
                        "email_id": email_id,
                        "mail_date_local": mail_date_local,
                        "subject": subject,
                        "sender": sender,
                        "source_kind": "attachment",
                        "source_url": "",
                        "file_name": saved.name,
                        "path": str(saved),
                        "ext": saved_ext,
                        "sha256": sha256_file(saved),
                        "bytes": saved.stat().st_size,
                    }
                    evidence_index.append(row)
                    append_jsonl(source_root / "document_index.jsonl", row)
                attachments.append({
                    "filename": filename,
                    "ext": ext,
                    "bytes": len(payload),
                    "saved": True,
                    "saved_count": len(saved_paths),
                })

            kept_url_candidates = []
            provider_groups = {}
            for link, anchor_text in prioritize_invoice_links(links_found):
                if _should_drop_baiwang_wrapper_url(
                    link,
                    sender_addr=sender_addr,
                    raw_attachment_exts=raw_attachment_exts,
                ):
                    continue
                decision = _build_link_candidate_decision(
                    link,
                    anchor_text,
                    tier=tier,
                    sender_addr=sender_addr,
                    subject=subject,
                    body_text=body_text,
                )
                if decision.get("candidate_action") == "drop":
                    url_rows.append({
                        "email_id": email_id,
                        "subject": subject,
                        "sender": sender,
                        "source_url": link,
                        "anchor_text": anchor_text,
                        "decision": decision,
                        "kept": False,
                    })
                    continue

                provider_family = decision.get("provider_family", "")
                if provider_family == "baiwang" or provider_family in DIRECT_INVOICE_FAMILIES:
                    group_key = _build_provider_group_key(
                        provider_family,
                        email_id=email_id,
                        candidate_urls=[decision.get("source_url", link)],
                        expected_fields=decision.get("provider_expected_fields", {}),
                    )
                    group_state = provider_groups.setdefault(
                        group_key,
                        {"provider_family": provider_family, "candidate_urls": [], "expected_fields": {}},
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
                        continue
                    emitted_provider_groups.add(provider_group_key)
                else:
                    fallback_key = (
                        decision.get("link_group_key", link),
                        decision.get("candidate_action", ""),
                        decision.get("provider_family", ""),
                    )
                    if fallback_key in emitted_fallback_groups:
                        continue
                    emitted_fallback_groups.add(fallback_key)

                group_state = provider_groups.get(provider_group_key, {})
                candidate_info = {
                    **decision,
                    "source_url": decision.get("source_url", link),
                    "provider_candidate_urls": group_state.get("candidate_urls", []),
                    "provider_expected_fields": _merge_provider_expected_fields(
                        group_state.get("expected_fields", {}),
                        decision.get("provider_expected_fields", {}),
                    ),
                }
                url_row = {
                    "email_id": email_id,
                    "mail_date_local": mail_date_local,
                    "subject": subject,
                    "sender": sender,
                    "source_url": candidate_info["source_url"],
                    "anchor_text": anchor_text,
                    "decision": decision,
                    "kept": True,
                }
                url_rows.append(url_row)
                append_jsonl(source_root / "url_candidates.jsonl", url_row)

                if decision.get("candidate_action") != "main_chain":
                    continue
                direct_paths = []
                try:
                    direct_paths = direct_download_url(
                        candidate_info["source_url"],
                        link_root / f"{email_id}_{safe_name(subject, 'email')}",
                        email_id=email_id,
                        index=len(link_download_rows) + 1,
                    )
                except Exception as exc:
                    direct_paths = []
                    append_jsonl(source_root / "link_downloads.jsonl", {
                        "email_id": email_id,
                        "mail_date_local": mail_date_local,
                        "subject": subject,
                        "sender": sender,
                        "source_url": candidate_info["source_url"],
                        "source_url": candidate_info["source_url"],
                        "status": "failed",
                        "reason_code": "TRUTH_DIRECT_DOWNLOAD_EXCEPTION",
                        "message": str(exc),
                    })

                if direct_paths:
                    metas = []
                    for saved_path in direct_paths:
                        key = f"{saved_path.suffix.lower().lstrip('_')}_path"
                        if saved_path.suffix.lower() == ".pdf":
                            key = "pdf_path"
                        elif saved_path.suffix.lower() == ".xml":
                            key = "xml_path"
                        elif saved_path.suffix.lower() == ".ofd":
                            key = "ofd_path"
                        metas.append({
                            "source_url": candidate_info["source_url"],
                            "resolved_url": candidate_info["source_url"],
                            "status": "downloaded",
                            "download_mode": "direct_http",
                            "provider_family": decision.get("provider_family", ""),
                            key: str(saved_path),
                        })
                elif decision.get("provider_family") in {"baiwang", *DIRECT_INVOICE_FAMILIES}:
                    try:
                        metas = converter.process_invoice_links(
                            candidate_info["source_url"],
                            subject,
                            email_id,
                            return_metadata=True,
                            candidate_info=candidate_info,
                        )
                    except Exception as exc:
                        metas = [{
                            "source_url": candidate_info["source_url"],
                            "status": "failed",
                            "reason_code": "TRUTH_PROVIDER_RECOVERY_EXCEPTION",
                            "message": str(exc),
                        }]
                else:
                    metas = [{
                        "source_url": candidate_info["source_url"],
                        "status": "skipped",
                        "reason_code": "TRUTH_GENERIC_WEB_LINK_SKIPPED",
                        "message": "Generic non-provider URL was retained as URL evidence but not rendered with Chromium.",
                    }]
                for meta in metas:
                    meta_row = {
                        "email_id": email_id,
                        "mail_date_local": mail_date_local,
                        "subject": subject,
                        "sender": sender,
                        **dict(meta or {}),
                    }
                    link_download_rows.append(meta_row)
                    append_jsonl(source_root / "link_downloads.jsonl", meta_row)
                    for key in ("pdf_path", "xml_path", "ofd_path"):
                        path_value = meta_row.get(key)
                        if not path_value:
                            continue
                        path = Path(path_value)
                        if not path.exists():
                            continue
                        row = {
                            "email_id": email_id,
                            "mail_date_local": mail_date_local,
                            "subject": subject,
                            "sender": sender,
                            "source_kind": "url",
                            "source_url": meta_row.get("source_url", candidate_info["source_url"]),
                            "file_name": path.name,
                            "path": str(path),
                            "ext": path.suffix.lower(),
                            "sha256": sha256_file(path),
                            "bytes": path.stat().st_size,
                            "provider_family": meta_row.get("provider_family", ""),
                        }
                        evidence_index.append(row)
                        append_jsonl(source_root / "document_index.jsonl", row)

            email_row = {
                "email_id": email_id,
                "ordinal": ordinal,
                "mail_date_local": mail_date_local,
                "subject": subject,
                "sender": sender,
                "tier": tier,
                "attachments": attachments,
                "link_count": len(links_found),
                "kept_url_count": len([row for row in url_rows if row.get("email_id") == email_id and row.get("kept")]),
            }
            email_rows.append(email_row)
            append_jsonl(source_root / "mailbox_inventory.jsonl", email_row)
    finally:
        try:
            if getattr(fetcher, "mail", None):
                fetcher.mail.logout()
        except Exception:
            pass

    inventory = {
        "date_from": args.date_from,
        "date_to": args.date_to,
        "before_exclusive": args.before_exclusive,
        "email_count": len(email_rows),
        "document_count": len(evidence_index),
        "url_candidate_count": len(url_rows),
        "link_download_count": len(link_download_rows),
        "emails": email_rows,
    }
    write_json(source_root / "mailbox_inventory.json", inventory)
    return inventory


def read_jsonl(path: Path):
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


def is_xml_like_file(path: Path) -> bool:
    try:
        prefix = path.read_bytes()[:512].lstrip(b"\xef\xbb\xbf\r\n\t ")
    except Exception:
        return False
    return prefix.startswith(b"<?xml") or prefix.startswith(b"<EInvoice") or prefix.startswith(b"<Invoice")


def effective_doc_ext(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".xml" or is_xml_like_file(path):
        return ".xml"
    return suffix


def enrich_doc_rows_from_link_downloads(doc_rows: list[dict], link_rows: list[dict]) -> list[dict]:
    """Add downloaded XML-like evidence whose malformed filename suffix kept it out of document_index."""
    seen_paths = {str(Path(row.get("path", ""))) for row in doc_rows if row.get("path")}
    enriched = list(doc_rows)
    for link in link_rows:
        for key, value in link.items():
            if not key.endswith("_path") or not value:
                continue
            path = Path(str(value))
            if str(path) in seen_paths or not path.exists():
                continue
            ext = effective_doc_ext(path)
            if ext not in DOC_EXTS:
                continue
            seen_paths.add(str(path))
            enriched.append({
                "email_id": str(link.get("email_id") or ""),
                "mail_date_local": link.get("mail_date_local", ""),
                "subject": link.get("subject", ""),
                "sender": link.get("sender", ""),
                "source_kind": "url",
                "source_url": link.get("source_url", ""),
                "file_name": path.name,
                "path": str(path),
                "ext": ext,
                "sha256": sha256_file(path),
                "bytes": path.stat().st_size,
                "provider_family": link.get("provider_family", ""),
            })
    return enriched


def parse_generic_xml(path: Path) -> dict:
    payload = path.read_bytes()
    values = {}
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(payload)
        for el in root.iter():
            tag = el.tag.split("}", 1)[-1]
            text = (el.text or "").strip()
            if text and tag not in values:
                values[tag] = text
    except Exception:
        values = {}
    fields = parse_direct_invoice_xml_fields(payload) or parse_baiwang_xml_fields(payload) or {}
    if fields:
        if values.get("ItemName") and not fields.get("item_name"):
            fields["item_name"] = values.get("ItemName", "")
        return fields
    return {
        "invoice_number": values.get("InvoiceNumber") or values.get("EIid") or "",
        "invoice_code": values.get("InvoiceCode") or "",
        "invoice_date": norm_date(values.get("IssueTime") or values.get("RequestTime") or ""),
        "seller": values.get("SellerName", ""),
        "purchaser": values.get("BuyerName", ""),
        "amount": norm_amount(
            values.get("TotalTax-includedAmount")
            or values.get("TotaltaxIncludedAmount")
            or values.get("TotalAmount")
            or ""
        ),
        "item_name": values.get("ItemName", ""),
    }


def companion_evidence_paths_for_primary(row_path: Path, invoice_number: str = "") -> list[Path]:
    ext = effective_doc_ext(row_path)
    if ext == ".pdf":
        allowed_exts = {".xml", ".ofd"}
        order = {".xml": 0, ".ofd": 1}
    elif ext == ".xml":
        allowed_exts = {".pdf", ".ofd"}
        order = {".pdf": 0, ".ofd": 1}
    else:
        return []
    try:
        candidates = list(row_path.parent.iterdir())
    except Exception:
        return []
    companions = []
    for candidate in candidates:
        if candidate == row_path or not candidate.is_file():
            continue
        candidate_ext = effective_doc_ext(candidate)
        if candidate_ext not in allowed_exts:
            continue
        if candidate.stem == row_path.stem or (invoice_number and invoice_number in candidate.name):
            companions.append(candidate)
    return sorted(companions, key=lambda item: (order.get(effective_doc_ext(item), 99), item.name))


def parse_companion_xml_for_pdf(path: Path) -> dict:
    for companion in companion_evidence_paths_for_primary(path):
        if effective_doc_ext(companion) != ".xml":
            continue
        try:
            fields = parse_generic_xml(companion)
        except Exception:
            continue
        if fields and (fields.get("invoice_number") or (fields.get("seller") and fields.get("amount"))):
            return fields
    return {}


def normalize_extractor_result(result: dict) -> dict:
    if not result:
        return {}
    return {
        "invoice_number": str(result.get("InvoiceNumber") or "").strip(),
        "invoice_code": str(result.get("InvoiceCode") or "").strip(),
        "invoice_date": norm_date(result.get("Date") or result.get("Departure_Date") or ""),
        "seller": normalize_ocr_compat_text(result.get("Seller") or "").strip(),
        "purchaser": normalize_ocr_compat_text(result.get("Purchaser") or "").strip(),
        "amount": norm_amount(result.get("Amount") or ""),
        "truth_type": str(result.get("Type") or result.get("category") or "").strip(),
        "category": str(result.get("category") or result.get("Type") or "").strip(),
    }


def read_pdf_text(path: Path, max_pages=2) -> str:
    try:
        import fitz

        with fitz.open(path) as doc:
            text = "\n".join(doc.load_page(i).get_text("text") for i in range(min(max_pages, len(doc))))
            return normalize_ocr_compat_text(text)
    except Exception:
        return ""


def parse_train_ticket_pdf(path: Path) -> dict:
    text = read_pdf_text(path)
    if "铁路电子客票" not in text and "电子客票号" not in text:
        return {}
    invoice_number = ""
    match = re.search(r"发票号码[:：]\s*(\d{8,})", text)
    if match:
        invoice_number = match.group(1)
    amount = ""
    match = re.search(r"票价[:：]\s*[￥¥]?\s*([0-9]+(?:\.[0-9]{1,2})?)", text)
    if match:
        amount = norm_amount(match.group(1))
    if not amount:
        amount = _first_money(re.findall(r"[￥¥]\s*([0-9]+(?:\.[0-9]{1,2})?)", text))
    travel_date = ""
    match = re.search(
        r"(20\d{2})年(\d{1,2})月(\d{1,2})日\s*电子发票（铁路电子客票）\s*\d{1,2}:\d{2}开",
        text,
        flags=re.DOTALL,
    )
    if not match:
        date_matches = list(re.finditer(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text))
        non_issue_dates = [
            candidate for candidate in date_matches
            if "开票日期" not in text[max(0, candidate.start() - 12): candidate.start()]
        ]
        match = non_issue_dates[0] if non_issue_dates else (date_matches[0] if date_matches else None)
    if match:
        travel_date = f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    purchaser = ""
    purchaser_match = re.search(r"(辉瑞[^\n\r]+)", text)
    if purchaser_match:
        purchaser = purchaser_match.group(1).strip()
    return {
        "invoice_number": invoice_number,
        "invoice_code": "",
        "invoice_date": travel_date,
        "seller": "中国铁路",
        "purchaser": purchaser,
        "amount": amount,
        "truth_type": "火车票",
        "category": "火车票",
    }


def parse_loose_standard_einvoice_pdf(path: Path, target_company: str) -> dict:
    text = read_pdf_text(path)
    if "电子发票" not in text or "发票号码" not in text:
        return {}
    number_match = re.search(r"(?<!\d)(\d{20})(?!\d)", text)
    date_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
    currency_values = re.findall(r"[¥￥]\s*(-?\d+(?:\.\d{2})?)", text)
    currency_values.extend(re.findall(r"(-?\d+(?:\.\d{2})?)\s*[¥￥]", text))
    if not number_match or not date_match or not currency_values:
        return {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    def clean_party_line(line: str) -> str:
        return re.sub(r"^名称[:：]?", "", line).strip()

    def looks_like_party_line(line: str) -> bool:
        cleaned = clean_party_line(line)
        if not cleaned:
            return False
        if cleaned.startswith("*"):
            return False
        if any(token in cleaned for token in ["统一社会信用代码", "电子发票", "项目名称", "规格型号"]):
            return False
        return any(
            token in cleaned
            for token in ["公司", "个体工商户", "酒店", "铁路", *FOOD_SELLER_TOKENS]
        )

    company_lines = [
        clean_party_line(line) for line in lines
        if looks_like_party_line(line)
        and clean_party_line(line) not in {"销售信息", "购买方信息", "销售方信息", "购买方", "销售方"}
    ]
    purchaser = ""
    if target_company:
        purchaser = next((line for line in company_lines if target_company in line), "")
    if not purchaser:
        purchaser = next((line for line in company_lines if "辉瑞" in line), "")
    seller = next((line for line in company_lines if line != purchaser), "")
    if not seller or not purchaser:
        return {}
    joined = f"{seller} {path.name}"
    truth_type = truth_type_from_seller(joined)
    return {
        "invoice_number": number_match.group(1),
        "invoice_code": "",
        "invoice_date": f"{date_match.group(1)}-{int(date_match.group(2)):02d}-{int(date_match.group(3)):02d}",
        "seller": seller,
        "purchaser": purchaser,
        "amount": _first_money(currency_values),
        "truth_type": truth_type,
        "category": truth_type,
    }


def _first_money(candidates, default=""):
    values = []
    for value in candidates:
        amount = norm_amount(value)
        if amount:
            values.append(amount)
    if not values:
        return default
    return max(values, key=lambda item: abs(float(item)))


def parse_ride_itinerary_pdf(path: Path) -> dict:
    text = read_pdf_text(path)
    if "滴滴出行-行程单" in text or "DIDI TRAVEL" in text:
        seller = "滴滴出行"
        truth_type = "行程单"
        amount_match = re.search(r"合计\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元", text)
        date_match = re.search(r"行程起止日期[:：]\s*(20\d{2}-\d{1,2}-\d{1,2})", text)
        return {
            "invoice_number": "",
            "invoice_code": "",
            "invoice_date": norm_date(date_match.group(1) if date_match else ""),
            "seller": seller,
            "purchaser": "个人",
            "amount": norm_amount(amount_match.group(1) if amount_match else ""),
            "truth_type": truth_type,
            "category": truth_type,
        }
    if "高德地图" in text and "行程单" in text:
        seller = "高德地图"
        amount_match = re.search(r"合计\s*([0-9]+(?:\.[0-9]{1,2})?)\s*元", text)
        date_match = re.search(r"行程时间[:：]\s*(20\d{2}-\d{1,2}-\d{1,2})", text)
        return {
            "invoice_number": "",
            "invoice_code": "",
            "invoice_date": norm_date(date_match.group(1) if date_match else ""),
            "seller": seller,
            "purchaser": "个人",
            "amount": norm_amount(amount_match.group(1) if amount_match else ""),
            "truth_type": "行程单",
            "category": "行程单",
        }
    return {}


def parse_marriott_folio_pdf(path: Path) -> dict:
    text = read_pdf_text(path)
    if "marriott" not in text.lower() and "万豪" not in text:
        return {}
    if not any(token in text for token in ["INFORMATION INVOICE", "Balance", "余额", "Folio"]):
        return {}
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    seller = lines[0] if lines else "Marriott"

    def _nonzero_money(value):
        normalized = norm_amount(value)
        if not normalized:
            return ""
        try:
            return normalized if abs(float(normalized)) > 0 else ""
        except ValueError:
            return normalized

    amount = ""
    total_match = re.search(
        r"Total\s*\n\s*([0-9]{1,3}(?:,[0-9]{3})*\.\d{2}|[0-9]+\.\d{2})",
        text,
        re.IGNORECASE,
    )
    if total_match:
        amount = _nonzero_money(total_match.group(1))
    if not amount:
        folio_match = re.search(r"\[FOLIO:([^\]]+)\]", text, re.IGNORECASE | re.DOTALL)
        if folio_match:
            detail_amounts = re.findall(r"\|([0-9]+(?:\.[0-9]{2}))", folio_match.group(1))
            if detail_amounts:
                amount = norm_amount(str(sum(float(value) for value in detail_amounts)))
    if not amount:
        for pattern in [
            r"Balance\s*(?:CNY\s*)?([0-9]{1,3}(?:,[0-9]{3})*\.\d{2}|[0-9]+\.\d{2})",
            r'"balance"\s*:\s*"([0-9]{1,3}(?:,[0-9]{3})*\.\d{2}|[0-9]+\.\d{2})"',
            r"AMT:\s*([0-9]{1,3}(?:,[0-9]{3})*\.\d{2}|[0-9]+\.\d{2})",
        ]:
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                amount = _nonzero_money(match.group(1))
                if amount:
                    break
    if not amount:
        money_values = re.findall(r"(?<!\d)(\d{1,3}(?:,\d{3})+\.\d{2}|\d{2,6}\.\d{2})(?!\d)", text)
        amount = _first_money(money_values)

    invoice_date = ""
    gd_match = re.search(r"GD:(20\d{2})-(\d{1,2})-(\d{1,2})", text, re.IGNORECASE)
    if gd_match:
        invoice_date = f"{gd_match.group(1)}-{int(gd_match.group(2)):02d}-{int(gd_match.group(3)):02d}"
    month_map = {
        "JAN": 1,
        "FEB": 2,
        "MAR": 3,
        "APR": 4,
        "MAY": 5,
        "JUN": 6,
        "JUL": 7,
        "AUG": 8,
        "SEP": 9,
        "OCT": 10,
        "NOV": 11,
        "DEC": 12,
    }
    printed_match = re.search(r"PRINTED\s+ON\s+(\d{1,2})-([A-Za-z]{3})-(\d{2})", text, re.IGNORECASE)
    if not invoice_date and printed_match:
        month = month_map.get(printed_match.group(2).upper())
        if month:
            invoice_date = f"{2000 + int(printed_match.group(3)):04d}-{month:02d}-{int(printed_match.group(1)):02d}"
    date_match = re.search(r"(\d{2})-(\d{2})-(\d{2})", text)
    if not invoice_date and date_match:
        first = int(date_match.group(1))
        second = int(date_match.group(2))
        year = int(date_match.group(3))
        if first > 12:
            invoice_date = f"20{year:02d}-{second:02d}-{first:02d}"
        else:
            invoice_date = f"20{year:02d}-{first:02d}-{second:02d}"
    purchaser = "亓勇" if "亓勇" in text or "YONG QI" in text.upper() else "个人"
    if not amount:
        return {}
    return {
        "invoice_number": "",
        "invoice_code": "",
        "invoice_date": invoice_date,
        "seller": seller,
        "purchaser": purchaser,
        "amount": amount,
        "truth_type": "住宿水单",
        "category": "住宿水单",
    }


def parse_foreign_invoice_pdf(path: Path) -> dict:
    text = read_pdf_text(path)
    if "IT7 Networks Inc" in text and "Invoice #" in text:
        number_match = re.search(r"Invoice\s+#\s*([0-9A-Za-z-]+)", text)
        date_match = re.search(r"Invoice Date:\s*(.+)", text)
        amount_match = re.search(r"Total\s*\n?\$([0-9]+(?:\.[0-9]{2})?)\s*USD", text)
        invoice_date = ""
        if date_match:
            cleaned_date = re.sub(r"\b(\d{1,2})(st|nd|rd|th)\b", r"\1", date_match.group(1).strip(), flags=re.IGNORECASE)
            for date_format in ("%A, %B %d, %Y", "%B %d, %Y"):
                try:
                    parsed = dt.datetime.strptime(cleaned_date, date_format)
                    invoice_date = parsed.date().isoformat()
                    break
                except Exception:
                    continue
        return {
            "invoice_number": number_match.group(1) if number_match else "",
            "invoice_code": "",
            "invoice_date": invoice_date,
            "seller": "IT7 Networks Inc",
            "purchaser": "Yong Qi",
            "amount": norm_amount(amount_match.group(1) if amount_match else ""),
            "truth_type": "非目标公司发票",
            "category": "非目标公司发票",
        }
    return {}


def parse_cits_pdf(path: Path) -> dict:
    text = read_pdf_text(path)
    subjectish = f"{path.name}\n{path.parent.name}"
    compact = re.sub(r"\s+", "", f"{text}\n{subjectish}")
    if "CITS" not in compact and "国旅运通" not in compact:
        return {}
    invoice_number = ""
    invoice_match = re.search(r"\b(SCCT[0-9]+)\b", text)
    if invoice_match:
        invoice_number = invoice_match.group(1)
    date_match = re.search(r"Date:\s*\n?.*?(\d{2}/\d{2}/\d{2})", text, flags=re.DOTALL)
    invoice_date = ""
    if date_match:
        dd, mm, yy = date_match.group(1).split("/")
        invoice_date = f"20{yy}-{mm}-{dd}"
    amount_value = InvoiceExtractor._extract_cits_total_amount(text)
    purchaser = "辉瑞投资有限公司" if "辉瑞投资有限公司" in text or "PFIZER" in text.upper() else "个人"
    hotel_itinerary = (
        not invoice_number
        and InvoiceExtractor._looks_like_cits_hotel_itinerary(text, subjectish)
    )
    is_air_invoice = any(token in text for token in ["机场", "首段行程", "Flight", "APT", "航班号", "航空公司"])
    is_travel_service_fee = (
        invoice_number
        and not is_air_invoice
        and any(token in text for token in ["Service Charge", "Hotel (GDS)", "GDS", "GBT Travel Services", "Dom Hotel"])
    )
    truth_type = "机票" if is_air_invoice else ("差旅服务费" if is_travel_service_fee else "其他")
    if hotel_itinerary:
        truth_type = "住宿水单"
    if not invoice_date:
        invoice_date_match = re.search(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})", text)
        invoice_date = norm_date(invoice_date_match.group(0) if invoice_date_match else invoice_date)
    seller = InvoiceExtractor._extract_cits_hotel_name(text) if hotel_itinerary else "CITS GBT"
    return {
        "invoice_number": invoice_number,
        "invoice_code": "",
        "invoice_date": invoice_date,
        "seller": seller or "CITS GBT",
        "purchaser": purchaser,
        "amount": norm_amount(amount_value),
        "truth_type": truth_type,
        "category": truth_type,
    }


def parse_ofd_from_metadata(path: Path, meta: dict, target_company: str) -> dict:
    text = " ".join([path.name, str(meta.get("subject") or "")])
    if "发票" not in text:
        return {}
    number_match = re.search(r"(?<!\d)(\d{20})(?!\d)", text)
    amount_match = re.search(r"(?:发票金额|开票金额)[:：]?([0-9]+(?:\.[0-9]{1,2})?)元", text)
    seller = ""
    seller_match = re.search(r"【电子发票】(.+?)（发票金额", text)
    if seller_match:
        seller = seller_match.group(1).strip()
    if not seller:
        seller_match = re.search(r"来自【?(.+?)】?的?电子发票", text)
        if seller_match:
            seller = seller_match.group(1).strip()
    if not number_match or not amount_match or not seller:
        return {}
    truth_type = truth_type_from_seller(seller)
    return {
        "invoice_number": number_match.group(1),
        "invoice_code": "",
        "invoice_date": "",
        "seller": seller,
        "purchaser": target_company,
        "amount": norm_amount(amount_match.group(1)),
        "truth_type": truth_type,
        "category": truth_type,
    }


def normalized_lines(text: str) -> str:
    text = unicodedata.normalize("NFKC", str(text or "").replace("\xa0", " "))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    return "\n".join(line for line in lines if line)


def label_value(text: str, label: str) -> str:
    patterns = [
        rf"{re.escape(label)}\s*[:：]?\s*\n+\s*([^\n]+)",
        rf"{re.escape(label)}\s*[:：]\s*([^\n]+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
    return ""


def truth_type_from_seller(seller: str) -> str:
    seller = normalize_ocr_compat_text(seller)
    if any(token in seller for token in ["酒店", "宾馆", "旅店", "万豪", "喜来登", "全季"]):
        return "住宿发票"
    if any(token in seller for token in FOOD_SELLER_TOKENS):
        return "餐饮"
    if any(token in seller for token in ["铁路", "航空"]):
        return "火车票" if "铁路" in seller else "机票"
    return "其他"


def parse_baiwang_email_text(text: str, target_company: str) -> dict:
    text = normalized_lines(text)
    if "百望" not in text and "baiwang" not in text.lower() and "为您开具了电子发票" not in text:
        return {}
    seller_match = re.search(r"您好[:：]?\s*\n?([\s\S]{1,140}?)为您开具了电子发票", text)
    seller = re.sub(r"\s+", "", seller_match.group(1)) if seller_match else ""
    amount = norm_amount(label_value(text, "发票金额"))
    invoice_date = norm_date(label_value(text, "开票日期"))
    purchaser = label_value(text, "购方名称") or target_company
    invoice_number = re.sub(r"\D", "", label_value(text, "发票号码"))
    if not (seller and amount and invoice_date and invoice_number):
        return {}
    truth_type = truth_type_from_seller(seller)
    return {
        "invoice_number": invoice_number,
        "invoice_code": "",
        "invoice_date": invoice_date,
        "seller": seller,
        "purchaser": purchaser,
        "amount": amount,
        "truth_type": truth_type,
        "category": truth_type,
    }


def parse_newtimeai_email_text(text: str, target_company: str) -> dict:
    text = normalized_lines(text)
    if "进行消费" not in text or "发票号码" not in text:
        return {}
    purchaser_match = re.search(r"尊敬的\s*([^,\n，]+)", text)
    seller_match = re.search(r"您于.*?在\s*\n?([\s\S]{1,120}?)\s*\n?进行消费", text, flags=re.DOTALL)
    seller = re.sub(r"\s+", "", seller_match.group(1)) if seller_match else ""
    purchaser = purchaser_match.group(1).strip() if purchaser_match else target_company
    invoice_number = re.sub(r"\D", "", label_value(text, "发票号码"))
    invoice_date = norm_date(label_value(text, "开票日期"))
    amount = norm_amount(label_value(text, "合计金额"))
    if not (seller and invoice_number and invoice_date and amount):
        return {}
    truth_type = truth_type_from_seller(seller)
    return {
        "invoice_number": invoice_number,
        "invoice_code": "",
        "invoice_date": invoice_date,
        "seller": seller,
        "purchaser": purchaser,
        "amount": amount,
        "truth_type": truth_type,
        "category": truth_type,
    }


def parse_icloud_receipt_text(text: str) -> dict:
    text = normalized_lines(text)
    if "iCloud" not in text or "收据" not in text:
        return {}
    invoice_date = norm_date(label_value(text, "日期"))
    if not invoice_date:
        date_match = re.search(r"(20\d{2})年(\d{1,2})月(\d{1,2})日", text)
        invoice_date = norm_date(date_match.group(0) if date_match else "")
    order_number = label_value(text, "订单号")
    document_number = label_value(text, "文稿编号") or label_value(text, "文稿")
    amount = _first_money(re.findall(r"[￥¥]\s*([0-9]+(?:\.[0-9]{1,2})?)", text))
    purchaser = "亓勇" if "亓勇" in text else "个人"
    seller = "云上艾珀（贵州）技术有限公司" if "云上艾珀" in text or "云上贵州" in text else "Apple"
    if not (invoice_date and order_number and document_number and amount):
        return {}
    return {
        "invoice_number": re.sub(r"\D", "", document_number),
        "invoice_code": order_number.strip(),
        "invoice_date": invoice_date,
        "seller": seller,
        "purchaser": purchaser,
        "amount": amount,
        "truth_type": "非目标公司发票",
        "category": "非目标公司发票",
    }


def parse_subject_invoice_notice(subject: str, target_company: str) -> dict:
    subject = unicodedata.normalize("NFKC", str(subject or ""))
    if "发票" not in subject:
        return {}
    seller = ""
    for pattern in (r"来自【(.+?)】", r"收到【(.+?)】", r"【电子发票】(.+?)（发票金额"):
        match = re.search(pattern, subject)
        if match:
            seller = match.group(1).strip()
            break
    amount_match = re.search(r"(?:价税合计金额为|发票金额[:：]?|金额[:：]?)([0-9]+(?:\.[0-9]{1,2})?)", subject)
    number_match = re.search(r"发票(?:号码|号)[:：]?(\d{8,20})", subject)
    purchaser_match = re.search(r"购方名称[:：]?([^\]\s]+)", subject)
    date_match = re.search(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})", subject)
    if not (seller and amount_match and number_match):
        return {}
    truth_type = truth_type_from_seller(seller)
    return {
        "invoice_number": number_match.group(1),
        "invoice_code": "",
        "invoice_date": norm_date(date_match.group(0) if date_match else ""),
        "seller": seller,
        "purchaser": purchaser_match.group(1).strip() if purchaser_match else target_company,
        "amount": norm_amount(amount_match.group(1)),
        "truth_type": truth_type,
        "category": truth_type,
    }


def parse_url_expected_fields(expected: dict, target_company: str) -> dict:
    if not expected:
        return {}
    seller = str(expected.get("seller") or "").strip()
    invoice_number = re.sub(r"\D", "", str(expected.get("invoice_number") or ""))
    invoice_date = norm_date(expected.get("invoice_date") or "")
    amount = norm_amount(expected.get("amount") or "")
    if not (seller and invoice_number and invoice_date and amount):
        return {}
    truth_type = truth_type_from_seller(seller)
    return {
        "invoice_number": invoice_number,
        "invoice_code": "",
        "invoice_date": invoice_date,
        "seller": seller,
        "purchaser": target_company,
        "amount": amount,
        "truth_type": truth_type,
        "category": truth_type,
    }


def fill_missing_date_from_external_evidence(fields: dict, evidence: dict) -> dict:
    if not fields or fields.get("invoice_date"):
        return fields
    invoice_number = re.sub(r"\D", "", str(fields.get("invoice_number") or ""))
    if not invoice_number:
        return fields
    external_text = "\n".join([
        evidence.get("body_text", ""),
        evidence.get("extra_text", ""),
    ])
    if invoice_number not in external_text:
        return fields
    date_match = re.search(r"(?:kprq|开票日期)[:=]?(20\d{6})", external_text)
    if not date_match:
        date_match = re.search(rf"{re.escape(invoice_number)}[^\n\r]{{0,120}}(20\d{{6}})", external_text)
    if date_match:
        patched = dict(fields)
        patched["invoice_date"] = norm_date(date_match.group(1))
        return patched
    return fields


def parse_email_truth_fields(email_row: dict, evidence: dict, expected: dict, target_company: str) -> tuple[dict, str]:
    text = evidence.get("body_text", "") if evidence else ""
    for engine, parser in (
        ("email_baiwang_body", lambda: parse_baiwang_email_text(text, target_company)),
        ("email_newtimeai_body", lambda: parse_newtimeai_email_text(text, target_company)),
        ("email_icloud_receipt_body", lambda: parse_icloud_receipt_text(text)),
        ("url_provider_expected_fields", lambda: parse_url_expected_fields(expected, target_company)),
        ("email_subject_invoice_notice", lambda: parse_subject_invoice_notice(email_row.get("subject", ""), target_company)),
    ):
        fields = parser()
        if fields:
            fields = fill_missing_date_from_external_evidence(fields, evidence or {})
            return fields, engine
    return {}, ""


def fetch_email_text_evidence(email_ids, source_root: Path, mailbox: str) -> dict:
    email_ids = sorted({str(item) for item in email_ids if str(item)})
    if not email_ids:
        return {}
    settings = UserSettingsStore().load()
    fetcher = EmailFetcher(
        settings["email"],
        settings["auth_code"],
        staging_dir=str(source_root / "email_evidence"),
        monitoring_dir=str(source_root / "monitoring"),
    )
    evidence = {}
    if not fetcher.connect():
        return evidence
    try:
        fetcher.mail.select(mailbox)
        for email_id in email_ids:
            raw_bytes = b""
            for mode_label, fetch_command in (("RFC822", "(RFC822)"), ("BODY.PEEK[]", "(BODY.PEEK[])")):
                raw_bytes, _attempt = fetcher._fetch_message_bytes(email_id.encode(), fetch_command, mode_label)
                if raw_bytes:
                    break
            if not raw_bytes:
                continue
            message = email.message_from_bytes(raw_bytes)
            subject = decode_str(message.get("Subject", ""))
            body_text, links = extract_email_body_and_links(message)
            evidence_dir = source_root / "email_evidence" / safe_name(f"{email_id}_{subject}", f"email_{email_id}")
            evidence_dir.mkdir(parents=True, exist_ok=True)
            raw_path = evidence_dir / f"{email_id}.eml"
            text_path = evidence_dir / f"{email_id}_body.txt"
            links_path = evidence_dir / f"{email_id}_links.json"
            raw_path.write_bytes(raw_bytes)
            text_path.write_text(normalized_lines(body_text), encoding="utf-8")
            write_json(links_path, [{"url": url, "text": text} for url, text in links])
            extra_paths = []
            extra_chunks = []
            for extra_path in (source_root / "email_evidence").glob(f"{email_id}_*"):
                if not extra_path.is_file():
                    continue
                if extra_path in {raw_path, text_path, links_path}:
                    continue
                try:
                    extra_chunks.append(extra_path.read_text(encoding="utf-8", errors="replace"))
                    extra_paths.append(str(extra_path))
                except Exception:
                    continue
            evidence[email_id] = {
                "raw_path": str(raw_path),
                "text_path": str(text_path),
                "links_path": str(links_path),
                "extra_paths": extra_paths,
                "extra_text": "\n".join(extra_chunks),
                "body_text": body_text,
                "links": links,
                "subject": subject,
            }
    finally:
        try:
            fetcher.close()
        except Exception:
            pass
    return evidence


def parse_pdf_local(extractor: InvoiceExtractor, path: Path, target_company: str) -> tuple[dict, str]:
    train_result = parse_train_ticket_pdf(path)
    if train_result:
        return train_result, "local_train_ticket_pdf"
    for parser_name, parser in (
        ("local_ride_itinerary_pdf", parse_ride_itinerary_pdf),
        ("local_marriott_folio_pdf", parse_marriott_folio_pdf),
        ("local_foreign_invoice_pdf", parse_foreign_invoice_pdf),
        ("local_cits_pdf", parse_cits_pdf),
    ):
        parsed = parser(path)
        if parsed and (parsed.get("amount") or parsed.get("invoice_number")):
            return parsed, parser_name
    companion_xml = parse_companion_xml_for_pdf(path)
    if companion_xml:
        return companion_xml, "companion_xml_for_pdf"
    for name in (
        "_try_extract_ihg_folio_from_pdf_text",
        "_try_extract_generic_hotel_folio_from_pdf_text",
        "_try_extract_standard_china_einvoice_from_pdf_text_v2",
        "_try_extract_standard_china_einvoice_from_pdf_text",
        "_try_extract_didi_invoice_from_pdf_text",
    ):
        try:
            result = getattr(extractor, name)(str(path))
        except Exception:
            result = None
        if result:
            return normalize_extractor_result(result), name
    loose_result = parse_loose_standard_einvoice_pdf(path, target_company)
    if loose_result:
        return loose_result, "local_loose_standard_einvoice_pdf"
    return {}, ""


def truth_type_from_fields(fields: dict, meta: dict, target_company: str) -> str:
    explicit = str(fields.get("truth_type") or fields.get("category") or "").strip()
    subject = str(meta.get("subject") or "")
    seller = str(fields.get("seller") or "")
    item_name = str(fields.get("item_name") or "")
    filename = str(meta.get("file_name") or "")
    if explicit:
        if explicit == "住宿":
            return "住宿发票"
        if explicit != "其他":
            return explicit
    joined = f"{subject} {seller} {item_name} {filename}"
    joined_lower = joined.lower()
    ride_platform_signal = (
        "滴滴" in joined
        or "didichuxing" in joined_lower
        or "高德打车" in joined
        or "打车电子发票" in joined
    )
    if ride_platform_signal:
        if not fields.get("invoice_number") and (
            "行程单" in filename or "报销单" in filename or "itinerary" in filename.lower()
        ):
            return "行程单"
        return "打车"
    if "水单" in joined or "folio" in joined.lower() or "结账单" in joined:
        return "住宿水单"
    if "行程单" in joined or "itinerary" in joined.lower() or "报销单" in joined:
        return "行程单"
    if any(token in joined for token in ["网络订餐配送费", "配送费", "信息系统增值服务"]):
        return explicit or "其他"
    if (
        ("12306" in joined or "rails.com.cn" in joined.lower() or "火车" in joined)
        and not any(token in joined for token in ["网络订餐", "配送费", "服务费", "信息系统增值服务"])
    ):
        return "火车票"
    seller_type = truth_type_from_seller(joined)
    if seller_type != "其他":
        return seller_type
    return explicit or "其他"


def expected_category_for(truth_type: str, purchaser: str, target_company: str) -> tuple[str, str]:
    if truth_type == "住宿水单":
        return "住宿水单", "住宿水单"
    if truth_type in {"行程单", "打车行程单"}:
        return truth_type, truth_type
    if target_company and purchaser and target_company not in purchaser:
        return "非目标公司发票", "非目标公司发票"
    if truth_type == "住宿":
        return "住宿发票", "住宿发票"
    if truth_type in {"住宿发票", "打车", "行程单", "火车票", "机票", "餐饮", "过路费"}:
        return truth_type, truth_type
    return truth_type or "其他", truth_type or "其他"


def document_role_for(truth_type: str, fields: dict, meta: dict) -> str:
    filename = str(meta.get("file_name") or "")
    if truth_type == "住宿水单":
        return "hotel_folio"
    if fields.get("invoice_number"):
        return "invoice"
    if "行程单" in truth_type or "itinerary" in filename.lower() or "报销单" in filename:
        return "itinerary"
    return "supporting_document"


def copy_evidence(paths, output_dir: Path):
    output_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    seen = set()
    for src in paths:
        src = Path(src)
        if not src.exists() or src in seen:
            continue
        seen.add(src)
        dst = unique_path(output_dir, src.name)
        shutil.copy2(src, dst)
        copied.append({
            "file_name": dst.name,
            "path": str(dst),
            "sha256": sha256_file(dst),
            "bytes": dst.stat().st_size,
        })
    return copied


def write_csv(path: Path, rows):
    keys = sorted({key for row in rows for key in row.keys()})
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def parent_has_primary_document(path: Path) -> bool:
    try:
        candidates = list(path.parent.iterdir())
    except Exception:
        return False
    for candidate in candidates:
        if candidate == path or not candidate.is_file():
            continue
        ext = effective_doc_ext(candidate)
        if ext == ".xml":
            return True
        if ext == ".pdf" and has_invoice_hint(candidate.name, path.parent.name):
            return True
    return False


def build_url_expected_by_email(url_rows: list[dict]) -> dict:
    by_email = {}
    for row in url_rows:
        email_id = str(row.get("email_id") or "")
        expected = ((row.get("decision") or {}).get("provider_expected_fields") or {})
        if not email_id or not expected:
            continue
        current = by_email.setdefault(email_id, {})
        for key, value in expected.items():
            if value and not current.get(key):
                current[key] = value
    return by_email


def infer_date_from_metadata(meta: dict) -> str:
    joined = " ".join(str(meta.get(key) or "") for key in ("subject", "file_name", "path"))
    full_date = norm_date(joined)
    if full_date:
        return full_date
    month_day = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})日", joined)
    mail_date = norm_date(meta.get("mail_date_local") or "")
    if month_day and mail_date:
        year = mail_date[:4]
        return f"{year}-{int(month_day.group(1)):02d}-{int(month_day.group(2)):02d}"
    return ""


def row_in_truth_window(row: dict, date_from: str, before_exclusive: str) -> bool:
    if not date_from and not before_exclusive:
        return True
    value = str(row.get("mail_date_local") or "").strip()
    if not value:
        return False
    parsed = None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            parsed = dt.datetime.strptime(value[:19] if fmt.endswith("%S") else value[:10], fmt)
            break
        except ValueError:
            continue
    if parsed is None:
        return False
    if date_from:
        since_dt = dt.datetime.strptime(date_from, "%Y-%m-%d")
        if parsed < since_dt:
            return False
    if before_exclusive:
        before_dt = dt.datetime.strptime(before_exclusive, "%Y-%m-%d")
        if parsed >= before_dt:
            return False
    return True


def build_truth(args, source_root: Path, output_root: Path):
    settings = UserSettingsStore().load()
    target_company = str(settings.get("company") or "").strip()
    doc_rows = read_jsonl(source_root / "document_index.jsonl")
    link_rows = read_jsonl(source_root / "link_downloads.jsonl")
    url_rows = read_jsonl(source_root / "url_candidates.jsonl")
    mailbox_rows = read_jsonl(source_root / "mailbox_inventory.jsonl")
    doc_rows = [row for row in doc_rows if row_in_truth_window(row, args.date_from, args.before_exclusive)]
    link_rows = [row for row in link_rows if row_in_truth_window(row, args.date_from, args.before_exclusive)]
    url_rows = [row for row in url_rows if row_in_truth_window(row, args.date_from, args.before_exclusive)]
    mailbox_rows = [row for row in mailbox_rows if row_in_truth_window(row, args.date_from, args.before_exclusive)]
    doc_rows = enrich_doc_rows_from_link_downloads(doc_rows, link_rows)
    url_expected_by_email = build_url_expected_by_email(url_rows)
    output_raw = output_root / "raw_documents"
    extractor = InvoiceExtractor(api_key="", output_dir=str(output_root / "_extractor_tmp"))
    parsed = []
    pending = []
    excluded = []
    sibling_exts_by_stem = {}
    primary_doc_email_ids = set()
    for row in doc_rows:
        path_value = row.get("path")
        if not path_value:
            continue
        path = Path(path_value)
        ext = effective_doc_ext(path)
        sibling_exts_by_stem.setdefault((str(path.parent), path.stem), set()).add(ext)
        if ext in {".pdf", ".xml"}:
            primary_doc_email_ids.add(str(row.get("email_id") or row.get("source_email_id") or ""))

    link_failures = [
        row for row in link_rows
        if row.get("status") == "failed" and has_invoice_hint(
            row.get("subject"), row.get("source_url"), row.get("reason_code")
        )
        and "ofd_read.zip" not in str(row.get("source_url") or "").lower()
    ]
    for row in link_failures:
        pending.append({
            "source_email_id": str(row.get("email_id", "")),
            "mail_date_local": row.get("mail_date_local", ""),
            "subject": row.get("subject", ""),
            "sender": row.get("sender", ""),
            "source_kind": "url",
            "source_url": row.get("source_url", ""),
            "reason": "invoice_link_download_not_confirmed",
            "status": row.get("status", ""),
            "reason_code": row.get("reason_code", ""),
        })

    for doc in doc_rows:
        path = Path(doc.get("path", ""))
        if not path.exists():
            continue
        ext = effective_doc_ext(path)
        fields = {}
        engine = ""
        if ext == ".xml":
            try:
                fields = parse_generic_xml(path)
                engine = "xml"
            except Exception as exc:
                pending.append({**doc, "reason": "xml_parse_failed", "error": str(exc)})
                continue
        elif ext == ".pdf":
            fields, engine = parse_pdf_local(extractor, path, target_company)
            if not fields:
                if has_invoice_hint(path.name, doc.get("subject"), doc.get("sender")):
                    pending.append({**doc, "reason": "pdf_parse_pending_review"})
                else:
                    excluded.append({**doc, "reason": "pdf_without_invoice_signal"})
                continue
        elif ext in {".jpg", ".jpeg", ".png", ".ofd"}:
            email_id = str(doc.get("email_id") or doc.get("source_email_id") or "")
            if parent_has_primary_document(path) or email_id in primary_doc_email_ids:
                excluded.append({**doc, "reason": "companion_image_or_ofd_covered_by_primary_document"})
                continue
            if has_invoice_hint(path.name, doc.get("subject"), doc.get("sender")):
                pending.append({**doc, "reason": f"{ext.lstrip('.')}_requires_manual_or_converter_review"})
            else:
                excluded.append({**doc, "reason": "non_invoice_image_or_ofd"})
            continue
        else:
            continue

        if not fields.get("amount") and not fields.get("invoice_number") and not fields.get("seller"):
            if has_invoice_hint(path.name, doc.get("subject"), doc.get("sender")):
                pending.append({**doc, "reason": "parsed_without_required_invoice_fields", "engine": engine})
            else:
                excluded.append({**doc, "reason": "no_invoice_fields"})
            continue

        truth_type = truth_type_from_fields(fields, doc, target_company)
        truth_type, expected_category = expected_category_for(truth_type, fields.get("purchaser", ""), target_company)
        role = document_role_for(truth_type, fields, doc)
        if not fields.get("invoice_date") and engine == "local_cits_pdf":
            fields["invoice_date"] = infer_date_from_metadata(doc)
        if str(doc.get("subject") or "").find("取消") >= 0 and norm_amount(fields.get("amount")) == "0.00":
            excluded.append({**doc, "reason": "zero_amount_cancellation_notice", "parsed_fields": fields, "parse_engine": engine})
            continue
        if not norm_amount(fields.get("amount")) or not is_valid_iso_date(fields.get("invoice_date")):
            pending.append({**doc, "reason": "parsed_missing_required_truth_fields", "parsed_fields": fields, "parse_engine": engine})
            continue
        parsed.append({
            **doc,
            **fields,
            "invoice_date": norm_date(fields.get("invoice_date")),
            "amount": norm_amount(fields.get("amount")),
            "truth_type": truth_type,
            "expected_category": expected_category,
            "document_role": role,
            "parse_engine": engine,
        })

    parsed_email_ids = {str(row.get("email_id") or row.get("source_email_id") or "") for row in parsed}
    email_candidates = [
        row for row in mailbox_rows
        if str(row.get("email_id") or "")
        and str(row.get("email_id") or "") not in parsed_email_ids
        and has_invoice_hint(row.get("subject"), row.get("sender"))
    ]
    email_evidence = fetch_email_text_evidence([row.get("email_id") for row in email_candidates], source_root, args.mailbox)
    for email_row in email_candidates:
        email_id = str(email_row.get("email_id") or "")
        evidence = email_evidence.get(email_id, {})
        fields, engine = parse_email_truth_fields(
            email_row,
            evidence,
            url_expected_by_email.get(email_id, {}),
            target_company,
        )
        if not fields:
            continue
        if not norm_amount(fields.get("amount")) or not is_valid_iso_date(fields.get("invoice_date")):
            pending.append({
                "source_email_id": email_id,
                "mail_date_local": email_row.get("mail_date_local", ""),
                "subject": email_row.get("subject", ""),
                "sender": email_row.get("sender", ""),
                "source_kind": "email",
                "source_url": "",
                "reason": "email_parsed_missing_required_truth_fields",
                "parsed_fields": fields,
                "parse_engine": engine,
                "evidence_path": evidence.get("text_path", ""),
            })
            continue
        truth_type = truth_type_from_fields(fields, email_row, target_company)
        truth_type, expected_category = expected_category_for(truth_type, fields.get("purchaser", ""), target_company)
        evidence_path = evidence.get("text_path") or ""
        if not evidence_path:
            continue
        source_url = ""
        for url, text in evidence.get("links", []):
            if "发票" in text or "baiwang" in url.lower() or "51fapiao" in url.lower() or "icloud-efapiao" in url.lower():
                source_url = url
                break
        parsed.append({
            **email_row,
            **fields,
            "path": evidence_path,
            "file_name": Path(evidence_path).name,
            "source_kind": "email",
            "source_url": source_url,
            "invoice_date": norm_date(fields.get("invoice_date")),
            "amount": norm_amount(fields.get("amount")),
            "truth_type": truth_type,
            "expected_category": expected_category,
            "document_role": document_role_for(truth_type, fields, {"file_name": Path(evidence_path).name}),
            "parse_engine": engine,
            "extra_evidence_paths": evidence.get("extra_paths", []),
        })

    parsed_email_ids = {str(row.get("email_id") or row.get("source_email_id") or "") for row in parsed}
    parsed_subjects = {str(row.get("subject") or "").strip() for row in parsed if row.get("subject")}
    evidence_email_ids = {str(row.get("email_id") or row.get("source_email_id") or "") for row in doc_rows}
    for email_row in mailbox_rows:
        email_id = str(email_row.get("email_id") or "")
        if not email_id or email_id in parsed_email_ids:
            continue
        if email_id in {str(item.get("source_email_id") or item.get("email_id") or "") for item in pending}:
            continue
        if str(email_row.get("subject") or "").strip() in parsed_subjects:
            continue
        if has_invoice_hint(email_row.get("subject"), email_row.get("sender")):
            pending.append({
                "source_email_id": email_id,
                "mail_date_local": email_row.get("mail_date_local", ""),
                "subject": email_row.get("subject", ""),
                "sender": email_row.get("sender", ""),
                "source_kind": "email",
                "source_url": "",
                "reason": "invoice_hint_email_without_parsed_document",
                "attachment_count": len(email_row.get("attachments") or []),
                "link_count": email_row.get("link_count", 0),
            })

    covered_pending_reasons = {
        "invoice_link_download_not_confirmed",
        "invoice_hint_email_without_parsed_document",
        "jpg_requires_manual_or_converter_review",
        "jpeg_requires_manual_or_converter_review",
        "png_requires_manual_or_converter_review",
        "ofd_requires_manual_or_converter_review",
    }
    filtered_pending = []
    for item in pending:
        email_id = str(item.get("email_id") or item.get("source_email_id") or "")
        reason = item.get("reason", "")
        if email_id in parsed_email_ids and reason in covered_pending_reasons:
            excluded.append({**item, "reason": f"covered_by_parsed_truth:{reason}"})
            continue
        filtered_pending.append(item)
    pending = filtered_pending

    parsed.sort(key=lambda row: (
        row.get("invoice_date") or "9999-99-99",
        row.get("source_email_id") or row.get("email_id") or "",
        row.get("invoice_number") or "",
        row.get("file_name") or "",
    ))

    included = []
    seen_invoice_numbers = {}
    seen_hashes = set()
    type_counts = {}
    for row in parsed:
        row_path = Path(row["path"])
        sha = row.get("sha256") or sha256_file(row_path)
        invoice_number = str(row.get("invoice_number") or "").strip()
        role = row.get("document_role", "")
        if invoice_number and role == "invoice":
            if invoice_number in seen_invoice_numbers:
                excluded.append({**row, "reason": "duplicate_invoice_number", "duplicate_of": seen_invoice_numbers[invoice_number]})
                continue
        elif sha in seen_hashes:
            excluded.append({**row, "reason": "duplicate_sha256"})
            continue

        type_counts[row["truth_type"]] = type_counts.get(row["truth_type"], 0) + 1
        prefix_date = compact_date(row.get("invoice_date")) or "unknown-date"
        slug_source = re.sub(r"\W+", "-", str(row.get("seller") or row.get("file_name") or "doc"))[:24].strip("-")
        truth_id = f"qq-{prefix_date}-{row['truth_type']}-{type_counts[row['truth_type']]:03d}-{slug_source or 'doc'}"
        evidence_paths = [row_path]
        for extra_path in row.get("extra_evidence_paths") or []:
            evidence_paths.append(Path(extra_path))
        if str(row.get("source_kind") or "") == "email":
            for candidate in row_path.parent.iterdir():
                if candidate.suffix.lower() in {".eml", ".json"}:
                    evidence_paths.append(candidate)
        evidence_paths.extend(companion_evidence_paths_for_primary(row_path, invoice_number))
        evidence = copy_evidence(evidence_paths, output_raw / truth_id)
        primary = next((item for item in evidence if item["file_name"] == row_path.name), evidence[0])
        included_row = {
            "truth_id": truth_id,
            "truth_status": "included",
            "source_email_id": str(row.get("email_id") or row.get("source_email_id") or ""),
            "source_uid": "",
            "mail_date_local": row.get("mail_date_local", ""),
            "source_kind": row.get("source_kind", ""),
            "source_url": row.get("source_url", ""),
            "subject": row.get("subject", ""),
            "sender": row.get("sender", ""),
            "file_name": row_path.name,
            "document_role": row.get("document_role", ""),
            "truth_type": row.get("truth_type", ""),
            "expected_category": row.get("expected_category", ""),
            "invoice_date": norm_date(row.get("invoice_date", "")),
            "seller": row.get("seller", ""),
            "purchaser": row.get("purchaser", ""),
            "amount": norm_amount(row.get("amount", "")),
            "invoice_number": invoice_number,
            "invoice_code": row.get("invoice_code", ""),
            "sha256": primary["sha256"],
            "raw_path": primary["path"],
            "evidence": evidence,
            "evidence_trace": [
                f"Parsed from {row.get('parse_engine')}",
                "Truth source is mailbox raw evidence, not app archive output.",
            ],
        }
        included.append(included_row)
        seen_hashes.add(sha)
        if invoice_number and role == "invoice":
            seen_invoice_numbers[invoice_number] = truth_id

    manifest = {
        "summary": {
            "dataset": output_root.name,
            "date_from": args.date_from,
            "date_to": args.date_to,
            "before_exclusive": args.before_exclusive,
            "mailbox": args.mailbox,
            "account_domain": settings.get("email", "").split("@")[-1] if "@" in settings.get("email", "") else "",
            "target_company": target_company,
            "included_count": len(included),
            "excluded_count": len(excluded),
            "pending_review_count": len(pending),
            "finalized": len(pending) == 0,
            "build_time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "included": included,
        "excluded": excluded,
        "pending_review": pending,
    }
    write_json(output_root / "truth_manifest.json", manifest)
    write_csv(output_root / "truth_included.csv", included)
    write_csv(output_root / "truth_pending_review.csv", pending)
    write_json(output_root / "truth_build_summary.json", manifest["summary"])
    return manifest


def main():
    parser = argparse.ArgumentParser(description="Collect QQ mailbox evidence and build a long-window invoice truth manifest.")
    parser.add_argument("--date-from", required=True)
    parser.add_argument("--date-to", required=True)
    parser.add_argument("--before-exclusive", default="")
    parser.add_argument("--mailbox", default="INBOX")
    parser.add_argument("--source-root", required=True)
    parser.add_argument("--output-root", required=True)
    parser.add_argument("--skip-collect", action="store_true")
    args = parser.parse_args()

    if not args.before_exclusive:
        args.before_exclusive = (dt.datetime.strptime(args.date_to, "%Y-%m-%d").date() + dt.timedelta(days=1)).isoformat()

    source_root = Path(args.source_root).resolve()
    output_root = Path(args.output_root).resolve()
    source_root.mkdir(parents=True, exist_ok=True)
    output_root.mkdir(parents=True, exist_ok=True)
    write_json(source_root / "truth_collection_config.json", vars(args))
    if not args.skip_collect:
        inventory = collect_raw_evidence(args, source_root)
        print(json.dumps({"event": "collected", **{k: inventory[k] for k in ["email_count", "document_count", "url_candidate_count", "link_download_count"]}}, ensure_ascii=False), flush=True)
    manifest = build_truth(args, source_root, output_root)
    print(json.dumps({"event": "truth_built", **manifest["summary"]}, ensure_ascii=False, indent=2), flush=True)
    return 0 if manifest["summary"]["finalized"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
