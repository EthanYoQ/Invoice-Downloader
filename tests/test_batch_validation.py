import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from batch_validation import (
    BatchValidationError,
    BatchValidator,
    compare_performance,
)


def _manifest() -> dict:
    return {
        "summary": {
            "dataset": "qq_fixture",
            "date_from": "2026-06-01",
            "date_to": "2026-06-13",
            "before_exclusive": "2026-06-14",
            "mailbox": "INBOX",
            "account_domain": "qq.com",
            "target_company": "目标公司",
            "included_count": 1,
            "excluded_count": 0,
            "pending_review_count": 0,
            "finalized": True,
        },
        "included": [{
            "truth_id": "t1",
            "truth_status": "included",
            "source_email_id": "100",
            "mail_date_local": "2026-06-10 10:00:00",
            "source_kind": "attachment",
            "file_name": "invoice.pdf",
            "document_role": "invoice",
            "truth_type": "餐饮",
            "expected_category": "餐饮",
            "invoice_date": "2026-06-10",
            "seller": "标准商户",
            "purchaser": "目标公司",
            "amount": "100.00",
            "invoice_number": "12345678",
            "invoice_code": "",
            "sha256": "a" * 64,
            "evidence": [{"sha256": "a" * 64, "bytes": 100}],
        }],
        "excluded": [],
        "pending_review": [],
    }


def _write_json(path: Path, payload: dict) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _run_root(tmp_path: Path) -> Path:
    root = tmp_path / "run"
    root.mkdir()
    _write_json(root / "monitoring" / "run_config.json", {
        "run_id": "run-1",
        "run_root": str(root.resolve()),
        "locked_date_from": "2026-06-01",
        "locked_date_to": "2026-06-13",
        "before_exclusive": "2026-06-14",
        "email_domain": "qq.com",
        "account_channel": "qq",
        "mailbox": "INBOX",
        "active_run_config": {"company": "目标公司"},
        "candidate_revision": "abc1234",
        "candidate_version": "2026.07.12",
    })
    _write_json(root / "diagnostics" / "run_evidence.json", {
        "run_id": "run-1",
        "run_root": str(root.resolve()),
        "started_monotonic_seconds": "100.00",
        "ended_monotonic_seconds": "2383.79",
        "elapsed_seconds": "2283.79",
        "hardware_mode": "windows-desktop-standard",
        "candidate_revision": "abc1234",
        "candidate_version": "2026.07.12",
    })
    _write_json(root / "diagnostics" / "strict_truth_audit.json", {
        "run_id": "run-1",
        "run_root": str(root.resolve()),
        "truth_summary": _manifest()["summary"],
        "artifact_count": 1,
        "p0_conclusion": {"count": 0, "passed": True, "bad_rows": []},
        "user_p1_conclusion": {"count": 0, "category_rows": [], "field_mismatch_rows": []},
        "p2_conclusion": {"count": 0, "passed": True, "bad_rows": []},
        "manual_check_rows": [],
        "matched_rows": [{"truth_id": "t1", "matched_path": str(root / "output" / "invoice.pdf")}],
        "exit_code": 0,
        "generated_at_utc": "2026-07-12T00:00:00Z",
        "candidate_revision": "abc1234",
    })
    return root


def test_batch_validator_accepts_exact_zero_incident_scope(tmp_path):
    root = _run_root(tmp_path)

    result = BatchValidator().validate(_manifest(), root)

    assert result.passed is True
    assert result.counts == {"p0": 0, "p1": 0, "p2": 0, "manual": 0, "matched": 1}
    assert result.run_id == "run-1"
    assert result.candidate_revision == "abc1234"


