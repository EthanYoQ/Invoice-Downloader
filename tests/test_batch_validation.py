import json
import os
import subprocess
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import pytest

from batch_validation import (
    BatchValidationError,
    BatchValidator,
    canonical_windows_path,
    compare_performance,
    compute_inventory,
    compute_performance_record_digest,
    compute_scope_digest,
)


REVISION = "05587e08d1ebdfba719657b63a5e45fa8ab317c0"
VERSION = "2026.07.12"
RUN_END = "2026-07-11T00:00:00Z"


def _manifest(rows=1) -> dict:
    included = []
    for index in range(rows):
        suffix = index + 1
        included.append({
            "truth_id": f"t{suffix}",
            "truth_status": "included",
            "source_email_id": str(99 + suffix),
            "mail_date_local": "2026-06-10 10:00:00",
            "source_kind": "attachment",
            "file_name": f"invoice-{suffix}.pdf",
            "document_role": "invoice",
            "truth_type": "餐饮",
            "expected_category": "餐饮",
            "invoice_date": "2026-06-10",
            "seller": f"标准商户{suffix}",
            "purchaser": "目标公司",
            "amount": f"{suffix * 100}.00",
            "invoice_number": str(12345677 + suffix),
            "invoice_code": "",
            "sha256": f"{suffix:x}" * 64,
            "evidence": [{"sha256": f"{suffix:x}" * 64, "bytes": suffix * 100}],
        })
    return {
        "summary": {
            "dataset": "qq_fixture",
            "date_from": "2026-06-01",
            "date_to": "2026-06-13",
            "before_exclusive": "2026-06-14",
            "mailbox": "INBOX",
            "account_domain": "qq.com",
            "target_company": "目标公司",
            "included_count": rows,
            "excluded_count": 0,
            "pending_review_count": 0,
            "finalized": True,
        },
        "included": included,
        "excluded": [],
        "pending_review": [],
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _write_jsonl(path: Path, rows: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows), encoding="utf-8")
    return path


def _run_root(tmp_path: Path, rows=1) -> Path:
    root = tmp_path / "run"
    output = root / "output" / "餐饮"
    output.mkdir(parents=True)
    events = []
    for index in range(rows):
        suffix = index + 1
        artifact = output / f"invoice-{suffix}.pdf"
        artifact.write_bytes(f"artifact-{suffix}".encode())
        events.append({
            "kind": "archive",
            "document_id": f"doc-{suffix}",
            "email_id": str(99 + suffix),
            "file_name": f"invoice-{suffix}.pdf",
            "path": str(artifact.resolve()),
            "category": "餐饮",
            "final_type": "餐饮",
            "seller": f"标准商户{suffix}",
        })
    _write_jsonl(root / "monitoring" / "artifact_events.jsonl", events)
    _write_json(root / "monitoring" / "run_config.json", {
        "run_id": "run-1",
        "run_root": str(root.resolve()),
        "locked_date_from": "2026-06-01",
        "locked_date_to": "2026-06-13",
        "before_exclusive": "2026-06-14",
        "email_domain": "qq.com",
        "account_channel": "qq",
        "mailbox": "INBOX",
        "run_mode": "clean-mailbox",
        "hardware_mode": "windows-desktop-standard",
        "hardware_fingerprint": "host-fixture-v1",
        "active_run_config": {"company": "目标公司"},
        "candidate_revision": REVISION,
        "candidate_version": VERSION,
    })
    _write_json(root / "diagnostics" / "run_evidence.json", {
        "run_id": "run-1",
        "run_root": str(root.resolve()),
        "started_monotonic_seconds": "100.00",
        "ended_monotonic_seconds": "2383.79",
        "elapsed_seconds": "2283.79",
        "started_at_utc": "2026-07-10T23:00:00Z",
        "ended_at_utc": RUN_END,
        "hardware_mode": "windows-desktop-standard",
        "hardware_fingerprint": "host-fixture-v1",
        "candidate_revision": REVISION,
        "candidate_version": VERSION,
    })
    return root


