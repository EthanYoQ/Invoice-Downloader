import datetime as dt
from email.message import EmailMessage

import pytest

from mailbox_scanner import MailboxScanError, MailboxScanner


def _header_part(sequence, uid, date_header="", internaldate="10-Jun-2026 10:00:00 +0800"):
    metadata = f'{sequence} (UID {uid} INTERNALDATE "{internaldate}" BODY[HEADER.FIELDS (DATE)]'.encode()
    payload = (f"Date: {date_header}\r\n" if date_header else "") + "\r\n"
    return metadata, payload.encode()


def _message(uid):
    message = EmailMessage()
    message["From"] = "billing@example.com"
    message["To"] = "invoice-user@example.com"
    message["Subject"] = f"Invoice {uid}"
    message.set_content(f"message-{uid}")
    return message.as_bytes()


class FakeUidMail:
    def __init__(self, search_uids, fetch_handler):
        self.search_uids = search_uids
        self.fetch_handler = fetch_handler
        self.uid_calls = []
        self.selected = None

    def select(self, mailbox, readonly=True):
        self.selected = (mailbox, readonly)
        return "ok", [b""]

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command.upper() == "SEARCH":
            return "ok", [self.search_uids]
        return self.fetch_handler(*args)


def test_scan_uses_uid_all_stable_dedupe_and_never_sequence_ids():
    def fetch(uid_set, _query):
        assert uid_set == b"91,7,105"
        return "ok", [
            b"interleaved noise",
            _header_part(2, 7, "Wed, 10 Jun 2026 09:00:00 +0800"),
            _header_part(1, 91, "Wed, 10 Jun 2026 08:00:00 +0800"),
            _header_part(3, 105, "Wed, 10 Jun 2026 10:00:00 +0800"),
            _header_part(9, 7, "Wed, 10 Jun 2026 11:00:00 +0800"),
            b")",
        ]

    mail = FakeUidMail(b"91 7 91 105", fetch)
    refs = MailboxScanner(mail).scan(dt.date(2026, 6, 10), dt.date(2026, 6, 11))

    assert mail.selected == ("INBOX", True)
    assert mail.uid_calls[0] == ("SEARCH", (None, "ALL"))
    assert [ref.uid for ref in refs] == [b"91", b"7", b"105"]


def test_scan_normalizes_shanghai_falls_back_and_retains_unknown_without_early_stop():
    def fetch(_uid_set, _query):
        return "OK", [
            _header_part(1, 1, "Wed, 04 Feb 2026 01:00:00 +0800"),
            _header_part(2, 2, "Wed, 04 Feb 2026 14:32:39 -0500"),
            _header_part(3, 3, "", "10-Jun-2026 10:00:00 +0800"),
            _header_part(4, 4, "not-a-date", "not-a-date"),
            _header_part(5, 5, "Mon, 15 Jun 2026 10:00:00 +0800"),
            _header_part(6, 6, "Wed, 10 Jun 2026 10:00:00 +0800"),
        ]

    refs = MailboxScanner(FakeUidMail(b"1 2 3 4 5 6", fetch)).scan(
        dt.date(2026, 2, 5), dt.date(2026, 6, 11)
    )

    assert [ref.uid for ref in refs] == [b"2", b"3", b"4", b"6"]
    assert refs[0].message_date.isoformat() == "2026-02-05T03:32:39+08:00"
    assert refs[1].message_date is None
    assert refs[1].internal_date.isoformat() == "2026-06-10T10:00:00+08:00"
    assert refs[2].message_date is None and refs[2].internal_date is None


def test_scan_recursively_splits_failed_and_malformed_header_batches():
    calls = []

    def fetch(uid_set, _query):
        calls.append(uid_set)
        if uid_set == b"1,2,3,4":
            return "NO", [b"temporary"]
        if uid_set == b"1,2":
            return "ok", [_header_part(1, 1), (b"2 (BODY[]", b"\r\n")]
        if uid_set == b"2":
            return "ok", [_header_part(22, 2)]
        return "ok", [_header_part(index, int(uid)) for index, uid in enumerate(uid_set.split(b","), 1)]

    refs = MailboxScanner(FakeUidMail(b"1 2 3 4", fetch), max_attempts=1).scan(
        dt.date(2026, 6, 1), dt.date(2026, 6, 14)
    )

    assert [ref.uid for ref in refs] == [b"1", b"2", b"3", b"4"]
    assert calls == [b"1,2,3,4", b"1,2", b"2", b"3,4"]


