import tempfile
import unittest
from email.message import EmailMessage
from pathlib import Path
import pytest

import email_fetcher as email_fetcher_module
from email_fetcher import EmailFetcher
from mailbox_scanner import MailboxScanError, UnresolvedMailboxInputError


def build_attachment_message(*, sender, subject, body, filename, payload=b"%PDF-1.4\n"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "invoice-user@example.com"
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


class FakeUidMail:
    def __init__(self, messages):
        self.messages = dict(messages)
        self.ids = list(messages)
        self.uid_calls = []

    def select(self, mailbox, readonly=True):
        self.selected = (mailbox, readonly)
        return "ok", [b""]

    def search(self, *_args):
        raise AssertionError("sequence SEARCH must not be used")

    def fetch(self, *_args):
        raise AssertionError("sequence FETCH must not be used")

    def uid(self, command, *args):
        self.uid_calls.append((command, args))
        if command.upper() == "SEARCH":
            return "ok", [b" ".join(self.ids)]
        uid_set, query = args
        requested = uid_set.split(b",")
        if "HEADER.FIELDS" in query:
            parts = []
            for sequence, uid in enumerate(reversed(requested), 700):
                value = int(uid)
                if value == 5:
                    date_header = "Date: Mon, 15 Jun 2026 10:00:00 +0800\r\n\r\n"
                    internaldate = "15-Jun-2026 10:00:00 +0800"
                elif value == 7:
                    date_header = "\r\n"
                    internaldate = "21-Feb-2025 10:00:00 +0800"
                elif value == 8:
                    date_header = "\r\n"
                    internaldate = "10-Jun-2026 10:00:00 +0800"
                elif value == 9:
                    date_header = "Date: not-a-real-date\r\n\r\n"
                    internaldate = "not-a-real-date"
                elif value == 10:
                    date_header = "Date: 04 Feb 2026 14:32:39 -0500\r\n\r\n"
                    internaldate = "04-Feb-2026 14:32:39 -0500"
                else:
                    date_header = "Date: Fri, 05 Jun 2026 10:00:00 +0800\r\n\r\n"
                    internaldate = "05-Jun-2026 10:00:00 +0800"
                metadata = (
                    f'{sequence} (UID {uid.decode()} INTERNALDATE "{internaldate}" '
                    "BODY[HEADER.FIELDS (DATE)]"
                ).encode()
                parts.extend([b"noise", (metadata, date_header.encode())])
            return "ok", parts

        parts = []
        for sequence, uid in enumerate(reversed(requested), 900):
            parts.append((f"{sequence} (UID {uid.decode()} RFC822".encode(), self.messages[uid]))
        return "ok", parts


class MissingOneBatchMail(FakeUidMail):
    def uid(self, command, *args):
        if command.upper() != "FETCH" or "HEADER.FIELDS" in args[1]:
            return super().uid(command, *args)
        self.uid_calls.append((command, args))
        uid_set, _query = args
        requested = uid_set.split(b",")
        parts = []
        for sequence, uid in enumerate(reversed(requested), 500):
            if uid == b"77" and len(requested) > 1:
                parts.append((f"{sequence} (RFC822".encode(), self.messages[uid]))
            else:
                parts.append((f"{sequence} (UID {uid.decode()} RFC822".encode(), self.messages[uid]))
        return "OK", parts


class EmailFetcherImapFilterTests(unittest.TestCase):
    def _fetcher(self, mail):
        fetcher = EmailFetcher(
            "invoice-user@example.com",
            "auth-code",
            staging_dir=tempfile.mkdtemp(),
        )
        fetcher.mail = mail
        return fetcher

    def test_local_date_filter_uses_uid_all_and_fail_open_unknown_dates(self):
        messages = {str(i).encode(): b"message" for i in range(1, 121)}
        mail = FakeUidMail(messages)

        result = self._fetcher(mail).fetch_emails_by_date("2026-06-01", "2026-06-14")

        self.assertEqual(mail.uid_calls[0], ("SEARCH", (None, "ALL")))
        header_calls = [call for call in mail.uid_calls if call[0] == "FETCH"]
        self.assertEqual(len(header_calls), 1)
        self.assertNotIn(b"5", result)
        self.assertNotIn(b"7", result)
        self.assertIn(b"8", result)
        self.assertIn(b"9", result)
        self.assertEqual(len(result), 117)

    def test_local_date_filter_uses_shanghai_date_for_timezone_aware_headers(self):
        mail = FakeUidMail({str(i).encode(): b"message" for i in range(1, 11)})

        result = self._fetcher(mail).fetch_emails_by_date("2026-02-05", "2026-02-06")

        self.assertIn(b"10", result)

    def test_forwarded_cits_gbt_invoice_subject_is_high_confidence_main_chain(self):
        raw_message = build_attachment_message(
            sender='"Xie Chaofeng" <xie.chaofeng@example.com>',
            subject=(
                "谢超锋转发: [EXTERNAL] CITS GBT Invoice SCCT00919573 "
                "(首段行程：上海虹桥-成都双流/2026-03-12)"
            ),
            body="Please find the attached invoice.",
            filename="3687447_SCCT00919573.pdf",
        )
        fetcher = self._fetcher(FakeUidMail({b"991": raw_message}))

        result = fetcher.extract_attachments([b"991"])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["email_id"], "991")
        self.assertEqual(result[0]["tier"], 2)
        self.assertEqual(result[0]["candidate_action"], "main_chain")
        self.assertEqual(result[0]["prefilter_reason_code"], "B_ATTACHMENT_MAIN_CHAIN")

    def test_extract_attachments_batches_205_messages_and_processes_each_uid_once(self):
        messages = {
            str(uid).encode(): build_attachment_message(
                sender="billing@example.com",
                subject=f"Invoice {uid}",
                body="Attached invoice",
                filename=f"invoice-{uid}.pdf",
            )
            for uid in range(1, 206)
        }
        mail = FakeUidMail(messages)

        result = self._fetcher(mail).extract_attachments(list(messages) + [b"1"])

        body_calls = [
            call for call in mail.uid_calls
            if call[0] == "FETCH" and "HEADER.FIELDS" not in call[1][1]
        ]
        self.assertEqual(len(body_calls), 9)
        self.assertTrue(all(len(call[1][0].split(b",")) <= 25 for call in body_calls))
        self.assertEqual(len(result), 205)
        self.assertEqual({item["email_id"] for item in result}, {str(uid) for uid in range(1, 206)})

    def test_extract_attachments_retries_only_uid_missing_from_batch_metadata(self):
        messages = {
            str(uid).encode(): build_attachment_message(
                sender="billing@example.com",
                subject=f"Invoice {uid}",
                body="Attached invoice",
                filename=f"invoice-{uid}.pdf",
            )
            for uid in range(70, 81)
        }
        mail = MissingOneBatchMail(messages)

        result = self._fetcher(mail).extract_attachments(list(messages))

        body_sets = [
            call[1][0] for call in mail.uid_calls
            if call[0] == "FETCH" and "HEADER.FIELDS" not in call[1][1]
        ]
        self.assertEqual(body_sets, [b"70,71,72,73,74,75,76,77,78,79,80", b"77"])
        self.assertEqual(len(result), 11)