def _validator(**kwargs) -> BatchValidator:
    return BatchValidator(revision_resolver=lambda: REVISION, **kwargs)


def _write_bound_supplied_audit(root: Path, result, **overrides) -> Path:
    payload = {
        "run_id": result.run_id,
        "run_root": result.run_root,
        "candidate_revision": result.candidate_revision,
        "generated_at_utc": result.audit_completed_at_utc,
        "manifest_sha256": result.manifest_sha256,
        "inventory_sha256": result.inventory_sha256,
        "matched_rows": [dict(item) for item in result.assignments],
    }
    payload.update(overrides)
    return _write_json(root / "diagnostics" / "strict_truth_audit.json", payload)


def test_validator_generates_fresh_bound_audit_and_inventory(tmp_path):
    root = _run_root(tmp_path)

    result = _validator().validate(_manifest(), root)

    assert result.passed is True
    assert result.counts == {"p0": 0, "p1": 0, "p2": 0, "manual": 0, "matched": 1}
    assert result.manifest_sha256 and result.inventory_sha256
    assert result.scope_digest
    assert datetime.fromisoformat(result.audit_started_at_utc.replace("Z", "+00:00")) <= datetime.fromisoformat(
        result.audit_completed_at_utc.replace("Z", "+00:00")
    )
    assert datetime.fromisoformat(result.audit_started_at_utc.replace("Z", "+00:00")) > datetime.fromisoformat(
        RUN_END.replace("Z", "+00:00")
    )
    assert result.assignments[0]["artifact_sha256"]
    assert result.assignments[0]["artifact_size"] == len(b"artifact-1")
    assert result.to_mapping()["fresh_audit"]["exit_code"] == 0


def test_forged_supplied_success_cannot_hide_missing_artifact(tmp_path):
    root = _run_root(tmp_path)
    initial = _validator().validate(_manifest(), root)
    _write_bound_supplied_audit(root, initial)
    (root / "output" / "餐饮" / "invoice-1.pdf").unlink()

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code in {"strict_audit_failed", "stale_supplied_audit", "invalid_artifact_assignment"}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"generated_at_utc": "2099-01-01T00:00:00Z"}, "stale_supplied_audit"),
        ({"run_id": "copied-old-run"}, "stale_supplied_audit"),
        ({"candidate_revision": "arbitrary-revision"}, "stale_supplied_audit"),
        ({"matched_rows": [{"truth_id": "t1", "matched_path": "C:/does/not/exist.pdf"}]}, "stale_supplied_audit"),
    ],
)
def test_supplied_audit_is_comparison_only_and_must_be_bound(tmp_path, overrides, code):
    root = _run_root(tmp_path)
    initial = _validator().validate(_manifest(), root)
    _write_bound_supplied_audit(root, initial, **overrides)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code == code


def test_tampered_output_invalidates_old_inventory_binding(tmp_path):
    root = _run_root(tmp_path)
    initial = _validator().validate(_manifest(), root)
    _write_bound_supplied_audit(root, initial)
    (root / "output" / "餐饮" / "invoice-1.pdf").write_bytes(b"tampered")

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code == "stale_supplied_audit"


def test_output_mutation_during_fresh_audit_is_rejected(tmp_path):
    root = _run_root(tmp_path)
    artifact = root / "output" / "餐饮" / "invoice-1.pdf"

    def mutating_runner(manifest, run_root):
        from strict_truth_audit import compare

        result = compare(manifest, run_root)
        artifact.write_bytes(b"changed-during-audit")
        return result

    with pytest.raises(BatchValidationError) as exc_info:
        _validator(audit_runner=mutating_runner).validate(_manifest(), root)

    assert exc_info.value.code == "inventory_changed_during_validation"


def test_config_revision_must_equal_trusted_current_revision(tmp_path):
    root = _run_root(tmp_path)
    config_path = root / "monitoring" / "run_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["candidate_revision"] = "forged"
    _write_json(config_path, config)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code == "version_mismatch"


