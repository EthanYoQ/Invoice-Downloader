from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import fitz
import pytest

from artifact_verifier import verify_final_artifact


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _truth(**overrides):
    value = {
        "truth_id": "truth-1",
        "sha256": hashlib.sha256(b"valid-source-evidence").hexdigest(),
        "invoice_number": "12345678",
        "invoice_code": "",
        "invoice_date": "2026-06-10",
        "amount": "100.00",
        "seller": "Standard Merchant",
        "purchaser": "Target Company",
        "document_role": "invoice",
        "truth_type": "餐饮",
    }
    value.update(overrides)
    return value


def _pdf(path: Path, text: str | None) -> Path:
    document = fitz.open()
    page = document.new_page()
    if text:
        page.insert_text((72, 72), text)
    document.save(path)
    document.close()
    return path


def _styled_pdf(
    path: Path,
    spans: list[tuple[tuple[float, float], str, tuple[float, float, float]]],
    *,
    tiny_mark: bool = False,
) -> Path:
    document = fitz.open()
    page = document.new_page()
    for point, text, color in spans:
        page.insert_text(point, text, color=color)
    if tiny_mark:
        page.draw_rect(fitz.Rect(1, 1, 1.1, 1.1), color=(0, 0, 0), fill=(0, 0, 0))
    document.save(path)
    document.close()
    return path


def test_transformed_arbitrary_blank_and_corrupt_pdf_fail_closed(tmp_path: Path):
    arbitrary = tmp_path / "arbitrary.pdf"
    arbitrary.write_bytes(b"not a pdf")
    blank = _pdf(tmp_path / "blank.pdf", None)
    corrupt = _pdf(tmp_path / "corrupt.pdf", "Invoice Number: 12345678")
    corrupt.write_bytes(corrupt.read_bytes()[:20])

    for path in (arbitrary, blank, corrupt):
        verdict = verify_final_artifact(
            _truth(),
            path,
            output_sha256=_sha(path),
            source_chain_sha256s=[_truth()["sha256"]],
        )
        assert verdict.passed is False
        assert verdict.manual_required is True
        assert verdict.reason_code in {
                "FINAL_FORMAT_INVALID",
                "FINAL_CONTENT_BLANK",
                "FINAL_CONTENT_UNPARSEABLE",
                "FINAL_VISUAL_BLANK",
            }


def test_valid_transformed_pdf_with_invoice_identity_passes(tmp_path: Path):
    path = _pdf(
        tmp_path / "invoice.pdf",
        "Invoice Number: 12345678\nInvoice Date: 2026-06-10\n"
        "Total Amount: 100.00\nSeller: Standard Merchant",
    )

    verdict = verify_final_artifact(
        _truth(),
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=[_truth()["sha256"]],
    )

    assert verdict.passed is True
    assert verdict.verification_mode == "transformed_content_identity"
    assert set(verdict.matched_fields) == {
        "invoice_number",
        "invoice_date",
        "amount",
        "seller",
    }


def test_regenerated_pdf_requires_explicit_semantic_source_identity(tmp_path: Path):
    truth = _truth(invoice_number="26110000000000000001")
    path = _pdf(
        tmp_path / "regenerated.pdf",
        "Invoice Number: 26110000000000000001\nInvoice Date: 2026-06-10\n"
        "Total Amount: 100.00\nSeller: Standard Merchant",
    )
    strict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
    )

    semantic = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert strict.passed is False
    assert strict.reason_code == "SOURCE_LINEAGE_MISSING"
    assert semantic.passed is True
    assert semantic.verification_mode == "semantic_source_identity"
    assert set(semantic.matched_fields) >= {
        "invoice_number",
        "invoice_date",
        "amount",
        "seller",
    }


def test_semantic_identity_rejects_invoice_number_only_referenced_in_remarks(
    tmp_path: Path,
):
    truth = _truth(invoice_number="26110000000000000001")
    path = _pdf(
        tmp_path / "wrong-invoice.pdf",
        "Invoice Number: 99999999999999999999\n"
        "Invoice Date: 2026-06-10\nTotal Amount: 100.00\n"
        "Seller: Standard Merchant\n"
        "Remark - related invoice: 26110000000000000001",
    )

    verdict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert verdict.passed is False
    assert verdict.reason_code == "FINAL_FIELD_VALUE_MISMATCH"


def test_semantic_identity_rejects_original_invoice_number_label_in_remarks(
    tmp_path: Path,
):
    truth = _truth(invoice_number="26110000000000000001")
    path = _pdf(
        tmp_path / "credit-note.pdf",
        "Invoice Number: 99999999999999999999\n"
        "Remark - Original Invoice Number: 26110000000000000001\n"
        "Invoice Date: 2026-06-10\nTotal Amount: 100.00\n"
        "Seller: Standard Merchant",
    )

    verdict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert verdict.passed is False
    assert verdict.reason_code == "FINAL_FIELD_VALUE_MISMATCH"


