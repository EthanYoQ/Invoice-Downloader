import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from pathlib import Path

import fitz

from run_evidence import validate_finalized_run_evidence
from truth_contracts import TruthContractError, TruthManifest


CAPTURE_KINDS = {"archive", "archived", "manual_check", "manual_review"}
OCR_COMPAT_TRANSLATION = str.maketrans({"⻔": "门", "⻝": "食", "⻨": "麦", "⻆": "角"})
ARCHIVE_FOLDERS = {
    "打车": "打车",
    "行程单": "打车",
    "火车票": "火车票",
    "机票": "机票",
    "住宿发票": "住宿发票",
    "住宿水单": "住宿发票",
    "餐饮": "餐饮",
    "过路费": "过路费",
    "定额发票": "定额发票",
    "其他": "其他",
    "航班行程单": "机票",
    "住宿确认单": "住宿发票",
    "差旅服务费": "差旅服务费",
    "非目标公司发票": "非目标公司发票",
    "个人非报销发票": "个人非报销发票",
}


def normalize_ocr_compat_text(value) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).translate(OCR_COMPAT_TRANSLATION)


def get_archive_folder(document_type: str) -> str:
    return ARCHIVE_FOLDERS.get(str(document_type or ""), str(document_type or "其他"))


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


def extract_output_pdf_fields(path: Path) -> dict:
    try:
        with fitz.open(path) as document:
            text = "\n".join(
                document.load_page(index).get_text("text") or ""
                for index in range(min(2, len(document)))
            )
    except Exception:
        return {}

    fields = parse_final_archive_fields(str(path))
    invoice_match = re.search(
        r"(?:发票号码|Invoice\s*Number)\s*[:：]?\s*([0-9]{8,20})",
        text,
        flags=re.IGNORECASE,
    )
    if not invoice_match:
        invoice_match = re.search(r"(?<!\d)(\d{20})(?!\d)", text)
    if invoice_match:
        fields["invoice_number"] = norm_invoice(invoice_match.group(1))
    return fields


def load_lineage_bindings(run_root: Path) -> tuple[dict[str, str] | None, list[str]]:
    evidence_path = run_root / "diagnostics" / "run_evidence.json"
    if not evidence_path.is_file():
        return None, ["missing_run_evidence"]
    try:
        evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, ["invalid_run_evidence"]
    try:
        return validate_finalized_run_evidence(evidence, run_root), []
    except ValueError as exc:
        return None, [str(exc) or "invalid_run_evidence"]


def load_artifacts(run_root: Path) -> tuple[list[dict], dict[str, str], dict]:
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
    lineage_bindings, authority_reasons = load_lineage_bindings(run_root)
    if output_root.exists():
        resolved_output_root = output_root.resolve()
        artifacts_by_path = {
            str(Path(item.get("path", "")).resolve()).lower(): item
            for item in by_doc.values()
            if item.get("path")
        }
        if lineage_bindings is not None:
            for resolved in lineage_bindings:
                if resolved in artifacts_by_path:
                    continue
                path = Path(resolved)
                item = by_doc.setdefault(
                    f"lineage:{resolved}",
                    {
                        "document_id": f"lineage:{resolved}",
                        "kind": "archive",
                        "path": str(path),
                        "category": path.parent.name,
                        "display_type": path.parent.name,
                    },
                )
                artifacts_by_path[resolved] = item
        lineage_backed_items = set()
        for resolved, item in artifacts_by_path.items():
            path = Path(resolved)
            try:
                is_run_output = path.is_relative_to(resolved_output_root)
            except ValueError:
                is_run_output = False
            if not is_run_output or not path.is_file():
                continue
            try:
                digest = sha256_file(path)
            except OSError:
                digest = ""
            if lineage_bindings is not None:
                expected_hash = lineage_bindings.get(resolved)
                inventory_only_terminal = (
                    is_retention_artifact(item)
                    or item.get("kind") in {"manual_check", "manual_review"}
                    or item.get("used_manual_check")
                    or path.parent.name in {"待人工复核", "Manual_Check"}
                )
                if not expected_hash and inventory_only_terminal:
                    continue
                if not expected_hash or digest != expected_hash:
                    if "lineage_output_mismatch" not in authority_reasons:
                        authority_reasons.append("lineage_output_mismatch")
                    continue
                lineage_backed_items.add(id(item))
            if path.suffix.lower() in {".pdf", ".xml", ".ofd"}:
                if digest:
                    file_hashes[digest] = str(path)
            if path.suffix.lower() != ".pdf":
                continue
            fields = extract_output_pdf_fields(path)
            for key in ("invoice_number", "date", "amount", "seller"):
                if fields.get(key) and not item.get(key):
                    item[key] = fields[key]
        if lineage_bindings is not None:
            by_doc = {
                key: item
                for key, item in by_doc.items()
                if id(item) in lineage_backed_items
            }
    for item in by_doc.values():
        p = Path(item.get("path", ""))
        if p.exists() and p.is_file():
            try:
                item["sha256"] = sha256_file(p)
            except OSError:
                item["sha256"] = ""
    authority = {
        "authoritative": not authority_reasons,
        "reasons": authority_reasons,
    }
    return list(by_doc.values()), file_hashes, authority


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


