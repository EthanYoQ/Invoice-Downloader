from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from batch_validation import BatchValidationError, BatchValidator
from strict_truth_audit import compare as strict_compare
from strict_truth_audit import write_markdown
from run_evidence import validate_finalized_run_evidence
from user_settings import UserSettingsStore, get_output_state_dir, get_settings_path


CANONICAL_INCLUDED_COUNT = 215
CANONICAL_DATE_FROM = "2025-11-25"
CANONICAL_DATE_TO = "2026-06-14"
CANONICAL_MANIFEST_SHA256 = (
    "88c9909d3ccd1610bd152523bba8e96ce9c1f24a50124c6de4204571da6e19be"
)


class AcceptanceSafetyError(RuntimeError):
    pass


class AcceptanceRuntimeError(RuntimeError):
    pass


@dataclass(frozen=True)
class CleanupEvidence:
    run_root: str
    output_state_dir: str
    settings_path: str
    settings_sha256_before: str
    settings_sha256_after: str


def _sha256_file(path: Path) -> str:
    if not path.is_file():
        return ""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _is_reparse_point(path: Path) -> bool:
    if not path.exists():
        return False
    info = os.lstat(path)
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400)


def _manual_runs_root(run_root: Path) -> Path:
    nominal = run_root.absolute()
    candidates = (nominal, *nominal.parents)
    manual_root = next(
        (path for path in candidates if path.name.casefold() == "manual_frontend_runs"),
        None,
    )
    if manual_root is None or nominal == manual_root:
        raise AcceptanceSafetyError("run_root_must_be_child_of_manual_frontend_runs")
    resolved_manual = manual_root.resolve()
    resolved_run = nominal.resolve()
    if not resolved_run.is_relative_to(resolved_manual):
        raise AcceptanceSafetyError("run_root_escape")
    current = manual_root
    relative = nominal.relative_to(manual_root)
    for part in relative.parts:
        current = current / part
        if _is_reparse_point(current):
            raise AcceptanceSafetyError("run_root_reparse_point")
    return resolved_manual


def _validate_output_state_dir(output_state_dir: Path) -> Path:
    nominal = output_state_dir.absolute()
    if (
        nominal.parent.name.casefold() != "output_scoped"
        or nominal.parent.parent.name.casefold() != "state"
        or not re.fullmatch(r"[0-9a-fA-F]{16}", nominal.name)
    ):
        raise AcceptanceSafetyError("invalid_output_state_dir")
    for path in (nominal.parent.parent.parent, nominal.parent.parent, nominal.parent, nominal):
        if _is_reparse_point(path):
            raise AcceptanceSafetyError("output_state_reparse_point")
    return nominal.resolve()


def prepare_clean_run_root(
    run_root: str | Path,
    output_state_dir: str | Path,
    *,
    settings_path: str | Path | None = None,
) -> CleanupEvidence:
    root = Path(run_root).absolute()
    _manual_runs_root(root)
    settings = Path(settings_path or get_settings_path()).absolute()
    state = _validate_output_state_dir(Path(output_state_dir))
    expected_state_root = (settings.parent / "state" / "output_scoped").resolve()
    if str(state.parent).casefold() != str(expected_state_root).casefold():
        raise AcceptanceSafetyError("output_state_root_mismatch")
    expected_state_key = hashlib.sha256(
        os.path.normcase(os.path.abspath(str(root / "output"))).encode("utf-8")
    ).hexdigest()[:16]
    if state.name.casefold() != expected_state_key.casefold():
        raise AcceptanceSafetyError("output_state_scope_mismatch")
    settings_before = _sha256_file(settings)

    if root.exists():
        if not root.is_dir():
            raise AcceptanceSafetyError("run_root_not_directory")
        shutil.rmtree(root)
    if state.exists():
        if not state.is_dir():
            raise AcceptanceSafetyError("output_state_not_directory")
        shutil.rmtree(state)

    for directory in (
        root / "output",
        root / "staging",
        root / "diagnostics",
        root / "monitoring" / "qc",
    ):
        directory.mkdir(parents=True, exist_ok=True)

    settings_after = _sha256_file(settings)
    if settings_before != settings_after:
        raise AcceptanceSafetyError("settings_changed_during_cleanup")
    return CleanupEvidence(
        run_root=str(root.resolve()),
        output_state_dir=str(state),
        settings_path=str(settings),
        settings_sha256_before=settings_before,
        settings_sha256_after=settings_after,
    )


