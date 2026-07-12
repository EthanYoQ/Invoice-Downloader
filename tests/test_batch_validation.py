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
)
from run_evidence import (
    compute_evidence_digest,
    compute_inventory_digest as compute_production_inventory_digest,
    compute_lineage_digest,
    compute_scope_digest as compute_evidence_scope_digest,
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


def _run_root(tmp_path: Path, rows=1, *, elapsed="2283.79") -> Path:
    root = tmp_path / "run"
    output = root / "output" / "餐饮"
    output.mkdir(parents=True)
    events = []
    lineage = []
    for index in range(rows):
        suffix = index + 1
        artifact = output / f"invoice-{suffix}.pdf"
        artifact.write_bytes(f"artifact-{suffix}".encode())
        artifact_bytes = artifact.read_bytes()
        artifact_sha = __import__("hashlib").sha256(artifact_bytes).hexdigest()
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
        lineage.append({
            "run_id": "run-1",
            "document_id": f"doc-{suffix}",
            "source_email_uid": str(99 + suffix),
            "source_chain_sha256s": [f"{suffix:x}" * 64],
            "output_relative_path": f"餐饮/invoice-{suffix}.pdf",
            "output_sha256": artifact_sha,
            "output_size": len(artifact_bytes),
            "invoice_identity": {
                "invoice_code": "",
                "invoice_number": str(12345677 + suffix),
                "invoice_date": "2026-06-10",
                "amount": f"{suffix * 100}.00",
                "seller": f"标准商户{suffix}",
                "purchaser": "目标公司",
                "document_type": "餐饮",
                "category": "餐饮",
            },
            "transformation_type": "attachment",
            "provider_type": "local",
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
    scope = {
        "date_from": "2026-06-01",
        "date_to": "2026-06-13",
        "before_exclusive": "2026-06-14",
        "account_domain": "qq.com",
        "account_channel": "qq",
        "mailbox": "INBOX",
        "target_identifier": "目标公司",
        "run_mode": "clean-mailbox",
        "hardware_mode": "windows-desktop-standard",
        "hardware_fingerprint": "host-fixture-v1",
    }
    inventory = [
        {
            "relative_path": row["output_relative_path"],
            "size": row["output_size"],
            "sha256": row["output_sha256"],
        }
        for row in lineage
    ]
    evidence = {
        "schema_version": 1,
        "run_id": "run-1",
        "run_root": str(root.resolve()),
        "started_monotonic_seconds": "100.00",
        "ended_monotonic_seconds": str(Decimal("100.00") + Decimal(elapsed)),
        "elapsed_seconds": elapsed,
        "started_at_utc": "2026-07-10T23:00:00Z",
        "ended_at_utc": RUN_END,
        "hardware_mode": "windows-desktop-standard",
        "hardware_fingerprint": "host-fixture-v1",
        "candidate_revision": REVISION,
        "candidate_version": VERSION,
        "scope": scope,
        "scope_digest": compute_evidence_scope_digest(scope),
        "lineage": lineage,
        "lineage_digest": compute_lineage_digest(lineage),
        "output_inventory": inventory,
        "inventory_sha256": compute_production_inventory_digest(inventory),
    }
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
    _write_json(root / "diagnostics" / "run_evidence.json", evidence)
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
    "overrides",
    [
        {"generated_at_utc": "2099-01-01T00:00:00Z"},
        {"run_id": "copied-old-run"},
        {"candidate_revision": "arbitrary-revision"},
        {"matched_rows": [{"truth_id": "t1", "matched_path": "C:/does/not/exist.pdf"}]},
    ],
)
def test_supplied_audit_is_ignored_and_never_used_as_decision_input(tmp_path, overrides):
    root = _run_root(tmp_path)
    initial = _validator().validate(_manifest(), root)
    _write_bound_supplied_audit(root, initial, **overrides)

    result = _validator().validate(_manifest(), root)

    assert result.passed is True
    assert [row["truth_id"] for row in result.assignments] == ["t1"]


def test_tampered_output_invalidates_old_inventory_binding(tmp_path):
    root = _run_root(tmp_path)
    initial = _validator().validate(_manifest(), root)
    _write_bound_supplied_audit(root, initial)
    (root / "output" / "餐饮" / "invoice-1.pdf").write_bytes(b"tampered")

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code in {"run_evidence_inventory_mismatch", "lineage_output_mismatch"}


def test_candidate_run_without_lineage_fails_closed(tmp_path):
    root = _run_root(tmp_path)
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["lineage"] = []
    evidence["lineage_digest"] = compute_lineage_digest([])
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code == "missing_document_lineage"


def test_weak_business_match_cannot_satisfy_truth_without_content_lineage(tmp_path):
    root = _run_root(tmp_path)
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["lineage"][0]["source_chain_sha256s"] = ["f" * 64]
    evidence["lineage_digest"] = compute_lineage_digest(evidence["lineage"])
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code == "truth_lineage_mismatch"


def test_lineage_run_id_is_bound_to_top_level_evidence(tmp_path):
    root = _run_root(tmp_path)
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["lineage"][0]["run_id"] = "copied-other-run"
    evidence["lineage_digest"] = compute_lineage_digest(evidence["lineage"])
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code == "invalid_document_lineage"


def test_top_level_hardware_is_bound_to_evidence_scope(tmp_path):
    root = _run_root(tmp_path)
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["hardware_fingerprint"] = "different-host"
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code == "run_evidence_scope_mismatch"


def test_replaced_output_bytes_fail_even_when_weak_fields_still_match(tmp_path):
    root = _run_root(tmp_path)
    (root / "output" / "餐饮" / "invoice-1.pdf").write_bytes(b"arbitrary replacement")

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), root)

    assert exc_info.value.code in {"run_evidence_inventory_mismatch", "lineage_output_mismatch"}


