import argparse
import hashlib
import json
import re
from pathlib import Path

from document_types import get_archive_folder
from invoice_extractor import normalize_ocr_compat_text


CAPTURE_KINDS = {"archive", "manual_check", "manual_review"}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append({"_parse_error": line[:200]})
    return rows


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def norm_invoice(value) -> str:
    text = str(value or "")
    if not text:
        return ""
    m = re.search(r"(\d{8,})", text)
    return m.group(1) if m else re.sub(r"\W+", "", text).lower()


def norm_amount(value) -> str:
    if value in (None, ""):
        return ""
    try:
        return f"{float(str(value).replace(',', '')):.2f}"
    except ValueError:
        return str(value)


def norm_date(value) -> str:
    text = re.sub(r"\D", "", str(value or ""))
    if len(text) >= 8:
        return text[:8]
    return text


def contains_fuzzy(expected: str, actual: str) -> bool:
    expected = re.sub(r"\s+", "", normalize_ocr_compat_text(expected))
    actual = re.sub(r"\s+", "", normalize_ocr_compat_text(actual))
    if not expected or not actual:
        return False
    return expected in actual or actual in expected


def parse_final_archive_fields(path_value: str, fallback_year: str = "") -> dict:
    name = Path(path_value or "").stem
    fields = {}
    train_match = re.match(r"^(20\d{6})-.+-火车票$", name)
    if train_match:
        fields["date"] = train_match.group(1)
        return fields

    hotel_match = re.match(r"^(20\d{6})-住宿-\d{2}-(发票|水单)_([0-9]+\.[0-9]{2})元$", name)
    if hotel_match:
        fields["date"] = hotel_match.group(1)
        fields["amount"] = norm_amount(hotel_match.group(3))
        return fields

    ride_match = re.match(r"^(\d{4})-(滴滴|高德)-\d{2}-(发票|行程单)_([0-9]+\.[0-9]{2})元$", name)
    if ride_match:
        fields["amount"] = norm_amount(ride_match.group(4))
        return fields

    standard_match = re.match(r"^(20\d{6})_([^_]+)_([0-9]+\.[0-9]{2})_(.+)$", name)
    if standard_match:
        fields["date"] = standard_match.group(1)
        fields["amount"] = norm_amount(standard_match.group(3))
        fields["seller"] = normalize_ocr_compat_text(standard_match.group(4))
        return fields
    return fields


def source_email_from_path(value: str) -> str:
    m = re.search(r"[\\/](?:staging|raw_documents)[\\/](\d{4})_", value or "")
    return m.group(1) if m else ""


def is_retention_artifact(artifact: dict) -> bool:
    path = str(artifact.get("path", "") or "").replace("\\", "/").lower()
    category = str(artifact.get("category", "") or "").lower()
    return (
        artifact.get("kind") == "retention"
        or "/_audit_retention/" in path
        or category in {"duplicates", "controlled_run_non_provider_url", "retention"}
    )


def amount_candidates_for_field_check(row: dict, artifact: dict) -> list[str]:
    candidates = []
    for value in (artifact.get("amount"),):
        amount = norm_amount(value)
        if amount and amount not in candidates:
            candidates.append(amount)

    if row.get("truth_type") == "打车" and row.get("document_role") == "invoice":
        for value in (artifact.get("extracted_amount"), artifact.get("final_amount")):
            amount = norm_amount(value)
            if amount and amount not in candidates:
                candidates.append(amount)
    return candidates