def _load_final_manifest(path: str | Path) -> dict[str, Any]:
    manifest_path = Path(path).resolve(strict=True)
    if _sha256_file(manifest_path).casefold() != CANONICAL_MANIFEST_SHA256.casefold():
        raise AcceptanceSafetyError("truth_manifest_digest_mismatch")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptanceSafetyError("invalid_truth_manifest") from exc
    summary = manifest.get("summary") if isinstance(manifest, dict) else None
    if not isinstance(summary, dict):
        raise AcceptanceSafetyError("invalid_truth_manifest_summary")
    if summary.get("finalized") is not True:
        raise AcceptanceSafetyError("truth_manifest_not_finalized")
    if int(summary.get("pending_review_count", -1) or 0) != 0:
        raise AcceptanceSafetyError("truth_manifest_pending_review")
    if int(summary.get("included_count", 0) or 0) != CANONICAL_INCLUDED_COUNT:
        raise AcceptanceSafetyError("truth_manifest_included_count_mismatch")
    return manifest


def _validate_manifest_range(
    manifest: Mapping[str, Any], *, date_from: str, date_to: str
) -> None:
    summary = manifest["summary"]
    if (
        str(summary.get("date_from") or "") != str(date_from)
        or str(summary.get("date_to") or "") != str(date_to)
        or str(date_from) != CANONICAL_DATE_FROM
        or str(date_to) != CANONICAL_DATE_TO
    ):
        raise AcceptanceSafetyError("truth_manifest_date_range_mismatch")


def build_run_context(
    run_root: str | Path,
    manifest_path: str | Path,
    *,
    date_from: str,
    date_to: str,
    run_id: str,
    autostart_delay_ms: int = 1000,
) -> dict[str, Any]:
    manifest = _load_final_manifest(manifest_path)
    _validate_manifest_range(manifest, date_from=date_from, date_to=date_to)
    included_count = int(manifest["summary"]["included_count"])
    root = Path(run_root).resolve()
    return {
        "enabled": True,
        "controlled_run": True,
        "run_id": str(run_id),
        "run_root": str(root),
        "locked_output_path": str(root / "output"),
        "staging_dir": str(root / "staging"),
        "diagnostics_dir": str(root / "diagnostics"),
        "monitoring_dir": str(root / "monitoring"),
        "qc_dir": str(root / "monitoring" / "qc"),
        "debug_trace_path": str(root / "diagnostics" / "debug_trace.jsonl"),
        "locked_date_from": str(date_from),
        "locked_date_to": str(date_to),
        "autostart_enabled": True,
        "autostart_mode": "controlled-run",
        "autostart_delay_ms": max(0, int(autostart_delay_ms)),
        "autostart_token": uuid.uuid4().hex,
        "validation_required": True,
        "manifest_included_count": included_count,
        "truth_manifest_path": str(Path(manifest_path).resolve()),
    }


def write_json_atomic(path: str | Path, payload: Mapping[str, Any]) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{output.name}.", suffix=".tmp", dir=output.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return output


def _restore_settings_snapshot(path: str | Path, payload: bytes) -> None:
    target = Path(path).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.read_bytes() == payload:
        return
    descriptor, temporary = tempfile.mkstemp(
        prefix=f"{target.name}.", suffix=".restore.tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def launch_frontend(
    executable: str | Path,
    context_path: str | Path,
    *,
    popen_factory: Callable[..., Any] = subprocess.Popen,
) -> Any:
    exe = Path(executable).resolve(strict=True)
    context = Path(context_path).resolve(strict=True)
    return popen_factory(
        [str(exe), "--run-context", str(context)],
        cwd=str(exe.parent),
        shell=False,
    )