MATCH_METHODS = (
    "invoice_number",
    "sha256",
    "source_email_id+file_name",
    "source_email_id+amount+seller",
    "date+amount+seller",
)


def _candidate_artifact_indexes(row: dict, artifacts: list[dict], output_hashes: dict) -> tuple[list[int], str]:
    invoice = norm_invoice(row.get("invoice_number"))
    if invoice:
        matches = [index for index, artifact in enumerate(artifacts) if invoice == artifact.get("invoice_number")]
        if matches:
            return matches, "invoice_number"

    truth_sha = row.get("sha256", "")
    if truth_sha and truth_sha in output_hashes:
        matched_path = str(Path(output_hashes[truth_sha]).resolve()).lower()
        matches = [
            index for index, artifact in enumerate(artifacts)
            if artifact.get("path") and str(Path(artifact["path"]).resolve()).lower() == matched_path
        ]
        if matches:
            return matches, "sha256"

    source_email = str(row.get("source_email_id", ""))
    file_name = row.get("file_name", "")
    matches = [
        index for index, artifact in enumerate(artifacts)
        if source_email
        and source_email == str(artifact.get("email_id", ""))
        and file_name
        and file_name == artifact.get("source_filename", "")
    ]
    if matches:
        return matches, "source_email_id+file_name"

    amount = norm_amount(row.get("amount", ""))
    seller = row.get("seller", "")
    matches = [
        index for index, artifact in enumerate(artifacts)
        if source_email
        and source_email == str(artifact.get("email_id", ""))
        and amount
        and amount == artifact.get("amount")
        and contains_fuzzy(seller, artifact.get("seller", ""))
    ]
    if matches:
        return matches, "source_email_id+amount+seller"

    date = norm_date(row.get("invoice_date", ""))
    matches = [
        index for index, artifact in enumerate(artifacts)
        if amount
        and amount == artifact.get("amount")
        and date
        and date == artifact.get("date")
        and contains_fuzzy(seller, artifact.get("seller", ""))
    ]
    if matches:
        return matches, "date+amount+seller"
    return [], "no_match"


def _maximum_matching(adjacency: dict[int, list[int]], rows: list[int], excluded_artifacts=frozenset()) -> dict[int, int]:
    artifact_to_row = {}

    def augment(row_index: int, seen: set[int]) -> bool:
        for artifact_index in adjacency.get(row_index, []):
            if artifact_index in excluded_artifacts or artifact_index in seen:
                continue
            seen.add(artifact_index)
            owner = artifact_to_row.get(artifact_index)
            if owner is None or augment(owner, seen):
                artifact_to_row[artifact_index] = row_index
                return True
        return False

    for row_index in rows:
        augment(row_index, set())
    return {row_index: artifact_index for artifact_index, row_index in artifact_to_row.items()}


def _possible_artifact_memberships(
    adjacency: dict[int, list[int]],
    rows: list[int],
    excluded_artifacts=frozenset(),
) -> dict[int, list[int]]:
    maximum_size = len(_maximum_matching(adjacency, rows, excluded_artifacts))
    possible = {}
    for row_index in rows:
        other_rows = [index for index in rows if index != row_index]
        possible[row_index] = [
            artifact_index
            for artifact_index in adjacency.get(row_index, [])
            if len(_maximum_matching(
                adjacency,
                other_rows,
                excluded_artifacts | {artifact_index},
            )) + 1 == maximum_size
        ]
    return possible


def validate_truth_ids(rows: list[dict]) -> None:
    seen = set()
    for index, row in enumerate(rows):
        truth_id = str(row.get("truth_id") or "")
        if not truth_id.strip():
            raise ValueError(f"included truth row at index {index} must have a nonempty truth_id")
        if truth_id in seen:
            raise ValueError(f"duplicate truth_id '{truth_id}' in included truth rows")
        seen.add(truth_id)


