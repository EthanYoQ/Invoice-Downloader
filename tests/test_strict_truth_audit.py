import copy
import hashlib
import json
import sys

import pytest
import fitz

import strict_truth_audit as audit
from run_evidence import (
    compute_evidence_digest,
    compute_inventory_digest,
    compute_lineage_digest,
    compute_scope_digest,
)


def _complete_summary():
    return {
        "dataset": "fixture",
        "date_from": "2026-06-01",
        "date_to": "2026-06-13",
        "before_exclusive": "2026-06-14",
        "mailbox": "INBOX",
        "account_domain": "qq.com",
        "target_company": "目标公司",
        "included_count": 0,
        "excluded_count": 0,
        "pending_review_count": 0,
        "finalized": True,
    }


def _write_full_evidence(run_root, output_path, *, output_hash=None):
    digest = output_hash or hashlib.sha256(output_path.read_bytes()).hexdigest()
    size = output_path.stat().st_size
    relative = output_path.relative_to(run_root / "output").as_posix()
    run_id = run_root.name
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
        "hardware_fingerprint": "f" * 64,
    }
    lineage = [
        {
            "run_id": run_id,
            "document_id": "lineage-bound-output",
            "source_email_uid": "101",
            "source_chain_sha256s": ["e" * 64],
            "output_relative_path": relative,
            "output_sha256": digest,
            "output_size": size,
            "artifact_role": "invoice",
            "transformation_type": "provider_download",
            "provider_type": "fpyun",
        }
    ]
    inventory = [{"relative_path": relative, "size": size, "sha256": digest}]
    evidence = {
        "schema_version": 1,
        "run_id": run_id,
        "run_root": str(run_root.resolve()),
        "candidate_revision": "a" * 40,
        "candidate_version": "test-version",
        "validation_required": True,
        "manifest_included_count": 1,
        "scope": scope,
        "scope_digest": compute_scope_digest(scope),
        "hardware_mode": scope["hardware_mode"],
        "hardware_fingerprint": scope["hardware_fingerprint"],
        "started_monotonic_seconds": "100.0",
        "ended_monotonic_seconds": "101.0",
        "elapsed_seconds": "1.0",
        "started_at_utc": "2026-07-12T00:00:00Z",
        "ended_at_utc": "2026-07-12T00:00:01Z",
        "lineage": lineage,
        "lineage_digest": compute_lineage_digest(lineage),
        "output_inventory": inventory,
        "inventory_sha256": compute_inventory_digest(inventory),
    }
    evidence["evidence_digest"] = compute_evidence_digest(evidence)
    diagnostics = run_root / "diagnostics"
    diagnostics.mkdir(exist_ok=True)
    (diagnostics / "run_evidence.json").write_text(
        json.dumps(evidence, ensure_ascii=False), encoding="utf-8"
    )


def test_assignment_cannot_reuse_one_artifact_for_two_truth_rows():
    rows = [
        {"truth_id": "t1", "invoice_number": "12345678"},
        {"truth_id": "t2", "invoice_number": "12345678"},
    ]
    artifacts = [{"document_id": "a1", "invoice_number": "12345678", "path": "a.pdf"}]
    original_rows = copy.deepcopy(rows)
    original_artifacts = copy.deepcopy(artifacts)

    assigned = audit.assign_truth_matches(rows, artifacts, {})

    assert [assigned[key][0] is not None for key in ("t1", "t2")].count(True) == 1
    assert rows == original_rows
    assert artifacts == original_artifacts


def test_two_by_two_invoice_number_matches_are_ambiguous():
    rows = [
        {"truth_id": "t1", "invoice_number": "12345678"},
        {"truth_id": "t2", "invoice_number": "12345678"},
    ]
    artifacts = [
        {"document_id": "a1", "invoice_number": "12345678", "path": "a.pdf"},
        {"document_id": "a2", "invoice_number": "12345678", "path": "b.pdf"},
    ]

    assigned = audit.assign_truth_matches(rows, artifacts, {})

    assert assigned == {
        "t1": (None, "ambiguous_match"),
        "t2": (None, "ambiguous_match"),
    }


def test_two_by_two_hash_matches_are_ambiguous():
    rows = [
        {"truth_id": "t1", "sha256": "same-hash"},
        {"truth_id": "t2", "sha256": "same-hash"},
    ]
    artifacts = [
        {"document_id": "a1", "path": "same.pdf"},
        {"document_id": "a2", "path": "same.pdf"},
    ]

    assigned = audit.assign_truth_matches(rows, artifacts, {"same-hash": "same.pdf"})

    assert assigned == {
        "t1": (None, "ambiguous_match"),
        "t2": (None, "ambiguous_match"),
    }