def wait_for_finalized_evidence(
    run_root: str | Path,
    process: Any,
    *,
    timeout_seconds: float,
    poll_seconds: float = 1.0,
    monotonic: Callable[[], float] = time.monotonic,
    sleeper: Callable[[float], None] = time.sleep,
    evidence_validator: Callable[[Mapping[str, Any], Path], Any] = validate_finalized_run_evidence,
) -> dict[str, Any]:
    root = Path(run_root).resolve()
    evidence_path = root / "diagnostics" / "run_evidence.json"
    deadline = monotonic() + max(0.001, float(timeout_seconds))
    while monotonic() < deadline:
        if evidence_path.is_file():
            try:
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                evidence = None
            if isinstance(evidence, dict) and evidence.get("evidence_digest"):
                try:
                    evidence_validator(evidence, root)
                except (OSError, TypeError, ValueError):
                    pass
                else:
                    return evidence
        returncode = process.poll()
        if returncode is not None:
            raise AcceptanceRuntimeError(f"frontend_exited_before_evidence:{returncode}")
        sleeper(max(0.01, float(poll_seconds)))
    raise AcceptanceRuntimeError("frontend_acceptance_timeout")


def evaluate_acceptance(
    *,
    counts: Mapping[str, int],
    elapsed_seconds: str | int | float,
    max_seconds: str | int | float,
    authoritative: bool,
    batch_validation_passed: bool,
    credentials_preserved: bool = True,
) -> dict[str, Any]:
    normalized_counts = {
        key: int(counts.get(key, -1)) for key in ("p0", "p1", "p2", "manual")
    }
    try:
        elapsed = Decimal(str(elapsed_seconds))
        maximum = Decimal(str(max_seconds))
        time_gate_passed = elapsed.is_finite() and maximum.is_finite() and elapsed <= maximum
    except (InvalidOperation, ValueError):
        elapsed = Decimal("NaN")
        maximum = Decimal(str(max_seconds))
        time_gate_passed = False
    zero_counts = all(value == 0 for value in normalized_counts.values())
    passed = bool(
        authoritative
        and batch_validation_passed
        and credentials_preserved
        and zero_counts
        and time_gate_passed
    )
    return {
        "passed": passed,
        "gate_passed": passed,
        "authoritative": bool(authoritative and batch_validation_passed),
        "batch_validation_passed": bool(batch_validation_passed),
        "credentials_preserved": bool(credentials_preserved),
        "p0_count": normalized_counts["p0"],
        "user_p1_count": normalized_counts["p1"],
        "p2_count": normalized_counts["p2"],
        "manual_check_count": normalized_counts["manual"],
        "elapsed_seconds": str(elapsed),
        "elapsed_seconds_max": str(maximum),
        "time_gate_passed": bool(time_gate_passed),
    }


def _strict_counts(audit: Mapping[str, Any]) -> dict[str, int]:
    return {
        "p0": int((audit.get("p0_conclusion") or {}).get("count", -1)),
        "p1": int((audit.get("user_p1_conclusion") or {}).get("count", -1)),
        "p2": int((audit.get("p2_conclusion") or {}).get("count", -1)),
        "manual": len(audit.get("manual_check_rows") or []),
    }