def assign_truth_matches(rows: list[dict], artifacts: list[dict], output_hashes: dict) -> dict[str, tuple[dict | None, str]]:
    validate_truth_ids(rows)
    assignment_artifacts = list(artifacts)
    known_paths = {
        str(Path(artifact.get("path", "")).resolve()).lower()
        for artifact in assignment_artifacts
        if artifact.get("path")
    }
    for row in rows:
        truth_sha = row.get("sha256", "")
        matched_path = output_hashes.get(truth_sha) if truth_sha else None
        if not matched_path:
            continue
        resolved = str(Path(matched_path).resolve()).lower()
        if resolved in known_paths:
            continue
        parent = Path(matched_path).parent.name
        assignment_artifacts.append({
            "path": matched_path,
            "sha256": truth_sha,
            "category": parent,
            "display_type": parent,
            "used_manual_check": parent == "待人工复核",
            "kind": "manual_check" if parent == "待人工复核" else "archive",
        })
        known_paths.add(resolved)

    candidates = {}
    methods = {}
    for row_index, row in enumerate(rows):
        indexes, method = _candidate_artifact_indexes(row, assignment_artifacts, output_hashes)
        candidates[row_index] = sorted(
            indexes,
            key=lambda index: (
                is_retention_artifact(assignment_artifacts[index]),
                str(assignment_artifacts[index].get("path", "")),
            ),
        )
        methods[row_index] = method

    assigned = {
        str(row.get("truth_id", "")): (None, "no_match")
        for row in rows
    }
    used_artifacts = set()
    assigned_rows = set()
    for method in ("invoice_number", "sha256"):
        method_rows = sorted(
            (row_index for row_index in range(len(rows)) if methods[row_index] == method),
            key=lambda index: str(rows[index].get("truth_id", "")),
        )
        adjacency = {}
        for row_index in method_rows:
            available = [index for index in candidates[row_index] if index not in used_artifacts]
            best_retention_rank = min(
                (is_retention_artifact(assignment_artifacts[index]) for index in available),
                default=False,
            )
            adjacency[row_index] = [
                index for index in available
                if is_retention_artifact(assignment_artifacts[index]) == best_retention_rank
            ]
        possible_by_row = _possible_artifact_memberships(adjacency, method_rows, used_artifacts)
        strong_edges = []
        for row_index, possible_artifacts in possible_by_row.items():
            truth_id = str(rows[row_index].get("truth_id", ""))
            if len(possible_artifacts) > 1:
                assigned[truth_id] = (None, "ambiguous_match")
                continue
            for artifact_index in possible_artifacts:
                strong_edges.append((
                    MATCH_METHODS.index(method),
                    is_retention_artifact(assignment_artifacts[artifact_index]),
                    str(assignment_artifacts[artifact_index].get("path", "")),
                    truth_id,
                    row_index,
                    artifact_index,
                ))
        for *_, row_index, artifact_index in sorted(strong_edges):
            if row_index in assigned_rows or artifact_index in used_artifacts:
                continue
            truth_id = str(rows[row_index].get("truth_id", ""))
            assigned[truth_id] = (assignment_artifacts[artifact_index], method)
            assigned_rows.add(row_index)
            used_artifacts.add(artifact_index)

    composite_rows = [
        row_index for row_index in range(len(rows))
        if row_index not in assigned_rows and methods[row_index] not in {"invoice_number", "sha256", "no_match"}
    ]
    composite_rows.sort(key=lambda index: str(rows[index].get("truth_id", "")))
    adjacency = {
        row_index: [index for index in candidates[row_index] if index not in used_artifacts]
        for row_index in composite_rows
    }
    baseline_size = len(_maximum_matching(adjacency, composite_rows, used_artifacts))
    for row_index in composite_rows:
        other_rows = [index for index in composite_rows if index != row_index]
        possible_artifacts = []
        for artifact_index in adjacency[row_index]:
            remaining = _maximum_matching(adjacency, other_rows, used_artifacts | {artifact_index})
            if len(remaining) + 1 == baseline_size:
                possible_artifacts.append(artifact_index)
        can_be_unmatched = len(_maximum_matching(adjacency, other_rows, used_artifacts)) == baseline_size
        truth_id = str(rows[row_index].get("truth_id", ""))
        if len(possible_artifacts) == 1 and not can_be_unmatched:
            assigned[truth_id] = (assignment_artifacts[possible_artifacts[0]], methods[row_index])
        elif possible_artifacts:
            assigned[truth_id] = (None, "ambiguous_match")
    return assigned


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
    for (date, amount), group_rows in sorted(groups.items()):
        invoices = [
            row for row in group_rows
            if row.get("truth_type") == "住宿发票" and row.get("document_role") != "hotel_folio"
        ]
        folios = [
            row for row in group_rows
            if row.get("truth_type") == "住宿水单" or row.get("document_role") == "hotel_folio"
        ]
        invoice_truth_ids = sorted(str(row.get("truth_id", "")) for row in invoices)
        companion_truth_ids = sorted(str(row.get("truth_id", "")) for row in folios)
        if len(invoices) == 1 and len(folios) == 1:
            required.append({
                "pair_key": f"hotel:{date}:{amount}",
                "status": "required",
                "invoice_truth_ids": invoice_truth_ids,
                "companion_truth_ids": companion_truth_ids,
                "reason": "single_hotel_invoice_and_folio_share_date_and_amount",
            })
        elif invoices and folios:
            required.append({
                "pair_key": f"hotel:{date}:{amount}",
                "status": "ambiguous",
                "invoice_truth_ids": invoice_truth_ids,
                "companion_truth_ids": companion_truth_ids,
                "reason": "multiple_hotel_pairings_share_date_and_amount",
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

    adjacency = {
        invoice_index: {
            itinerary_index for itinerary_index, itinerary in enumerate(itineraries)
            if _amounts_match_for_ride(
                norm_amount(invoice.get("amount", "")),
                norm_amount(itinerary.get("amount", "")),
            )
        }
        for invoice_index, invoice in enumerate(invoices)
    }
    required = []
    visited_invoices = set()
    visited_itineraries = set()
    for start_invoice in range(len(invoices)):
        if start_invoice in visited_invoices or not adjacency[start_invoice]:
            continue
        component_invoices = set()
        component_itineraries = set()
        pending_invoices = [start_invoice]
        while pending_invoices:
            invoice_index = pending_invoices.pop()
            if invoice_index in component_invoices:
                continue
            component_invoices.add(invoice_index)
            for itinerary_index in adjacency[invoice_index]:
                if itinerary_index in component_itineraries:
                    continue
                component_itineraries.add(itinerary_index)
                pending_invoices.extend(
                    other_invoice
                    for other_invoice, matches in adjacency.items()
                    if itinerary_index in matches and other_invoice not in component_invoices
                )
        visited_invoices.update(component_invoices)
        visited_itineraries.update(component_itineraries)
        invoice_truth_ids = sorted(str(invoices[index].get("truth_id", "")) for index in component_invoices)
        companion_truth_ids = sorted(str(itineraries[index].get("truth_id", "")) for index in component_itineraries)
        amounts = sorted({norm_amount(invoices[index].get("amount", "")) for index in component_invoices})
        pair_key = f"ride:{'+'.join(amounts)}"
        is_required = len(component_invoices) == 1 and len(component_itineraries) == 1
        required.append({
            "pair_key": pair_key,
            "status": "required" if is_required else "ambiguous",
            "invoice_truth_ids": invoice_truth_ids,
            "companion_truth_ids": companion_truth_ids,
            "reason": (
                "single_ride_invoice_and_itinerary_have_compatible_amounts"
                if is_required else "multiple_ride_pairings_share_amount"
            ),
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
        if pair["status"] == "ambiguous":
            bad_rows.append(pair)
            continue
        invoice_artifact = matched_by_truth_id.get(pair["invoice_truth_ids"][0])
        folio_artifact = matched_by_truth_id.get(pair["companion_truth_ids"][0])
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
        if pair["status"] == "ambiguous":
            bad_rows.append(pair)
            continue
        invoice_artifact = matched_by_truth_id.get(pair["invoice_truth_ids"][0])
        itinerary_artifact = matched_by_truth_id.get(pair["companion_truth_ids"][0])
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
    included_rows = manifest.get("included", [])
    validate_truth_ids(included_rows)
    artifacts, output_hashes, authority = load_artifacts(run_root)
    assignments = assign_truth_matches(included_rows, artifacts, output_hashes)
    p0_rows = []
    matched_rows = []
    manual_check_rows = []
    user_p1_rows = []
    field_mismatch_rows = []
    matched_by_truth_id = {}

    for row in included_rows:
        artifact, method = assignments[str(row.get("truth_id", ""))]
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

    p2_conclusion = evaluate_p2_pairs(manifest, matched_by_truth_id)
    return {
        "run_root": str(run_root),
        "audit_authority": authority,
        "gate_passed": bool(
            authority["authoritative"]
            and not p0_rows
            and not user_p1_rows
            and not field_mismatch_rows
            and not manual_check_rows
            and p2_conclusion["count"] == 0
        ),
        "truth_summary": manifest.get("summary", {}),
        "artifact_count": len(artifacts),
        "p0_conclusion": {
            "count": len(p0_rows),
            "passed": bool(authority["authoritative"] and len(p0_rows) == 0),
            "bad_rows": p0_rows,
        },
        "user_p1_conclusion": {
            "definition": "classification/category or archived field mismatch per current user request",
            "count": len({r["truth_id"] for r in user_p1_rows + field_mismatch_rows}),
            "category_rows": user_p1_rows,
            "field_mismatch_rows": field_mismatch_rows,
        },
        "p2_conclusion": p2_conclusion,
        "manual_check_rows": manual_check_rows,
        "matched_rows": matched_rows,
    }


def write_markdown(result: dict, path: Path):
    lines = [
        "# Strict Truth Audit",
        "",
        f"- Authoritative: `{result.get('audit_authority', {}).get('authoritative', False)}`",
        f"- Authority reasons: `{', '.join(result.get('audit_authority', {}).get('reasons', [])) or 'none'}`",
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


def write_json_atomic(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise


def candidate_revision(run_root: Path) -> str:
    config_path = run_root / "monitoring" / "run_config.json"
    if config_path.exists():
        try:
            revision = str(json.loads(config_path.read_text(encoding="utf-8")).get("candidate_revision") or "").strip()
            if revision:
                return revision
        except (OSError, json.JSONDecodeError):
            pass
    override = os.environ.get("INVOICEFLOW_CANDIDATE_REVISION", "").strip()
    if override:
        return override
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def audit_run_id(run_root: Path) -> str:
    config_path = run_root / "monitoring" / "run_config.json"
    if config_path.exists():
        try:
            run_id = str(json.loads(config_path.read_text(encoding="utf-8")).get("run_id") or "").strip()
            if run_id:
                return run_id
        except (OSError, json.JSONDecodeError):
            pass
    return run_root.resolve().name


def strict_exit_code(summary: dict) -> int:
    counts = {key: int(summary.get(key, 0) or 0) for key in ("p0", "p1", "p2", "manual")}
    return 1 if any(counts.values()) else 0


def main():
    parser = argparse.ArgumentParser(description="Strictly compare a finalized invoice truth set with a batch run.")
    parser.add_argument("--truth-manifest", required=True)
    parser.add_argument("--run-root", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    try:
        raw_manifest = json.loads(Path(args.truth_manifest).read_text(encoding="utf-8"))
        validate_truth_ids(raw_manifest.get("included", []))
        manifest = TruthManifest.from_mapping(raw_manifest).to_mapping()
    except (OSError, json.JSONDecodeError, TruthContractError, ValueError) as exc:
        raise SystemExit(f"invalid truth manifest: {exc}") from None
    try:
        result = compare(manifest, Path(args.run_root))
    except ValueError as exc:
        raise SystemExit(f"invalid truth manifest: {exc}") from None
    output = Path(args.output) if args.output else Path(args.truth_manifest).with_name("strict_truth_audit_result.json")
    summary_counts = {
        "p0": result["p0_conclusion"]["count"],
        "p1": result["user_p1_conclusion"]["count"],
        "p2": result["p2_conclusion"]["count"],
        "manual": len(result["manual_check_rows"]),
    }
    result["generated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    result["run_id"] = audit_run_id(Path(args.run_root))
    result["candidate_revision"] = candidate_revision(Path(args.run_root))
    result["exit_code"] = (
        strict_exit_code(summary_counts)
        if result.get("audit_authority", {}).get("authoritative")
        else 1
    )
    write_json_atomic(output, result)
    markdown_output = output.with_suffix(".md")
    write_markdown(result, markdown_output)
    comparison_report = output.parent / "comparison_report.md"
    if comparison_report.name != markdown_output.name:
        comparison_report.write_text(markdown_output.read_text(encoding="utf-8"), encoding="utf-8")
    summary = {
        "p0_count": result["p0_conclusion"]["count"],
        "p0_passed": result["p0_conclusion"]["passed"],
        "user_p1_count": result["user_p1_conclusion"]["count"],
        "p2_count": result["p2_conclusion"]["count"],
        "p2_passed": result["p2_conclusion"]["passed"],
        "manual_check_count": len(result["manual_check_rows"]),
        "authoritative": result.get("audit_authority", {}).get("authoritative", False),
        "authority_reasons": result.get("audit_authority", {}).get("reasons", []),
        "output": str(output),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    raise SystemExit(result["exit_code"])


if __name__ == "__main__":
    main()
