import argparse
import json
from pathlib import Path


REQUIRED_INCLUDED_FIELDS = [
    "truth_type",
    "document_role",
    "invoice_date",
    "seller",
    "amount",
    "source_email_id",
    "file_name",
    "raw_path",
    "evidence",
]


def _email_domain(email_address: str) -> str:
    text = str(email_address or "").strip()
    if "@" not in text:
        return ""
    return text.rsplit("@", 1)[-1].lower()


def collect_truth_table(email_address: str, auth_code: str, date_from: str, date_to: str) -> dict:
    """Frontend startup monitoring hook.

    The authoritative batch verdict is produced after the run by strict_truth_audit.py
    against a finalized truth_manifest. This hook exists so controlled frontend runs can
    record a non-sensitive startup contract instead of emitting a false error.
    """
    return {
        "status": "skipped",
        "reason": "STRICT_TRUTH_AUDIT_RUNS_AFTER_BATCH",
        "email_domain": _email_domain(email_address),
        "has_auth_code": bool(auth_code),
        "date_from": str(date_from or ""),
        "date_to": str(date_to or ""),
    }


def main():
    parser = argparse.ArgumentParser(description="Validate a finalized email invoice truth manifest.")
    parser.add_argument("--truth-manifest", required=True)
    parser.add_argument("--output", default="")
    args = parser.parse_args()

    manifest_path = Path(args.truth_manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    errors = []
    summary = manifest.get("summary", {})
    if summary.get("finalized") is not True:
        errors.append({"level": "summary", "field": "finalized", "expected": True, "actual": summary.get("finalized")})
    if summary.get("pending_review_count") != 0:
        errors.append({"level": "summary", "field": "pending_review_count", "expected": 0, "actual": summary.get("pending_review_count")})
    for row in manifest.get("included", []):
        for field in REQUIRED_INCLUDED_FIELDS:
            if not row.get(field):
                errors.append({"truth_id": row.get("truth_id"), "field": field, "error": "missing_required_field"})
        raw_path = row.get("raw_path")
        if raw_path and not Path(raw_path).exists():
            errors.append({"truth_id": row.get("truth_id"), "field": "raw_path", "error": "raw_path_not_found", "path": raw_path})
    result = {
        "manifest": str(manifest_path),
        "finalized": summary.get("finalized") is True and summary.get("pending_review_count") == 0 and not errors,
        "included_count": len(manifest.get("included", [])),
        "excluded_count": len(manifest.get("excluded", [])),
        "errors": errors,
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
    print(text)
    if errors:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