def audit_completed_run(
    manifest_path: str | Path,
    run_root: str | Path,
    *,
    max_seconds: str | int | float,
    strict_runner: Callable[[dict[str, Any], Path], dict[str, Any]] = strict_compare,
    validator: BatchValidator | None = None,
) -> dict[str, Any]:
    manifest = _load_final_manifest(manifest_path)
    root = Path(run_root).resolve(strict=True)
    evidence_path = root / "diagnostics" / "run_evidence.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    strict = strict_runner(manifest, root)
    strict_path = write_json_atomic(root / "diagnostics" / "strict_truth_audit.json", strict)
    write_markdown(strict, strict_path.with_suffix(".md"))
    counts = _strict_counts(strict)
    authority = bool((strict.get("audit_authority") or {}).get("authoritative"))

    batch_validator = validator or BatchValidator()
    validation_passed = False
    validation_error = ""
    validation_report = ""
    try:
        validation = batch_validator.validate(Path(manifest_path), root)
        validation_report = str(batch_validator.write_report(validation, root))
        validation_passed = bool(validation.passed)
    except BatchValidationError as exc:
        validation_error = exc.code

    result = evaluate_acceptance(
        counts=counts,
        elapsed_seconds=evidence.get("elapsed_seconds", "NaN"),
        max_seconds=max_seconds,
        authoritative=authority,
        batch_validation_passed=validation_passed,
    )
    result.update(
        {
            "run_id": str(evidence.get("run_id") or root.name),
            "run_root": str(root),
            "truth_manifest": str(Path(manifest_path).resolve()),
            "candidate_revision": str(evidence.get("candidate_revision") or ""),
            "strict_truth_audit": str(strict_path),
            "batch_validation_report": validation_report,
            "validation_error": validation_error,
        }
    )
    write_json_atomic(root / "acceptance_result.json", result)
    return result


def _credential_fingerprint(settings: Mapping[str, Any]) -> str:
    api_key = str(settings.get("api_key") or "")
    auth_code = str(settings.get("auth_code") or "")
    if not api_key or not auth_code:
        raise AcceptanceSafetyError("required_credentials_missing")
    return hashlib.sha256(f"{api_key}\0{auth_code}".encode("utf-8")).hexdigest()


def ensure_no_secret_leaks(paths: list[Path], secrets: list[str]) -> None:
    needles = [str(value).encode("utf-8") for value in secrets if str(value)]
    if not needles:
        return
    for path in paths:
        candidate = Path(path)
        if not candidate.is_file():
            continue
        try:
            payload = candidate.read_bytes()
        except OSError:
            continue
        if any(secret in payload for secret in needles):
            raise AcceptanceSafetyError(f"secret_leak_detected:{candidate.name}")


def terminate_frontend(
    process: Any,
    *,
    timeout_seconds: float = 5.0,
    command_runner: Callable[..., Any] = subprocess.run,
    monotonic: Callable[[], float] = time.monotonic,
) -> None:
    if process is None or process.poll() is not None:
        return
    deadline = monotonic() + max(0.001, float(timeout_seconds))

    def remaining() -> float:
        return max(0.001, min(float(timeout_seconds), deadline - monotonic()))

    if os.name == "nt":
        try:
            command_runner(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                check=False,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=remaining(),
            )
        except (OSError, subprocess.TimeoutExpired, TimeoutError):
            pass
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=remaining())
    except (OSError, subprocess.TimeoutExpired, TimeoutError):
        try:
            process.kill()
        except OSError:
            return
        try:
            process.wait(timeout=remaining())
        except (OSError, subprocess.TimeoutExpired, TimeoutError):
            pass


