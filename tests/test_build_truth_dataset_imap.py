from build_truth_dataset import fetch_internaldate_local


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