def test_inventory_streams_regular_files_and_is_deterministic(tmp_path):
    root = _run_root(tmp_path)

    first = compute_inventory(root / "output")
    second = compute_inventory(root / "output")

    assert first.digest == second.digest
    assert first.entries[0].sha256
    assert first.entries[0].size == len(b"artifact-1")


def test_windows_canonical_paths_casefold_and_normalize_slashes():
    assert canonical_windows_path(r"C:\RUN\Output\Invoice.PDF") == canonical_windows_path(
        "c:/run/output/invoice.pdf"
    )


def test_case_variant_assignments_are_duplicate_before_filesystem_lookup(tmp_path):
    root = _run_root(tmp_path, rows=2)
    actual = str((root / "output" / "餐饮" / "invoice-1.pdf").resolve())

    def forged_runner(_manifest_payload, _root):
        return {
            "run_root": str(root.resolve()),
            "truth_summary": _manifest(rows=2)["summary"],
            "artifact_count": 2,
            "p0_conclusion": {"count": 0, "passed": True, "bad_rows": []},
            "user_p1_conclusion": {"count": 0, "category_rows": [], "field_mismatch_rows": []},
            "p2_conclusion": {"count": 0, "passed": True, "bad_rows": []},
            "manual_check_rows": [],
            "matched_rows": [
                {"truth_id": "t1", "matched_path": actual},
                {"truth_id": "t2", "matched_path": actual.swapcase()},
            ],
        }

    with pytest.raises(BatchValidationError) as exc_info:
        _validator(audit_runner=forged_runner).validate(_manifest(rows=2), root)

    assert exc_info.value.code == "duplicate_artifact_assignment"


def test_reparse_component_is_rejected_without_platform_skip(tmp_path):
    root = _run_root(tmp_path)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator(reparse_checker=lambda path: path.name == "output").validate(_manifest(), root)

    assert exc_info.value.code == "reparse_point_rejected"


def test_actual_symlink_or_windows_junction_is_rejected(tmp_path):
    root = _run_root(tmp_path)
    external = tmp_path / "external"
    external.mkdir()
    (external / "payload.pdf").write_bytes(b"outside")
    link = root / "output" / "linked"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(external)],
            capture_output=True, text=True, check=False,
        )
        assert created.returncode == 0, created.stderr
    else:
        link.symlink_to(external, target_is_directory=True)

    with pytest.raises(BatchValidationError) as exc_info:
        compute_inventory(root / "output")

    assert exc_info.value.code == "reparse_point_rejected"


def _performance_payload(*, seconds: str, revision: str, run_id: str, start: str, end: str) -> dict:
    value = {
        "date_from": "2025-11-25",
        "date_to": "2026-06-14",
        "before_exclusive": "2026-06-15",
        "account_domain": "qq.com",
        "account_channel": "qq",
        "mailbox": "INBOX",
        "target_identifier": "辉瑞",
        "run_mode": "clean-mailbox",
        "hardware_mode": "windows-desktop-standard",
        "hardware_fingerprint": "host-fixture-v1",
        "revision": revision,
        "run_id": run_id,
        "started_at_utc": start,
        "ended_at_utc": end,
        "started_monotonic_seconds": "100.00",
        "ended_monotonic_seconds": str(Decimal("100.00") + Decimal(seconds)),
        "elapsed_seconds": seconds,
    }
    value["scope_digest"] = compute_scope_digest(value)
    value["record_digest"] = compute_performance_record_digest(value)
    return value


