import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from build_truth_dataset import (
    TruthBuildError,
    fetch_internaldate_local,
    load_truth_build_identity,
    parse_generic_xml,
    resolve_email_text_evidence,
    truth_hotel_folio_date,
    validate_truth_build_paths,
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


def _source_snapshot(root: Path) -> dict[str, tuple[str, int]]:
    snapshot = {}
    for path in sorted(item for item in root.rglob("*") if item.is_file()):
        snapshot[path.relative_to(root).as_posix()] = (
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_mtime_ns,
        )
    return snapshot


def _offline_source(tmp_path: Path, *, include_identity=True) -> Path:
    source = tmp_path / "source"
    source.mkdir()
    config = {
        "target_company": "目标公司",
        "account_domain": "qq.com",
        "mailbox": "INBOX",
        "date_from": "2026-06-01",
        "date_to": "2026-06-13",
        "before_exclusive": "2026-06-14",
    } if include_identity else {"mailbox": "INBOX"}
    (source / "truth_collection_config.json").write_text(json.dumps(config), encoding="utf-8")
    for name in ("document_index.jsonl", "link_downloads.jsonl", "url_candidates.jsonl", "mailbox_inventory.jsonl"):
        (source / name).write_text("", encoding="utf-8")
    return source


def test_skip_collect_cli_is_source_read_only_and_blocks_imap_imports(tmp_path):
    source = _offline_source(tmp_path)
    output = tmp_path / "output"
    before = _source_snapshot(source)
    entry = Path(__file__).resolve().parents[1] / "build_truth_dataset.py"
    hook = """
import builtins, importlib, runpy, sys
blocked = {'email_fetcher', 'mailbox_scanner', 'pdf_converter', 'user_settings', 'imaplib'}
original_import = builtins.__import__
original_dynamic = importlib.import_module
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in blocked:
        raise RuntimeError('blocked import:' + name)
    return original_import(name, *args, **kwargs)
def guarded_dynamic(name, *args, **kwargs):
    if name.split('.')[0] in blocked:
        raise RuntimeError('blocked dynamic import:' + name)
    return original_dynamic(name, *args, **kwargs)
builtins.__import__ = guarded
importlib.import_module = guarded_dynamic
source, output, entry = sys.argv[1:4]
sys.argv = ['build_truth_dataset.py', '--date-from', '2026-06-01', '--date-to', '2026-06-13',
            '--before-exclusive', '2026-06-14', '--mailbox', 'INBOX', '--source-root', source,
            '--output-root', output, '--skip-collect']
runpy.run_path(entry, run_name='__main__')
"""

    completed = subprocess.run(
        [sys.executable, "-c", hook, str(source), str(output), str(entry)],
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert _source_snapshot(source) == before
    assert (output / "truth_build_config.json").exists()
    assert (output / "truth_manifest.json").exists()


def test_skip_collect_requires_immutable_target_identity(tmp_path):
    source = _offline_source(tmp_path, include_identity=False)

    with pytest.raises(TruthBuildError) as exc_info:
        load_truth_build_identity(source, explicit_target="", explicit_domain="")

    assert exc_info.value.code == "target_company_required"


@pytest.mark.parametrize("placement", ["same", "inside"])
def test_truth_output_cannot_mutate_source_tree(tmp_path, placement):
    source = _offline_source(tmp_path)
    output = source if placement == "same" else source / "generated"

    with pytest.raises(TruthBuildError) as exc_info:
        validate_truth_build_paths(source, output)

    assert exc_info.value.code == "output_overlaps_source"
