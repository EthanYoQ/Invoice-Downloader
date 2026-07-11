from __future__ import annotations

from datetime import date
from decimal import Decimal
import random

from pairing_engine import PairingDocument, pair_documents


def doc(
    document_id: str,
    role: str,
    amount: str | None,
    *,
    business_date: date | None = date(2026, 6, 1),
    provider: str = "",
    merchant_tokens: frozenset[str] = frozenset(),
    source_message_uid: str = "",
) -> PairingDocument:
    return PairingDocument(
        id=document_id,
        role=role,
        amount=Decimal(amount) if amount is not None else None,
        business_date=business_date,
        provider=provider,
        merchant_tokens=merchant_tokens,
        source_message_uid=source_message_uid,
        path=f"C:/archive/{document_id}.pdf",
    )


def pair_ids(result):
    return tuple((invoice.id, companion.id) for invoice, companion in result.pairs)


def test_ride_pairing_never_crosses_known_providers():
    invoices = [
        doc("di", "ride_invoice", "100.00", provider="didi"),
        doc("ga", "ride_invoice", "100.00", provider="gaode"),
    ]
    companions = [
        doc("git", "ride_itinerary", "100.00", provider="gaode"),
        doc("dit", "ride_itinerary", "100.00", provider="didi"),
    ]

    result = pair_documents("ride", invoices, companions)

    assert set(pair_ids(result)) == {("di", "dit"), ("ga", "git")}
    assert result.ambiguities == ()


def test_equal_score_membership_tie_is_not_guessed():
    invoices = [
        doc("hi1", "hotel_invoice", "500.00"),
        doc("hi2", "hotel_invoice", "500.00"),
    ]
    folios = [
        doc("hf1", "hotel_folio", "500.00"),
        doc("hf2", "hotel_folio", "500.00"),
    ]

    result = pair_documents("hotel", invoices, folios)

    assert result.pairs == ()
    assert tuple(item.id for item in result.unmatched_invoices) == ("hi1", "hi2")
    assert tuple(item.id for item in result.unmatched_companions) == ("hf1", "hf2")
    assert len(result.ambiguities) == 1
    assert result.ambiguities[0].document_ids == ("hf1", "hf2", "hi1", "hi2")
    assert result.ambiguities[0].reason == "multiple_optimal_pair_memberships"


def test_assignment_uses_each_document_at_most_once():
    invoices = [
        doc("i1", "ride_invoice", "100.00", source_message_uid="m1"),
        doc("i2", "ride_invoice", "100.00", source_message_uid="m2"),
    ]
    itineraries = [doc("t1", "ride_itinerary", "100.00", source_message_uid="m1")]

    result = pair_documents("ride", invoices, itineraries)

    assert pair_ids(result) == (("i1", "t1"),)
    assert tuple(item.id for item in result.unmatched_invoices) == ("i2",)
    assert result.unmatched_companions == ()


def test_pairing_is_deterministic_under_shuffled_input():
    invoices = [
        doc("i1", "ride_invoice", "100.00", provider="didi"),
        doc("i2", "ride_invoice", "200.00", provider="gaode"),
    ]
    itineraries = [
        doc("t1", "ride_itinerary", "100.00", provider="didi"),
        doc("t2", "ride_itinerary", "200.00", provider="gaode"),
    ]
    expected = (("i1", "t1"), ("i2", "t2"))

    for seed in range(10):
        shuffled_invoices = invoices[:]
        shuffled_itineraries = itineraries[:]
        random.Random(seed).shuffle(shuffled_invoices)
        random.Random(seed + 100).shuffle(shuffled_itineraries)
        assert pair_ids(pair_documents("ride", shuffled_invoices, shuffled_itineraries)) == expected


def test_ride_pairing_accepts_three_percent_tax_tolerance_but_not_beyond_it():
    invoices = [
        doc("accepted", "ride_invoice", "100.00"),
        doc("rejected", "ride_invoice", "200.00"),
    ]
    itineraries = [
        doc("taxed", "ride_itinerary", "103.00"),
        doc("too_far", "ride_itinerary", "207.00"),
    ]

    result = pair_documents("ride", invoices, itineraries)

    assert pair_ids(result) == (("accepted", "taxed"),)
    assert tuple(item.id for item in result.unmatched_invoices) == ("rejected",)
    assert tuple(item.id for item in result.unmatched_companions) == ("too_far",)


