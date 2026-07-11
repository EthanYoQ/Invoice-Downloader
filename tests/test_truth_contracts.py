import ast
import copy
import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from truth_contracts import TruthContractError, TruthManifest


REAL_MANIFEST = Path(
    r"C:\Vibe Coding Project\Invoice-Downloader\test_dataset"
    r"\qq_20251125_20260614_rebuilt_20260614_1035\truth_manifest.json"
)
FORBIDDEN_MODULES = {
    "app_api",
    "archive_service",
    "candidate_pipeline",
    "invoice_extractor",
}


def _valid_manifest() -> dict:
    return {
        "summary": {
            "dataset": "qq_fixture",
            "date_from": "2026-06-01",
            "date_to": "2026-06-13",
            "before_exclusive": "2026-06-14",
            "mailbox": "INBOX",
            "account_domain": "qq.com",
            "target_company": "目标公司",
            "included_count": 2,
            "excluded_count": 1,
            "pending_review_count": 0,
            "finalized": True,
        },
        "included": [
            {
                "truth_id": "invoice-1",
                "truth_status": "included",
                "source_email_id": "100",
                "mail_date_local": "2026-06-10 10:00:00",
                "source_kind": "attachment",
                "file_name": "invoice.pdf",
                "document_role": "invoice",
                "truth_type": "住宿发票",
                "expected_category": "住宿发票",
                "invoice_date": "2026-06-10",
                "seller": "标准商户",
                "purchaser": "目标公司",
                "amount": "1000.00",
                "invoice_number": "12345678",
                "invoice_code": "",
                "sha256": "a" * 64,
                "pair_key": "hotel-1",
                "evidence": [{"sha256": "a" * 64, "bytes": 100}],
            },
            {
                "truth_id": "folio-1",
                "truth_status": "included",
                "source_email_id": "101",
                "mail_date_local": "2026-06-10 11:00:00",
                "source_kind": "attachment",
                "file_name": "folio.pdf",
                "document_role": "hotel_folio",
                "truth_type": "住宿水单",
                "expected_category": "住宿水单",
                "invoice_date": "2026-06-10",
                "seller": "标准酒店",
                "purchaser": "个人",
                "amount": "1000.00",
                "invoice_number": "",
                "invoice_code": "",
                "sha256": "b" * 64,
                "pair_key": "hotel-1",
                "evidence": [{"sha256": "b" * 64, "bytes": 200}],
            },
        ],
        "excluded": [
            {
                "email_id": "102",
                "mail_date_local": "2026-06-11 12:00:00",
                "source_kind": "attachment",
                "file_name": "logo.png",
                "sha256": "c" * 64,
                "reason": "non_invoice_image",
            }
        ],
        "pending_review": [],
    }


def test_real_finalized_manifest_is_parse_compatible_and_frozen():
    parsed = TruthManifest.from_path(REAL_MANIFEST)

    assert parsed.finalized is True
    assert parsed.account_channel == "qq"
    assert len(parsed.included) == 215
    assert len(parsed.excluded) == 253
    assert parsed.pending_review == ()
    assert parsed.included[0].amount.as_tuple().exponent == -2
    with pytest.raises(FrozenInstanceError):
        parsed.included[0].seller = "mutated"


@pytest.mark.parametrize(
    ("mutation", "code"),
    [
        (lambda value: value["summary"].update(finalized=False), "manifest_not_finalized"),
        (lambda value: value["summary"].update(pending_review_count=1), "pending_review_not_zero"),
        (lambda value: value["included"][1].update(truth_id="invoice-1"), "duplicate_truth_id"),
        (lambda value: value["included"][0].update(truth_id=""), "missing_truth_id"),
        (lambda value: value["included"][0].update(amount="NaN"), "invalid_amount"),
        (lambda value: value["included"][0].update(invoice_date="2026-02-30"), "invalid_invoice_date"),
        (lambda value: value["included"][0].update(sha256="not-a-hash"), "invalid_artifact_hash"),
        (lambda value: value["included"][0].update(source_email_id=""), "missing_source_identity"),
        (lambda value: value["included"][0].update(document_role=""), "missing_document_role"),
        (lambda value: value["included"][0].update(expected_category=""), "missing_expected_category"),
        (lambda value: value["included"][0].update(truth_status="suspected"), "invalid_truth_decision"),
        (lambda value: value["summary"].update(account_domain="example.com"), "unsupported_account_channel"),
        (lambda value: value["summary"].update(before_exclusive="2026-06-13"), "invalid_date_scope"),
        (lambda value: value["included"][1].update(amount="999.00"), "missing_pair_evidence"),
    ],
)
def test_invalid_truth_is_rejected_with_deterministic_code(mutation, code):
    payload = _valid_manifest()
    mutation(payload)

    with pytest.raises(TruthContractError) as exc_info:
        TruthManifest.from_mapping(payload)

    assert exc_info.value.code == code
    assert "suspect" not in str(exc_info.value).lower()


def test_manifest_parser_does_not_mutate_input():
    payload = _valid_manifest()
    original = copy.deepcopy(payload)

    TruthManifest.from_mapping(payload)

    assert payload == original


def _direct_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.add(node.module.split(".")[0])
    return imports


def test_truth_and_strict_audit_import_boundary_is_independent():
    root = Path(__file__).resolve().parents[1]
    allowed_local = {"truth_contracts"}
    pending = [root / "truth_contracts.py", root / "strict_truth_audit.py"]
    visited = set()
    all_imports = set()
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        visited.add(path)
        imports = _direct_imports(path)
        all_imports.update(imports)
        for module in imports & allowed_local:
            pending.append(root / f"{module}.py")

    assert all_imports.isdisjoint(FORBIDDEN_MODULES)
    assert "document_types" not in all_imports


def test_truth_builder_does_not_use_runtime_extractor_or_classifier_rules():
    root = Path(__file__).resolve().parents[1]
    path = root / "build_truth_dataset.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports = _direct_imports(path)
    forbidden_calls = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Name) and owner.id in {"InvoiceExtractor", "extractor"}:
            forbidden_calls.append(f"{owner.id}.{node.func.attr}")

    assert "invoice_extractor" not in imports
    assert "candidate_pipeline" not in imports
    assert "archive_service" not in imports
    assert forbidden_calls == []


def test_serialized_contract_preserves_public_manifest_field_names():
    payload = _valid_manifest()
    parsed = TruthManifest.from_mapping(payload)

    roundtrip = parsed.to_mapping()

    assert set(roundtrip) == {"summary", "included", "excluded", "pending_review"}
    assert roundtrip["included"][0]["truth_id"] == "invoice-1"
    assert roundtrip["included"][0]["amount"] == "1000.00"
    assert json.dumps(roundtrip, ensure_ascii=False)
