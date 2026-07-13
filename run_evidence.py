"""Production-owned immutable run evidence and document lineage."""

from __future__ import annotations

from dataclasses import dataclass
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
from decimal import Decimal, InvalidOperation
import re
import shutil
import subprocess
import sys
import tempfile
import time
from typing import Any, Callable, Mapping


UTC = dt.timezone.utc


def _json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _utc_text(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class RevisionUnavailable(RuntimeError):
    reason_code = "TRUSTED_REVISION_UNAVAILABLE"
    user_message = "当前程序缺少可信构建版本信息。"

    def __init__(self) -> None:
        super().__init__("trusted_revision_unavailable")


def _default_identity_paths() -> tuple[Path, ...]:
    module_dir = Path(__file__).resolve().parent
    paths = [module_dir / "build" / "windows" / "build-identity.generated.json"]
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        meipass = Path(getattr(sys, "_MEIPASS", module_dir)).resolve()
        paths.insert(0, meipass / "build_meta" / "build-identity.generated.json")
        paths.insert(
            1,
            Path(sys.executable).resolve().parent
            / "_internal"
            / "build_meta"
            / "build-identity.generated.json",
        )
    return tuple(paths)


def _full_revision(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{40}", text) else ""


def _full_sha256(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if re.fullmatch(r"[0-9a-f]{64}", text) else ""


def default_revision(*, identity_paths: tuple[Path, ...] | None = None) -> str:
    candidates = identity_paths if identity_paths is not None else _default_identity_paths()
    for path in candidates:
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if isinstance(payload, Mapping):
            revision = _full_revision(payload.get("source_revision"))
            if revision:
                return revision

    git = shutil.which("git")
    if not git:
        raise RevisionUnavailable()
    completed = subprocess.run(
        [git, "rev-parse", "HEAD"],
        cwd=Path(__file__).resolve().parent,
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    revision = _full_revision(completed.stdout if completed.returncode == 0 else "")
    if not revision:
        raise RevisionUnavailable()
    return revision


def default_hardware() -> tuple[str, str]:
    mode = "windows-desktop-standard" if os.name == "nt" else "desktop-standard"
    raw = "|".join((platform.system(), platform.machine(), platform.processor()))
    return mode, hashlib.sha256(raw.encode("utf-8", errors="replace")).hexdigest()


def compute_inventory_digest(entries: list[dict[str, Any]]) -> str:
    payload = [
        {
            "relative_path": str(row["relative_path"]),
            "size": int(row["size"]),
            "sha256": str(row["sha256"]).lower(),
        }
        for row in entries
    ]
    payload.sort(key=lambda row: row["relative_path"].replace("\\", "/").casefold())
    return _sha256_json(payload)


def compute_scope_digest(scope: Mapping[str, Any]) -> str:
    return _sha256_json({key: str(scope[key]) for key in sorted(scope)})


def compute_lineage_digest(rows: list[dict[str, Any]]) -> str:
    return _sha256_json(sorted(rows, key=lambda row: str(row["document_id"])))


def compute_evidence_digest(evidence: Mapping[str, Any]) -> str:
    return _sha256_json(
        {key: value for key, value in evidence.items() if key != "evidence_digest"}
    )


def validate_finalized_run_evidence(
    evidence: Mapping[str, Any], run_root: Path
) -> dict[str, str]:
    """Validate immutable production evidence and return canonical lineage hashes."""

    required = {
        "schema_version",
        "run_id",
        "run_root",
        "candidate_revision",
        "candidate_version",
        "validation_required",
        "manifest_included_count",
        "scope",
        "scope_digest",
        "hardware_mode",
        "hardware_fingerprint",
        "started_monotonic_seconds",
        "ended_monotonic_seconds",
        "elapsed_seconds",
        "started_at_utc",
        "ended_at_utc",
        "lineage",
        "lineage_digest",
        "output_inventory",
        "inventory_sha256",
        "evidence_digest",
    }
    if not isinstance(evidence, Mapping) or not required.issubset(evidence):
        raise ValueError("invalid_run_evidence")
    root = Path(run_root).resolve(strict=False)
    try:
        evidence_root = Path(str(evidence["run_root"])).resolve(strict=False)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("invalid_run_evidence") from exc
    if (
        evidence.get("schema_version") != 1
        or str(evidence_root).casefold() != str(root).casefold()
        or not str(evidence.get("run_id") or "").strip()
        or not _full_revision(evidence.get("candidate_revision"))
        or not str(evidence.get("candidate_version") or "").strip()
    ):
        raise ValueError("invalid_run_evidence")
    try:
        included_count = int(evidence.get("manifest_included_count"))
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_run_evidence") from exc
    if evidence.get("validation_required") is not True or included_count <= 0:
        raise ValueError("validation_evidence_scope_mismatch")
    if str(evidence.get("evidence_digest") or "") != compute_evidence_digest(evidence):
        raise ValueError("run_evidence_digest_mismatch")

    scope = evidence.get("scope")
    if (
        not isinstance(scope, Mapping)
        or not scope
        or not all(str(value or "").strip() for value in scope.values())
        or str(evidence.get("scope_digest") or "") != compute_scope_digest(scope)
        or str(evidence.get("hardware_mode") or "")
        != str(scope.get("hardware_mode") or "")
        or str(evidence.get("hardware_fingerprint") or "")
        != str(scope.get("hardware_fingerprint") or "")
    ):
        raise ValueError("run_evidence_scope_mismatch")
    try:
        start = Decimal(str(evidence["started_monotonic_seconds"]))
        end = Decimal(str(evidence["ended_monotonic_seconds"]))
        elapsed = Decimal(str(evidence["elapsed_seconds"]))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("invalid_timing_boundary") from exc
    if (
        not all(value.is_finite() and value >= 0 for value in (start, end, elapsed))
        or end < start
        or elapsed != end - start
    ):
        raise ValueError("invalid_timing_boundary")
    try:
        wall_start = dt.datetime.fromisoformat(
            str(evidence["started_at_utc"]).replace("Z", "+00:00")
        )
        wall_end = dt.datetime.fromisoformat(
            str(evidence["ended_at_utc"]).replace("Z", "+00:00")
        )
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid_run_time") from exc
    if wall_start.tzinfo is None or wall_end.tzinfo is None or wall_end < wall_start:
        raise ValueError("invalid_run_time")

    output_root = root / "output"
    if not output_root.is_dir():
        raise ValueError("lineage_output_mismatch")
    lineage_preview = evidence.get("lineage")
    if isinstance(lineage_preview, list):
        resolved_output_root = output_root.resolve(strict=False)
        for row in lineage_preview:
            if not isinstance(row, Mapping):
                continue
            relative = str(row.get("output_relative_path") or "").strip()
            if not relative:
                continue
            path = (resolved_output_root / Path(relative)).resolve(strict=False)
            try:
                path.relative_to(resolved_output_root)
            except ValueError as exc:
                raise ValueError("lineage_output_escape") from exc
            if not path.is_file():
                raise ValueError("lineage_output_mismatch")
            try:
                expected_size = int(row.get("output_size"))
            except (TypeError, ValueError) as exc:
                raise ValueError("lineage_output_mismatch") from exc
            actual_hash, actual_size = _sha256_file(path)
            if (
                str(row.get("output_sha256") or "").lower() != actual_hash
                or expected_size != actual_size
            ):
                raise ValueError("lineage_output_mismatch")
    actual_inventory = []
    for path in sorted(
        (item for item in output_root.rglob("*") if item.is_file()),
        key=lambda item: item.relative_to(output_root).as_posix().casefold(),
    ):
        digest, size = _sha256_file(path)
        actual_inventory.append(
            {
                "relative_path": path.relative_to(output_root).as_posix(),
                "size": size,
                "sha256": digest,
            }
        )
    raw_inventory = evidence.get("output_inventory")
    if not isinstance(raw_inventory, list):
        raise ValueError("run_evidence_inventory_mismatch")
    try:
        supplied_inventory_digest = compute_inventory_digest(raw_inventory)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("run_evidence_inventory_mismatch") from exc
    actual_inventory_digest = compute_inventory_digest(actual_inventory)
    if (
        supplied_inventory_digest != actual_inventory_digest
        or str(evidence.get("inventory_sha256") or "") != actual_inventory_digest
    ):
        raise ValueError("run_evidence_inventory_mismatch")

    lineage = evidence.get("lineage")
    if not isinstance(lineage, list) or not lineage:
        raise ValueError("missing_document_lineage")
    try:
        lineage_digest = compute_lineage_digest(lineage)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid_document_lineage") from exc
    if str(evidence.get("lineage_digest") or "") != lineage_digest:
        raise ValueError("lineage_digest_mismatch")
    allowed_fields = {
        "run_id",
        "document_id",
        "source_email_uid",
        "source_chain_sha256s",
        "output_relative_path",
        "output_sha256",
        "output_size",
        "artifact_role",
        "transformation_type",
        "provider_type",
    }
    inventory_by_relative = {
        str(item["relative_path"]).replace("\\", "/").casefold(): item
        for item in actual_inventory
    }
    bindings = {}
    document_ids = set()
    run_id = str(evidence["run_id"])
    for row in lineage:
        if not isinstance(row, Mapping):
            raise ValueError("invalid_document_lineage")
        if set(row) != allowed_fields:
            raise ValueError("lineage_contains_forbidden_identity")
        document_id = str(row.get("document_id") or "").strip()
        source_uid = str(row.get("source_email_uid") or "").strip()
        source_chain = row.get("source_chain_sha256s")
        relative = str(row.get("output_relative_path") or "").replace("\\", "/")
        expected_hash = str(row.get("output_sha256") or "").lower()
        if (
            str(row.get("run_id") or "") != run_id
            or not document_id
            or document_id in document_ids
            or not source_uid
            or not isinstance(source_chain, list)
            or not source_chain
            or not all(_full_sha256(value) for value in source_chain)
            or not all(
                str(row.get(field) or "").strip()
                for field in ("artifact_role", "transformation_type", "provider_type")
            )
        ):
            raise ValueError("invalid_document_lineage")
        path = (output_root / Path(relative)).resolve(strict=False)
        try:
            path.relative_to(output_root.resolve(strict=False))
        except ValueError as exc:
            raise ValueError("lineage_output_escape") from exc
        inventory_row = inventory_by_relative.get(relative.casefold())
        try:
            expected_size = int(row.get("output_size"))
        except (TypeError, ValueError) as exc:
            raise ValueError("lineage_output_mismatch") from exc
        if (
            inventory_row is None
            or not path.is_file()
            or expected_hash != str(inventory_row["sha256"]).lower()
            or expected_size != int(inventory_row["size"])
        ):
            raise ValueError("lineage_output_mismatch")
        canonical = str(path).casefold()
        if canonical in bindings:
            raise ValueError("duplicate_lineage_assignment")
        document_ids.add(document_id)
        bindings[canonical] = expected_hash
    return bindings


def atomic_write_json(path: Path, payload: Mapping[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f"{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except BaseException:
        Path(temp_name).unlink(missing_ok=True)
        raise
    return path


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return {}


def _payload_parts(outcome: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    payload = _mapping(getattr(outcome, "to_legacy_payload", lambda: {})())
    info = _mapping(payload.get("info_json") or payload)
    metadata = _mapping(payload.get("metadata"))
    return payload, {**metadata, **info}


_SOURCE_PATH_KEYS = frozenset(
    {
        "filepath",
        "pdf_path",
        "source_path",
        "original_path",
        "staged_path",
        "downloaded_path",
        "converted_path",
        "converted_pdf_path",
    }
)


def _source_path_values(value: Any, key: str = "") -> list[Path]:
    found: list[Path] = []
    if isinstance(value, Mapping):
        for child_key, child in value.items():
            found.extend(_source_path_values(child, str(child_key)))
    elif isinstance(value, (list, tuple)):
        for child in value:
            found.extend(_source_path_values(child, key))
    elif isinstance(value, str) and key.lower() in _SOURCE_PATH_KEYS:
        path = Path(value)
        if path.is_file():
            found.append(path)
    return found


@dataclass(frozen=True)
class _CapturedRun:
    run_id: str
    run_root: Path
    output_root: Path
    scope: dict[str, str]
    candidate_revision: str
    candidate_version: str
    hardware_mode: str
    hardware_fingerprint: str
    started_monotonic_seconds: str
    started_at_utc: str
    lineage: list[dict[str, Any]]


class RunEvidenceWriter:
    def __init__(
        self,
        *,
        revision_resolver: Callable[[], str] | None = None,
        version_resolver: Callable[[], str] | None = None,
        hardware_resolver: Callable[[], tuple[str, str]] | None = None,
        monotonic: Callable[[], float] | None = None,
        utc_clock: Callable[[], dt.datetime] | None = None,
        capture_promoter: Callable[[], None] | None = None,
    ) -> None:
        self._revision = revision_resolver or default_revision
        self._version = version_resolver or (lambda: "source")
        self._hardware = hardware_resolver or default_hardware
        self._monotonic = monotonic or time.monotonic
        self._utc_clock = utc_clock or (lambda: dt.datetime.now(UTC))
        self._capture_promoter = capture_promoter or (lambda: None)
        self._captured: dict[str, _CapturedRun] = {}
        self._abandoned: set[str] = set()

    @staticmethod
    def _lineage(run_id: str, output_root: Path, archive_report: Any) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        seen_documents: set[str] = set()
        for archived in tuple(getattr(archive_report, "outcomes", ()) or ()):
            if bool(getattr(archived, "duplicate", False)):
                continue
            outcome = getattr(archived, "outcome", None)
            if str(getattr(outcome, "status", "") or "") != "resolved":
                continue
            candidate = getattr(outcome, "candidate", None)
            identity = getattr(candidate, "identity", None)
            document_id = str(getattr(identity, "document_id", "") or "")
            source_email_uid = str(
                getattr(identity, "source_message_uid", "") or ""
            ).strip()
            if not document_id or document_id in seen_documents:
                raise ValueError("lineage_document_identity_invalid")
            if not source_email_uid:
                raise ValueError("lineage_source_uid_missing")
            output_path = Path(str(getattr(archived, "archive_path", "") or ""))
            if not output_path.is_file():
                raise ValueError("lineage_output_missing")
            output_real = output_path.resolve(strict=True)
            output_base = output_root.resolve(strict=True)
            try:
                relative = output_real.relative_to(output_base).as_posix()
            except ValueError as exc:
                raise ValueError("lineage_output_escape") from exc
            output_hash, output_size = _sha256_file(output_real)
            payload, combined = _payload_parts(outcome)
            metadata = _mapping(getattr(candidate, "to_legacy", lambda: {})())
            source_paths = [Path(str(getattr(candidate, "source_path", "") or ""))]
            source_paths.extend(_source_path_values(payload))
            source_paths.extend(_source_path_values(metadata))
            source_hashes: list[str] = []
            for source_path in source_paths:
                if source_path.is_file():
                    source_real = source_path.resolve(strict=True)
                    try:
                        source_real.relative_to(output_base)
                    except ValueError:
                        pass
                    else:
                        continue
                    digest, _size = _sha256_file(source_real)
                    if digest not in source_hashes:
                        source_hashes.append(digest)
            if not source_hashes:
                raise ValueError("lineage_source_chain_missing")
            source_hashes.sort()
            transformation = str(
                getattr(identity, "source_kind", "")
                or metadata.get("transformation_type")
                or metadata.get("download_mode")
                or combined.get("transformation_type")
                or "unknown"
            )
            provider = str(
                metadata.get("provider_family")
                or combined.get("provider_family")
                or combined.get("model")
                or "local"
            )
            artifact_role = str(
                metadata.get("artifact_role")
                or metadata.get("document_role")
                or combined.get("artifact_role")
                or combined.get("document_role")
                or ("invoice" if combined.get("InvoiceNumber") else "document")
            )
            rows.append(
                {
                    "run_id": run_id,
                    "document_id": document_id,
                    "source_email_uid": source_email_uid,
                    "source_chain_sha256s": source_hashes,
                    "output_relative_path": relative,
                    "output_sha256": output_hash,
                    "output_size": output_size,
                    "artifact_role": artifact_role,
                    "transformation_type": transformation,
                    "provider_type": provider,
                }
            )
            seen_documents.add(document_id)
        return rows

    @staticmethod
    def _quarantine_capture(context: Any, reason_code: str) -> None:
        root = Path(context.run_root or context.output_dir).resolve()
        marker = (
            root
            / "diagnostics"
            / "quarantined"
            / str(context.run_id)
            / "evidence_capture_late.json"
        )
        atomic_write_json(
            marker,
            {
                "run_id": str(context.run_id),
                "status": "quarantined",
                "reason_code": reason_code,
            },
        )

    def abandon(self, context: Any) -> None:
        run_id = str(context.run_id)
        self._abandoned.add(run_id)
        self._captured.pop(run_id, None)

    def capture(
        self,
        context: Any,
        result: Any,
        *,
        authorization: Callable[[], bool] | None = None,
    ) -> bool:
        request = context.request
        authorized = authorization or (lambda: True)
        revision = _full_revision(getattr(request, "trusted_revision", ""))
        if not revision:
            raise RevisionUnavailable()
        version = str(self._version() or "").strip()
        hardware_mode, hardware_fingerprint = self._hardware()
        scope = {
            "date_from": str(request.date_from),
            "date_to": str(request.date_to),
            "before_exclusive": str(request.before_exclusive),
            "account_domain": str(request.account_domain).lower(),
            "account_channel": str(request.channel_id).lower(),
            "mailbox": str(request.mailbox),
            "target_identifier": str(request.target_identifier),
            "run_mode": str(request.run_mode),
            "hardware_mode": str(hardware_mode),
            "hardware_fingerprint": str(hardware_fingerprint),
        }
        if not version or not all(scope.values()):
            raise ValueError("incomplete_production_evidence_scope")
        lineage = self._lineage(
            request.run_id,
            Path(request.save_path),
            result.archive_report,
        )
        validation_required = bool(getattr(request, "validation_required", False))
        included_count = int(getattr(request, "manifest_included_count", 0) or 0)
        processing_failed = bool(
            str(getattr(result, "reason_code", "") or "")
            or str(getattr(result, "error", "") or "")
        )
        if (
            not lineage
            and not bool(getattr(result, "cancelled", False))
            and (
                (validation_required and included_count > 0)
                or processing_failed
            )
        ):
            raise ValueError("required_lineage_empty")
        captured = _CapturedRun(
            run_id=request.run_id,
            run_root=Path(request.run_root).resolve(strict=True),
            output_root=Path(request.save_path).resolve(strict=True),
            scope=scope,
            candidate_revision=revision,
            candidate_version=version,
            hardware_mode=str(hardware_mode),
            hardware_fingerprint=str(hardware_fingerprint),
            started_monotonic_seconds=str(context.started_monotonic_seconds),
            started_at_utc=str(context.started_at_utc),
            lineage=lineage,
        )
        self._capture_promoter()
        if not authorized() or request.run_id in self._abandoned:
            self._quarantine_capture(context, "EVIDENCE_CAPTURE_AUTHORIZATION_REVOKED")
            return False
        self._captured[request.run_id] = captured
        if not authorized() or request.run_id in self._abandoned:
            self._captured.pop(request.run_id, None)
            self._quarantine_capture(context, "EVIDENCE_CAPTURE_AUTHORIZATION_REVOKED")
            return False
        return True

    def capture_for_test(
        self, *, run_id: str, run_root: Path, output_root: Path, archive_report: Any
    ) -> None:
        self._lineage(run_id, output_root, archive_report)

    def finalize(self, context: Any, result: Any) -> Path:
        del result
        captured = self._captured.pop(context.run_id, None)
        if captured is None:
            raise ValueError("run_lineage_not_captured")
        entries: list[dict[str, Any]] = []
        for path in sorted(
            (item for item in captured.output_root.rglob("*") if item.is_file()),
            key=lambda item: item.relative_to(captured.output_root).as_posix().casefold(),
        ):
            digest, size = _sha256_file(path)
            entries.append(
                {
                    "relative_path": path.relative_to(captured.output_root).as_posix(),
                    "size": size,
                    "sha256": digest,
                }
            )
        ended_monotonic = str(self._monotonic())
        ended_utc = _utc_text(self._utc_clock())
        elapsed = format(
            Decimal(ended_monotonic) - Decimal(captured.started_monotonic_seconds),
            "f",
        )
        evidence: dict[str, Any] = {
            "schema_version": 1,
            "run_id": captured.run_id,
            "run_root": str(captured.run_root),
            "candidate_revision": captured.candidate_revision,
            "candidate_version": captured.candidate_version,
            "validation_required": bool(context.request.validation_required),
            "manifest_included_count": int(context.request.manifest_included_count),
            "scope": captured.scope,
            "scope_digest": compute_scope_digest(captured.scope),
            "hardware_mode": captured.hardware_mode,
            "hardware_fingerprint": captured.hardware_fingerprint,
            "started_monotonic_seconds": captured.started_monotonic_seconds,
            "ended_monotonic_seconds": ended_monotonic,
            "elapsed_seconds": elapsed,
            "started_at_utc": captured.started_at_utc,
            "ended_at_utc": ended_utc,
            "lineage": captured.lineage,
            "lineage_digest": compute_lineage_digest(captured.lineage),
            "output_inventory": entries,
            "inventory_sha256": compute_inventory_digest(entries),
        }
        evidence["evidence_digest"] = compute_evidence_digest(evidence)
        return atomic_write_json(
            captured.run_root / "diagnostics" / "run_evidence.json", evidence
        )
