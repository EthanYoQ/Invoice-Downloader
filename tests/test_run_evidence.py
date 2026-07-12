from __future__ import annotations

import hashlib
import gc
import json
import shutil
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from archive_service import ArchivedOutcome, ArchiveReport, ArchiveService
from candidate_pipeline import CandidatePipeline
from extraction_pipeline import ExtractionOutcome
from report_service import ReportService, RunFinalizationContext
from run_coordinator import RunCoordinator, RunDependencies, RunRequest
from run_evidence import (
    RevisionUnavailable,
    RunEvidenceWriter,
    compute_evidence_digest,
    default_revision,
)
from run_lifecycle import RunLifecycle, RunState
from run_state_store import RunStateStore


REVISION = "a" * 40


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _evidence_threads() -> set[int]:
    return {
        thread.ident
        for thread in threading.enumerate()
        if thread.ident is not None and thread.name.startswith("EvidenceCapture")
    }


def _wait_for_evidence_threads(baseline: set[int], timeout: float = 1.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline and _evidence_threads() != baseline:
        time.sleep(0.01)
    assert _evidence_threads() == baseline


def test_non_validation_services_never_start_evidence_threads():
    baseline = _evidence_threads()
    services = [ReportService(evidence_writer=object()) for _ in range(5)]
    context = RunFinalizationContext(
        run_id="non-validation",
        staging_dir=Path("staging"),
        output_dir=Path("output"),
        request=SimpleNamespace(evidence_required=False),
    )
    for service in services:
        assert service.callbacks(context, SimpleNamespace(cancelled=False), None) == []
    del services
    gc.collect()

    assert _evidence_threads() == baseline


def test_unused_required_dispatcher_close_is_idempotent_and_exits():
    baseline = _evidence_threads()
    service = ReportService(evidence_writer=object(), evidence_required=True)
    context = RunFinalizationContext(
        run_id="unused-required",
        staging_dir=Path("staging"),
        output_dir=Path("output"),
        request=SimpleNamespace(evidence_required=False),
    )

    assert service.callbacks(context, SimpleNamespace(cancelled=False), None) == []
    service.close()
    service.close()

    _wait_for_evidence_threads(baseline)


@pytest.mark.parametrize("mode", ["success", "failure", "cancel", "start_failure"])
def test_required_dispatcher_exits_for_terminal_capture_paths(tmp_path: Path, mode: str):
    baseline = _evidence_threads()

    class Writer:
        def capture(self, _context, _result, *, authorization):
            if mode == "failure":
                raise RuntimeError("private capture failure")
            return bool(authorization())

        def abandon(self, _context):
            return None

    def launcher(target):
        if mode == "start_failure":
            raise RuntimeError("private start failure")
        target()

    service = ReportService(
        evidence_writer=Writer(),
        evidence_capture_launcher=launcher,
        evidence_capture_timeout_seconds=0.1,
        evidence_required=True,
    )
    context = RunFinalizationContext(
        run_id=f"capture-{mode}",
        staging_dir=tmp_path / "staging",
        output_dir=tmp_path / "output",
        request=SimpleNamespace(evidence_required=True),
    )
    callback = dict(
        service.callbacks(
            context,
            SimpleNamespace(cancelled=mode == "cancel"),
            None,
        )
    )["evidence_capture"]

    if mode in {"failure", "start_failure"}:
        with pytest.raises(Exception):
            callback()
    else:
        callback()
    service.close()
    service.close()

    _wait_for_evidence_threads(baseline)


def test_production_facade_records_lineage_before_cleanup_and_finalizes_atomically(tmp_path: Path):
    baseline_threads = _evidence_threads()
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
        revision_resolver=lambda: "b" * 40,
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
        evidence_required=True,
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
        trusted_revision=REVISION,
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
    assert row["artifact_role"] == "invoice"
    rendered = json.dumps(row, ensure_ascii=False)
    assert "invoice_identity" not in rendered
    assert "12345678" not in rendered
    assert "标准商户" not in rendered
    assert "目标公司" not in rendered
    assert "100.00" not in rendered
    _wait_for_evidence_threads(baseline_threads)


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


def test_evidence_lineage_excludes_retention_and_manual_review_outputs(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    source_a = tmp_path / "source-a.pdf"
    source_b = tmp_path / "source-b.pdf"
    source_a.write_bytes(b"invoice")
    source_b.write_bytes(b"non-business")
    candidates = CandidatePipeline().collect(
        [
            {"filepath": str(source_a), "message_uid": "100"},
            {"filepath": str(source_b), "message_uid": "101"},
        ]
    )
    archived_path = output / "invoice.pdf"
    retained_path = output / "_audit_retention" / "notice.pdf"
    manual_path = output / "待人工复核" / "notice.pdf"
    archived_path.write_bytes(source_a.read_bytes())
    retained_path.parent.mkdir()
    retained_path.write_bytes(source_b.read_bytes())
    manual_path.parent.mkdir()
    manual_path.write_bytes(source_b.read_bytes())
    resolved = ExtractionOutcome.resolved(candidates[0], {"pdf_path": str(source_a)})
    retained = ExtractionOutcome(
        candidate=candidates[1],
        status="retained",
        reason_code="KNOWN_NON_BUSINESS",
        message="KNOWN_NON_BUSINESS",
        artifact_path=str(retained_path),
    )
    manual = ExtractionOutcome(
        candidate=candidates[1],
        status="manual_review",
        reason_code="REVIEW",
        message="REVIEW",
        artifact_path=str(manual_path),
    )
    report = ArchiveReport(
        outcomes=(
            ArchivedOutcome(resolved, str(archived_path)),
            ArchivedOutcome(retained, str(retained_path)),
            ArchivedOutcome(manual, str(manual_path)),
        ),
        archived_count=1,
        retained_count=1,
        manual_count=1,
        unresolved_count=0,
        duplicate_count=0,
    )

    rows = RunEvidenceWriter._lineage("run-1", output, report)

    assert len(rows) == 1
    assert rows[0]["document_id"] == candidates[0].identity.document_id


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
        evidence_required=True,
        timeout_seconds=0.2,
    )
    request = RunRequest(
        "run-1", "2026-06-01", "2026-06-13", str(root / "output"), "", "account", "qq",
        before_exclusive="2026-06-14", account_domain="qq.com", mailbox="INBOX",
        target_identifier="目标公司", run_mode="clean-mailbox", run_root=str(root),
        evidence_required=True, trusted_revision=REVISION,
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


def test_packaged_revision_succeeds_with_empty_path_and_generated_identity(
    tmp_path: Path, monkeypatch
):
    revision = "a" * 40
    identity = tmp_path / "build-identity.generated.json"
    identity.write_text(json.dumps({"source_revision": revision}), encoding="utf-8")
    monkeypatch.setenv("PATH", "")

    assert default_revision(identity_paths=(identity,)) == revision


def test_missing_packaged_identity_and_git_fails_safely(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PATH", "")

    try:
        default_revision(identity_paths=(tmp_path / "missing.json",))
    except RevisionUnavailable as exc:
        assert str(exc) == "trusted_revision_unavailable"
    else:
        raise AssertionError("missing immutable identity and git must fail closed")


def test_missing_revision_fails_run_releases_handle_and_preserves_staging(
    tmp_path: Path, monkeypatch
):
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

    def archive_file(_outcome, output):
        target = output / "invoice.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return str(target)

    archive = ArchiveService(writer=archive_file).archive([outcome], root / "output")
    monkeypatch.setenv("PATH", "")
    writer = RunEvidenceWriter(
        revision_resolver=lambda: default_revision(
            identity_paths=(tmp_path / "missing.json",)
        )
    )
    cleaned: list[bool] = []
    report = ReportService(
        cleanup_callback=lambda context: (
            cleaned.append(True),
            shutil.rmtree(context.staging_dir),
        ),
        evidence_writer=writer,
        evidence_required=True,
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
    assert lifecycle.can_begin is True
    assert cleaned == []
    assert staging.exists()
    assert "trusted_revision_unavailable" not in result.error


def test_windows_package_manifest_includes_generated_build_identity():
    manifest_path = Path(__file__).resolve().parents[1] / "build" / "windows" / "resources.manifest.json"
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {
        "source": "build/windows/build-identity.generated.json",
        "target": "build_meta",
        "optional": False,
    } in payload["datas"]


def _zero_lineage_run(tmp_path: Path, *, scan, included_count: int):
    root = tmp_path / "run"
    output = root / "output"
    output.mkdir(parents=True)
    staging = root / "staging" / "run-1"
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-1", staging)
    cleaned: list[bool] = []
    report = ReportService(
        cleanup_callback=lambda context: (
            cleaned.append(True),
            shutil.rmtree(context.staging_dir),
        ),
        evidence_writer=RunEvidenceWriter(revision_resolver=lambda: REVISION),
        evidence_required=True,
    )
    request = RunRequest(
        "run-1", "2026-06-01", "2026-06-13", str(output), "", "account", "qq",
        before_exclusive="2026-06-14", account_domain="qq.com", mailbox="INBOX",
        target_identifier="目标公司", run_mode="clean-mailbox", run_root=str(root),
        evidence_required=True, validation_required=True,
        manifest_included_count=included_count, trusted_revision=REVISION,
    )
    dependencies = RunDependencies(
        connect=lambda _request: object(),
        scan=scan,
        report_service=report,
    )
    state = RunStateStore()
    state.reset("run-1")
    result = RunCoordinator(lifecycle, state, dependencies).run(request, handle=handle)
    return root, staging, lifecycle, cleaned, result


def test_validation_required_non_cancelled_zero_lineage_fails_and_preserves_staging(
    tmp_path: Path,
):
    root, staging, lifecycle, cleaned, result = _zero_lineage_run(
        tmp_path,
        scan=lambda *_args: [],
        included_count=1,
    )

    assert result.state is RunState.FAILED
    assert lifecycle.can_begin is True
    assert cleaned == []
    assert staging.exists()
    assert not (root / "diagnostics" / "run_evidence.json").exists()


def test_processing_failure_without_source_capture_preserves_staging(tmp_path: Path):
    def failed_scan(*_args):
        raise RuntimeError("private source failure")

    root, staging, lifecycle, cleaned, result = _zero_lineage_run(
        tmp_path,
        scan=failed_scan,
        included_count=0,
    )

    assert result.state is RunState.FAILED
    assert lifecycle.can_begin is True
    assert cleaned == []
    assert staging.exists()
    assert not (root / "diagnostics" / "run_evidence.json").exists()


def _capture_deadline_fixture(
    tmp_path: Path,
    *,
    evidence_writer: RunEvidenceWriter,
    timeout_seconds: float = 0.02,
    evidence_capture_launcher=None,
):
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

    def archive_file(_outcome, output):
        target = output / "invoice.pdf"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
        return str(target)

    archive = ArchiveService(writer=archive_file).archive([outcome], root / "output")
    cleaned: list[bool] = []
    report = ReportService(
        cleanup_callback=lambda context: (
            cleaned.append(True),
            shutil.rmtree(context.staging_dir),
        ),
        evidence_writer=evidence_writer,
        evidence_capture_timeout_seconds=timeout_seconds,
        evidence_capture_launcher=evidence_capture_launcher,
        evidence_required=True,
    )
    request = RunRequest(
        "run-1", "2026-06-01", "2026-06-13", str(root / "output"), "", "account", "qq",
        before_exclusive="2026-06-14", account_domain="qq.com", mailbox="INBOX",
        target_identifier="目标公司", run_mode="clean-mailbox", run_root=str(root),
        evidence_required=True, validation_required=True, manifest_included_count=1,
        trusted_revision=REVISION,
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
    coordinator = RunCoordinator(lifecycle, state, dependencies)
    return root, staging, lifecycle, cleaned, state, coordinator, request, handle


def _assert_capture_timeout_is_quarantined(
    fixture,
    *,
    entered: threading.Event,
    release: threading.Event,
    baseline_threads: set[int],
):
    root, staging, lifecycle, cleaned, state, coordinator, request, handle = fixture
    results = []
    worker = threading.Thread(
        target=lambda: results.append(coordinator.run(request, handle=handle)),
        name="coordinator-under-evidence-deadline",
    )
    worker.start()
    assert entered.wait(1)
    worker.join(0.25)
    bounded = not worker.is_alive()
    if not bounded:
        release.set()
        worker.join(1)
    assert bounded, "evidence deadline waited for blocked worker"
    result = results[0]
    assert result.state is RunState.FAILED
    assert lifecycle.can_begin is True
    assert cleaned == []
    assert staging.exists()
    assert not (root / "diagnostics" / "run_evidence.json").exists()
    state_before = state.snapshot()
    release.set()
    deadline = time.time() + 1
    quarantine = (
        root
        / "diagnostics"
        / "quarantined"
        / "run-1"
        / "evidence_capture_late.json"
    )
    while time.time() < deadline and not quarantine.exists():
        time.sleep(0.01)
    assert quarantine.exists()
    assert not (root / "diagnostics" / "run_evidence.json").exists()
    assert state.snapshot() == state_before
    _wait_for_evidence_threads(baseline_threads)


def test_evidence_hash_timeout_is_bounded_and_late_worker_is_quarantined(
    tmp_path: Path, monkeypatch
):
    import run_evidence as module

    entered = threading.Event()
    release = threading.Event()
    original = module._sha256_file

    def blocked_hash(path):
        if "staging" in Path(path).parts:
            entered.set()
            assert release.wait(2)
        return original(path)

    monkeypatch.setattr(module, "_sha256_file", blocked_hash)
    baseline_threads = _evidence_threads()
    writer = RunEvidenceWriter(revision_resolver=lambda: REVISION)
    fixture = _capture_deadline_fixture(tmp_path, evidence_writer=writer)
    _assert_capture_timeout_is_quarantined(
        fixture,
        entered=entered,
        release=release,
        baseline_threads=baseline_threads,
    )


def test_evidence_promotion_timeout_cannot_publish_after_deadline(tmp_path: Path):
    entered = threading.Event()
    release = threading.Event()

    def blocked_promote():
        entered.set()
        assert release.wait(2)

    baseline_threads = _evidence_threads()
    writer = RunEvidenceWriter(
        revision_resolver=lambda: REVISION,
        capture_promoter=blocked_promote,
    )
    fixture = _capture_deadline_fixture(tmp_path, evidence_writer=writer)
    _assert_capture_timeout_is_quarantined(
        fixture,
        entered=entered,
        release=release,
        baseline_threads=baseline_threads,
    )


def test_evidence_worker_start_failure_fails_and_preserves_staging(
    tmp_path: Path, monkeypatch
):
    import report_service as module

    baseline_threads = _evidence_threads()
    writer = RunEvidenceWriter(revision_resolver=lambda: REVISION)
    fixture = _capture_deadline_fixture(tmp_path, evidence_writer=writer)
    root, staging, lifecycle, cleaned, _state, coordinator, request, handle = fixture
    original_start = module.threading.Thread.start

    def fail_capture_start(thread):
        if thread.name.startswith("EvidenceCapture-"):
            raise RuntimeError("private start failure")
        return original_start(thread)

    monkeypatch.setattr(module.threading.Thread, "start", fail_capture_start)
    result = coordinator.run(request, handle=handle)

    assert result.state is RunState.FAILED
    assert lifecycle.can_begin is True
    assert cleaned == []
    assert staging.exists()
    assert "private" not in result.error
    assert not (root / "diagnostics" / "run_evidence.json").exists()
    _wait_for_evidence_threads(baseline_threads)


def test_evidence_launch_stall_is_inside_deadline_and_late_work_is_quarantined(
    tmp_path: Path,
):
    entered = threading.Event()
    release = threading.Event()

    def blocked_launcher(target):
        entered.set()
        assert release.wait(0.3)
        target()

    baseline_threads = _evidence_threads()
    writer = RunEvidenceWriter(revision_resolver=lambda: REVISION)
    fixture = _capture_deadline_fixture(
        tmp_path,
        evidence_writer=writer,
        timeout_seconds=0.02,
        evidence_capture_launcher=blocked_launcher,
    )
    root, staging, lifecycle, cleaned, state, coordinator, request, handle = fixture
    started = time.perf_counter()
    result = coordinator.run(request, handle=handle)
    elapsed = time.perf_counter() - started

    assert entered.is_set()
    assert elapsed < 0.15
    assert result.state is RunState.FAILED
    assert lifecycle.can_begin is True
    assert cleaned == []
    assert staging.exists()
    state_before = state.snapshot()
    release.set()
    quarantine = (
        root / "diagnostics" / "quarantined" / "run-1" / "evidence_capture_late.json"
    )
    deadline = time.time() + 1
    while time.time() < deadline and not quarantine.exists():
        time.sleep(0.01)
    assert quarantine.exists()
    assert not (root / "diagnostics" / "run_evidence.json").exists()
    assert state.snapshot() == state_before
    _wait_for_evidence_threads(baseline_threads)
