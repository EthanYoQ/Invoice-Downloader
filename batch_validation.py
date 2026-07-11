"""Credential-free validation of immutable batch evidence against finalized truth."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Mapping

from truth_contracts import TruthContractError, TruthManifest


class BatchValidationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _load_json(path: Path, code: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BatchValidationError(code, path.name) from exc
    if not isinstance(value, dict):
        raise BatchValidationError(code, path.name)
    return value


def _decimal(value: Any, *, code: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise BatchValidationError(code) from exc
    if not result.is_finite() or result < 0:
        raise BatchValidationError(code)
    return result


def _resolved(value: Any) -> Path:
    return Path(str(value or "")).resolve()


@dataclass(frozen=True)
class BatchValidationResult:
    passed: bool
    run_id: str
    run_root: str
    candidate_revision: str
    candidate_version: str
    counts: Mapping[str, int]
    scope: Mapping[str, str]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "run_id": self.run_id,
            "run_root": self.run_root,
            "candidate_revision": self.candidate_revision,
            "candidate_version": self.candidate_version,
            "counts": dict(self.counts),
            "scope": dict(self.scope),
        }


@dataclass(frozen=True)
class PerformanceVerdict:
    baseline_seconds: str
    candidate_seconds: str
    target_seconds: str
    speedup_fraction: str
    threshold_fraction: str
    passed: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "baseline_seconds": self.baseline_seconds,
            "candidate_seconds": self.candidate_seconds,
            "target_seconds": self.target_seconds,
            "speedup_fraction": self.speedup_fraction,
            "threshold_fraction": self.threshold_fraction,
            "passed": self.passed,
        }


class BatchValidator:
    CONFIG_PATH = Path("monitoring/run_config.json")
    EVIDENCE_PATH = Path("diagnostics/run_evidence.json")
    AUDIT_PATH = Path("diagnostics/strict_truth_audit.json")

    def validate(self, manifest: TruthManifest | Mapping[str, Any] | str | Path, run_root: str | Path) -> BatchValidationResult:
        try:
            truth = self._truth(manifest)
        except TruthContractError as exc:
            raise BatchValidationError(exc.code, exc.detail) from exc
        root = Path(run_root).resolve()
        if not root.is_dir():
            raise BatchValidationError("invalid_run_root")
        config = _load_json(root / self.CONFIG_PATH, "missing_run_config")
        evidence = _load_json(root / self.EVIDENCE_PATH, "missing_run_evidence")
        audit = _load_json(root / self.AUDIT_PATH, "missing_strict_audit")

        run_id = str(config.get("run_id") or "").strip()
        revision = str(config.get("candidate_revision") or "").strip()
        version = str(config.get("candidate_version") or "").strip()
        if not run_id:
            raise BatchValidationError("missing_run_id")
        if not revision or not version:
            raise BatchValidationError("missing_candidate_version")
        if _resolved(config.get("run_root")) != root:
            raise BatchValidationError("scope_mismatch", "run_root")

        active_run_config = config.get("active_run_config", {})
        if not isinstance(active_run_config, Mapping):
            raise BatchValidationError("invalid_run_config")

        config_scope = {
            "date_from": str(config.get("locked_date_from") or config.get("date_from") or ""),
            "date_to": str(config.get("locked_date_to") or config.get("date_to") or ""),
            "before_exclusive": str(config.get("before_exclusive") or ""),
            "account_domain": str(config.get("email_domain") or config.get("account_domain") or "").lower(),
            "account_channel": str(config.get("account_channel") or "").lower(),
            "mailbox": str(config.get("mailbox") or ""),
            "target_company": str(
                config.get("target_company")
                or config.get("target_company_id")
                or active_run_config.get("company")
                or ""
            ),
        }
        truth_scope = {
            "date_from": truth.date_from.isoformat(),
            "date_to": truth.date_to.isoformat(),
            "before_exclusive": truth.before_exclusive.isoformat(),
            "account_domain": truth.account_domain,
            "account_channel": truth.account_channel,
            "mailbox": truth.mailbox,
            "target_company": truth.target_company,
        }
        if config_scope != truth_scope:
            mismatch = next(key for key in truth_scope if config_scope.get(key) != truth_scope[key])
            raise BatchValidationError("scope_mismatch", mismatch)

        self._validate_evidence(evidence, root, run_id, revision, version)
        counts = self._validate_audit(audit, truth, root, run_id, revision)
        return BatchValidationResult(
            passed=True,
            run_id=run_id,
            run_root=str(root),
            candidate_revision=revision,
            candidate_version=version,
            counts=counts,
            scope=truth_scope,
        )

    @staticmethod
    def _truth(value: TruthManifest | Mapping[str, Any] | str | Path) -> TruthManifest:
        if isinstance(value, TruthManifest):
            return value
        if isinstance(value, Mapping):
            return TruthManifest.from_mapping(value)
        return TruthManifest.from_path(value)

    @staticmethod
    def _validate_evidence(evidence: Mapping[str, Any], root: Path, run_id: str, revision: str, version: str) -> None:
        if str(evidence.get("run_id") or "") != run_id or _resolved(evidence.get("run_root")) != root:
            raise BatchValidationError("scope_mismatch", "run evidence")
        if evidence.get("candidate_revision") != revision or evidence.get("candidate_version") != version:
            raise BatchValidationError("version_mismatch")
        if not str(evidence.get("hardware_mode") or "").strip():
            raise BatchValidationError("missing_hardware_mode")
        if "started_monotonic_seconds" not in evidence or "ended_monotonic_seconds" not in evidence:
            raise BatchValidationError("missing_timing_boundary")
        start = _decimal(evidence["started_monotonic_seconds"], code="invalid_timing_boundary")
        end = _decimal(evidence["ended_monotonic_seconds"], code="invalid_timing_boundary")
        elapsed = _decimal(evidence.get("elapsed_seconds"), code="invalid_timing_boundary")
        if end < start or elapsed != end - start:
            raise BatchValidationError("invalid_timing_boundary")

    @staticmethod
    def _validate_audit(
        audit: Mapping[str, Any],
        truth: TruthManifest,
        root: Path,
        run_id: str,
        revision: str,
    ) -> dict[str, int]:
        if _resolved(audit.get("run_root")) != root:
            raise BatchValidationError("scope_mismatch", "strict audit run_root")
        if audit.get("candidate_revision") != revision or audit.get("run_id") != run_id:
            raise BatchValidationError("stale_audit")
        generated = str(audit.get("generated_at_utc") or "")
        try:
            generated_at = dt.datetime.fromisoformat(generated.replace("Z", "+00:00"))
        except ValueError as exc:
            raise BatchValidationError("stale_audit") from exc
        if generated_at.tzinfo is None:
            raise BatchValidationError("stale_audit")
        audit_summary = audit.get("truth_summary")
        if not isinstance(audit_summary, Mapping):
            raise BatchValidationError("scope_mismatch", "strict audit truth summary")
        for field in (
            "dataset", "date_from", "date_to", "before_exclusive", "mailbox",
            "account_domain", "target_company", "included_count", "pending_review_count", "finalized",
        ):
            if audit_summary.get(field) != truth.summary.get(field):
                raise BatchValidationError("scope_mismatch", f"strict audit {field}")

        try:
            p0 = int((audit.get("p0_conclusion") or {}).get("count", -1))
            p1 = int((audit.get("user_p1_conclusion") or {}).get("count", -1))
            p2 = int((audit.get("p2_conclusion") or {}).get("count", -1))
        except (AttributeError, TypeError, ValueError) as exc:
            raise BatchValidationError("invalid_audit_result") from exc
        manual_rows = audit.get("manual_check_rows")
        manual = len(manual_rows) if isinstance(manual_rows, list) else -1
        if audit.get("exit_code") != 0 or any(count != 0 for count in (p0, p1, p2, manual)):
            raise BatchValidationError("strict_audit_failed")
        matched = audit.get("matched_rows")
        if not isinstance(matched, list):
            raise BatchValidationError("matched_count_mismatch")
        paths = [str(row.get("matched_path") or "") for row in matched if isinstance(row, Mapping)]
        for path_value in paths:
            candidate = Path(path_value)
            if not path_value or not candidate.is_absolute():
                raise BatchValidationError("invalid_artifact_assignment")
            try:
                candidate.resolve().relative_to(root)
            except ValueError as exc:
                raise BatchValidationError("invalid_artifact_assignment") from exc
        if len(paths) != len(set(paths)):
            raise BatchValidationError("duplicate_artifact_assignment")
        truth_ids = [str(row.get("truth_id") or "") for row in matched if isinstance(row, Mapping)]
        expected_ids = {row.truth_id for row in truth.included}
        if len(matched) != len(truth.included) or set(truth_ids) != expected_ids:
            raise BatchValidationError("matched_count_mismatch")
        return {"p0": p0, "p1": p1, "p2": p2, "manual": manual, "matched": len(matched)}

    def write_report(self, result: BatchValidationResult, run_root: str | Path) -> Path:
        root = Path(run_root).resolve()
        diagnostics = root / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        resolved_diagnostics = diagnostics.resolve()
        try:
            resolved_diagnostics.relative_to(root)
        except ValueError as exc:
            raise BatchValidationError("output_path_escape") from exc
        output = resolved_diagnostics / "batch_validation.json"
        payload = json.dumps(result.to_mapping(), ensure_ascii=False, indent=2)
        descriptor, temp_name = tempfile.mkstemp(prefix="batch_validation.", suffix=".tmp", dir=resolved_diagnostics)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, output)
        except BaseException:
            try:
                Path(temp_name).unlink(missing_ok=True)
            finally:
                raise
        return output


def compare_performance(baseline_json: str | Path, candidate_json: str | Path) -> PerformanceVerdict:
    baseline_payload = _load_json(Path(baseline_json), "invalid_baseline_metrics")
    candidate_payload = _load_json(Path(candidate_json), "invalid_candidate_metrics")
    for field in ("scope_id", "hardware_mode"):
        left = str(baseline_payload.get(field) or "")
        right = str(candidate_payload.get(field) or "")
        if not left or left != right:
            raise BatchValidationError("performance_contract_mismatch", field)
    baseline = _performance_elapsed(baseline_payload)
    candidate = _performance_elapsed(candidate_payload)
    if baseline <= 0:
        raise BatchValidationError("invalid performance seconds")
    threshold = Decimal("0.30")
    target = (baseline * (Decimal("1") - threshold)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    speedup = (baseline - candidate) / baseline
    return PerformanceVerdict(
        baseline_seconds=format(baseline, "f"),
        candidate_seconds=format(candidate, "f"),
        target_seconds=format(target, ".2f"),
        speedup_fraction=format(speedup, ".8f"),
        threshold_fraction=format(threshold, ".2f"),
        passed=candidate <= target,
    )


def _performance_elapsed(payload: Mapping[str, Any]) -> Decimal:
    elapsed = _decimal(payload.get("elapsed_seconds"), code="invalid performance seconds")
    start = _decimal(payload.get("started_monotonic_seconds"), code="invalid performance seconds")
    end = _decimal(payload.get("ended_monotonic_seconds"), code="invalid performance seconds")
    if end < start or end - start != elapsed:
        raise BatchValidationError("invalid_performance_boundaries")
    return elapsed


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a finalized truth manifest against immutable run evidence.")
    parser.add_argument("--truth-manifest", required=True, help="Path to the finalized truth manifest JSON")
    parser.add_argument("--run-root", required=True, help="Path to the completed run root")
    args = parser.parse_args()
    validator = BatchValidator()
    try:
        result = validator.validate(Path(args.truth_manifest), Path(args.run_root))
        output = validator.write_report(result, Path(args.run_root))
    except (BatchValidationError, TruthContractError) as exc:
        print(json.dumps({"passed": False, "error_code": exc.code}, ensure_ascii=False))
        return 1
    print(json.dumps({"passed": True, "counts": dict(result.counts), "output": str(output.resolve())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
