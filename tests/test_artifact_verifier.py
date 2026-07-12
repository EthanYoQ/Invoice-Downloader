from __future__ import annotations

import ast
import hashlib
from pathlib import Path

import fitz

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
    assert verdict.matched_fields == ("invoice_number",)


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