@pytest.mark.parametrize(
    ("target", "mutate", "code"),
    [
        ("manifest", lambda p: p["summary"].update(finalized=False), "manifest_not_finalized"),
        ("manifest", lambda p: p["summary"].update(pending_review_count=1), "pending_review_not_zero"),
        ("config", lambda p: p.update(locked_date_from="2026-06-02"), "scope_mismatch"),
        ("config", lambda p: p.update(email_domain="163.com"), "scope_mismatch"),
        ("config", lambda p: p.update(candidate_revision="other"), "version_mismatch"),
        ("evidence", lambda p: p.pop("ended_monotonic_seconds"), "missing_timing_boundary"),
        ("evidence", lambda p: p.update(ended_monotonic_seconds="99"), "invalid_timing_boundary"),
        ("evidence", lambda p: p.update(candidate_revision="other"), "version_mismatch"),
        ("audit", lambda p: p["p0_conclusion"].update(count=1, passed=False), "strict_audit_failed"),
        ("audit", lambda p: p["user_p1_conclusion"].update(count=1), "strict_audit_failed"),
        ("audit", lambda p: p["p2_conclusion"].update(count=1, passed=False), "strict_audit_failed"),
        ("audit", lambda p: p.update(manual_check_rows=[{}]), "strict_audit_failed"),
        ("audit", lambda p: p.update(exit_code=1), "strict_audit_failed"),
        ("audit", lambda p: p.update(matched_rows=[]), "matched_count_mismatch"),
        ("audit", lambda p: p["matched_rows"].append({"truth_id": "other", "matched_path": p["matched_rows"][0]["matched_path"]}), "duplicate_artifact_assignment"),
        ("audit", lambda p: p.update(candidate_revision="other"), "stale_audit"),
        ("audit", lambda p: p.pop("run_id"), "stale_audit"),
        ("audit", lambda p: p.pop("generated_at_utc"), "stale_audit"),
        ("audit", lambda p: p["p0_conclusion"].update(count="bad"), "invalid_audit_result"),
    ],
)
def test_batch_validator_fails_closed_on_tampering(tmp_path, target, mutate, code):
    root = _run_root(tmp_path)
    manifest = _manifest()
    paths = {
        "config": root / "monitoring" / "run_config.json",
        "evidence": root / "diagnostics" / "run_evidence.json",
        "audit": root / "diagnostics" / "strict_truth_audit.json",
    }
    if target == "manifest":
        payload = manifest
    else:
        payload = json.loads(paths[target].read_text(encoding="utf-8"))
    mutate(payload)
    if target != "manifest":
        _write_json(paths[target], payload)

    with pytest.raises(BatchValidationError) as exc_info:
        BatchValidator().validate(manifest, root)

    assert exc_info.value.code == code


def test_batch_validator_rejects_malformed_active_run_config_deterministically(tmp_path):
    root = _run_root(tmp_path)
    config_path = root / "monitoring" / "run_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config.pop("target_company", None)
    config["active_run_config"] = []
    _write_json(config_path, config)

    with pytest.raises(BatchValidationError) as exc_info:
        BatchValidator().validate(_manifest(), root)

    assert exc_info.value.code == "invalid_run_config"


@pytest.mark.parametrize(
    ("candidate", "passed"),
    [("2283.79", True), ("2283.80", False)],
)
def test_performance_threshold_is_decimal_and_inclusive(tmp_path, candidate, passed):
    baseline = _write_json(tmp_path / "baseline.json", {
        "elapsed_seconds": "3262.55",
        "started_monotonic_seconds": "100.00",
        "ended_monotonic_seconds": "3362.55",
        "scope_id": "qq-long-range-v1",
        "hardware_mode": "windows-desktop-standard",
    })
    candidate_path = _write_json(tmp_path / "candidate.json", {
        "elapsed_seconds": candidate,
        "started_monotonic_seconds": "100.00",
        "ended_monotonic_seconds": str(100 + float(candidate)),
        "scope_id": "qq-long-range-v1",
        "hardware_mode": "windows-desktop-standard",
    })

    verdict = compare_performance(baseline, candidate_path)

    assert verdict.target_seconds == "2283.79"
    assert verdict.passed is passed
    assert set(verdict.to_mapping()) == {
        "baseline_seconds", "candidate_seconds", "target_seconds",
        "speedup_fraction", "threshold_fraction", "passed",
    }


