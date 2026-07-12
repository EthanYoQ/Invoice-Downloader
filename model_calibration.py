from __future__ import annotations

import argparse
import hashlib
import json
import math
from dataclasses import asdict, dataclass
from decimal import Decimal
from pathlib import Path


REQUIRED_METRICS = (
    "artifact_id",
    "accepted",
    "truth_finalized",
    "schema_valid",
    "latency_ms",
    "p0",
    "p1",
    "p2",
    "manual",
    "entitlement_success",
    "model_name",
    "run_id",
    "role",
    "artifact_set_sha256",
)
MAX_SAFE_LATENCY_MS = Decimal("1000000000000")
MAX_SAFE_COUNT = 1_000_000_000


@dataclass(frozen=True)
class CalibrationVerdict:
    approved: bool
    accepted_identities_match: bool
    truth_finalized: bool
    schema_rate: float
    p0: int
    p1: int
    p2: int
    manual: int
    entitlement_success: bool
    reference_p50_ms: float
    reference_p95_ms: float
    candidate_p50_ms: float
    candidate_p95_ms: float
    reference_model_name: str
    candidate_model_name: str
    reference_run_id: str
    candidate_run_id: str
    reference_artifact_set_sha256: str
    candidate_artifact_set_sha256: str
    reasons: tuple[str, ...]

    def to_dict(self):
        return asdict(self)


def _invalid_verdict(reasons=("invalid_evidence",)):
    return CalibrationVerdict(
        approved=False,
        accepted_identities_match=False,
        truth_finalized=False,
        schema_rate=0.0,
        p0=0,
        p1=0,
        p2=0,
        manual=0,
        entitlement_success=False,
        reference_p50_ms=0.0,
        reference_p95_ms=0.0,
        candidate_p50_ms=0.0,
        candidate_p95_ms=0.0,
        reference_model_name="",
        candidate_model_name="",
        reference_run_id="",
        candidate_run_id="",
        reference_artifact_set_sha256="",
        candidate_artifact_set_sha256="",
        reasons=tuple(reasons),
    )


