from build_truth_dataset import (
    fetch_internaldate_local,
    parse_generic_xml,
    resolve_email_text_evidence,
    truth_hotel_folio_date,
)


class Mail:
    def __init__(self, metadata):
        self.metadata = metadata
        self.calls = []

    def fetch(self, *_args):
        raise AssertionError("sequence FETCH must never be used")

    def uid(self, command, *args):
        self.calls.append((command, args))
        return "ok", [(self.metadata, b"")]


class Fetcher:
    def __init__(self, metadata):
        self.mail = Mail(metadata)


def test_truth_internaldate_uses_uid_fetch_and_exact_uid_match():
    fetcher = Fetcher(b'19 (UID 99 INTERNALDATE "10-Jun-2026 10:00:00 +0800")')

    result = fetch_internaldate_local(fetcher, b"99")

    assert result == "2026-06-10 10:00:00"
    assert fetcher.mail.calls == [("FETCH", (b"99", "(UID INTERNALDATE)"))]


def test_truth_internaldate_rejects_mismatched_or_missing_uid_metadata():
    mismatched = Fetcher(b'19 (UID 98 INTERNALDATE "10-Jun-2026 10:00:00 +0800")')
    missing = Fetcher(b'19 (INTERNALDATE "10-Jun-2026 10:00:00 +0800")')

    assert fetch_internaldate_local(mismatched, b"99") == ""
    assert fetch_internaldate_local(missing, b"99") == ""


def test_skip_collect_email_evidence_is_offline(monkeypatch, tmp_path):
    evidence_dir = tmp_path / "email_evidence" / "99_fixture"
    evidence_dir.mkdir(parents=True)
    (evidence_dir / "99_body.txt").write_text("本地发票正文", encoding="utf-8")

    def unexpected_network(*_args, **_kwargs):
        raise AssertionError("offline truth rebuild must not connect to IMAP")

    monkeypatch.setattr("build_truth_dataset.fetch_email_text_evidence", unexpected_network)

    result = resolve_email_text_evidence(["99"], tmp_path, "INBOX", allow_network=False)

    assert result["99"]["body_text"] == "本地发票正文"


def test_truth_xml_uses_document_order_for_total_field_aliases(tmp_path):
    path = tmp_path / "invoice.xml"
    path.write_text(
        "<Invoice><TotalTax-includedAmount>330.00</TotalTax-includedAmount>"
        "<Jshj>311.32</Jshj><InvoiceNumber>12345678</InvoiceNumber></Invoice>",
        encoding="utf-8",
    )

    assert parse_generic_xml(path)["amount"] == "330.00"


def test_truth_hotel_date_preserves_printed_date_priority():
    text = "离店日期: 2026-05-22\n打印日期: 2026-05-21\n酒店结账单"

    assert truth_hotel_folio_date(text) == "2026-05-21"
