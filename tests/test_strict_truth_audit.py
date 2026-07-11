import copy
import json
import sys

import pytest

import strict_truth_audit as audit


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
        json.dumps({"summary": {"finalized": True, "pending_review_count": 0}, "included": []}),
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


def test_main_rejects_duplicate_truth_ids_with_clear_cli_error(monkeypatch, tmp_path):
    manifest_path = tmp_path / "manifest.json"
    run_root = tmp_path / "run"
    run_root.mkdir()
    manifest_path.write_text(json.dumps({
        "summary": {"finalized": True, "pending_review_count": 0},
        "included": [{"truth_id": "t1"}, {"truth_id": "t1"}],
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