def compare_calibration(
    reference_path: Path,
    candidate_path: Path,
    *,
    reference_model=None,
    candidate_model=None,
) -> CalibrationVerdict:
    try:
        resolved_reference = Path(reference_path).resolve(strict=False)
        resolved_candidate = Path(candidate_path).resolve(strict=False)
    except (OSError, RuntimeError, TypeError, ValueError):
        return _invalid_verdict()
    if resolved_reference == resolved_candidate:
        return _invalid_verdict(("same_evidence_path",))
    try:
        reference = _load_jsonl(resolved_reference)
        candidate = _load_jsonl(resolved_candidate)
    except (OSError, ValueError, TypeError, OverflowError, ArithmeticError):
        return _invalid_verdict()
    reasons = []
    _validate_rows(reference, reasons)
    _validate_rows(candidate, reasons)

    reference_identity = _evidence_identity(reference, "reference", reasons)
    candidate_identity = _evidence_identity(candidate, "candidate", reasons)
    if (
        reference_identity["run_id"]
        and reference_identity["run_id"] == candidate_identity["run_id"]
    ):
        _add_reason(reasons, "same_run_id")
    if reference_model is not None and str(reference_model) != reference_identity["model_name"]:
        _add_reason(reasons, "model_argument_mismatch")
    if candidate_model is not None and str(candidate_model) != candidate_identity["model_name"]:
        _add_reason(reasons, "model_argument_mismatch")

    reference_ids = _accepted_identities(reference, reasons)
    candidate_ids = _accepted_identities(candidate, reasons)
    identities_match = bool(reference_ids) and reference_ids == candidate_ids
    if not identities_match:
        _add_reason(reasons, "accepted_artifact_identity_mismatch")
    if any(row.get("accepted") is not True for row in [*reference, *candidate]):
        _add_reason(reasons, "unaccepted_artifact")

    truth_finalized = bool(reference and candidate) and all(
        row.get("truth_finalized") is True for row in [*reference, *candidate]
    )
    if not truth_finalized:
        _add_reason(reasons, "truth_not_finalized")

    p0 = _sum_integer_metric(candidate, "p0", reasons)
    p1 = _sum_integer_metric(candidate, "p1", reasons)
    p2 = _sum_integer_metric(candidate, "p2", reasons)
    manual = _sum_integer_metric(candidate, "manual", reasons)
    for name, value in (("p0", p0), ("p1", p1), ("p2", p2), ("manual", manual)):
        if value != 0:
            _add_reason(reasons, f"{name}_nonzero")
    for name in ("p0", "p1", "p2", "manual"):
        if _sum_integer_metric(reference, name, reasons) != 0:
            _add_reason(reasons, f"{name}_nonzero")

    schema_rate = (
        sum(row.get("schema_valid") is True for row in candidate) / len(candidate)
        if candidate
        else 0.0
    )
    reference_schema_valid = all(row.get("schema_valid") is True for row in reference)
    if schema_rate != 1.0 or not reference_schema_valid:
        _add_reason(reasons, "schema_invalid")

    entitlement_success = bool(reference and candidate) and all(
        row.get("entitlement_success") is True for row in [*reference, *candidate]
    )
    if not entitlement_success:
        _add_reason(reasons, "entitlement_failed")

    reference_latencies = _latencies(reference, reasons)
    candidate_latencies = _latencies(candidate, reasons)
    reference_p50 = _percentile(reference_latencies, 50)
    reference_p95 = _percentile(reference_latencies, 95)
    candidate_p50 = _percentile(candidate_latencies, 50)
    candidate_p95 = _percentile(candidate_latencies, 95)
    if not reference_latencies or not candidate_latencies:
        _add_reason(reasons, "latency_regression")
    else:
        if candidate_p50 > reference_p50:
            _add_reason(reasons, "candidate_p50_regression")
            _add_reason(reasons, "latency_regression")
        if candidate_p95 > reference_p95:
            _add_reason(reasons, "candidate_p95_regression")
            _add_reason(reasons, "latency_regression")

    return CalibrationVerdict(
        approved=not reasons,
        accepted_identities_match=identities_match,
        truth_finalized=truth_finalized,
        schema_rate=round(schema_rate, 6),
        p0=p0,
        p1=p1,
        p2=p2,
        manual=manual,
        entitlement_success=entitlement_success,
        reference_p50_ms=reference_p50,
        reference_p95_ms=reference_p95,
        candidate_p50_ms=candidate_p50,
        candidate_p95_ms=candidate_p95,
        reference_model_name=reference_identity["model_name"],
        candidate_model_name=candidate_identity["model_name"],
        reference_run_id=reference_identity["run_id"],
        candidate_run_id=candidate_identity["run_id"],
        reference_artifact_set_sha256=reference_identity["artifact_set_sha256"],
        candidate_artifact_set_sha256=candidate_identity["artifact_set_sha256"],
        reasons=tuple(reasons),
    )


def _load_jsonl(path):
    rows = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                value = json.loads(
                    line,
                    parse_int=_parse_json_integer,
                    parse_float=Decimal,
                    parse_constant=Decimal,
                )
            except (json.JSONDecodeError, ValueError, ArithmeticError) as exc:
                raise ValueError(f"invalid JSONL row {line_number}") from exc
            if not isinstance(value, dict):
                raise ValueError(f"JSONL row {line_number} must be an object")
            rows.append(value)
    if not rows:
        raise ValueError("calibration evidence must not be empty")
    return rows


def _parse_json_integer(value):
    digits = value.lstrip("-")
    if len(digits) > 1000:
        return Decimal(value)
    return int(value)


def _validate_rows(rows, reasons):
    for row in rows:
        for field in REQUIRED_METRICS:
            if field not in row:
                _add_reason(reasons, f"missing_metric:{field}")
        if "artifact_id" in row and (not isinstance(row["artifact_id"], str) or not row["artifact_id"].strip()):
            _add_reason(reasons, "invalid_metric:artifact_id")
        for field in ("model_name", "run_id", "role", "artifact_set_sha256"):
            if field in row and (
                not isinstance(row[field], str) or not row[field].strip()
            ):
                _add_reason(reasons, f"invalid_metric:{field}")
        for field in ("accepted", "truth_finalized", "schema_valid", "entitlement_success"):
            if field in row and not isinstance(row[field], bool):
                _add_reason(reasons, f"invalid_metric:{field}")
        for field in ("p0", "p1", "p2", "manual"):
            if field in row and (
                isinstance(row[field], bool)
                or not isinstance(row[field], int)
                or row[field] < 0
                or row[field] > MAX_SAFE_COUNT
            ):
                _add_reason(reasons, f"invalid_metric:{field}")
        if "latency_ms" in row and not _is_finite_nonnegative_number(row["latency_ms"]):
            _add_reason(reasons, "invalid_metric:latency_ms")