@pytest.mark.parametrize(("candidate", "passed"), [("2283.79", True), ("2283.80", False)])
def test_performance_threshold_is_decimal_and_inclusive(tmp_path, candidate, passed):
    baseline = _write_json(tmp_path / "baseline.json", _performance_payload(
        seconds="3262.55", revision="baseline-d46a504", run_id="baseline-run",
        start="2026-06-24T00:00:00Z", end="2026-06-24T00:54:22.55Z",
    ))
    candidate_path = _write_json(tmp_path / "candidate.json", _performance_payload(
        seconds=candidate, revision=REVISION, run_id="candidate-run",
        start="2026-07-12T00:00:00Z", end="2026-07-12T00:38:03.80Z",
    ))

    verdict = compare_performance(baseline, candidate_path, revision_resolver=lambda: REVISION)

    assert verdict.target_seconds == "2283.79"
    assert verdict.passed is passed
    assert verdict.scope_digest == compute_scope_digest(json.loads(baseline.read_text(encoding="utf-8")))
    assert verdict.baseline_revision == "baseline-d46a504"
    assert verdict.candidate_revision == REVISION


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value.update(scope_id="attacker-chosen"), "arbitrary_scope_id"),
        (lambda value: value.update(scope_digest="0" * 64), "scope_digest_mismatch"),
        (lambda value: value.update(hardware_fingerprint="other-host"), "scope_digest_mismatch"),
        (lambda value: value.update(account_domain="163.com"), "scope_digest_mismatch"),
        (lambda value: value.update(revision="forged"), "performance_revision_mismatch"),
        (lambda value: value.update(run_id=""), "invalid_performance_run"),
        (lambda value: value.update(ended_at_utc="not-a-time"), "invalid_performance_time"),
    ],
)
def test_performance_rejects_scope_hardware_revision_and_time_tampering(tmp_path, mutation, code):
    baseline_payload = _performance_payload(
        seconds="3262.55", revision="baseline-d46a504", run_id="baseline-run",
        start="2026-06-24T00:00:00Z", end="2026-06-24T00:54:22.55Z",
    )
    candidate_payload = _performance_payload(
        seconds="2283.79", revision=REVISION, run_id="candidate-run",
        start="2026-07-12T00:00:00Z", end="2026-07-12T00:38:03.79Z",
    )
    mutation(candidate_payload)

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(
            _write_json(tmp_path / "baseline.json", baseline_payload),
            _write_json(tmp_path / "candidate.json", candidate_payload),
            revision_resolver=lambda: REVISION,
        )

    assert exc_info.value.code == code


@pytest.mark.parametrize("field", ["hardware_fingerprint", "account_domain", "run_mode"])
def test_performance_rejects_self_consistent_but_different_scope(tmp_path, field):
    baseline_payload = _performance_payload(
        seconds="3262.55", revision="baseline-d46a504", run_id="baseline-run",
        start="2026-06-24T00:00:00Z", end="2026-06-24T00:54:22.55Z",
    )
    candidate_payload = _performance_payload(
        seconds="2283.79", revision=REVISION, run_id="candidate-run",
        start="2026-07-12T00:00:00Z", end="2026-07-12T00:38:03.79Z",
    )
    candidate_payload[field] = "different"
    candidate_payload["scope_digest"] = compute_scope_digest(candidate_payload)
    candidate_payload["record_digest"] = compute_performance_record_digest(candidate_payload)

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(
            _write_json(tmp_path / "baseline.json", baseline_payload),
            _write_json(tmp_path / "candidate.json", candidate_payload),
            revision_resolver=lambda: REVISION,
        )

    assert exc_info.value.code == "performance_contract_mismatch"


def test_cli_remains_path_only_and_credential_free(tmp_path):
    root = _run_root(tmp_path)
    manifest_path = _write_json(tmp_path / "manifest.json", _manifest())
    current_revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    config_path = root / "monitoring" / "run_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["candidate_revision"] = current_revision
    _write_json(config_path, config)
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["candidate_revision"] = current_revision
    _write_json(evidence_path, evidence)
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "batch_validation.py"),
         "--truth-manifest", str(manifest_path), "--run-root", str(root)],
        capture_output=True, text=True, check=False,
    )

    assert completed.returncode == 0
    payload = json.loads(completed.stdout)
    report = root / "diagnostics" / "batch_validation.json"
    assert payload["output"] == str(report.resolve())
    assert report.exists()
    help_text = subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parents[1] / "batch_validation.py"), "--help"],
        capture_output=True, text=True, check=False,
    ).stdout.lower()
    assert "auth" not in help_text and "api-key" not in help_text and "email" not in help_text