def test_scan_keeps_single_uid_unknown_after_bounded_fetch_failure():
    events = []

    def fetch(uid_set, _query):
        assert uid_set == b"8"
        raise OSError("socket failed for secret@example.com")

    refs = MailboxScanner(
        FakeUidMail(b"8", fetch), max_attempts=2, diagnostic_callback=events.append
    ).scan(dt.date(2026, 6, 1), dt.date(2026, 6, 14))

    assert [ref.uid for ref in refs] == [b"8"]
    assert len([event for event in events if event["event"] == "imap_fetch_failed"]) == 1
    assert "secret@example.com" not in repr(events)


def test_scan_raises_on_search_failure_instead_of_claiming_empty_mailbox():
    class SearchFailureMail(FakeUidMail):
        def uid(self, command, *args):
            if command.upper() == "SEARCH":
                return "NO", [b"authentication details"]
            raise AssertionError("FETCH must not run")

    with pytest.raises(MailboxScanError, match="UID SEARCH"):
        MailboxScanner(SearchFailureMail(b"", None)).scan(dt.date(2026, 6, 1), None)


def test_search_accepts_multiple_chunks_none_and_interleaved_entries_stably():
    def fetch(uid_set, _query):
        return "ok", [
            _header_part(index, int(uid))
            for index, uid in enumerate(uid_set.split(b","), 1)
        ]

    mail = FakeUidMail(b"unused", fetch)

    def uid(command, *args):
        mail.uid_calls.append((command, args))
        if command.upper() == "SEARCH":
            return "ok", [b"91 7", None, (b"ignored",), bytearray(b"91 105"), b""]
        return fetch(*args)

    mail.uid = uid
    refs = MailboxScanner(mail).scan(dt.date(2026, 6, 1), dt.date(2026, 6, 14))

    assert [ref.uid for ref in refs] == [b"91", b"7", b"105"]


def test_empty_search_is_empty_but_nonempty_malformed_ok_response_fails_closed():
    empty = FakeUidMail(b"", lambda *_args: (_ for _ in ()).throw(AssertionError("no FETCH")))
    assert MailboxScanner(empty).scan(dt.date(2026, 6, 1), None) == []

    malformed = FakeUidMail(
        b"not-a-uid response",
        lambda *_args: (_ for _ in ()).throw(AssertionError("no FETCH")),
    )
    with pytest.raises(MailboxScanError, match="malformed UID SEARCH"):
        MailboxScanner(malformed).scan(dt.date(2026, 6, 1), None)


def test_iter_message_batches_caps_buffer_at_25_and_maps_each_uid_once():
    messages = {str(uid).encode(): _message(uid) for uid in range(1, 206)}
    calls = []

    def fetch(uid_set, _query):
        calls.append(uid_set)
        requested = uid_set.split(b",")
        parts = []
        for sequence, uid in enumerate(reversed(requested), 900):
            if uid == b"77" and len(requested) > 1:
                parts.append((f"{sequence} (RFC822".encode(), messages[uid]))
                continue
            parts.extend([b"noise", (f"{sequence} (UID {uid.decode()} RFC822".encode(), messages[uid])])
        return "ok", parts

    events = []
    mail = FakeUidMail(b"", fetch)
    scanner = MailboxScanner(
        mail,
        body_batch_size=25,
        max_attempts=1,
        diagnostic_callback=events.append,
    )
    seen = []
    batch_sizes = []
    for batch in scanner.iter_message_batches(list(messages)):
        batch_sizes.append(len(batch.messages))
        seen.extend(uid for uid, _payload in batch.messages)

    assert seen == list(messages)
    assert max(batch_sizes) <= 25
    assert all(len(call.split(b",")) <= 25 for call in calls)
    assert sum(b"77" in call.split(b",") for call in calls) == 2
    assert len(calls) == 10
    buffered = [event["buffered_count"] for event in events if event["event"] == "imap_message_batch_ready"]
    assert buffered and max(buffered) <= 25


def test_fetch_messages_deduplicates_input_and_rejects_payload_without_uid_metadata():
    calls = []

    def fetch(uid_set, _query):
        calls.append(uid_set)
        if uid_set == b"10,11":
            return "ok", [(b"1 (RFC822", _message(10)), (b"2 (UID 11 RFC822", _message(11))]
        if uid_set == b"10":
            return "ok", [(b"9 (UID 10 RFC822", _message(10))]
        raise AssertionError(uid_set)

    fetched = MailboxScanner(FakeUidMail(b"", fetch), max_attempts=1).fetch_messages(
        [b"10", b"11", b"10"]
    )

    assert list(fetched) == [b"10", b"11"]
    assert calls == [b"10,11", b"10"]
