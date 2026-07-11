from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from types import MappingProxyType
from typing import Any, Iterable, Mapping
from urllib.parse import urlsplit, urlunsplit

from invoice_domain import DocumentIdentity
from url_trace_sanitizer import build_url_history_key


class _FrozenList(tuple):
    pass


class _FrozenSet(frozenset):
    pass


class _FrozenBytearray(bytes):
    pass


def freeze_legacy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: freeze_legacy_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(freeze_legacy_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(freeze_legacy_value(item) for item in value)
    if isinstance(value, set):
        return _FrozenSet(freeze_legacy_value(item) for item in value)
    if isinstance(value, bytearray):
        return _FrozenBytearray(value)
    return value


def thaw_legacy_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: thaw_legacy_value(item) for key, item in value.items()}
    if isinstance(value, _FrozenList):
        return [thaw_legacy_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(thaw_legacy_value(item) for item in value)
    if isinstance(value, _FrozenSet):
        return {thaw_legacy_value(item) for item in value}
    if isinstance(value, _FrozenBytearray):
        return bytearray(value)
    return value


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return freeze_legacy_value(dict(value or {}))


def _stream_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalized_url_digest(value: str) -> str:
    parsed = urlsplit(str(value or "").strip())
    hostname = (parsed.hostname or "").lower()
    if parsed.port:
        hostname = f"{hostname}:{parsed.port}"
    normalized = urlunsplit(
        ((parsed.scheme or "https").lower(), hostname, parsed.path or "/", parsed.query, "")
    )
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def build_compatibility_history_key(
    info: Mapping[str, Any] | None, file_name: str, source_path: str
) -> str:
    """Produce the exact pre-canonical processing-history key."""
    metadata = dict(info or {})
    legacy_key = hashlib.md5(
        f"{metadata.get('subject', '')}_{file_name}_{metadata.get('tier', 0)}".encode(
            "utf-8"
        )
    ).hexdigest()
    if metadata.get("is_url", False):
        expected = metadata.get("provider_expected_fields") or {}
        invoice_number = str(
            expected.get("invoice_number") or expected.get("InvoiceNumber") or ""
        ).strip()
        return build_url_history_key(
            provider_family=str(metadata.get("provider_family") or "").strip(),
            email_id=str(metadata.get("email_id") or "").strip(),
            invoice_number=invoice_number,
            source_url=str(
                metadata.get("source_url") or source_path or file_name or legacy_key
            ).strip()
            or legacy_key,
        )
    try:
        return f"att:{_stream_sha256(source_path)}"
    except Exception:
        return f"att-legacy:{legacy_key}"


@dataclass(frozen=True)
class DocumentCandidate:
    identity: DocumentIdentity
    sequence: int
    source_path: str = ""
    source_url: str = ""
    channel: str = ""
    source_filename: str = ""
    compatibility_history_key: str = ""
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("DocumentCandidate.sequence must be non-negative")
        if not self.compatibility_history_key:
            object.__setattr__(
                self,
                "compatibility_history_key",
                build_compatibility_history_key(
                    self.metadata,
                    self.source_filename,
                    self.source_url or self.source_path,
                ),
            )
        object.__setattr__(self, "trace_context", _freeze_mapping(self.trace_context))
        object.__setattr__(self, "metadata", _freeze_mapping(self.metadata))

    @property
    def parallel_safe(self) -> bool:
        explicit = self.metadata.get("parallel_safe")
        if explicit is not None:
            return bool(explicit)
        return not bool(
            self.source_url
            or self.metadata.get("is_url")
            or self.metadata.get("provider_family")
            or self.metadata.get("browser_recovery")
            or self.metadata.get("provider_recovery")
        )

    def to_legacy(self) -> dict[str, Any]:
        return thaw_legacy_value(self.metadata)


class CandidatePipeline:
    """Turn mailbox artifacts into immutable, ordered processing candidates."""

    def __init__(self, *, channel: str = "") -> None:
        self._channel = str(channel or "")

    def collect(
        self,
        message_refs: Iterable[Mapping[str, Any] | DocumentCandidate],
        *,
        sequence_offset: int = 0,
    ) -> list[DocumentCandidate]:
        candidates: list[DocumentCandidate] = []
        for offset, source in enumerate(message_refs):
            sequence = sequence_offset + offset
            if isinstance(source, DocumentCandidate):
                if source.sequence == sequence:
                    candidates.append(source)
                else:
                    candidates.append(
                        DocumentCandidate(
                            identity=source.identity,
                            sequence=sequence,
                            source_path=source.source_path,
                            source_url=source.source_url,
                            channel=source.channel,
                            source_filename=source.source_filename,
                            compatibility_history_key=source.compatibility_history_key,
                            trace_context=source.trace_context,
                            metadata=source.metadata,
                        )
                    )
                continue
            if not isinstance(source, Mapping):
                source = {
                    "filepath": "",
                    "candidate_action": "manual_review",
                    "prefilter_reason_code": "MALFORMED_DOCUMENT_CANDIDATE",
                    "raw_type": type(source).__name__,
                }

            metadata = dict(source)
            source_url = str(metadata.get("source_url") or "")
            source_path = str(metadata.get("filepath") or source_url or "")
            metadata["filepath"] = source_path
            if not source_path:
                metadata.setdefault("candidate_action", "manual_review")
                metadata.setdefault(
                    "prefilter_reason_code", "MALFORMED_DOCUMENT_CANDIDATE"
                )
            is_url = bool(metadata.get("is_url") or source_url)
            filename = (
                os.path.basename(urlsplit(source_url or source_path).path)
                if is_url
                else os.path.basename(source_path)
            ) or str(metadata.get("filename") or "")
            message_uid = str(
                metadata.get("message_uid")
                or metadata.get("source_message_uid")
                or metadata.get("source_email_id")
                or metadata.get("email_id")
                or metadata.get("uid")
                or ""
            )
            provider_group_key = str(metadata.get("provider_group_key") or "")
            legacy_document_seed = "|".join(
                (
                    source_path,
                    str(metadata.get("subject") or ""),
                    filename,
                    str(metadata.get("tier", 0)),
                    str(sequence),
                )
            )
            if is_url:
                source_digest = _normalized_url_digest(source_url or source_path)
                source_locator = f"url_sha256:{source_digest}"
            else:
                attachment_part_id = str(
                    metadata.get("attachment_part_id")
                    or metadata.get("part_id")
                    or metadata.get("content_id")
                    or ""
                )
                if source_path and os.path.isfile(source_path):
                    source_digest = _stream_sha256(source_path)
                elif attachment_part_id:
                    source_digest = hashlib.sha256(
                        attachment_part_id.encode("utf-8")
                    ).hexdigest()
                else:
                    source_digest = hashlib.sha256(
                        os.path.abspath(source_path).encode("utf-8", errors="replace")
                    ).hexdigest()
                source_locator = source_path
            stable_document_seed = "\0".join(
                (
                    message_uid,
                    source_digest,
                    filename,
                    provider_group_key,
                    str(metadata.get("subject") or ""),
                    str(metadata.get("tier", 0)),
                )
            )
            document_id = hashlib.sha256(
                stable_document_seed.encode("utf-8")
            ).hexdigest()
            legacy_document_id = str(
                metadata.get("legacy_document_id")
                or metadata.get("document_id")
                or hashlib.md5(legacy_document_seed.encode("utf-8")).hexdigest()
            )
            source_kind = "url" if is_url else "attachment"
            identity = DocumentIdentity(
                document_id=document_id,
                source_message_uid=message_uid,
                source_filename=filename,
                source_locator=source_locator,
                source_kind=source_kind,
                provider_group_key=provider_group_key,
            )
            trace_context = {
                "candidate_index": sequence + 1,
                "legacy_document_id": legacy_document_id,
                "tier": metadata.get("tier", 0),
                "provider_family": metadata.get("provider_family", ""),
                "compatibility_history_key": build_compatibility_history_key(
                    metadata, filename, source_path
                ),
            }
            candidates.append(
                DocumentCandidate(
                    identity=identity,
                    sequence=sequence,
                    source_path=source_path,
                    source_url=source_url,
                    channel=str(metadata.get("channel") or self._channel),
                    source_filename=filename,
                    compatibility_history_key=trace_context[
                        "compatibility_history_key"
                    ],
                    trace_context=trace_context,
                    metadata=metadata,
                )
            )
        return candidates


class CandidatePreflight:
    """Serial qualification, recovery, dedupe, and pure-local extraction."""

    def __init__(
        self,
        *,
        api: Any,
        extractor: Any,
        working_history: set[str],
        sidecar: dict[str, dict[str, Any]],
        sidecar_lock: Any,
        converter_factory: Any,
    ) -> None:
        self.api = api
        self.extractor = extractor
        self.working_history = working_history
        self.sidecar = sidecar
        self.sidecar_lock = sidecar_lock
        self.converter_factory = converter_factory
        self.seen_identities: set[str] = set()
        self.seen_history_keys: set[str] = set()
        self.seen_provider_groups: set[str] = set()

    @staticmethod
    def terminal(candidate, reason_code, *, status="retained"):
        from extraction_pipeline import ExtractionOutcome

        return ExtractionOutcome(
            candidate=candidate,
            status=status,
            reason_code=str(reason_code or status),
            message=str(reason_code or status),
            artifact_path=candidate.source_path,
        )

    def _recover_url(self, candidate, legacy):
        provider_group = str(legacy.get("provider_group_key") or "")
        if provider_group and provider_group in self.seen_provider_groups:
            return self.terminal(
                candidate, "PROVIDER_GROUP_ALREADY_PROCESSED", status="duplicate"
            )
        if self.api._should_gate_controlled_run_url(legacy):
            return self.terminal(candidate, "CONTROLLED_RUN_NON_PROVIDER_URL_SKIPPED")
        converter = self.converter_factory()
        self.api._append_log(
            "抓取:",
            f"正在启动无头浏览器抓取网页: {self.api._url_candidate_label(legacy)}",
            "text-blue-400",
        )
        try:
            results = converter.process_invoice_links(
                candidate.source_path,
                legacy.get("subject", "Link_Invoice"),
                f"url_{candidate.sequence}",
                return_metadata=True,
                candidate_info=legacy,
            )
        except Exception:
            return self.terminal(candidate, "URL_DOWNLOAD_FAILED", status="unresolved")
        if not results:
            return self.terminal(candidate, "URL_DOWNLOAD_FAILED", status="unresolved")
        result = dict(results[0] or {})
        if str(result.get("status") or "").lower() in {"failed", "skipped"}:
            return self.terminal(
                candidate,
                str(result.get("reason_code") or "URL_DOWNLOAD_FAILED"),
                status="unresolved",
            )
        pdf_path = str(result.get("pdf_path") or "")
        if not pdf_path:
            return self.terminal(candidate, "URL_DOWNLOAD_FAILED", status="unresolved")
        if provider_group:
            self.seen_provider_groups.add(provider_group)
        legacy.update(
            {
                "filepath": pdf_path,
                "resolved_url": result.get("resolved_url", ""),
                "download_mode": result.get("download_mode", ""),
                "provider_family": result.get(
                    "provider_family", legacy.get("provider_family", "")
                ),
                "provider_recovered_fields": result.get("selected_fields", {}),
            }
        )
        return pdf_path

    def __call__(self, candidate):
        from extraction_pipeline import ExtractionOutcome

        legacy = candidate.to_legacy()
        canonical_id = candidate.identity.document_id
        compatibility_key = candidate.compatibility_history_key
        if (
            canonical_id in self.seen_identities
            or compatibility_key in self.seen_history_keys
        ):
            return self.terminal(candidate, "CURRENT_RUN_DUPLICATE_SKIP", status="duplicate")
        if canonical_id in self.working_history or compatibility_key in self.working_history:
            return self.terminal(candidate, "HISTORY_DUPLICATE_SKIP", status="duplicate")
        self.seen_identities.add(canonical_id)
        self.seen_history_keys.add(compatibility_key)
        self.working_history.add(canonical_id)
        self.working_history.add(compatibility_key)

        action = str(legacy.get("candidate_action") or "")
        if action == "retain_only":
            return self.terminal(
                candidate, legacy.get("prefilter_reason_code") or "P0_B_RETENTION"
            )
        if action == "manual_review":
            return self.terminal(
                candidate,
                legacy.get("prefilter_reason_code") or "P0_C_MANUAL_REVIEW",
                status="manual_review",
            )
        if action == "skip":
            return self.terminal(candidate, "PREFILTER_SKIP", status="duplicate")

        pdf_path = candidate.source_path
        if candidate.identity.source_kind == "url":
            recovery = self._recover_url(candidate, legacy)
            if isinstance(recovery, ExtractionOutcome):
                return recovery
            pdf_path = recovery

        probe = self.extractor.probe_local_only(pdf_path, document_context=legacy)
        if probe.status == "resolved":
            return ExtractionOutcome.resolved(
                candidate,
                {
                    "pdf_path": pdf_path,
                    "metadata": legacy,
                    "info_json": probe.result,
                    "extraction_trace": {
                        "engine": probe.engine,
                        "reason_code": probe.reason_code,
                    },
                    "extraction_timing": {},
                },
            )
        if probe.status != "needs_remote":
            return self.terminal(
                candidate, probe.reason_code or "LOCAL_PREFLIGHT_FAILED"
            )
        base64_img = self.extractor.pdf_to_base64_image(pdf_path)
        if not base64_img:
            return self.terminal(candidate, "PDF_TO_IMAGE_FAILED")
        with self.sidecar_lock:
            self.sidecar[canonical_id] = {
                "pdf_path": pdf_path,
                "metadata": legacy,
                "base64_img": base64_img,
            }
        return None