def test_extract_attachments_second_select_failure_stops_before_uid_fetch(tmp_path):
    raw = build_attachment_message(
        sender="billing@example.com",
        subject="Invoice",
        body="Attached",
        filename="invoice.pdf",
    )
    mail = FakeUidMail({b"1": raw})
    select_calls = 0

    def select(_mailbox, readonly=True):
        nonlocal select_calls
        select_calls += 1
        assert readonly is True
        return ("ok", [b""]) if select_calls == 1 else ("NO", [b"failed"])

    mail.select = select
    fetcher = EmailFetcher("invoice-user@example.com", "auth-code", staging_dir=str(tmp_path))
    fetcher.mail = mail
    assert fetcher.fetch_emails_by_date("2026-06-01", "2026-06-14") == [b"1"]
    uid_calls_before_second_select = len(mail.uid_calls)

    with pytest.raises(MailboxScanError, match="SELECT"):
        fetcher.extract_attachments([b"1"])

    assert len(mail.uid_calls) == uid_calls_before_second_select


def test_readable_inputs_are_staged_then_unreadable_uid_raises_safe_aggregate(tmp_path):
    raw = build_attachment_message(
        sender="billing@example.com",
        subject="Invoice 1",
        body="Attached",
        filename="invoice-1.pdf",
    )
    mail = FakeUidMail({b"1": raw, b"2": b"private-message-body"})
    original_uid = mail.uid

    def uid(command, *args):
        if command.upper() != "FETCH" or "HEADER.FIELDS" in args[1]:
            return original_uid(command, *args)
        mail.uid_calls.append((command, args))
        requested = args[0].split(b",")
        parts = []
        for sequence, requested_uid in enumerate(requested, 1):
            if requested_uid == b"1":
                parts.append((b"1 (UID 1 RFC822", raw))
        return "ok", parts

    mail.uid = uid
    monitoring = tmp_path / "monitoring"
    fetcher = EmailFetcher(
        "invoice-user@example.com",
        "auth-code",
        staging_dir=str(tmp_path / "staging"),
        monitoring_dir=str(monitoring),
    )
    fetcher.mail = mail

    with pytest.raises(UnresolvedMailboxInputError) as caught:
        fetcher.extract_attachments([b"1", b"2"])

    assert caught.value.unresolved_count == 1
    safe_error = repr(caught.value)
    assert "auth-code" not in safe_error
    assert "private-message-body" not in safe_error
    assert "UID 2" not in safe_error and "b'2'" not in safe_error
    assert len(caught.value.uid_hashes) == 1
    assert list((tmp_path / "staging").rglob("invoice-1.pdf"))
    diagnostics = (monitoring / "extract_attachments_diagnostics.jsonl").read_text(encoding="utf-8")
    assert "fetch_no_usable_bytes" in diagnostics


