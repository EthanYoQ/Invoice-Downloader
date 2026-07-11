import tempfile
import unittest
from email.message import EmailMessage

from email_fetcher import EmailFetcher


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


if __name__ == "__main__":
    unittest.main()
