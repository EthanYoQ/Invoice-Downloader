from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from archive_service import ArchiveService
from candidate_pipeline import CandidatePipeline
from extraction_pipeline import ExtractionOutcome
from report_service import ReportService
from run_coordinator import RunCoordinator, RunDependencies, RunRequest
from run_evidence import RunEvidenceWriter, compute_evidence_digest
from run_lifecycle import RunLifecycle, RunState
from run_state_store import RunStateStore


REVISION = "a" * 40


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_production_facade_records_lineage_before_cleanup_and_finalizes_atomically(tmp_path: Path):
    root = tmp_path / "run"
    staging = root / "staging" / "run-1"
    output = root / "output"
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-1", staging)
    source = staging / "mail-100.pdf"
    source.write_bytes(b"raw-mail-attachment")
    calls: list[str] = []

    candidate = CandidatePipeline(channel="qq").collect(
        [{"filepath": str(source), "message_uid": "100", "transformation_type": "pdf"}]
    )[0]
    outcome = ExtractionOutcome.resolved(
        candidate,
        {
            "info_json": {
                "InvoiceNumber": "12345678",
                "InvoiceCode": "",
                "Date": "2026-06-10",
                "Amount": "100.00",
                "Seller": "标准商户",
                "Purchaser": "目标公司",
                "document_type": "餐饮",
            },
            "pdf_path": str(source),
        },
    )

    def writer(_outcome, target_root):
        target = target_root / "餐饮" / "invoice.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
        return str(target)

    archive = ArchiveService(writer=writer).archive([outcome], output)
    evidence_writer = RunEvidenceWriter(
        revision_resolver=lambda: REVISION,
        version_resolver=lambda: "2026.07.12",
        hardware_resolver=lambda: ("windows-desktop-standard", "fixture-host"),
    )
    report = ReportService(
        report_callback=lambda *_args: calls.append("report"),
        cleanup_callback=lambda context: (
            calls.append("cleanup"),
            shutil.rmtree(context.staging_dir),
        ),
        evidence_writer=evidence_writer,
    )
    request = RunRequest(
        run_id="run-1",
        date_from="2026-06-01",
        date_to="2026-06-13",
        save_path=str(output),
        rules_text="",
        account_id="account-hash",
        channel_id="qq",
        before_exclusive="2026-06-14",
        account_domain="qq.com",
        mailbox="INBOX",
        target_identifier="目标公司",
        run_mode="clean-mailbox",
        run_root=str(root),
        evidence_required=True,
    )
    dependencies = RunDependencies(
        connect=lambda _request: object(),
        scan=lambda *_args: ["mail"],
        candidate=lambda *_args: [candidate],
        extract=lambda *_args: [outcome],
        archive=lambda *_args: archive,
        report_service=report,
    )
    state = RunStateStore()
    state.reset(request.run_id)

    result = RunCoordinator(lifecycle, state, dependencies).run(request, handle=handle)

    assert result.state is RunState.COMPLETED
    assert calls == ["report", "cleanup"]
    assert not staging.exists()
    evidence_path = root / "diagnostics" / "run_evidence.json"
    assert evidence_path.exists()
    assert not list(evidence_path.parent.glob("*.tmp"))
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["candidate_revision"] == REVISION
    assert evidence["evidence_digest"] == compute_evidence_digest(evidence)
    assert len(evidence["lineage"]) == 1
    row = evidence["lineage"][0]
    assert row["run_id"] == "run-1"
    assert row["document_id"] == candidate.identity.document_id
    assert row["source_email_uid"] == "100"
    assert _sha(b"raw-mail-attachment") in row["source_chain_sha256s"]
    assert row["output_relative_path"] == "餐饮/invoice.pdf"
    assert row["output_sha256"] == _sha(b"raw-mail-attachment")
    assert row["output_size"] == len(b"raw-mail-attachment")
    assert row["invoice_identity"]["invoice_number"] == "12345678"


def test_evidence_capture_rejects_archive_without_real_output(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    candidate = CandidatePipeline().collect(
        [{"filepath": str(source), "message_uid": "100"}]
    )[0]
    outcome = ExtractionOutcome.resolved(candidate, {"InvoiceNumber": "12345678"})
    archive = ArchiveService(writer=lambda *_args: str(tmp_path / "missing.pdf")).archive(
        [outcome], tmp_path / "output"
    )

    writer = RunEvidenceWriter(revision_resolver=lambda: REVISION)
    try:
        writer.capture_for_test(
            run_id="run-1",
            run_root=tmp_path,
            output_root=tmp_path / "output",
            archive_report=archive,
        )
    except ValueError as exc:
        assert str(exc) == "lineage_output_missing"
    else:
        raise AssertionError("missing production output must fail evidence capture")


def test_evidence_capture_rejects_archived_document_without_source_uid(tmp_path: Path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"source")
    candidate = CandidatePipeline().collect([{"filepath": str(source)}])[0]
    outcome = ExtractionOutcome.resolved(candidate, {"InvoiceNumber": "12345678"})

    def archive_file(_outcome, root):
        target = root / "invoice.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return str(target)

    output = tmp_path / "output"
    archive = ArchiveService(writer=archive_file).archive([outcome], output)

    try:
        RunEvidenceWriter(revision_resolver=lambda: REVISION).capture_for_test(
            run_id="run-1",
            run_root=tmp_path,
            output_root=output,
            archive_report=archive,
        )
    except ValueError as exc:
        assert str(exc) == "lineage_source_uid_missing"
    else:
        raise AssertionError("archived lineage without source UID must fail")


def test_failed_lineage_capture_preserves_staging_instead_of_cleaning(tmp_path: Path):
    root = tmp_path / "run"
    staging = root / "staging" / "run-1"
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-1", staging)
    source = staging / "source.pdf"
    source.write_bytes(b"source")
    candidate = CandidatePipeline().collect(
        [{"filepath": str(source), "message_uid": "100"}]
    )[0]
    outcome = ExtractionOutcome.resolved(candidate, {"InvoiceNumber": "12345678"})
    archive = ArchiveService(
        writer=lambda *_args: str(root / "output" / "missing.pdf")
    ).archive([outcome], root / "output")
    cleaned: list[bool] = []
    report = ReportService(
        cleanup_callback=lambda context: (
            cleaned.append(True),
            shutil.rmtree(context.staging_dir),
        ),
        evidence_writer=RunEvidenceWriter(revision_resolver=lambda: REVISION),
        timeout_seconds=0.2,
    )
    request = RunRequest(
        "run-1", "2026-06-01", "2026-06-13", str(root / "output"), "", "account", "qq",
        before_exclusive="2026-06-14", account_domain="qq.com", mailbox="INBOX",
        target_identifier="目标公司", run_mode="clean-mailbox", run_root=str(root),
        evidence_required=True,
    )
    dependencies = RunDependencies(
        connect=lambda _request: object(),
        scan=lambda *_args: ["mail"],
        candidate=lambda *_args: [candidate],
        extract=lambda *_args: [outcome],
        archive=lambda *_args: archive,
        report_service=report,
    )
    state = RunStateStore()
    state.reset("run-1")

    result = RunCoordinator(lifecycle, state, dependencies).run(request, handle=handle)

    assert result.state is RunState.FAILED
    assert cleaned == []
    assert staging.exists()
    assert source.exists()