def run_acceptance(
    *,
    executable: str | Path,
    manifest_path: str | Path,
    date_from: str,
    date_to: str,
    max_seconds: float,
    run_root: str | Path,
    poll_seconds: float = 1.0,
) -> dict[str, Any]:
    root = Path(run_root).absolute()
    output_dir = root / "output"
    settings_store = UserSettingsStore()
    settings_path = Path(get_settings_path()).absolute()
    if not settings_path.is_file():
        raise AcceptanceSafetyError("required_settings_missing")
    settings_snapshot = settings_path.read_bytes()
    settings_snapshot_sha256 = hashlib.sha256(settings_snapshot).hexdigest()
    settings_before = settings_store.load() or {}
    credentials_before = _credential_fingerprint(settings_before)
    protected_values = [
        str(settings_before.get("api_key") or ""),
        str(settings_before.get("auth_code") or ""),
    ]
    output_state_dir = Path(get_output_state_dir(output_dir))
    cleanup = prepare_clean_run_root(
        root,
        output_state_dir,
        settings_path=settings_path,
    )
    context = build_run_context(
        root,
        manifest_path,
        date_from=date_from,
        date_to=date_to,
        run_id=root.name,
    )
    context_path = write_json_atomic(root / "monitoring" / "run_context.json", context)
    process = None
    result = None
    try:
        process = launch_frontend(executable, context_path)
        wait_for_finalized_evidence(
            root,
            process,
            timeout_seconds=max_seconds,
            poll_seconds=poll_seconds,
        )
        result = audit_completed_run(
            manifest_path,
            root,
            max_seconds=max_seconds,
        )
    finally:
        terminate_frontend(process)
        _restore_settings_snapshot(settings_path, settings_snapshot)

    credentials_after = _credential_fingerprint(settings_store.load() or {})
    credentials_preserved = credentials_before == credentials_after
    settings_after_run_sha256 = _sha256_file(settings_path)
    settings_restored = settings_after_run_sha256 == settings_snapshot_sha256
    if not settings_restored:
        raise AcceptanceSafetyError("settings_snapshot_restore_failed")
    if result is None:
        raise AcceptanceRuntimeError("acceptance_result_missing")
    leak_scan_paths = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.suffix.casefold()
        in {".json", ".jsonl", ".log", ".txt", ".md", ".csv"}
    ]
    ensure_no_secret_leaks(leak_scan_paths, protected_values)
    result.update(
        {
            "credentials_preserved": credentials_preserved,
            "settings_cleanup_sha256_before": cleanup.settings_sha256_before,
            "settings_cleanup_sha256_after": cleanup.settings_sha256_after,
            "settings_sha256_before_run": settings_snapshot_sha256,
            "settings_sha256_after_run": settings_after_run_sha256,
            "settings_restored": settings_restored,
        }
    )
    result["passed"] = bool(
        result["passed"] and credentials_preserved and settings_restored
    )
    result["gate_passed"] = result["passed"]
    write_json_atomic(root / "acceptance_result.json", result)
    return result


def _date(value: str) -> str:
    try:
        return dt.date.fromisoformat(value).isoformat()
    except ValueError as exc:
        raise argparse.ArgumentTypeError("date must be YYYY-MM-DD") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Launch and validate one clean packaged frontend truth run."
    )
    parser.add_argument("--exe", required=True)
    parser.add_argument("--truth-manifest", required=True)
    parser.add_argument("--date-from", required=True, type=_date)
    parser.add_argument("--date-to", required=True, type=_date)
    parser.add_argument("--max-seconds", required=True, type=float)
    parser.add_argument("--run-root", default="")
    parser.add_argument("--poll-seconds", type=float, default=1.0)
    args = parser.parse_args(argv)
    if args.date_from > args.date_to:
        parser.error("date-from must not be after date-to")
    if args.max_seconds <= 0:
        parser.error("max-seconds must be positive")

    run_root = Path(args.run_root) if args.run_root else (
        PROJECT_ROOT
        / "manual_frontend_runs"
        / f"frontend_acceptance_{time.strftime('%Y%m%d_%H%M%S')}"
    )
    try:
        result = run_acceptance(
            executable=args.exe,
            manifest_path=args.truth_manifest,
            date_from=args.date_from,
            date_to=args.date_to,
            max_seconds=args.max_seconds,
            run_root=run_root,
            poll_seconds=args.poll_seconds,
        )
    except (AcceptanceSafetyError, AcceptanceRuntimeError, OSError, ValueError) as exc:
        failure = {
            "passed": False,
            "gate_passed": False,
            "run_root": str(Path(run_root).absolute()),
            "error_code": str(exc),
        }
        try:
            write_json_atomic(Path(run_root) / "acceptance_result.json", failure)
        except OSError:
            pass
        print(json.dumps(failure, ensure_ascii=False))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(main())
