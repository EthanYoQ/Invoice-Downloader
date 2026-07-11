import copy

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


@pytest.mark.parametrize("field", ["p0", "p1", "p2", "manual"])
def test_any_strict_failure_returns_nonzero(field):
    summary = {"p0": 0, "p1": 0, "p2": 0, "manual": 0}
    summary[field] = 1

    assert audit.strict_exit_code(summary) == 1


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