def load_artifacts(run_root: Path) -> list[dict]:
    events = read_jsonl(run_root / "monitoring" / "artifact_events.jsonl")
    traces = read_jsonl(run_root / "diagnostics" / "debug_trace.jsonl")
    by_doc = {}

    for event in events:
        if event.get("kind") not in CAPTURE_KINDS:
            continue
        doc = event.get("document_id") or event.get("path") or event.get("file_name")
        item = by_doc.setdefault(doc, {})
        item.update({
            "document_id": event.get("document_id", doc),
            "kind": event.get("kind"),
            "email_id": str(event.get("email_id") or event.get("metadata", {}).get("email_id") or ""),
            "source_filename": event.get("original_filename") or event.get("file_name") or event.get("metadata", {}).get("file_name") or "",
            "path": event.get("path", ""),
            "category": event.get("category", ""),
            "display_type": event.get("final_type", "") or event.get("display_type", ""),
            "seller_event": event.get("seller", ""),
        })

    for trace in traces:
        doc = trace.get("document_id") or trace.get("archive_target") or trace.get("source_filename")
        item = by_doc.setdefault(doc, {})
        nf = trace.get("normalized_fields") or {}
        raw_fields = (trace.get("extractor_raw_result") or {}).get("result") or {}
        cls = trace.get("classification_result") or {}
        naming = trace.get("naming_result") or {}
        source_path = trace.get("source_path", "")
        category = cls.get("category") or cls.get("final_type") or naming.get("target_folder") or item.get("category", "")
        date_value = norm_date(nf.get("Date", ""))
        if category == "火车票" and raw_fields.get("Departure_Date"):
            date_value = norm_date(raw_fields.get("Departure_Date"))
        fallback_year = date_value[:4] if len(date_value) >= 4 else ""
        final_path = trace.get("archive_target") or naming.get("final_path") or item.get("path", "")
        final_fields = parse_final_archive_fields(final_path, fallback_year=fallback_year)
        document_type_text = " ".join(
            str(value or "")
            for value in (
                nf.get("Type"),
                naming.get("display_type"),
                cls.get("final_type"),
                category,
            )
        )
        preserve_extracted_date = any(token in document_type_text for token in ("住宿水单", "水单", "行程单"))
        extracted_amount = norm_amount(nf.get("Amount", ""))
        final_amount = final_fields.get("amount", "")
        item.update({
            "document_id": trace.get("document_id", doc),
            "source_filename": trace.get("source_filename") or item.get("source_filename", ""),
            "email_id": item.get("email_id") or source_email_from_path(source_path),
            "path": final_path,
            "category": category,
            "display_type": naming.get("display_type", ""),
            "used_manual_check": bool(naming.get("used_manual_check")),
            "invoice_number": norm_invoice(nf.get("InvoiceNumber", "")),
            "date": date_value if preserve_extracted_date else (final_fields.get("date") or date_value),
            "amount": final_amount or extracted_amount,
            "final_amount": final_amount,
            "extracted_amount": extracted_amount,
            "seller": final_fields.get("seller") or normalize_ocr_compat_text(nf.get("Seller", "") or item.get("seller_event", "")),
            "purchaser": nf.get("Purchaser", ""),
            "is_invoice": nf.get("is_invoice"),
            "source_path": source_path,
            "combine_result": trace.get("combine_result") or item.get("combine_result", {}),
        })

    output_root = run_root / "output"
    file_hashes = {}
    if output_root.exists():
        for path in output_root.rglob("*"):
            if path.is_file() and path.suffix.lower() in {".pdf", ".xml", ".ofd"}:
                try:
                    file_hashes[sha256_file(path)] = str(path)
                except OSError:
                    pass
    for item in by_doc.values():
        p = Path(item.get("path", ""))
        if p.exists() and p.is_file():
            try:
                item["sha256"] = sha256_file(p)
            except OSError:
                item["sha256"] = ""
    return list(by_doc.values()), file_hashes