@pytest.mark.parametrize("bad", ["NaN", "Infinity", "-1"])
def test_performance_rejects_invalid_numbers(tmp_path, bad):
    baseline = _write_json(tmp_path / "baseline.json", {
        "elapsed_seconds": "3262.55", "started_monotonic_seconds": "0", "ended_monotonic_seconds": "3262.55",
        "scope_id": "scope", "hardware_mode": "hardware",
    })
    candidate = _write_json(tmp_path / "candidate.json", {
        "elapsed_seconds": bad, "started_monotonic_seconds": "0", "ended_monotonic_seconds": bad,
        "scope_id": "scope", "hardware_mode": "hardware",
    })

    with pytest.raises(BatchValidationError, match="invalid performance seconds"):
        compare_performance(baseline, candidate)


@pytest.mark.parametrize("field", ["scope_id", "hardware_mode"])
def test_performance_rejects_mismatched_contract(tmp_path, field):
    baseline_payload = {
        "elapsed_seconds": "3262.55", "started_monotonic_seconds": "0", "ended_monotonic_seconds": "3262.55",
        "scope_id": "scope", "hardware_mode": "hardware",
    }
    candidate_payload = deepcopy(baseline_payload)
    candidate_payload[field] = "different"

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(
            _write_json(tmp_path / "baseline.json", baseline_payload),
            _write_json(tmp_path / "candidate.json", candidate_payload),
        )

    assert exc_info.value.code == "performance_contract_mismatch"


def test_performance_rejects_elapsed_value_not_proven_by_boundaries(tmp_path):
    baseline = _write_json(tmp_path / "baseline.json", {
        "elapsed_seconds": "3262.55", "started_monotonic_seconds": "10", "ended_monotonic_seconds": "3272.55",
        "scope_id": "scope", "hardware_mode": "hardware",
    })
    candidate = _write_json(tmp_path / "candidate.json", {
        "elapsed_seconds": "2283.79", "started_monotonic_seconds": "20", "ended_monotonic_seconds": "100",
        "scope_id": "scope", "hardware_mode": "hardware",
    })

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(baseline, candidate)

    assert exc_info.value.code == "invalid_performance_boundaries"


@pytest.mark.parametrize("matched_path", ["", "../outside.pdf"])
def test_batch_validator_rejects_unaccounted_or_out_of_run_assignment(tmp_path, matched_path):
    root = _run_root(tmp_path)
    audit_path = root / "diagnostics" / "strict_truth_audit.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    audit["matched_rows"][0]["matched_path"] = matched_path
    _write_json(audit_path, audit)

    with pytest.raises(BatchValidationError) as exc_info:
        BatchValidator().validate(_manifest(), root)

    assert exc_info.value.code == "invalid_artifact_assignment"


def test_cli_is_credential_free_atomic_and_confines_output(tmp_path):
    root = _run_root(tmp_path)
    manifest_path = _write_json(tmp_path / "manifest.json", _manifest())
    command = [
        sys.executable,
        str(Path(__file__).resolve().parents[1] / "batch_validation.py"),
        "--truth-manifest", str(manifest_path),
        "--run-root", str(root),
    ]

    completed = subprocess.run(command, capture_output=True, text=True, check=False)

    assert completed.returncode == 0
    report = root / "diagnostics" / "batch_validation.json"
    assert report.exists()
    assert not (root / "diagnostics" / "batch_validation.json.tmp").exists()
    stdout = completed.stdout
    stdout_payload = json.loads(stdout)
    assert "@qq.com" not in stdout
    assert "http://" not in stdout and "https://" not in stdout
    assert stdout_payload["output"] == str(report.resolve())
    help_text = subprocess.run(command[:2] + ["--help"], capture_output=True, text=True).stdout
    assert "auth" not in help_text.lower()
    assert "api-key" not in help_text.lower()
    assert "email" not in help_text.lower()


def test_cli_rejects_diagnostics_symlink_escape_when_supported(tmp_path):
    if os.name == "nt":
        pytest.skip("creating symlinks is not reliably available on Windows test hosts")
    root = _run_root(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    diagnostics = root / "diagnostics"
    for child in diagnostics.iterdir():
        child.unlink()
    diagnostics.rmdir()
    diagnostics.symlink_to(outside, target_is_directory=True)

    with pytest.raises(BatchValidationError) as exc_info:
        BatchValidator().write_report(BatchValidator().validate(_manifest(), root), root)

    assert exc_info.value.code == "output_path_escape"