def test_strong_identity_prefers_archive_over_retention_before_tie_analysis():
    rows = [{"truth_id": "t1", "invoice_number": "12345678"}]
    artifacts = [
        {"document_id": "archive", "invoice_number": "12345678", "path": "archive.pdf"},
        {
            "document_id": "retention",
            "invoice_number": "12345678",
            "path": "_audit_retention/duplicates/copy.pdf",
            "kind": "retention",
        },
    ]

    assigned = audit.assign_truth_matches(rows, artifacts, {})

    assert assigned["t1"] == (artifacts[0], "invoice_number")


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ([{"truth_id": "", "invoice_number": "12345678"}], "nonempty truth_id"),
        ([{"truth_id": "t1"}, {"truth_id": "t1"}], "duplicate truth_id 't1'"),
    ],
)
def test_assignment_rejects_invalid_truth_ids(rows, message):
    with pytest.raises(ValueError, match=message):
        audit.assign_truth_matches(rows, [], {})


def test_compare_rejects_duplicate_truth_ids_before_loading_artifacts(monkeypatch, tmp_path):
    def unexpected_load(_run_root):
        raise AssertionError("artifacts must not load for an invalid manifest")

    monkeypatch.setattr(audit, "load_artifacts", unexpected_load)
    manifest = {"included": [{"truth_id": "t1"}, {"truth_id": "t1"}]}

    with pytest.raises(ValueError, match="duplicate truth_id 't1'"):
        audit.compare(manifest, tmp_path)


