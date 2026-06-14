import tempfile
import unittest
from email.message import EmailMessage

from email_fetcher import EmailFetcher


class FakeBatchDateMail:
    def __init__(self, total=120):
        self.ids = [str(i).encode("ascii") for i in range(1, total + 1)]
        self.fetch_calls = []

    def select(self, mailbox, readonly=True):
        self.selected = (mailbox, readonly)
        return "OK", [b""]

    def search(self, charset, criteria):
        return "OK", [b" ".join(self.ids)]

    def fetch(self, sequence_set, command):
        self.fetch_calls.append(sequence_set)
        if isinstance(sequence_set, bytes):
            sequence_text = sequence_set.decode("ascii")
        else:
            sequence_text = str(sequence_set)
        requested_ids = [item.strip() for item in sequence_text.split(",") if item.strip()]
        parts = []
        for item in requested_ids:
            if item == "5":
                date_header = "Date: Mon, 15 Jun 2026 10:00:00 +0800\r\n\r\n"
                internaldate = 'INTERNALDATE "15-Jun-2026 10:00:00 +0800"'
            elif item == "7":
                date_header = "\r\n"
                internaldate = 'INTERNALDATE "21-Feb-2025 10:00:00 +0800"'
            elif item == "8":
                date_header = "\r\n"
                internaldate = 'INTERNALDATE "10-Jun-2026 10:00:00 +0800"'
            elif item == "9":
                date_header = "Date: not-a-real-date\r\n\r\n"
                internaldate = 'INTERNALDATE "not-a-real-date"'
            elif item == "10":
                date_header = "Date: 04 Feb 2026 14:32:39 -0500\r\n\r\n"
                internaldate = 'INTERNALDATE "04-Feb-2026 14:32:39 -0500"'
            else:
                date_header = "Date: Fri, 05 Jun 2026 10:00:00 +0800\r\n\r\n"
                internaldate = 'INTERNALDATE "05-Jun-2026 10:00:00 +0800"'
            parts.append((f"{item} ({internaldate} BODY[HEADER.FIELDS (DATE)]".encode("ascii"), date_header.encode("ascii")))
        return "OK", parts


class FakeAttachmentMail:
    def __init__(self, raw_message):
        self.raw_message = raw_message

    def select(self, mailbox, readonly=True):
        self.selected = (mailbox, readonly)
        return "OK", [b""]

    def fetch(self, e_id, command):
        return "OK", [(b"1 (RFC822 {bytes})", self.raw_message)]


def build_attachment_message(*, sender, subject, body, filename, payload=b"%PDF-1.4\n"):
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "invoice-user@example.com"
    msg["Subject"] = subject
    msg.set_content(body)
    msg.add_attachment(payload, maintype="application", subtype="pdf", filename=filename)
    return msg.as_bytes()


class EmailFetcherImapFilterTests(unittest.TestCase):
    def test_local_date_filter_batches_header_fetches_without_dropping_parse_failures(self):
        mail = FakeBatchDateMail(total=120)
        fetcher = EmailFetcher(
            "invoice-user@example.com",
            "auth-code",
            staging_dir=tempfile.mkdtemp(),
        )
        fetcher.mail = mail

        result = fetcher.fetch_emails_by_date("2026-06-01", "2026-06-14")

        self.assertEqual(len(mail.fetch_calls), 2)
        self.assertNotIn(b"5", result)
        self.assertNotIn(b"7", result)
        self.assertIn(b"8", result)
        self.assertIn(b"9", result)
        self.assertEqual(len(result), 117)

    def test_local_date_filter_uses_shanghai_date_for_timezone_aware_headers(self):
        mail = FakeBatchDateMail(total=10)
        fetcher = EmailFetcher(
            "invoice-user@example.com",
            "auth-code",
            staging_dir=tempfile.mkdtemp(),
        )
        fetcher.mail = mail

        result = fetcher.fetch_emails_by_date("2026-02-05", "2026-02-06")

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
        fetcher = EmailFetcher(
            "invoice-user@example.com",
            "auth-code",
            staging_dir=tempfile.mkdtemp(),
        )
        fetcher.mail = FakeAttachmentMail(raw_message)

        result = fetcher.extract_attachments([b"1"])

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["tier"], 2)
        self.assertEqual(result[0]["candidate_action"], "main_chain")
        self.assertEqual(result[0]["prefilter_reason_code"], "B_ATTACHMENT_MAIN_CHAIN")


if __name__ == "__main__":
    unittest.main()