def test_post_fetch_message_exception_processes_remaining_uid_then_raises_safe_aggregate(
    tmp_path, monkeypatch, caplog
):
    messages = {
        b"1": build_attachment_message(
            sender="billing@example.com",
            subject="Boom private subject",
            body="private body",
            filename="invoice-1.pdf",
        ),
        b"2": build_attachment_message(
            sender="billing@example.com",
            subject="Invoice 2",
            body="Attached",
            filename="invoice-2.pdf",
        ),
    }
    secret_error = "https://private.example/?token=credential-secret"
    original_classifier = email_fetcher_module._classify_email_tier

    def classifier(sender, subject, body_text):
        if "Boom" in subject:
            raise RuntimeError(secret_error)
        return original_classifier(sender, subject, body_text)

    monkeypatch.setattr(email_fetcher_module, "_classify_email_tier", classifier)
    monitoring = tmp_path / "monitoring"
    fetcher = EmailFetcher(
        "invoice-user@example.com",
        "auth-code",
        staging_dir=str(tmp_path / "staging"),
        monitoring_dir=str(monitoring),
    )
    fetcher.mail = FakeUidMail(messages)

    with pytest.raises(UnresolvedMailboxInputError) as caught:
        fetcher.extract_attachments([b"1", b"2"])

    assert caught.value.unresolved_count == 1
    assert list((tmp_path / "staging").rglob("invoice-2.pdf"))
    diagnostic_text = "\n".join(
        path.read_text(encoding="utf-8") for path in monitoring.glob("*.jsonl")
    )
    assert "message_processing_exception" in diagnostic_text
    assert "RuntimeError" in diagnostic_text
    assert "exception_fingerprint" in diagnostic_text
    assert "Boom private subject" not in diagnostic_text
    assert "private body" not in diagnostic_text
    assert "private.example" not in diagnostic_text
    assert "credential-secret" not in diagnostic_text
    assert "private.example" not in caplog.text
    assert "credential-secret" not in caplog.text


def test_post_fetch_exception_with_broken_string_representation_is_still_aggregated(
    tmp_path, monkeypatch
):
    class UnprintableProcessingError(RuntimeError):
        def __str__(self):
            raise RuntimeError("credential-secret")

    messages = {
        b"1": build_attachment_message(
            sender="billing@example.com",
            subject="Boom",
            body="private body",
            filename="invoice-1.pdf",
        ),
        b"2": build_attachment_message(
            sender="billing@example.com",
            subject="Invoice 2",
            body="Attached",
            filename="invoice-2.pdf",
        ),
    }
    original_classifier = email_fetcher_module._classify_email_tier

    def classifier(sender, subject, body_text):
        if subject == "Boom":
            raise UnprintableProcessingError()
        return original_classifier(sender, subject, body_text)

    monkeypatch.setattr(email_fetcher_module, "_classify_email_tier", classifier)
    monitoring = tmp_path / "monitoring"
    fetcher = EmailFetcher(
        "invoice-user@example.com",
        "auth-code",
        staging_dir=str(tmp_path / "staging"),
        monitoring_dir=str(monitoring),
    )
    fetcher.mail = FakeUidMail(messages)

    with pytest.raises(UnresolvedMailboxInputError) as caught:
        fetcher.extract_attachments([b"1", b"2"])

    assert caught.value.unresolved_count == 1
    assert list((tmp_path / "staging").rglob("invoice-2.pdf"))
    diagnostic_text = "\n".join(
        path.read_text(encoding="utf-8") for path in monitoring.glob("*.jsonl")
    )
    assert "UnprintableProcessingError" in diagnostic_text
    assert "exception_fingerprint" in diagnostic_text
    assert "credential-secret" not in diagnostic_text