def match_truth(row: dict, artifacts: list[dict], output_hashes: dict) -> tuple[dict | None, str]:
    invoice = norm_invoice(row.get("invoice_number"))
    source_email = str(row.get("source_email_id", ""))
    file_name = row.get("file_name", "")
    truth_sha = row.get("sha256", "")
    amount = norm_amount(row.get("amount", ""))
    seller = row.get("seller", "")
    date = norm_date(row.get("invoice_date", ""))

    if invoice:
        invoice_matches = [art for art in artifacts if invoice == art.get("invoice_number")]
        if invoice_matches:
            invoice_matches.sort(key=lambda art: (is_retention_artifact(art), str(art.get("path", ""))))
            return invoice_matches[0], "invoice_number"

    if truth_sha and truth_sha in output_hashes:
        matched_path = output_hashes[truth_sha]
        matched_resolved = str(Path(matched_path).resolve()).lower()
        for art in artifacts:
            art_path = art.get("path", "")
            if art_path and str(Path(art_path).resolve()).lower() == matched_resolved:
                return art, "sha256"
        parent = Path(matched_path).parent.name
        return {
            "path": matched_path,
            "sha256": truth_sha,
            "category": parent,
            "display_type": parent,
            "used_manual_check": parent == "待人工复核",
            "kind": "manual_check" if parent == "待人工复核" else "archive",
        }, "sha256"

    for art in artifacts:
        if source_email and source_email == str(art.get("email_id", "")) and file_name and file_name == art.get("source_filename", ""):
            return art, "source_email_id+file_name"

    for art in artifacts:
        if source_email and source_email == str(art.get("email_id", "")) and amount and amount == art.get("amount") and contains_fuzzy(seller, art.get("seller", "")):
            return art, "source_email_id+amount+seller"

    for art in artifacts:
        if amount and amount == art.get("amount") and date and date == art.get("date") and contains_fuzzy(seller, art.get("seller", "")):
            return art, "date+amount+seller"

    return None, "no_match"


def category_matches_expected(row: dict, artifact: dict) -> bool:
    expected = row.get("expected_category", "")
    if not expected:
        return True
    actual_category = artifact.get("category", "")
    actual_display = artifact.get("display_type", "") or artifact.get("final_type", "")
    if expected in {actual_category, actual_display}:
        return True
    expected_archive = get_archive_folder(expected)
    if expected_archive != actual_category:
        return False
    if expected == "住宿水单":
        filename = Path(artifact.get("path", "")).name.lower()
        return actual_display == expected or any(token in filename for token in ["水单", "folio", "账单", "明细"])
    return True


def _hotel_pair_key(row: dict) -> tuple[str, str]:
    return norm_date(row.get("invoice_date", "")), norm_amount(row.get("amount", ""))


def infer_required_hotel_pairs(rows: list[dict]) -> list[dict]:
    groups = {}
    for row in rows:
        truth_type = row.get("truth_type", "")
        role = row.get("document_role", "")
        if truth_type not in {"住宿发票", "住宿水单"} and role != "hotel_folio":
            continue
        date, amount = _hotel_pair_key(row)
        if not date or not amount:
            continue
        groups.setdefault((date, amount), []).append(row)

    required = []
    for (date, amount), group_rows in groups.items():
        invoices = [
            row for row in group_rows
            if row.get("truth_type") == "住宿发票" and row.get("document_role") != "hotel_folio"
        ]
        folios = [
            row for row in group_rows
            if row.get("truth_type") == "住宿水单" or row.get("document_role") == "hotel_folio"
        ]
        if len(invoices) == 1 and len(folios) == 1:
            required.append({
                "pair_key": f"hotel:{date}:{amount}",
                "date": date,
                "amount": amount,
                "invoice_truth_id": invoices[0].get("truth_id"),
                "folio_truth_id": folios[0].get("truth_id"),
            })
    return required


def _amounts_match_for_ride(left: str, right: str) -> bool:
    try:
        left_value = float(left)
        right_value = float(right)
    except (TypeError, ValueError):
        return False
    return (
        abs(left_value - right_value) < 0.01
        or abs(left_value * 1.03 - right_value) < 0.50
        or abs(right_value * 1.03 - left_value) < 0.50
    )