def _evidence_identity(rows, expected_role, reasons):
    identity = {
        "model_name": "",
        "run_id": "",
        "role": "",
        "artifact_set_sha256": "",
    }
    for field in identity:
        values = [row.get(field) for row in rows if isinstance(row.get(field), str) and row.get(field).strip()]
        distinct = set(values)
        if len(distinct) > 1:
            _add_reason(reasons, f"inconsistent_evidence:{field}")
        if len(distinct) == 1:
            identity[field] = values[0]

    if identity["role"] != expected_role:
        _add_reason(reasons, f"invalid_{expected_role}_role")

    artifact_ids = [
        row.get("artifact_id")
        for row in rows
        if isinstance(row.get("artifact_id"), str) and row.get("artifact_id").strip()
    ]
    if len(artifact_ids) == len(rows):
        canonical = json.dumps(
            sorted(artifact_ids),
            ensure_ascii=False,
            separators=(",", ":"),
        )
        expected_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        if identity["artifact_set_sha256"] != expected_hash:
            _add_reason(reasons, "artifact_identity_hash_mismatch")
    return identity


def _accepted_identities(rows, reasons):
    identities = []
    for row in rows:
        if row.get("accepted") is True and isinstance(row.get("artifact_id"), str) and row["artifact_id"].strip():
            identities.append(row["artifact_id"])
    if len(identities) != len(set(identities)):
        _add_reason(reasons, "duplicate_artifact_id")
    return frozenset(identities)


def _sum_integer_metric(rows, name, reasons):
    total = 0
    for row in rows:
        value = row.get(name)
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or value > MAX_SAFE_COUNT
        ):
            if name in row:
                _add_reason(reasons, f"invalid_metric:{name}")
            continue
        total += value
    return total


def _latencies(rows, reasons):
    values = []
    for row in rows:
        value = row.get("latency_ms")
        if not _is_finite_nonnegative_number(value):
            if "latency_ms" in row:
                _add_reason(reasons, "invalid_metric:latency_ms")
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError, OverflowError, ArithmeticError):
            _add_reason(reasons, "invalid_metric:latency_ms")
    return values


def _is_finite_nonnegative_number(value):
    if isinstance(value, bool):
        return False
    try:
        if isinstance(value, Decimal):
            return value.is_finite() and Decimal(0) <= value <= MAX_SAFE_LATENCY_MS
        if isinstance(value, int):
            return 0 <= value <= int(MAX_SAFE_LATENCY_MS)
        if isinstance(value, float):
            return math.isfinite(value) and 0 <= value <= float(MAX_SAFE_LATENCY_MS)
    except (TypeError, ValueError, OverflowError, ArithmeticError):
        return False
    return False


def _percentile(values, percentile):
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * (percentile / 100.0)
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = position - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


def _add_reason(reasons, reason):
    if reason not in reasons:
        reasons.append(reason)


def _error_payload(reference_model, candidate_model):
    return {
        "accepted_identities_match": False,
        "approved": False,
        "candidate_model": candidate_model,
        "candidate_p50_ms": 0.0,
        "candidate_p95_ms": 0.0,
        "entitlement_success": False,
        "manual": 0,
        "p0": 0,
        "p1": 0,
        "p2": 0,
        "reasons": ["invalid_evidence"],
        "reference_model": reference_model,
        "reference_p50_ms": 0.0,
        "reference_p95_ms": 0.0,
        "schema_rate": 0.0,
        "truth_finalized": False,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description="Compare credential-free GLM calibration evidence.")
    parser.add_argument("reference", type=Path)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("--reference-model", required=True)
    parser.add_argument("--candidate-model", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)

    try:
        verdict = compare_calibration(
            args.reference,
            args.candidate,
            reference_model=args.reference_model,
            candidate_model=args.candidate_model,
        )
        payload = {
            **verdict.to_dict(),
            "reference_model": args.reference_model,
            "candidate_model": args.candidate_model,
        }
    except (OSError, ValueError, TypeError, OverflowError, ArithmeticError):
        payload = _error_payload(args.reference_model, args.candidate_model)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["approved"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