def test_hotel_pairing_requires_dates_within_three_days():
    invoice = doc("invoice", "hotel_invoice", "500.00", business_date=date(2026, 6, 1))
    folios = [
        doc("within", "hotel_folio", "500.00", business_date=date(2026, 6, 4)),
        doc("outside", "hotel_folio", "500.00", business_date=date(2026, 6, 5)),
    ]

    result = pair_documents("hotel", [invoice], folios)

    assert pair_ids(result) == (("invoice", "within"),)
    assert tuple(item.id for item in result.unmatched_companions) == ("outside",)


def test_incompatible_and_missing_amount_documents_are_unmatched():
    invoices = [
        doc("missing", "ride_invoice", None),
        doc("wrong_role", "hotel_invoice", "100.00"),
    ]
    itineraries = [doc("itinerary", "ride_itinerary", "100.00")]

    result = pair_documents("ride", invoices, itineraries)

    assert result.pairs == ()
    assert tuple(item.id for item in result.unmatched_invoices) == ("missing", "wrong_role")
    assert tuple(item.id for item in result.unmatched_companions) == ("itinerary",)
    assert result.ambiguities == ()


def test_higher_scoring_merchant_and_uid_evidence_breaks_amount_tie():
    invoices = [
        doc("i1", "hotel_invoice", "500.00", merchant_tokens=frozenset({"sheraton"}), source_message_uid="m1"),
        doc("i2", "hotel_invoice", "500.00", merchant_tokens=frozenset({"marriott"}), source_message_uid="m2"),
    ]
    folios = [
        doc("f2", "hotel_folio", "500.00", merchant_tokens=frozenset({"marriott"}), source_message_uid="m2"),
        doc("f1", "hotel_folio", "500.00", merchant_tokens=frozenset({"sheraton"}), source_message_uid="m1"),
    ]

    result = pair_documents("hotel", invoices, folios)

    assert pair_ids(result) == (("i1", "f1"), ("i2", "f2"))
    assert result.ambiguities == ()


def test_archive_ride_adapter_uses_filename_provider_without_changing_wrapper_shape():
    from archive_pairing import assign_ride_pairs, match_ride_pairs

    invoices = [
        {"filename": "20260605_打车_100.00_滴滴出行.pdf", "amount": "100.00", "date": "20260605", "seller": "滴滴出行"},
        {"filename": "20260605_打车_100.00_高德打车.pdf", "amount": "100.00", "date": "20260605", "seller": "高德打车"},
    ]
    itineraries = [
        {"filename": "20260605_行程单_100.00_高德打车.pdf", "amount": "100.00", "date": "20260605", "seller": "高德打车"},
        {"filename": "20260605_行程单_100.00_滴滴出行.pdf", "amount": "100.00", "date": "20260605", "seller": "滴滴出行"},
    ]

    assignment = assign_ride_pairs(invoices, itineraries)

    assert {(a["seller"], b["seller"]) for a, b in assignment.pairs} == {
        ("滴滴出行", "滴滴出行"),
        ("高德打车", "高德打车"),
    }
    assert match_ride_pairs(invoices, itineraries) == list(assignment.pairs)


def test_archive_hotel_adapter_exposes_ambiguity_without_guessing_wrapper_pairs():
    from archive_pairing import assign_hotel_pairs, match_hotel_pairs

    invoices = [
        {"document_id": "i1", "filename": "20260601_住宿发票_500.00_酒店.pdf", "amount": "500.00", "date": "20260601"},
        {"document_id": "i2", "filename": "20260601_住宿发票_500.00_酒店.pdf", "amount": "500.00", "date": "20260601"},
    ]
    folios = [
        {"document_id": "f1", "filename": "20260601_住宿水单_500.00_酒店.pdf", "amount": "500.00", "date": "20260601"},
        {"document_id": "f2", "filename": "20260601_住宿水单_500.00_酒店.pdf", "amount": "500.00", "date": "20260601"},
    ]

    assignment = assign_hotel_pairs(invoices, folios)

    assert assignment.pairs == ()
    assert len(assignment.ambiguities) == 1
    assert match_hotel_pairs(invoices, folios) == []