def test_semantic_identity_uses_first_field_value_not_later_same_row_reference(
    tmp_path: Path,
):
    truth = _truth(invoice_number="26110000000000000001")
    path = _styled_pdf(
        tmp_path / "same-row-reference.pdf",
        [
            ((72, 72), "Invoice Number:", (0, 0, 0)),
            ((180, 72), "99999999999999999999", (0, 0, 0)),
            ((330, 72), "Red", (0, 0, 0)),
            ((370, 72), "26110000000000000001", (0, 0, 0)),
            ((72, 110), "Invoice Date: 2026-06-10", (0, 0, 0)),
            ((72, 140), "Total Amount: 100.00", (0, 0, 0)),
            ((72, 170), "Seller: Standard Merchant", (0, 0, 0)),
        ],
    )

    verdict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert verdict.passed is False
    assert verdict.reason_code == "FINAL_FIELD_VALUE_MISMATCH"


def test_semantic_receipt_uses_current_order_not_referenced_order(tmp_path: Path):
    truth = _truth(
        invoice_number="778080227734",
        invoice_code="MTFKT9WLF9",
        seller="Cloud Merchant",
    )
    path = _pdf(
        tmp_path / "wrong-receipt.pdf",
        "EMAIL_BODY_RECEIPT_CANONICAL\n"
        "Document No: 778080227734\nOrder Number: WRONGCODE0\n"
        "Original Receipt Order Number: MTFKT9WLF9\n"
        "Invoice Date: 2026-06-10\nTotal Amount: 100.00\n"
        "Seller: Cloud Merchant",
    )

    verdict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert verdict.passed is False
    assert verdict.reason_code == "FINAL_FIELD_VALUE_MISMATCH"


def test_semantic_receipt_rejects_only_qualified_order_reference(tmp_path: Path):
    truth = _truth(
        invoice_number="778080227734",
        invoice_code="MTFKT9WLF9",
        seller="Cloud Merchant",
    )
    path = _pdf(
        tmp_path / "referenced-order-only.pdf",
        "EMAIL_BODY_RECEIPT_CANONICAL\n"
        "Document No: 778080227734\n"
        "Original Receipt Order Number: MTFKT9WLF9\n"
        "Invoice Date: 2026-06-10\nTotal Amount: 100.00\n"
        "Seller: Cloud Merchant",
    )

    verdict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert verdict.passed is False
    assert verdict.reason_code == "FINAL_FIELD_NOT_LABELED"


@pytest.mark.parametrize("value_y, expected_pass", [(72, True), (84, False)])
def test_semantic_field_binding_requires_same_visual_baseline(
    tmp_path: Path, value_y: int, expected_pass: bool
):
    truth = _truth(invoice_number="26110000000000000001")
    path = _styled_pdf(
        tmp_path / f"baseline-{value_y}.pdf",
        [
            ((72, 72), "Invoice Number:", (0, 0, 0)),
            ((180, value_y), "26110000000000000001", (0, 0, 0)),
            ((72, 110), "Invoice Date: 2026-06-10", (0, 0, 0)),
            ((72, 140), "Total Amount: 100.00", (0, 0, 0)),
            ((72, 170), "Seller: Standard Merchant", (0, 0, 0)),
        ],
    )

    verdict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert verdict.passed is expected_pass


def test_semantic_receipt_accepts_labeled_order_and_document_numbers(tmp_path: Path):
    truth = _truth(
        invoice_number="778080227734",
        invoice_code="MTFKT9WLF9",
        seller="Cloud Merchant",
    )
    path = _pdf(
        tmp_path / "receipt.pdf",
        "EMAIL_BODY_RECEIPT_CANONICAL\n"
        "Document No: 778080227734\nOrder Number: MTFKT9WLF9\n"
        "Invoice Date: 2026-06-10\nTotal Amount: 100.00\n"
        "Seller: Cloud Merchant",
    )

    verdict = verify_final_artifact(
        truth,
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=["f" * 64],
        allow_semantic_source_identity=True,
    )

    assert verdict.passed is True


def test_white_on_white_and_near_blank_pdf_fail_visual_verification(tmp_path: Path):
    white = _styled_pdf(
        tmp_path / "white-text.pdf",
        [((72, 72), "Invoice Number 12345678", (1, 1, 1))],
    )
    near_blank = _styled_pdf(
        tmp_path / "near-blank.pdf",
        [((72, 72), "Invoice Number 12345678", (1, 1, 1))],
        tiny_mark=True,
    )

    for path in (white, near_blank):
        verdict = verify_final_artifact(
            _truth(),
            path,
            output_sha256=_sha(path),
            source_chain_sha256s=[_truth()["sha256"]],
        )
        assert verdict.passed is False
        assert verdict.manual_required is True
        assert verdict.reason_code in {
            "FINAL_VISUAL_BLANK",
            "FINAL_FIELD_NOT_VISIBLE",
        }