def infer_required_ride_pairs(rows: list[dict]) -> list[dict]:
    invoices = []
    itineraries = []
    for row in rows:
        truth_type = str(row.get("truth_type", ""))
        role = str(row.get("document_role", ""))
        if truth_type == "打车" and role == "invoice":
            invoices.append(row)
        elif "行程单" in truth_type or "itinerary" in role or "报销单" in role:
            itineraries.append(row)

    required = []
    used_itineraries = set()
    for invoice in invoices:
        invoice_amount = norm_amount(invoice.get("amount", ""))
        matches = []
        for index, itinerary in enumerate(itineraries):
            if index in used_itineraries:
                continue
            itinerary_amount = norm_amount(itinerary.get("amount", ""))
            if _amounts_match_for_ride(invoice_amount, itinerary_amount):
                matches.append((index, itinerary))
        if len(matches) == 1:
            index, itinerary = matches[0]
            used_itineraries.add(index)
            required.append({
                "pair_key": f"ride:{invoice_amount}:{invoice.get('truth_id')}:{itinerary.get('truth_id')}",
                "amount": invoice_amount,
                "invoice_truth_id": invoice.get("truth_id"),
                "itinerary_truth_id": itinerary.get("truth_id"),
            })
    return required


def hotel_pair_is_combined(invoice_artifact: dict, folio_artifact: dict) -> bool:
    invoice_result = invoice_artifact.get("combine_result") or {}
    folio_result = folio_artifact.get("combine_result") or {}
    if invoice_result.get("status") == "matched" and folio_result.get("status") == "matched":
        return True

    invoice_name = Path(invoice_artifact.get("path", "")).name
    folio_name = Path(folio_artifact.get("path", "")).name
    pattern = re.compile(r"^(\d{8})-住宿-(\d{2})-(发票|水单)_([0-9]+\.[0-9]{2})元", re.IGNORECASE)
    invoice_match = pattern.search(invoice_name)
    folio_match = pattern.search(folio_name)
    if not invoice_match or not folio_match:
        return False
    return (
        invoice_match.group(1) == folio_match.group(1)
        and invoice_match.group(2) == folio_match.group(2)
        and invoice_match.group(4) == folio_match.group(4)
        and {invoice_match.group(3), folio_match.group(3)} == {"发票", "水单"}
    )


def ride_pair_is_combined(invoice_artifact: dict, itinerary_artifact: dict) -> bool:
    invoice_result = invoice_artifact.get("combine_result") or {}
    itinerary_result = itinerary_artifact.get("combine_result") or {}
    if invoice_result.get("status") == "matched" and itinerary_result.get("status") == "matched":
        return True

    invoice_name = Path(invoice_artifact.get("path", "")).name
    itinerary_name = Path(itinerary_artifact.get("path", "")).name
    pattern = re.compile(r"^(\d{4})-(滴滴|高德)-(\d{2})-(发票|行程单)_([0-9]+\.[0-9]{2})元", re.IGNORECASE)
    invoice_match = pattern.search(invoice_name)
    itinerary_match = pattern.search(itinerary_name)
    if not invoice_match or not itinerary_match:
        return False
    return (
        invoice_match.group(1) == itinerary_match.group(1)
        and invoice_match.group(2) == itinerary_match.group(2)
        and invoice_match.group(3) == itinerary_match.group(3)
        and invoice_match.group(5) == itinerary_match.group(5)
        and {invoice_match.group(4), itinerary_match.group(4)} == {"发票", "行程单"}
    )