def test_tampered_supplied_audit_is_ignored_in_favor_of_fresh_evidence(tmp_path):
    root = _run_root(tmp_path)
    _write_json(root / "diagnostics" / "strict_truth_audit.json", {
        "exit_code": 0,
        "matched_rows": [{"truth_id": "forged", "matched_path": "C:/forged.pdf"}],
        "generated_at_utc": "2099-01-01T00:00:00Z",
    })

    result = _validator().validate(_manifest(), root)

    assert result.passed is True
    assert [row["truth_id"] for row in result.assignments] == ["t1"]


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


def test_nominal_run_root_junction_is_rejected(tmp_path):
    target = _run_root(tmp_path / "target")
    nominal = tmp_path / "nominal-run"
    if os.name == "nt":
        created = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(nominal), str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert created.returncode == 0, created.stderr
    else:
        nominal.symlink_to(target, target_is_directory=True)

    with pytest.raises(BatchValidationError) as exc_info:
        _validator().validate(_manifest(), nominal)

    assert exc_info.value.code == "reparse_point_rejected"


def _candidate_report(tmp_path: Path, *, seconds: str):
    root = _run_root(tmp_path, elapsed=seconds)
    manifest_path = _write_json(tmp_path / "manifest.json", _manifest())
    validator = _validator()
    result = validator.validate(manifest_path, root)
    report_path = validator.write_report(result, root)
    return root, manifest_path, report_path, result


def _baseline_contract(path: Path, candidate_result, *, seconds="3262.55") -> Path:
    payload = {
        "schema_version": 1,
        "accepted": True,
        "run_id": "accepted-baseline-run",
        "run_root": "C:/accepted/baseline/run",
        "revision": "baseline-d46a504",
        "elapsed_seconds": seconds,
        "scope": dict(candidate_result.scope),
        "scope_digest": candidate_result.scope_digest,
        "hardware_mode": candidate_result.scope["hardware_mode"],
        "hardware_fingerprint": candidate_result.scope["hardware_fingerprint"],
        "inventory_sha256": "b" * 64,
        "strict_digest": "c" * 64,
        "manifest_sha256": candidate_result.manifest_sha256,
    }
    payload["contract_digest"] = compute_performance_record_digest(payload)
    return _write_json(path, payload)


@pytest.mark.parametrize(("candidate", "passed"), [("2283.79", True), ("2283.80", False)])
def test_performance_threshold_is_decimal_and_inclusive(tmp_path, candidate, passed):
    _root, _manifest_path, candidate_path, result = _candidate_report(
        tmp_path, seconds=candidate
    )
    baseline = _baseline_contract(tmp_path / "baseline.json", result)

    verdict = compare_performance(
        baseline,
        candidate_path,
        revision_resolver=lambda: REVISION,
    )

    assert verdict.target_seconds == "2283.79"
    assert verdict.passed is passed
    assert verdict.scope_digest == result.scope_digest
    assert verdict.baseline_revision == "baseline-d46a504"
    assert verdict.candidate_revision == REVISION


def test_forged_caller_created_candidate_report_cannot_pass(tmp_path):
    _root, _manifest_path, report_path, result = _candidate_report(
        tmp_path, seconds="2283.79"
    )
    baseline = _baseline_contract(tmp_path / "baseline.json", result)
    forged = json.loads(report_path.read_text(encoding="utf-8"))
    forged["validation_digest"] = "f" * 64
    _write_json(report_path, forged)

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(baseline, report_path, revision_resolver=lambda: REVISION)

    assert exc_info.value.code == "candidate_validation_report_mismatch"


def test_independent_candidate_metrics_json_is_rejected(tmp_path):
    _root, _manifest_path, _report_path, result = _candidate_report(
        tmp_path / "real", seconds="2283.79"
    )
    baseline = _baseline_contract(tmp_path / "baseline.json", result)
    arbitrary = _write_json(tmp_path / "candidate.json", {
        "run_id": "invented",
        "elapsed_seconds": "1.00",
        "revision": REVISION,
    })

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(baseline, arbitrary, revision_resolver=lambda: REVISION)

    assert exc_info.value.code == "candidate_validation_report_invalid"


def test_candidate_timing_tampering_invalidates_revalidation(tmp_path):
    root, _manifest_path, report_path, result = _candidate_report(
        tmp_path, seconds="2283.79"
    )
    baseline = _baseline_contract(tmp_path / "baseline.json", result)
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    evidence["elapsed_seconds"] = "1.00"
    evidence["ended_monotonic_seconds"] = "101.00"
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
    _write_json(evidence_path, evidence)

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(baseline, report_path, revision_resolver=lambda: REVISION)

    assert exc_info.value.code == "candidate_validation_report_mismatch"


@pytest.mark.parametrize("field", ["hardware_fingerprint", "account_domain", "run_mode"])
def test_baseline_scope_or_hardware_mismatch_is_rejected(tmp_path, field):
    _root, _manifest_path, report_path, result = _candidate_report(
        tmp_path, seconds="2283.79"
    )
    baseline = _baseline_contract(tmp_path / "baseline.json", result)
    payload = json.loads(baseline.read_text(encoding="utf-8"))
    payload["scope"][field] = "different"
    if field in {"hardware_mode", "hardware_fingerprint"}:
        payload[field] = "different"
    payload["scope_digest"] = compute_evidence_scope_digest(payload["scope"])
    payload["contract_digest"] = compute_performance_record_digest(payload)
    _write_json(baseline, payload)

    with pytest.raises(BatchValidationError) as exc_info:
        compare_performance(baseline, report_path, revision_resolver=lambda: REVISION)

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
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
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
