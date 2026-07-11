from __future__ import annotations

import datetime as dt
import email
import email.utils
import hashlib
import re
from dataclasses import dataclass
from typing import Callable, Iterable
from zoneinfo import ZoneInfo


SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
_UID_PATTERN = re.compile(rb"\bUID\s+(\d+)\b", flags=re.IGNORECASE)
_INTERNALDATE_PATTERN = re.compile(
    rb'\bINTERNALDATE\s+"([^"]+)"', flags=re.IGNORECASE
)


class MailboxScanError(RuntimeError):
    """Raised when mailbox scope cannot be established without risking P0."""


@dataclass(frozen=True)
class MessageRef:
    uid: bytes
    message_date: dt.datetime | None
    internal_date: dt.datetime | None


class MailboxScanner:
    HEADER_QUERY = "(UID INTERNALDATE BODY.PEEK[HEADER.FIELDS (DATE)])"
    MESSAGE_QUERY = "(UID RFC822)"

    def __init__(
        self,
        mail,
        *,
        header_batch_size: int = 200,
        body_batch_size: int = 25,
        max_attempts: int = 2,
        diagnostic_callback: Callable[[dict], None] | None = None,
    ):
        if header_batch_size < 1 or body_batch_size < 1 or max_attempts < 1:
            raise ValueError("batch sizes and max_attempts must be positive")
        self.mail = mail
        self.header_batch_size = header_batch_size
        self.body_batch_size = body_batch_size
        self.max_attempts = max_attempts
        self.diagnostic_callback = diagnostic_callback

    def scan(
        self,
        since: dt.date,
        before: dt.date | None,
        mailbox: str = "INBOX",
    ) -> list[MessageRef]:
        since = self._require_date(since, "since")
        before = self._require_date(before, "before", allow_none=True)
        if before is not None and before <= since:
            raise ValueError("before must be after since")

        try:
            status, _ = self.mail.select(mailbox, readonly=True)
        except Exception as exc:
            raise MailboxScanError("IMAP SELECT failed") from exc
        if not self._is_ok(status):
            raise MailboxScanError("IMAP SELECT failed")

        uids = self._search_all_uids()
        headers: dict[bytes, MessageRef] = {}
        for chunk in self._chunks(uids, self.header_batch_size):
            headers.update(self._fetch_header_subset(chunk))

        retained = []
        for uid in uids:
            ref = headers.get(uid, MessageRef(uid, None, None))
            effective_date = ref.message_date or ref.internal_date
            if effective_date is None:
                retained.append(ref)
                continue
            local_day = effective_date.astimezone(SHANGHAI_TZ).date()
            if local_day < since or (before is not None and local_day >= before):
                continue
            retained.append(ref)
        self._emit("imap_scan_complete", searched=len(uids), retained=len(retained))
        return retained

    def fetch_messages(
        self, uids: Iterable[bytes], query: str | None = None
    ) -> dict[bytes, bytes]:
        ordered_uids = self._stable_uids(uids)
        query = self._ensure_uid_query(query or self.MESSAGE_QUERY)
        fetched: dict[bytes, bytes] = {}
        for chunk in self._chunks(ordered_uids, self.body_batch_size):
            fetched.update(self._fetch_message_subset(chunk, query))
        return {uid: fetched[uid] for uid in ordered_uids if uid in fetched}

    def _search_all_uids(self) -> list[bytes]:
        try:
            status, response = self.mail.uid("SEARCH", None, "ALL")
        except Exception as exc:
            raise MailboxScanError("UID SEARCH ALL failed") from exc
        if not self._is_ok(status):
            raise MailboxScanError("UID SEARCH ALL failed")

        tokens = []
        for item in response if isinstance(response, (list, tuple)) else [response]:
            if isinstance(item, bytearray):
                item = bytes(item)
            if not isinstance(item, bytes):
                continue
            tokens.extend(re.findall(rb"\b\d+\b", item))
        return self._stable_uids(tokens)

    def _fetch_header_subset(self, uids: list[bytes]) -> dict[bytes, MessageRef]:
        if not uids:
            return {}
        response = self._uid_fetch_with_retry(uids, self.HEADER_QUERY, "header")
        parsed = self._parse_headers(response, set(uids)) if response is not None else {}
        missing = [uid for uid in uids if uid not in parsed]
        if not missing:
            return parsed
        if len(uids) == 1:
            self._emit_failed("header", uids)
            parsed[uids[0]] = MessageRef(uids[0], None, None)
            return parsed
        for subset in self._split(missing):
            parsed.update(self._fetch_header_subset(subset))
        return parsed

    def _fetch_message_subset(self, uids: list[bytes], query: str) -> dict[bytes, bytes]:
        if not uids:
            return {}
        response = self._uid_fetch_with_retry(uids, query, "message")
        parsed = self._parse_payloads(response, set(uids)) if response is not None else {}
        missing = [uid for uid in uids if uid not in parsed]
        if not missing:
            return parsed
        if len(uids) == 1:
            self._emit_failed("message", uids)
            return parsed
        for subset in self._split(missing):
            parsed.update(self._fetch_message_subset(subset, query))
        return parsed

    @staticmethod
    def _ensure_uid_query(query: str) -> str:
        normalized = str(query or "").strip()
        if not normalized.startswith("(") or not normalized.endswith(")"):
            raise ValueError("FETCH query must be parenthesized")
        inner = normalized[1:-1].strip()
        if re.search(r"(?:^|\s)UID(?:\s|$)", inner, flags=re.IGNORECASE):
            return normalized
        return f"(UID {inner})"

    def _uid_fetch_with_retry(self, uids: list[bytes], query: str, purpose: str):
        uid_set = b",".join(uids)
        last_error_type = "malformed_response"
        for _attempt in range(1, self.max_attempts + 1):
            try:
                status, response = self.mail.uid("FETCH", uid_set, query)
                if self._is_ok(status) and isinstance(response, (list, tuple)):
                    return response
                last_error_type = "fetch_status"
            except Exception as exc:
                last_error_type = type(exc).__name__
        self._emit(
            "imap_fetch_retry_exhausted",
            purpose=purpose,
            batch_size=len(uids),
            attempts=self.max_attempts,
            reason=last_error_type,
        )
        return None

    @classmethod
    def _parse_headers(cls, response, requested: set[bytes]) -> dict[bytes, MessageRef]:
        parsed = {}
        for metadata, payload in cls._response_tuples(response):
            uid = cls._metadata_uid(metadata)
            if uid not in requested or uid in parsed:
                continue
            internal_date = cls._metadata_internal_date(metadata)
            message_date = None
            try:
                header = email.message_from_bytes(payload)
                raw_date = header.get("Date")
                if raw_date:
                    message_date = cls._to_shanghai(email.utils.parsedate_to_datetime(raw_date))
            except Exception:
                message_date = None
            parsed[uid] = MessageRef(uid, message_date, internal_date)
        return parsed

    @classmethod
    def _parse_payloads(cls, response, requested: set[bytes]) -> dict[bytes, bytes]:
        parsed = {}
        for metadata, payload in cls._response_tuples(response):
            uid = cls._metadata_uid(metadata)
            if uid not in requested or uid in parsed or not payload:
                continue
            parsed[uid] = payload
        return parsed

    @staticmethod
    def _response_tuples(response):
        if not isinstance(response, (list, tuple)):
            return
        for item in response:
            if not isinstance(item, tuple) or len(item) < 2:
                continue
            metadata, payload = item[0], item[1]
            if isinstance(metadata, bytearray):
                metadata = bytes(metadata)
            if isinstance(payload, bytearray):
                payload = bytes(payload)
            if isinstance(metadata, bytes) and isinstance(payload, bytes):
                yield metadata, payload

    @staticmethod
    def _metadata_uid(metadata: bytes) -> bytes:
        match = _UID_PATTERN.search(metadata)
        return match.group(1) if match else b""

    @classmethod
    def _metadata_internal_date(cls, metadata: bytes) -> dt.datetime | None:
        match = _INTERNALDATE_PATTERN.search(metadata)
        if not match:
            return None
        try:
            parsed = email.utils.parsedate_to_datetime(match.group(1).decode("ascii"))
        except Exception:
            return None
        return cls._to_shanghai(parsed)

    @staticmethod
    def _to_shanghai(value: dt.datetime) -> dt.datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TZ)
        return value.astimezone(SHANGHAI_TZ)

    @staticmethod
    def _require_date(value, name, allow_none=False):
        if value is None and allow_none:
            return None
        if isinstance(value, dt.datetime) or not isinstance(value, dt.date):
            raise TypeError(f"{name} must be a date")
        return value

    @staticmethod
    def _is_ok(status) -> bool:
        if isinstance(status, bytes):
            status = status.decode("ascii", errors="ignore")
        return str(status or "").casefold() == "ok"

    @staticmethod
    def _stable_uids(uids: Iterable[bytes]) -> list[bytes]:
        result = []
        seen = set()
        for uid in uids:
            if isinstance(uid, bytearray):
                uid = bytes(uid)
            elif isinstance(uid, str):
                uid = uid.encode("ascii", errors="ignore")
            if not isinstance(uid, bytes) or not uid.isdigit() or uid in seen:
                continue
            seen.add(uid)
            result.append(uid)
        return result

    @staticmethod
    def _chunks(values: list[bytes], size: int):
        for start in range(0, len(values), size):
            yield values[start : start + size]

    @staticmethod
    def _split(values: list[bytes]):
        midpoint = max(1, len(values) // 2)
        return [values[:midpoint], values[midpoint:]] if len(values) > 1 else [values]

    def _emit_failed(self, purpose: str, uids: list[bytes]):
        digest = hashlib.sha256(b",".join(uids)).hexdigest()[:12]
        self._emit(
            "imap_fetch_failed",
            purpose=purpose,
            batch_size=len(uids),
            uid_set_hash=digest,
        )

    def _emit(self, event: str, **fields):
        if self.diagnostic_callback is None:
            return
        try:
            self.diagnostic_callback({"event": event, **fields})
        except Exception:
            pass