def evaluate_p2_pairs(manifest: dict, matched_by_truth_id: dict[str, dict]) -> dict:
    bad_rows = []
    for pair in infer_required_hotel_pairs(manifest.get("included", [])):
        invoice_artifact = matched_by_truth_id.get(pair["invoice_truth_id"])
        folio_artifact = matched_by_truth_id.get(pair["folio_truth_id"])
        if not invoice_artifact or not folio_artifact:
            continue
        if not hotel_pair_is_combined(invoice_artifact, folio_artifact):
            bad_rows.append({
                **pair,
                "reason": "required_hotel_pair_not_combined",
                "invoice_path": invoice_artifact.get("path", ""),
                "folio_path": folio_artifact.get("path", ""),
            })
    for pair in infer_required_ride_pairs(manifest.get("included", [])):
        invoice_artifact = matched_by_truth_id.get(pair["invoice_truth_id"])
        itinerary_artifact = matched_by_truth_id.get(pair["itinerary_truth_id"])
        if not invoice_artifact or not itinerary_artifact:
            continue
        if not ride_pair_is_combined(invoice_artifact, itinerary_artifact):
            bad_rows.append({
                **pair,
                "reason": "required_ride_pair_not_combined",
                "invoice_path": invoice_artifact.get("path", ""),
                "itinerary_path": itinerary_artifact.get("path", ""),
            })
    return {
        "definition": "required invoice/supporting-document pair is captured but not combined",
        "count": len(bad_rows),
        "passed": len(bad_rows) == 0,
        "bad_rows": bad_rows,
    }


def compare(manifest: dict, run_root: Path) -> dict:
    artifacts, output_hashes = load_artifacts(run_root)
    p0_rows = []
    matched_rows = []
    manual_check_rows = []
    user_p1_rows = []
    field_mismatch_rows = []
    matched_by_truth_id = {}

    for row in manifest.get("included", []):
        artifact, method = match_truth(row, artifacts, output_hashes)
        if not artifact:
            p0_rows.append({
                "truth_id": row.get("truth_id"),
                "source_email_id": row.get("source_email_id"),
                "file_name": row.get("file_name"),
                "invoice_number": row.get("invoice_number"),
                "seller": row.get("seller"),
                "amount": row.get("amount"),
                "reason": "truth included document has no captured archive/manual-review/output match",
            })
            continue

        matched = {
            "truth_id": row.get("truth_id"),
            "source_email_id": row.get("source_email_id"),
            "match_method": method,
            "matched_path": artifact.get("path", ""),
            "actual_category": artifact.get("category", ""),
            "expected_category": row.get("expected_category", ""),
        }
        matched_rows.append(matched)
        matched_by_truth_id[row.get("truth_id")] = artifact

        if artifact.get("used_manual_check") or artifact.get("kind") in {"manual_check", "manual_review"}:
            manual_check_rows.append({**matched, "reason": "captured through manual review route"})
            continue

        expected_category = row.get("expected_category", "")
        actual_category = artifact.get("category", "")
        if expected_category and actual_category and not category_matches_expected(row, artifact):
            user_p1_rows.append({**matched, "reason": "category_mismatch"})

        mismatches = []
        expected_amount = norm_amount(row.get("amount"))
        amount_candidates = amount_candidates_for_field_check(row, artifact)
        if expected_amount and amount_candidates and expected_amount not in amount_candidates:
            mismatch = {"field": "amount", "expected": expected_amount, "actual": artifact.get("amount")}
            if len(amount_candidates) > 1:
                mismatch["actual_candidates"] = amount_candidates
            mismatches.append(mismatch)
        expected_date = norm_date(row.get("invoice_date"))
        if expected_date and artifact.get("date") and expected_date != artifact.get("date"):
            mismatches.append({"field": "date", "expected": expected_date, "actual": artifact.get("date")})
        if row.get("seller") and artifact.get("seller") and not contains_fuzzy(row.get("seller"), artifact.get("seller")):
            mismatches.append({"field": "seller", "expected": row.get("seller"), "actual": artifact.get("seller")})
        if mismatches:
            field_mismatch_rows.append({**matched, "mismatches": mismatches})

    return {
        "run_root": str(run_root),
        "truth_summary": manifest.get("summary", {}),
        "artifact_count": len(artifacts),
        "p0_conclusion": {
            "count": len(p0_rows),
            "passed": len(p0_rows) == 0,
            "bad_rows": p0_rows,
        },
        "user_p1_conclusion": {
            "definition": "classification/category or archived field mismatch per current user request",
            "count": len({r["truth_id"] for r in user_p1_rows + field_mismatch_rows}),
            "category_rows": user_p1_rows,
            "field_mismatch_rows": field_mismatch_rows,
        },
        "p2_conclusion": evaluate_p2_pairs(manifest, matched_by_truth_id),
        "manual_check_rows": manual_check_rows,
        "matched_rows": matched_rows,
    }