def test_off_page_identity_text_cannot_satisfy_visual_verification(tmp_path: Path):
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, page.rect.height + 100), "Invoice Number 12345678")
    path = tmp_path / "off-page.pdf"
    document.save(path)
    document.close()

    verdict = verify_final_artifact(
        _truth(),
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=[_truth()["sha256"]],
    )

    assert verdict.passed is False
    assert verdict.manual_required is True


def test_visible_split_sequence_in_independent_blocks_passes(tmp_path: Path):
    path = _styled_pdf(
        tmp_path / "visible-split.pdf",
        [
            ((72, 72), "Invoice", (0, 0, 0)),
            ((160, 72), "Number", (0, 0, 0)),
            ((245, 72), "1234", (0, 0, 0)),
            ((290, 72), "5678", (0, 0, 0)),
            ((72, 110), "Invoice Date 2026-06-10", (0, 0, 0)),
            ((72, 140), "Total Amount 100.00", (0, 0, 0)),
            ((72, 170), "Seller Standard Merchant", (0, 0, 0)),
        ],
    )

    verdict = verify_final_artifact(
        _truth(),
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=[_truth()["sha256"]],
    )

    assert verdict.passed is True
    assert set(verdict.matched_fields) == {
        "invoice_number",
        "invoice_date",
        "amount",
        "seller",
    }


def test_number_only_and_number_plus_date_fail_completeness_quorum(tmp_path: Path):
    number_only = _pdf(tmp_path / "number-only.pdf", "Invoice Number: 12345678")
    number_and_date = _pdf(
        tmp_path / "number-date-only.pdf",
        "Invoice Number: 12345678\nInvoice Date: 2026-06-10",
    )

    for path in (number_only, number_and_date):
        verdict = verify_final_artifact(
            _truth(),
            path,
            output_sha256=_sha(path),
            source_chain_sha256s=[_truth()["sha256"]],
        )
        assert verdict.passed is False
        assert verdict.manual_required is True
        assert verdict.reason_code == "FINAL_QUORUM_MISMATCH"


def test_supporting_document_date_only_fails_role_aware_quorum(tmp_path: Path):
    path = _pdf(
        tmp_path / "support-date-only.pdf",
        "Document Role: hotel_folio\nInvoice Date: 2026-06-10",
    )

    verdict = verify_final_artifact(
        _truth(invoice_number="", document_role="hotel_folio"),
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=[_truth()["sha256"]],
    )

    assert verdict.passed is False
    assert verdict.manual_required is True
    assert verdict.reason_code == "FINAL_QUORUM_MISMATCH"


def test_distant_words_cannot_be_concatenated_into_strong_identity(tmp_path: Path):
    document = fitz.open()
    page = document.new_page()
    page.draw_rect(page.rect, color=(0.8, 0.8, 0.8), fill=(0.8, 0.8, 0.8))
    page.insert_text((72, 72), "1234")
    page.insert_text((72, 700), "5678")
    path = tmp_path / "distant-sequence.pdf"
    document.save(path)
    document.close()

    verdict = verify_final_artifact(
        _truth(),
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=[_truth()["sha256"]],
    )

    assert verdict.passed is False


def test_valid_transformed_xml_without_number_requires_multifield_quorum(tmp_path: Path):
    path = tmp_path / "folio.xml"
    path.write_text(
        "<Document><InvoiceDate>2026-06-10</InvoiceDate><TotalAmount>100.00</TotalAmount>"
        "<SellerName>Standard Merchant</SellerName><DocumentRole>hotel_folio</DocumentRole></Document>",
        encoding="utf-8",
    )

    verdict = verify_final_artifact(
        _truth(invoice_number="", document_role="hotel_folio"),
        path,
        output_sha256=_sha(path),
        source_chain_sha256s=[_truth()["sha256"]],
    )

    assert verdict.passed is True
    assert set(verdict.matched_fields) >= {"invoice_date", "amount", "seller"}


def test_unchanged_artifact_requires_exact_truth_hash(tmp_path: Path):
    path = tmp_path / "source.bin"
    path.write_bytes(b"exact-source")
    digest = _sha(path)

    verdict = verify_final_artifact(
        _truth(sha256=digest),
        path,
        output_sha256=digest,
        source_chain_sha256s=[digest],
    )

    assert verdict.passed is True
    assert verdict.verification_mode == "unchanged_sha256"


def test_artifact_verifier_has_no_runtime_classifier_or_model_imports():
    path = Path(__file__).resolve().parents[1] / "artifact_verifier.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    assert imports.isdisjoint(
        {
            "app_api",
            "invoice_extractor",
            "candidate_pipeline",
            "archive_service",
            "document_types",
            "company_rules",
            "glm_runtime",
        }
    )