@pytest.mark.parametrize("failure_stage", ["parse", "walk"])
def test_post_fetch_mime_exception_is_sanitized_and_aggregated(
    tmp_path, monkeypatch, caplog, failure_stage
):
    secret_error = "private subject https://private.example/?token=credential-secret"

    if failure_stage == "parse":
        def fail_after_fetch(_raw):
            raise ValueError(secret_error)

        monkeypatch.setattr(email_fetcher_module.email, "message_from_bytes", fail_after_fetch)
    else:
        class UnwalkableMessage:
            def get(self, _name, default=""):
                return default

            def walk(self):
                raise LookupError(secret_error)

        monkeypatch.setattr(
            email_fetcher_module.email,
            "message_from_bytes",
            lambda _raw: UnwalkableMessage(),
        )

    monitoring = tmp_path / "monitoring"
    fetcher = EmailFetcher(
        "invoice-user@example.com",
        "auth-code",
        staging_dir=str(tmp_path / "staging"),
        monitoring_dir=str(monitoring),
    )
    fetcher.mail = FakeUidMail({b"1": b"valid-fetched-payload"})

    with pytest.raises(UnresolvedMailboxInputError) as caught:
        fetcher.extract_attachments([b"1"])

    assert caught.value.unresolved_count == 1
    diagnostic_text = "\n".join(
        path.read_text(encoding="utf-8") for path in monitoring.glob("*.jsonl")
    )
    expected_type = "ValueError" if failure_stage == "parse" else "LookupError"
    assert "message_processing_exception" in diagnostic_text
    assert expected_type in diagnostic_text
    assert "exception_fingerprint" in diagnostic_text
    assert "private subject" not in diagnostic_text
    assert "private.example" not in diagnostic_text
    assert "credential-secret" not in diagnostic_text
    assert "private.example" not in caplog.text
    assert "credential-secret" not in caplog.text


def test_tolerated_empty_text_part_cannot_hide_valid_attachment(tmp_path, monkeypatch):
    class EmptyTextPart:
        def get_content_type(self):
            return "text/plain"

        def get(self, _name, default=""):
            return default

        def get_payload(self, decode=False):
            return None

        def get_filename(self):
            return None

    message = EmailMessage()
    message["From"] = "billing@example.com"
    message["Subject"] = "Invoice with empty text part"
    message.set_content("fallback")
    message.add_attachment(
        b"%PDF-1.4\n",
        maintype="application",
        subtype="pdf",
        filename="still-visible.pdf",
    )
    original_walk = list(message.walk())
    message.walk = lambda: iter([original_walk[0], EmptyTextPart(), *original_walk[1:]])
    monkeypatch.setattr(email_fetcher_module.email, "message_from_bytes", lambda _raw: message)
    fetcher = EmailFetcher(
        "invoice-user@example.com", "auth-code", staging_dir=str(tmp_path / "staging")
    )
    fetcher.mail = FakeUidMail({b"1": b"raw"})

    result = fetcher.extract_attachments([b"1"])

    assert len(result) == 1
    assert result[0]["original_filename"] == "still-visible.pdf"


def test_invalid_zip_exception_retains_original_container_as_candidate(tmp_path):
    raw = build_attachment_message(
        sender="billing@example.com",
        subject="Invoice archive",
        body="Attached archive",
        filename="invoice-container.zip",
        payload=b"not-a-valid-zip-but-original-evidence",
    )
    fetcher = EmailFetcher(
        "invoice-user@example.com", "auth-code", staging_dir=str(tmp_path / "staging")
    )
    fetcher.mail = FakeUidMail({b"1": raw})

    result = fetcher.extract_attachments([b"1"])

    assert len(result) == 1
    assert result[0]["original_filename"] == "invoice-container.zip"
    assert Path(result[0]["filepath"]).read_bytes() == b"not-a-valid-zip-but-original-evidence"


if __name__ == "__main__":
    unittest.main()