def write_markdown(result: dict, path: Path):
    lines = [
        "# Strict Truth Audit",
        "",
        f"- P0 passed: `{result['p0_conclusion']['passed']}`",
        f"- P0 count: `{result['p0_conclusion']['count']}`",
        f"- User P1 count: `{result['user_p1_conclusion']['count']}`",
        f"- P2 passed: `{result.get('p2_conclusion', {}).get('passed')}`",
        f"- P2 count: `{result.get('p2_conclusion', {}).get('count')}`",
        f"- Manual check rows: `{len(result['manual_check_rows'])}`",
        "",
        "## P0 Bad Rows",
        "",
    ]
    if not result["p0_conclusion"]["bad_rows"]:
        lines.append("- none")
    else:
        for row in result["p0_conclusion"]["bad_rows"]:
            lines.append(f"- {row['truth_id']}: {row.get('invoice_number') or row.get('file_name')} / {row['seller']} / {row['amount']}")
    lines.extend(["", "## User P1 Category Rows", ""])
    category_rows = result["user_p1_conclusion"]["category_rows"]
    if not category_rows:
        lines.append("- none")
    else:
        for row in category_rows:
            lines.append(f"- {row['truth_id']}: expected `{row.get('expected_category')}`, actual `{row.get('actual_category')}`, path `{row.get('matched_path')}`")
    lines.extend(["", "## User P1 Field Mismatch Rows", ""])
    field_rows = result["user_p1_conclusion"]["field_mismatch_rows"]
    if not field_rows:
        lines.append("- none")
    else:
        for row in field_rows:
            parts = ", ".join(f"{m['field']} expected `{m['expected']}` actual `{m['actual']}`" for m in row.get("mismatches", []))
            lines.append(f"- {row['truth_id']}: {parts}, path `{row.get('matched_path')}`")
    lines.extend(["", "## P2 Pairing Rows", ""])
    p2_rows = result.get("p2_conclusion", {}).get("bad_rows", [])
    if not p2_rows:
        lines.append("- none")
    else:
        for row in p2_rows:
            lines.append(f"- {row['pair_key']}: {row['reason']}, invoice `{row.get('invoice_path')}`, folio `{row.get('folio_path')}`")
    path.write_text("\n".join(lines), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Strictly compare a finalized invoice truth set with a batch run.")
    parser.add_argument("--truth-manifest", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    manifest = json.loads(Path(args.truth_manifest).read_text(encoding="utf-8"))
    if manifest.get("summary", {}).get("finalized") is not True or manifest.get("summary", {}).get("pending_review_count") != 0:
        raise SystemExit("truth manifest is not finalized or pending_review_count is not 0")
    result = compare(manifest, Path(args.run_root))
    output = Path(args.output) if args.output else Path(args.truth_manifest).with_name("strict_truth_audit_result.json")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    markdown_output = output.with_suffix(".md")
    write_markdown(result, markdown_output)
    comparison_report = output.parent / "comparison_report.md"
    if comparison_report.name != markdown_output.name:
        comparison_report.write_text(markdown_output.read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps({
        "p0_count": result["p0_conclusion"]["count"],
        "p0_passed": result["p0_conclusion"]["passed"],
        "user_p1_count": result["user_p1_conclusion"]["count"],
        "p2_count": result["p2_conclusion"]["count"],
        "p2_passed": result["p2_conclusion"]["passed"],
        "manual_check_count": len(result["manual_check_rows"]),
        "output": str(output),
    }, ensure_ascii=False, indent=2))
    if result["p0_conclusion"]["count"]:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