def test_compare_matches_transformed_output_from_independent_pdf_content(tmp_path):
    run_root = tmp_path / "run"
    output = run_root / "output" / "餐饮"
    output.mkdir(parents=True)
    pdf_path = output / "20260525_餐饮_482.00_测试餐厅.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Invoice Number: 26372000002439975871")
    document.save(pdf_path)
    document.close()
    monitoring = run_root / "monitoring"
    monitoring.mkdir()
    (monitoring / "artifact_events.jsonl").write_text(
        json.dumps(
            {
                "kind": "archived",
                "document_id": "transformed-output",
                "path": str(pdf_path),
                "category": "餐饮",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    manifest = {
        "summary": {**_complete_summary(), "included_count": 1},
        "included": [
            {
                "truth_id": "transformed-url-invoice",
                "truth_status": "included",
                "truth_type": "餐饮",
                "document_role": "invoice",
                "invoice_date": "2026-05-25",
                "invoice_number": "26372000002439975871",
                "seller": "测试餐厅",
                "amount": "482.00",
                "sha256": "source-hash-differs-from-transformed-output",
            }
        ],
        "excluded": [],
        "pending_review": [],
    }

    result = audit.compare(manifest, run_root)

    assert result["p0_conclusion"]["count"] == 0
    assert result["matched_rows"][0]["match_method"] == "invoice_number"
    assert result["audit_authority"]["authoritative"] is False


def test_compare_does_not_accept_unproven_output_pdf(tmp_path):
    run_root = tmp_path / "run"
    output = run_root / "output" / "餐饮"
    output.mkdir(parents=True)
    pdf_path = output / "20260525_餐饮_482.00_测试餐厅.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Invoice Number: 26372000002439975871")
    document.save(pdf_path)
    document.close()
    manifest = {
        "summary": {**_complete_summary(), "included_count": 1},
        "included": [
            {
                "truth_id": "unproven-output",
                "truth_status": "included",
                "truth_type": "餐饮",
                "document_role": "invoice",
                "invoice_date": "2026-05-25",
                "invoice_number": "26372000002439975871",
                "seller": "测试餐厅",
                "amount": "482.00",
            }
        ],
        "excluded": [],
        "pending_review": [],
    }

    result = audit.compare(manifest, run_root)

    assert result["p0_conclusion"]["count"] == 1
    assert result["matched_rows"] == []
    assert result["p0_conclusion"]["bad_rows"][0]["truth_id"] == "unproven-output"


def test_compare_authoritative_output_requires_lineage_path_and_hash(tmp_path):
    run_root = tmp_path / "run"
    output = run_root / "output" / "餐饮"
    output.mkdir(parents=True)
    pdf_path = output / "20260525_餐饮_482.00_测试餐厅.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Invoice Number: 26372000002439975871")
    document.save(pdf_path)
    document.close()
    _write_full_evidence(run_root, pdf_path)
    manifest = {
        "summary": {**_complete_summary(), "included_count": 1},
        "included": [
            {
                "truth_id": "lineage-bound-output",
                "truth_status": "included",
                "truth_type": "餐饮",
                "document_role": "invoice",
                "invoice_date": "2026-05-25",
                "invoice_number": "26372000002439975871",
                "seller": "测试餐厅",
                "amount": "482.00",
            }
        ],
        "excluded": [],
        "pending_review": [],
    }

    result = audit.compare(manifest, run_root)

    assert result["audit_authority"]["authoritative"] is True
    assert result["p0_conclusion"]["count"] == 0

    _write_full_evidence(run_root, pdf_path, output_hash="0" * 64)

    mismatched = audit.compare(manifest, run_root)

    assert mismatched["audit_authority"]["authoritative"] is False
    assert "lineage_output_mismatch" in mismatched["audit_authority"]["reasons"]
    assert mismatched["p0_conclusion"]["count"] == 1


def test_compare_rejects_incomplete_evidence_and_missing_lineage_output(tmp_path):
    run_root = tmp_path / "run"
    output = run_root / "output" / "餐饮"
    output.mkdir(parents=True)
    pdf_path = output / "invoice.pdf"
    pdf_path.write_bytes(b"%PDF-1.5\nfixture")
    diagnostics = run_root / "diagnostics"
    diagnostics.mkdir()
    (diagnostics / "run_evidence.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_root": str(run_root.resolve()),
                "lineage": [
                    {
                        "output_relative_path": "餐饮/invoice.pdf",
                        "output_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    incomplete = audit.compare(
        {"summary": _complete_summary(), "included": []}, run_root
    )

    assert incomplete["audit_authority"]["authoritative"] is False
    assert "invalid_run_evidence" in incomplete["audit_authority"]["reasons"]

    _write_full_evidence(run_root, pdf_path)
    pdf_path.unlink()

    missing = audit.compare(
        {"summary": _complete_summary(), "included": []}, run_root
    )

    assert missing["audit_authority"]["authoritative"] is False
    assert "lineage_output_mismatch" in missing["audit_authority"]["reasons"]


@pytest.mark.parametrize("field", ["p0", "p1", "p2", "manual"])
def test_any_strict_failure_returns_nonzero(field):
    summary = {"p0": 0, "p1": 0, "p2": 0, "manual": 0}
    summary[field] = 1

    assert audit.strict_exit_code(summary) == 1


def _main_result(strict_field):
    counts = {"p0": 0, "p1": 0, "p2": 0, "manual": 0}
    if strict_field:
        counts[strict_field] = 1
    return {
        "run_root": "synthetic",
        "audit_authority": {"authoritative": True, "reasons": []},
        "truth_summary": {},
        "artifact_count": 0,
        "p0_conclusion": {
            "count": counts["p0"],
            "passed": counts["p0"] == 0,
            "bad_rows": [],
        },
        "user_p1_conclusion": {
            "count": counts["p1"],
            "category_rows": [],
            "field_mismatch_rows": [],
        },
        "p2_conclusion": {
            "count": counts["p2"],
            "passed": counts["p2"] == 0,
            "bad_rows": [],
        },
        "manual_check_rows": [{}] * counts["manual"],
        "matched_rows": [],
    }


@pytest.mark.parametrize(
    ("strict_field", "expected_code"),
    [("p0", 1), ("p1", 1), ("p2", 1), ("manual", 1), (None, 0)],
)
def test_main_exits_from_every_strict_category(monkeypatch, tmp_path, strict_field, expected_code):
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / f"{strict_field or 'clean'}.json"
    run_root = tmp_path / "run"
    run_root.mkdir()
    manifest_path.write_text(
        json.dumps({"summary": _complete_summary(), "included": [], "excluded": [], "pending_review": []}),
        encoding="utf-8",
    )
    monkeypatch.setattr(audit, "compare", lambda manifest, root: _main_result(strict_field))
    monkeypatch.setattr(sys, "argv", [
        "strict_truth_audit.py",
        "--truth-manifest", str(manifest_path),
        "--run-root", str(run_root),
        "--output", str(output_path),
    ])

    with pytest.raises(SystemExit) as exc_info:
        audit.main()

    assert exc_info.value.code == expected_code
    assert output_path.exists()
    assert output_path.with_suffix(".md").exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["exit_code"] == expected_code
    assert payload["candidate_revision"]
    assert payload["generated_at_utc"].endswith("Z")


def test_main_fails_closed_when_audit_is_not_authoritative(monkeypatch, tmp_path):
    manifest_path = tmp_path / "manifest.json"
    output_path = tmp_path / "diagnostic.json"
    run_root = tmp_path / "run"
    run_root.mkdir()
    manifest_path.write_text(
        json.dumps(
            {
                "summary": _complete_summary(),
                "included": [],
                "excluded": [],
                "pending_review": [],
            }
        ),
        encoding="utf-8",
    )
    result = _main_result(None)
    result["audit_authority"] = {
        "authoritative": False,
        "reasons": ["missing_run_evidence"],
    }
    monkeypatch.setattr(audit, "compare", lambda manifest, root: result)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "strict_truth_audit.py",
            "--truth-manifest",
            str(manifest_path),
            "--run-root",
            str(run_root),
            "--output",
            str(output_path),
        ],
    )

    with pytest.raises(SystemExit) as exc_info:
        audit.main()

    assert exc_info.value.code == 1
    assert json.loads(output_path.read_text(encoding="utf-8"))["exit_code"] == 1


def test_main_rejects_duplicate_truth_ids_with_clear_cli_error(monkeypatch, tmp_path):
    manifest_path = tmp_path / "manifest.json"
    run_root = tmp_path / "run"
    run_root.mkdir()
    manifest_path.write_text(json.dumps({
        "summary": {**_complete_summary(), "included_count": 2},
        "included": [{"truth_id": "t1"}, {"truth_id": "t1"}],
        "excluded": [],
        "pending_review": [],
    }), encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [
        "strict_truth_audit.py",
        "--truth-manifest", str(manifest_path),
        "--run-root", str(run_root),
    ])

    with pytest.raises(SystemExit, match="invalid truth manifest: duplicate truth_id 't1'") as exc_info:
        audit.main()

    assert exc_info.value.code != 0


def test_hotel_pair_inference_reports_two_by_two_ambiguity():
    rows = [
        {"truth_id": "hotel-invoice-1", "truth_type": "住宿发票", "document_role": "invoice", "invoice_date": "2026-07-01", "amount": "200.00"},
        {"truth_id": "hotel-invoice-2", "truth_type": "住宿发票", "document_role": "invoice", "invoice_date": "2026-07-01", "amount": "200.00"},
        {"truth_id": "hotel-folio-1", "truth_type": "住宿水单", "document_role": "hotel_folio", "invoice_date": "2026-07-01", "amount": "200.00"},
        {"truth_id": "hotel-folio-2", "truth_type": "住宿水单", "document_role": "hotel_folio", "invoice_date": "2026-07-01", "amount": "200.00"},
    ]

    pairs = audit.infer_required_hotel_pairs(rows)

    assert pairs == [{
        "pair_key": "hotel:20260701:200.00",
        "status": "ambiguous",
        "invoice_truth_ids": ["hotel-invoice-1", "hotel-invoice-2"],
        "companion_truth_ids": ["hotel-folio-1", "hotel-folio-2"],
        "reason": "multiple_hotel_pairings_share_date_and_amount",
    }]


def test_ride_pair_inference_reports_two_by_two_ambiguity():
    rows = [
        {"truth_id": "ride-invoice-1", "truth_type": "打车", "document_role": "invoice", "amount": "100.00"},
        {"truth_id": "ride-invoice-2", "truth_type": "打车", "document_role": "invoice", "amount": "100.00"},
        {"truth_id": "ride-itinerary-1", "truth_type": "打车行程单", "document_role": "itinerary", "amount": "100.00"},
        {"truth_id": "ride-itinerary-2", "truth_type": "打车行程单", "document_role": "itinerary", "amount": "100.00"},
    ]

    pairs = audit.infer_required_ride_pairs(rows)

    assert pairs == [{
        "pair_key": "ride:100.00",
        "status": "ambiguous",
        "invoice_truth_ids": ["ride-invoice-1", "ride-invoice-2"],
        "companion_truth_ids": ["ride-itinerary-1", "ride-itinerary-2"],
        "reason": "multiple_ride_pairings_share_amount",
    }]
