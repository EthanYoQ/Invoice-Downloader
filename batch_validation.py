"""Fresh, credential-free validation of a completed batch against finalized truth."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import posixpath
import stat
import tempfile
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from strict_truth_audit import compare as run_strict_audit
from truth_contracts import TruthContractError, TruthManifest
from run_evidence import (
    compute_evidence_digest,
    compute_inventory_digest as compute_production_inventory_digest,
    compute_lineage_digest,
    compute_scope_digest as compute_evidence_scope_digest,
    default_revision,
)
from artifact_verifier import verify_final_artifact


UTC = dt.timezone.utc
PERFORMANCE_SCOPE_FIELDS = (
    "date_from",
    "date_to",
    "before_exclusive",
    "account_domain",
    "account_channel",
    "mailbox",
    "target_identifier",
    "run_mode",
    "hardware_mode",
    "hardware_fingerprint",
)
PINNED_BASELINE_PAYLOAD: dict[str, Any] = {
    "schema_version": 2,
    "accepted": True,
    "run_id": "refactor_range2_20251125_20260614_20260624_231421",
    "run_root": (
        "manual_program_runs/"
        "refactor_range2_20251125_20260614_20260624_231421"
    ),
    "elapsed_seconds": "3262.55",
    "scope": {
        "date_from": "2025-11-25",
        "date_to": "2026-06-14",
        "before_exclusive": "2026-06-15",
        "account_domain": "qq.com",
        "account_channel": "qq",
        "mailbox": "INBOX",
        "target_identifier": "辉瑞",
        "run_mode": "clean-mailbox",
        "hardware_mode": "windows-desktop-standard",
        "hardware_fingerprint": (
            "9412c455e777cd3d0a8ccf557bc067c5ddfe45ea2c4ae61636cc4cf4cf9e85d1"
        ),
    },
    "manifest_sha256": (
        "c1ea5ac2e8ccc3cfa2f5f96d217ed8f8459a213f389b0654b116e6d9bc13c8b7"
    ),
    "inventory_sha256": (
        "f44b7ba5dc2080133b13e6f8320ec386b8ddc621877b5549206673ceff96fe5d"
    ),
    "strict_digest": (
        "212ffb87c0d8f29810f035737a4913512303ce917eaeee544ccbf9c029822aea"
    ),
    "source_revision": "unrecorded",
    "hardware_fingerprint": (
        "9412c455e777cd3d0a8ccf557bc067c5ddfe45ea2c4ae61636cc4cf4cf9e85d1"
    ),
}
PINNED_BASELINE_CONTRACT_SHA256 = (
    "a10ee5f35418d33eec4e42378b7851ce7eec086f7b74ac480d00a6d33b349883"
)


class BatchValidationError(ValueError):
    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_json_bytes(value)).hexdigest()


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


def _utc(value: Any, *, code: str) -> dt.datetime:
    text = str(value or "").strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BatchValidationError(code) from exc
    if parsed.tzinfo is None:
        raise BatchValidationError(code)
    return parsed.astimezone(UTC)


def _utc_text(value: dt.datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def canonical_windows_path(value: str | Path) -> str:
    text = str(value).replace("\\", "/")
    return posixpath.normpath(text).casefold()


def resolve_current_revision() -> str:
    try:
        return default_revision()
    except RuntimeError as exc:
        raise BatchValidationError("trusted_revision_unavailable") from exc


def _default_reparse_checker(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError as exc:
        raise BatchValidationError("inventory_read_failed", path.name) from exc
    attributes = int(getattr(info, "st_file_attributes", 0) or 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & 0x400)


def _secure_directory(
    value: str | Path,
    *,
    checker: Callable[[Path], bool],
    missing_code: str,
) -> Path:
    nominal = Path(value).absolute()
    if not nominal.is_dir():
        raise BatchValidationError(missing_code)
    anchor = Path(nominal.anchor)
    current = anchor
    for part in nominal.parts[1:]:
        current = current / part
        if current.exists() and checker(current):
            raise BatchValidationError("reparse_point_rejected", current.name)
    try:
        return nominal.resolve(strict=True)
    except OSError as exc:
        raise BatchValidationError(missing_code) from exc


def _sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise BatchValidationError("inventory_read_failed", path.name) from exc
    return digest.hexdigest(), size


@dataclass(frozen=True)
class InventoryEntry:
    relative_path: str
    absolute_path: str
    canonical_path: str
    size: int
    sha256: str

    def to_mapping(self) -> dict[str, Any]:
        return {
            "relative_path": self.relative_path,
            "absolute_path": self.absolute_path,
            "canonical_path": self.canonical_path,
            "size": self.size,
            "sha256": self.sha256,
        }


@dataclass(frozen=True)
class InventorySnapshot:
    root: str
    digest: str
    entries: tuple[InventoryEntry, ...]

    @property
    def by_canonical_path(self) -> Mapping[str, InventoryEntry]:
        return MappingProxyType({entry.canonical_path: entry for entry in self.entries})


def compute_inventory(
    output_root: str | Path,
    *,
    reparse_checker: Callable[[Path], bool] | None = None,
) -> InventorySnapshot:
    root_input = Path(output_root).absolute()
    checker = reparse_checker or _default_reparse_checker
    if not root_input.is_dir():
        raise BatchValidationError("missing_output_root")
    if checker(root_input):
        raise BatchValidationError("reparse_point_rejected", root_input.name)
    try:
        root = root_input.resolve(strict=True)
    except OSError as exc:
        raise BatchValidationError("inventory_read_failed", root_input.name) from exc
    entries: list[InventoryEntry] = []

    def visit(directory: Path) -> None:
        try:
            children = sorted(os.scandir(directory), key=lambda item: item.name.casefold())
        except OSError as exc:
            raise BatchValidationError("inventory_read_failed", directory.name) from exc
        for child in children:
            path = Path(child.path)
            if checker(path):
                raise BatchValidationError("reparse_point_rejected", path.name)
            try:
                child_stat = child.stat(follow_symlinks=False)
            except OSError as exc:
                raise BatchValidationError("inventory_read_failed", path.name) from exc
            if stat.S_ISDIR(child_stat.st_mode):
                visit(path)
                continue
            if not stat.S_ISREG(child_stat.st_mode):
                raise BatchValidationError("non_regular_output_artifact", path.name)
            sha256, size = _sha256_file(path)
            try:
                resolved_path = path.resolve(strict=True)
                resolved_path.relative_to(root)
            except (OSError, ValueError) as exc:
                raise BatchValidationError("inventory_path_escape", path.name) from exc
            absolute = str(resolved_path)
            entries.append(InventoryEntry(
                relative_path=path.relative_to(root).as_posix(),
                absolute_path=absolute,
                canonical_path=canonical_windows_path(absolute),
                size=size,
                sha256=sha256,
            ))

    visit(root)
    entries.sort(key=lambda item: canonical_windows_path(item.relative_path))
    canonical_paths = [entry.canonical_path for entry in entries]
    if len(canonical_paths) != len(set(canonical_paths)):
        raise BatchValidationError("duplicate_inventory_path")
    digest_payload = [
        {"relative_path": entry.relative_path, "size": entry.size, "sha256": entry.sha256}
        for entry in entries
    ]
    return InventorySnapshot(root=str(root), digest=_sha256_json(digest_payload), entries=tuple(entries))


def compute_scope_digest(value: Mapping[str, Any]) -> str:
    scope = {field: str(value.get(field) or "").strip() for field in PERFORMANCE_SCOPE_FIELDS}
    if not all(scope.values()):
        raise BatchValidationError("incomplete_performance_scope")
    scope["account_domain"] = scope["account_domain"].lower()
    scope["account_channel"] = scope["account_channel"].lower()
    return _sha256_json(scope)


def compute_performance_record_digest(value: Mapping[str, Any]) -> str:
    return _sha256_json(
        {
            key: item
            for key, item in value.items()
            if key not in {"record_digest", "contract_digest"}
        }
    )


def pinned_baseline_contract() -> dict[str, Any]:
    payload = json.loads(json.dumps(PINNED_BASELINE_PAYLOAD, ensure_ascii=False))
    payload["contract_sha256"] = PINNED_BASELINE_CONTRACT_SHA256
    return payload


@dataclass(frozen=True)
class BatchValidationResult:
    passed: bool
    run_id: str
    run_root: str
    candidate_revision: str
    candidate_version: str
    manifest_sha256: str
    inventory_sha256: str
    scope_digest: str
    evidence_digest: str
    lineage_digest: str
    strict_digest: str
    validation_digest: str
    manifest_path: str
    validation_started_at_utc: str
    audit_started_at_utc: str
    audit_completed_at_utc: str
    validation_completed_at_utc: str
    counts: Mapping[str, int]
    scope: Mapping[str, str]
    assignments: tuple[Mapping[str, Any], ...]
    fresh_audit: Mapping[str, Any]

    def to_mapping(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "run_id": self.run_id,
            "run_root": self.run_root,
            "candidate_revision": self.candidate_revision,
            "candidate_version": self.candidate_version,
            "manifest_sha256": self.manifest_sha256,
            "inventory_sha256": self.inventory_sha256,
            "scope_digest": self.scope_digest,
            "evidence_digest": self.evidence_digest,
            "lineage_digest": self.lineage_digest,
            "strict_digest": self.strict_digest,
            "validation_digest": self.validation_digest,
            "manifest_path": self.manifest_path,
            "validation_started_at_utc": self.validation_started_at_utc,
            "audit_started_at_utc": self.audit_started_at_utc,
            "audit_completed_at_utc": self.audit_completed_at_utc,
            "validation_completed_at_utc": self.validation_completed_at_utc,
            "counts": dict(self.counts),
            "scope": dict(self.scope),
            "assignments": [dict(item) for item in self.assignments],
            "fresh_audit": dict(self.fresh_audit),
        }


@dataclass(frozen=True)
class PerformanceVerdict:
    baseline_seconds: str
    candidate_seconds: str
    target_seconds: str
    speedup_fraction: str
    threshold_fraction: str
    scope_digest: str
    baseline_revision: str
    candidate_revision: str
    baseline_record_digest: str
    candidate_record_digest: str
    passed: bool

    def to_mapping(self) -> dict[str, Any]:
        return {
            "baseline_seconds": self.baseline_seconds,
            "candidate_seconds": self.candidate_seconds,
            "target_seconds": self.target_seconds,
            "speedup_fraction": self.speedup_fraction,
            "threshold_fraction": self.threshold_fraction,
            "scope_digest": self.scope_digest,
            "baseline_revision": self.baseline_revision,
            "candidate_revision": self.candidate_revision,
            "baseline_record_digest": self.baseline_record_digest,
            "candidate_record_digest": self.candidate_record_digest,
            "passed": self.passed,
        }


class BatchValidator:
    CONFIG_PATH = Path("monitoring/run_config.json")
    EVIDENCE_PATH = Path("diagnostics/run_evidence.json")
    SUPPLIED_AUDIT_PATH = Path("diagnostics/strict_truth_audit.json")
    REPORT_PATH = Path("diagnostics/batch_validation.json")

    def __init__(
        self,
        *,
        revision_resolver: Callable[[], str] | None = None,
        clock: Callable[[], dt.datetime] | None = None,
        audit_runner: Callable[[dict, Path], dict] | None = None,
        reparse_checker: Callable[[Path], bool] | None = None,
    ) -> None:
        self._revision_resolver = revision_resolver or resolve_current_revision
        self._clock = clock or (lambda: dt.datetime.now(UTC))
        self._audit_runner = audit_runner or run_strict_audit
        self._reparse_checker = reparse_checker or _default_reparse_checker

    def validate(
        self,
        manifest: TruthManifest | Mapping[str, Any] | str | Path,
        run_root: str | Path,
    ) -> BatchValidationResult:
        validation_started = self._now()
        manifest_path = ""
        if isinstance(manifest, (str, Path)):
            manifest_path = str(Path(manifest).resolve(strict=True))
        try:
            truth = self._truth(manifest)
        except TruthContractError as exc:
            raise BatchValidationError(exc.code, exc.detail) from exc
        truth_mapping = truth.to_mapping()
        manifest_sha256 = _sha256_json(truth_mapping)
        root = _secure_directory(
            run_root,
            checker=self._reparse_checker,
            missing_code="invalid_run_root",
        )
        config = _load_json(root / self.CONFIG_PATH, "missing_run_config")
        evidence = _load_json(root / self.EVIDENCE_PATH, "missing_run_evidence")
        revision = str(self._revision_resolver() or "").strip()
        if not revision:
            raise BatchValidationError("trusted_revision_unavailable")
        run_id, version, scope = self._validate_config(config, truth, root, revision)
        run_end = self._validate_run_evidence(evidence, root, run_id, revision, version)
        if validation_started <= run_end:
            raise BatchValidationError("validation_precedes_run_end")

        inventory = compute_inventory(root / "output", reparse_checker=self._reparse_checker)
        audit_started = self._now()
        if audit_started < validation_started:
            raise BatchValidationError("invalid_validator_clock")
        fresh = self._audit_runner(truth_mapping, root)
        audit_completed = self._now()
        if audit_completed < audit_started or audit_started <= run_end:
            raise BatchValidationError("invalid_fresh_audit_time")
        post_audit_inventory = compute_inventory(root / "output", reparse_checker=self._reparse_checker)
        if post_audit_inventory.digest != inventory.digest:
            raise BatchValidationError("inventory_changed_during_validation")
        inventory = post_audit_inventory
        counts, assignments = self._validate_fresh_audit(fresh, truth, root, inventory)
        assignments = self._validate_production_lineage(
            evidence,
            truth=truth,
            assignments=assignments,
            inventory=inventory,
            scope=scope,
        )
        validation_completed = self._now()
        if validation_completed < audit_completed:
            raise BatchValidationError("invalid_validator_clock")
        scope_digest = _sha256_json(scope)
        strict_digest = _sha256_json(
            {
                "counts": counts,
                "assignments": [
                    {
                        "truth_id": row["truth_id"],
                        "canonical_path": row["canonical_path"],
                        "artifact_sha256": row["artifact_sha256"],
                        "document_id": row["document_id"],
                        "artifact_verification_mode": row[
                            "artifact_verification_mode"
                        ],
                        "artifact_verification_fields": row[
                            "artifact_verification_fields"
                        ],
                    }
                    for row in assignments
                ],
            }
        )
        validation_binding = {
            "run_id": run_id,
            "run_root": str(root),
            "candidate_revision": revision,
            "candidate_version": version,
            "manifest_sha256": manifest_sha256,
            "inventory_sha256": inventory.digest,
            "scope_digest": scope_digest,
            "evidence_digest": str(evidence["evidence_digest"]),
            "lineage_digest": str(evidence["lineage_digest"]),
            "strict_digest": strict_digest,
        }
        validation_digest = _sha256_json(validation_binding)
        fresh_bound = {
            "run_id": run_id,
            "run_root": str(root),
            "candidate_revision": revision,
            "candidate_version": version,
            "manifest_sha256": manifest_sha256,
            "inventory_sha256": inventory.digest,
            "scope_digest": scope_digest,
            "audit_started_at_utc": _utc_text(audit_started),
            "audit_completed_at_utc": _utc_text(audit_completed),
            "artifact_count": int(fresh.get("artifact_count", 0) or 0),
            "exit_code": 0,
            "counts": dict(counts),
        }
        return BatchValidationResult(
            passed=True,
            run_id=run_id,
            run_root=str(root),
            candidate_revision=revision,
            candidate_version=version,
            manifest_sha256=manifest_sha256,
            inventory_sha256=inventory.digest,
            scope_digest=scope_digest,
            evidence_digest=str(evidence["evidence_digest"]),
            lineage_digest=str(evidence["lineage_digest"]),
            strict_digest=strict_digest,
            validation_digest=validation_digest,
            manifest_path=manifest_path,
            validation_started_at_utc=_utc_text(validation_started),
            audit_started_at_utc=_utc_text(audit_started),
            audit_completed_at_utc=_utc_text(audit_completed),
            validation_completed_at_utc=_utc_text(validation_completed),
            counts=MappingProxyType(dict(counts)),
            scope=MappingProxyType(dict(scope)),
            assignments=tuple(MappingProxyType(dict(item)) for item in assignments),
            fresh_audit=MappingProxyType(fresh_bound),
        )

    def _now(self) -> dt.datetime:
        value = self._clock()
        if not isinstance(value, dt.datetime):
            raise BatchValidationError("invalid_validator_clock")
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    @staticmethod
    def _truth(value: TruthManifest | Mapping[str, Any] | str | Path) -> TruthManifest:
        if isinstance(value, TruthManifest):
            return value
        if isinstance(value, Mapping):
            return TruthManifest.from_mapping(value)
        return TruthManifest.from_path(value)

    @staticmethod
    def _validate_config(
        config: Mapping[str, Any],
        truth: TruthManifest,
        root: Path,
        revision: str,
    ) -> tuple[str, str, dict[str, str]]:
        run_id = str(config.get("run_id") or "").strip()
        version = str(config.get("candidate_version") or "").strip()
        if not run_id:
            raise BatchValidationError("missing_run_id")
        if not version:
            raise BatchValidationError("missing_candidate_version")
        if str(config.get("candidate_revision") or "") != revision:
            raise BatchValidationError("version_mismatch")
        if canonical_windows_path(str(config.get("run_root") or "")) != canonical_windows_path(str(root)):
            raise BatchValidationError("scope_mismatch", "run_root")
        active = config.get("active_run_config", {})
        if not isinstance(active, Mapping):
            raise BatchValidationError("invalid_run_config")
        scope = {
            "date_from": str(config.get("locked_date_from") or config.get("date_from") or ""),
            "date_to": str(config.get("locked_date_to") or config.get("date_to") or ""),
            "before_exclusive": str(config.get("before_exclusive") or ""),
            "account_domain": str(config.get("email_domain") or config.get("account_domain") or "").lower(),
            "account_channel": str(config.get("account_channel") or "").lower(),
            "mailbox": str(config.get("mailbox") or ""),
            "target_identifier": str(
                config.get("target_company") or config.get("target_company_id") or active.get("company") or ""
            ),
            "run_mode": str(config.get("run_mode") or ""),
            "hardware_mode": str(config.get("hardware_mode") or ""),
            "hardware_fingerprint": str(config.get("hardware_fingerprint") or ""),
        }
        expected = {
            "date_from": truth.date_from.isoformat(),
            "date_to": truth.date_to.isoformat(),
            "before_exclusive": truth.before_exclusive.isoformat(),
            "account_domain": truth.account_domain,
            "account_channel": truth.account_channel,
            "mailbox": truth.mailbox,
            "target_identifier": truth.target_company,
        }
        for field, value in expected.items():
            if scope.get(field) != value:
                raise BatchValidationError("scope_mismatch", field)
        if not all(scope.values()):
            raise BatchValidationError("incomplete_run_scope")
        return run_id, version, scope

    @staticmethod
    def _validate_run_evidence(
        evidence: Mapping[str, Any],
        root: Path,
        run_id: str,
        revision: str,
        version: str,
    ) -> dt.datetime:
        if str(evidence.get("run_id") or "") != run_id:
            raise BatchValidationError("scope_mismatch", "run evidence id")
        if canonical_windows_path(str(evidence.get("run_root") or "")) != canonical_windows_path(str(root)):
            raise BatchValidationError("scope_mismatch", "run evidence root")
        if evidence.get("candidate_revision") != revision or evidence.get("candidate_version") != version:
            raise BatchValidationError("version_mismatch")
        if int(evidence.get("schema_version", 0) or 0) != 1:
            raise BatchValidationError("invalid_run_evidence_schema")
        if str(evidence.get("evidence_digest") or "") != compute_evidence_digest(evidence):
            raise BatchValidationError("run_evidence_digest_mismatch")
        start = _decimal(evidence.get("started_monotonic_seconds"), code="invalid_timing_boundary")
        end = _decimal(evidence.get("ended_monotonic_seconds"), code="invalid_timing_boundary")
        elapsed = _decimal(evidence.get("elapsed_seconds"), code="invalid_timing_boundary")
        if end < start or elapsed != end - start:
            raise BatchValidationError("invalid_timing_boundary")
        wall_start = _utc(evidence.get("started_at_utc"), code="invalid_run_time")
        wall_end = _utc(evidence.get("ended_at_utc"), code="invalid_run_time")
        if wall_end < wall_start:
            raise BatchValidationError("invalid_run_time")
        return wall_end

    @staticmethod
    def _validate_production_lineage(
        evidence: Mapping[str, Any],
        *,
        truth: TruthManifest,
        assignments: list[dict[str, Any]],
        inventory: InventorySnapshot,
        scope: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        if (
            evidence.get("validation_required") is not True
            or evidence.get("manifest_included_count") != len(truth.included)
        ):
            raise BatchValidationError("validation_evidence_scope_mismatch")
        evidence_scope = evidence.get("scope")
        if not isinstance(evidence_scope, Mapping) or dict(evidence_scope) != dict(scope):
            raise BatchValidationError("run_evidence_scope_mismatch")
        if (
            str(evidence.get("hardware_mode") or "")
            != str(evidence_scope.get("hardware_mode") or "")
            or str(evidence.get("hardware_fingerprint") or "")
            != str(evidence_scope.get("hardware_fingerprint") or "")
        ):
            raise BatchValidationError("run_evidence_scope_mismatch")
        if str(evidence.get("scope_digest") or "") != compute_evidence_scope_digest(evidence_scope):
            raise BatchValidationError("run_evidence_scope_mismatch")
        raw_inventory = evidence.get("output_inventory")
        if not isinstance(raw_inventory, list):
            raise BatchValidationError("run_evidence_inventory_mismatch")
        try:
            evidence_inventory_digest = compute_production_inventory_digest(raw_inventory)
        except (KeyError, TypeError, ValueError) as exc:
            raise BatchValidationError("run_evidence_inventory_mismatch") from exc
        if (
            evidence_inventory_digest != inventory.digest
            or str(evidence.get("inventory_sha256") or "") != inventory.digest
        ):
            raise BatchValidationError("run_evidence_inventory_mismatch")
        expected_inventory = {
            canonical_windows_path(entry.relative_path): (entry.sha256, entry.size)
            for entry in inventory.entries
        }
        supplied_inventory: dict[str, tuple[str, int]] = {}
        for item in raw_inventory:
            if not isinstance(item, Mapping):
                raise BatchValidationError("run_evidence_inventory_mismatch")
            canonical = canonical_windows_path(str(item.get("relative_path") or ""))
            if not canonical or canonical in supplied_inventory:
                raise BatchValidationError("run_evidence_inventory_mismatch")
            try:
                supplied_inventory[canonical] = (
                    str(item.get("sha256") or "").lower(),
                    int(item.get("size")),
                )
            except (TypeError, ValueError) as exc:
                raise BatchValidationError("run_evidence_inventory_mismatch") from exc
        if supplied_inventory != expected_inventory:
            raise BatchValidationError("run_evidence_inventory_mismatch")

        lineage = evidence.get("lineage")
        if not isinstance(lineage, list) or not lineage:
            raise BatchValidationError("missing_document_lineage")
        if str(evidence.get("lineage_digest") or "") != compute_lineage_digest(lineage):
            raise BatchValidationError("lineage_digest_mismatch")
        by_path: dict[str, Mapping[str, Any]] = {}
        document_ids: set[str] = set()
        output_root = Path(inventory.root)
        allowed_lineage_fields = {
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
        for row in lineage:
            if not isinstance(row, Mapping):
                raise BatchValidationError("invalid_document_lineage")
            if set(row) != allowed_lineage_fields:
                raise BatchValidationError("lineage_contains_forbidden_identity")
            document_id = str(row.get("document_id") or "").strip()
            relative = str(row.get("output_relative_path") or "").strip()
            source_uid = str(row.get("source_email_uid") or "").strip()
            source_chain = row.get("source_chain_sha256s")
            if (
                str(row.get("run_id") or "") != str(evidence.get("run_id") or "")
                or
                not document_id
                or document_id in document_ids
                or not source_uid
                or not isinstance(source_chain, list)
                or not source_chain
            ):
                raise BatchValidationError("invalid_document_lineage")
            absolute = output_root / Path(relative)
            canonical = canonical_windows_path(str(absolute.resolve()))
            if canonical in by_path:
                raise BatchValidationError("duplicate_lineage_assignment")
            entry = inventory.by_canonical_path.get(canonical)
            if entry is None:
                raise BatchValidationError("lineage_output_mismatch")
            try:
                output_size = int(row.get("output_size"))
            except (TypeError, ValueError) as exc:
                raise BatchValidationError("lineage_output_mismatch") from exc
            if (
                str(row.get("output_sha256") or "").lower() != entry.sha256
                or output_size != entry.size
            ):
                raise BatchValidationError("lineage_output_mismatch")
            if not all(
                isinstance(value, str)
                and len(value) == 64
                and all(char in "0123456789abcdefABCDEF" for char in value)
                for value in source_chain
            ):
                raise BatchValidationError("invalid_document_lineage")
            if (
                not str(row.get("artifact_role") or "")
                or not str(row.get("transformation_type") or "")
                or not str(row.get("provider_type") or "")
            ):
                raise BatchValidationError("invalid_document_lineage")
            document_ids.add(document_id)
            by_path[canonical] = row

        truth_by_id = {row.truth_id: row for row in truth.included}
        bound: list[dict[str, Any]] = []
        for assignment in assignments:
            lineage_row = by_path.get(str(assignment["canonical_path"]))
            if lineage_row is None:
                raise BatchValidationError("missing_assignment_lineage")
            truth_row = truth_by_id[str(assignment["truth_id"])]
            strong_hashes = {
                str(value).lower()
                for value in lineage_row["source_chain_sha256s"]
            }
            strong_hashes.add(str(lineage_row["output_sha256"]).lower())
            if truth_row.artifact_sha256.lower() not in strong_hashes:
                raise BatchValidationError("truth_lineage_mismatch", truth_row.truth_id)
            verification = verify_final_artifact(
                truth_row.to_mapping(),
                assignment["matched_path"],
                output_sha256=str(lineage_row["output_sha256"]),
                source_chain_sha256s=list(lineage_row["source_chain_sha256s"]),
            )
            if not verification.passed:
                raise BatchValidationError(
                    "artifact_content_verification_failed",
                    f"{truth_row.truth_id}:{verification.reason_code}",
                )
            bound.append(
                {
                    **assignment,
                    "document_id": str(lineage_row["document_id"]),
                    "source_email_uid": str(lineage_row["source_email_uid"]),
                    "lineage_output_sha256": str(lineage_row["output_sha256"]),
                    "artifact_verification_mode": verification.verification_mode,
                    "artifact_verification_fields": list(verification.matched_fields),
                }
            )
        return bound

    @staticmethod
    def _strict_counts(audit: Mapping[str, Any]) -> dict[str, int]:
        try:
            p0 = int((audit.get("p0_conclusion") or {}).get("count", -1))
            p1 = int((audit.get("user_p1_conclusion") or {}).get("count", -1))
            p2 = int((audit.get("p2_conclusion") or {}).get("count", -1))
        except (AttributeError, TypeError, ValueError) as exc:
            raise BatchValidationError("invalid_audit_result") from exc
        manual_rows = audit.get("manual_check_rows")
        manual = len(manual_rows) if isinstance(manual_rows, list) else -1
        return {"p0": p0, "p1": p1, "p2": p2, "manual": manual}

    @classmethod
    def _validate_fresh_audit(
        cls,
        audit: Mapping[str, Any],
        truth: TruthManifest,
        root: Path,
        inventory: InventorySnapshot,
    ) -> tuple[dict[str, int], list[dict[str, Any]]]:
        if not isinstance(audit, Mapping):
            raise BatchValidationError("invalid_audit_result")
        if canonical_windows_path(str(audit.get("run_root") or "")) != canonical_windows_path(str(root)):
            raise BatchValidationError("scope_mismatch", "fresh audit root")
        counts = cls._strict_counts(audit)
        if any(count != 0 for count in counts.values()):
            raise BatchValidationError("strict_audit_failed")
        matched = audit.get("matched_rows")
        if not isinstance(matched, list) or len(matched) != len(truth.included):
            raise BatchValidationError("matched_count_mismatch")
        raw_paths = [str(row.get("matched_path") or "") for row in matched if isinstance(row, Mapping)]
        canonical_paths = [canonical_windows_path(path) for path in raw_paths]
        if len(raw_paths) != len(matched):
            raise BatchValidationError("invalid_artifact_assignment")
        if len(canonical_paths) != len(set(canonical_paths)):
            raise BatchValidationError("duplicate_artifact_assignment")
        inventory_map = inventory.by_canonical_path
        assignments = []
        truth_ids = []
        for row, canonical in zip(matched, canonical_paths):
            path_value = str(row.get("matched_path") or "")
            if not path_value or not Path(path_value).is_absolute():
                raise BatchValidationError("invalid_artifact_assignment")
            entry = inventory_map.get(canonical)
            if entry is None:
                raise BatchValidationError("invalid_artifact_assignment")
            claimed_hash = str(row.get("artifact_sha256") or row.get("sha256") or "")
            if claimed_hash and claimed_hash.lower() != entry.sha256:
                raise BatchValidationError("artifact_hash_mismatch")
            truth_id = str(row.get("truth_id") or "")
            truth_ids.append(truth_id)
            assignments.append({
                **dict(row),
                "matched_path": entry.absolute_path,
                "canonical_path": entry.canonical_path,
                "artifact_sha256": entry.sha256,
                "artifact_size": entry.size,
            })
        expected_ids = {row.truth_id for row in truth.included}
        if set(truth_ids) != expected_ids or len(truth_ids) != len(set(truth_ids)):
            raise BatchValidationError("matched_count_mismatch")
        counts["matched"] = len(assignments)
        return counts, assignments


    def write_report(self, result: BatchValidationResult, run_root: str | Path) -> Path:
        root = _secure_directory(
            run_root,
            checker=self._reparse_checker,
            missing_code="invalid_run_root",
        )
        diagnostics = root / "diagnostics"
        diagnostics.mkdir(parents=True, exist_ok=True)
        _secure_directory(
            diagnostics,
            checker=self._reparse_checker,
            missing_code="output_path_escape",
        )
        if not canonical_windows_path(str(diagnostics)).startswith(canonical_windows_path(str(root)) + "/"):
            raise BatchValidationError("output_path_escape")
        output = diagnostics / "batch_validation.json"
        descriptor, temp_name = tempfile.mkstemp(prefix="batch_validation.", suffix=".tmp", dir=diagnostics)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(json.dumps(result.to_mapping(), ensure_ascii=False, indent=2).encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, output)
        except BaseException:
            Path(temp_name).unlink(missing_ok=True)
            raise
        return output


def _accepted_baseline(payload: Mapping[str, Any]) -> tuple[Decimal, dict[str, str], str]:
    expected = pinned_baseline_contract()
    if dict(payload) != expected:
        raise BatchValidationError("invalid_baseline_contract")
    canonical = {
        key: value for key, value in payload.items() if key != "contract_sha256"
    }
    if (
        _sha256_json(canonical) != PINNED_BASELINE_CONTRACT_SHA256
        or payload.get("contract_sha256") != PINNED_BASELINE_CONTRACT_SHA256
    ):
        raise BatchValidationError("invalid_baseline_contract")
    return (
        Decimal("3262.55"),
        {str(key): str(value) for key, value in PINNED_BASELINE_PAYLOAD["scope"].items()},
        PINNED_BASELINE_CONTRACT_SHA256,
    )


def _revalidate_candidate_report(
    report_path: Path,
    *,
    revision_resolver: Callable[[], str],
) -> tuple[BatchValidationResult, Mapping[str, Any], Decimal]:
    try:
        report = _load_json(report_path, "candidate_validation_report_invalid")
        root = Path(str(report.get("run_root") or "")).resolve(strict=True)
    except (OSError, TypeError, ValueError) as exc:
        raise BatchValidationError("candidate_validation_report_invalid") from exc
    expected_report = root / BatchValidator.REPORT_PATH
    if canonical_windows_path(str(report_path.resolve())) != canonical_windows_path(str(expected_report)):
        raise BatchValidationError("candidate_validation_report_invalid")
    manifest_path = str(report.get("manifest_path") or "")
    if not manifest_path:
        raise BatchValidationError("candidate_validation_report_invalid")
    validator = BatchValidator(revision_resolver=revision_resolver)
    fresh = validator.validate(manifest_path, root)
    stable_fields = (
        "run_id",
        "run_root",
        "candidate_revision",
        "candidate_version",
        "manifest_sha256",
        "inventory_sha256",
        "scope_digest",
        "evidence_digest",
        "lineage_digest",
        "strict_digest",
        "validation_digest",
    )
    fresh_mapping = fresh.to_mapping()
    if any(str(report.get(field) or "") != str(fresh_mapping[field]) for field in stable_fields):
        raise BatchValidationError("candidate_validation_report_mismatch")
    evidence = _load_json(root / BatchValidator.EVIDENCE_PATH, "missing_run_evidence")
    if str(evidence.get("evidence_digest") or "") != fresh.evidence_digest:
        raise BatchValidationError("candidate_validation_report_mismatch")
    elapsed = _decimal(evidence.get("elapsed_seconds"), code="invalid_performance_boundaries")
    start = _decimal(evidence.get("started_monotonic_seconds"), code="invalid_performance_boundaries")
    end = _decimal(evidence.get("ended_monotonic_seconds"), code="invalid_performance_boundaries")
    if elapsed <= 0 or end - start != elapsed:
        raise BatchValidationError("invalid_performance_boundaries")
    return fresh, evidence, elapsed


def compare_performance(
    baseline_json: str | Path,
    candidate_json: str | Path,
    *,
    revision_resolver: Callable[[], str] | None = None,
) -> PerformanceVerdict:
    baseline_payload = _load_json(Path(baseline_json), "invalid_baseline_metrics")
    resolver = revision_resolver or resolve_current_revision
    baseline, baseline_scope, baseline_record = _accepted_baseline(baseline_payload)
    candidate_result, _candidate_evidence, candidate = _revalidate_candidate_report(
        Path(candidate_json), revision_resolver=resolver
    )
    candidate_scope = dict(candidate_result.scope)
    if any(candidate_scope.get(key) != value for key, value in baseline_scope.items()):
        raise BatchValidationError("performance_contract_mismatch")
    if PINNED_BASELINE_PAYLOAD["manifest_sha256"] != candidate_result.manifest_sha256:
        raise BatchValidationError("performance_contract_mismatch")
    if baseline_payload.get("run_id") == candidate_result.run_id:
        raise BatchValidationError("performance_run_reused")
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
        scope_digest=candidate_result.scope_digest,
        baseline_revision=str(PINNED_BASELINE_PAYLOAD["source_revision"]),
        candidate_revision=candidate_result.candidate_revision,
        baseline_record_digest=baseline_record,
        candidate_record_digest=candidate_result.validation_digest,
        passed=candidate <= target,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate finalized truth against fresh run evidence.")
    parser.add_argument("--truth-manifest", required=True, help="Path to finalized truth JSON")
    parser.add_argument("--run-root", required=True, help="Path to completed run root")
    args = parser.parse_args()
    validator = BatchValidator()
    try:
        result = validator.validate(Path(args.truth_manifest), Path(args.run_root))
        output = validator.write_report(result, Path(args.run_root))
    except (BatchValidationError, TruthContractError) as exc:
        print(json.dumps({"passed": False, "error_code": exc.code}, ensure_ascii=False))
        return 1
    print(json.dumps({"passed": True, "counts": dict(result.counts), "output": str(output.absolute())}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
