from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import os
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from invoice_domain import DocumentIdentity


class _FrozenList(tuple):
    pass


class _FrozenSet(frozenset):
    pass


class _FrozenBytearray(bytes):
    pass


def _freeze_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, set):
        return _FrozenSet(_freeze_value(item) for item in value)
    if isinstance(value, bytearray):
        return _FrozenBytearray(value)
    return value


def _thaw_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, _FrozenList):
        return [_thaw_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_thaw_value(item) for item in value)
    if isinstance(value, _FrozenSet):
        return {_thaw_value(item) for item in value}
    if isinstance(value, _FrozenBytearray):
        return bytearray(value)
    return value


def _freeze_mapping(value: Mapping[str, Any] | None) -> Mapping[str, Any]:
    return _freeze_value(dict(value or {}))


@dataclass(frozen=True)
class DocumentCandidate:
    identity: DocumentIdentity
    sequence: int
    source_path: str = ""
    source_url: str = ""
    channel: str = ""
    source_filename: str = ""
    trace_context: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.sequence < 0:
            raise ValueError("DocumentCandidate.sequence must be non-negative")
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
        return _thaw_value(self.metadata)


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
                            trace_context=source.trace_context,
                            metadata=source.metadata,
                        )
                    )
                continue
            if not isinstance(source, Mapping):
                source = {"candidate_action": "manual_review", "raw_type": type(source).__name__}

            metadata = dict(source)
            source_path = str(metadata.get("filepath") or "")
            source_url = str(metadata.get("source_url") or "")
            filename = os.path.basename(source_path) or str(metadata.get("filename") or "")
            message_uid = str(
                metadata.get("message_uid")
                or metadata.get("source_message_uid")
                or metadata.get("uid")
                or ""
            )
            provider_group_key = str(metadata.get("provider_group_key") or "")
            document_seed = "|".join(
                (
                    source_path,
                    str(metadata.get("subject") or ""),
                    filename,
                    str(metadata.get("tier", 0)),
                    str(sequence),
                )
            )
            document_id = str(metadata.get("document_id") or "") or hashlib.md5(
                document_seed.encode("utf-8")
            ).hexdigest()
            source_locator = source_url or source_path
            source_kind = "url" if metadata.get("is_url") or source_url else "attachment"
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
                "tier": metadata.get("tier", 0),
                "provider_family": metadata.get("provider_family", ""),
            }
            candidates.append(
                DocumentCandidate(
                    identity=identity,
                    sequence=sequence,
                    source_path=source_path,
                    source_url=source_url,
                    channel=str(metadata.get("channel") or self._channel),
                    source_filename=filename,
                    trace_context=trace_context,
                    metadata=metadata,
                )
            )
        return candidates
