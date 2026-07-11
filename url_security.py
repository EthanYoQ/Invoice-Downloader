import ipaddress
import socket
from dataclasses import dataclass
from typing import Callable, Sequence
from urllib.parse import urljoin, urlsplit, urlunsplit


Resolver = Callable[[str, int], Sequence[str]]
PeerGetter = Callable[[object], str]


def _default_resolver(host: str, port: int) -> Sequence[str]:
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [item[4][0] for item in results]


def _default_peer_getter(response: object) -> str:
    raw = getattr(response, "raw", None)
    candidates = (
        getattr(getattr(raw, "_connection", None), "sock", None),
        getattr(getattr(raw, "connection", None), "sock", None),
        getattr(
            getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None),
            "_sock",
            None,
        ),
    )
    for sock in candidates:
        if sock is not None:
            peer = sock.getpeername()
            return str(peer[0] if isinstance(peer, tuple) else peer)
    raise ValueError("connected peer address is unavailable")


def _safe_url(url: str) -> str:
    try:
        parsed = urlsplit(str(url or ""))
        scheme = parsed.scheme.lower()
        host = parsed.hostname or ""
        if not scheme or not host:
            return "<invalid-url>"
        host_text = f"[{host}]" if ":" in host else host
        try:
            port = parsed.port
        except ValueError:
            port = None
        if port is not None:
            host_text = f"{host_text}:{port}"
        return urlunsplit((scheme, host_text, parsed.path or "/", "", ""))
    except Exception:
        return "<invalid-url>"


class PublicUrlPolicyError(ValueError):
    def __init__(self, url: str, reason: str):
        self.safe_url = _safe_url(url)
        self.reason = reason
        super().__init__(f"URL_POLICY_REJECTED: {reason}; url={self.safe_url}")


@dataclass(frozen=True)
class ValidatedPublicUrl:
    url: str
    host: str
    port: int
    resolved_addresses: tuple[str, ...]


class PublicUrlPolicy:
    _DEFAULT_PORTS = {"http": 80, "https": 443}

    def __init__(
        self,
        resolver: Resolver | None = None,
        peer_getter: PeerGetter | None = None,
    ):
        self._resolver = resolver or _default_resolver
        self._peer_getter = peer_getter or _default_peer_getter

    @staticmethod
    def sanitize(url: str) -> str:
        return _safe_url(url)

    @staticmethod
    def _public_address(value: str, url: str) -> str:
        try:
            address = ipaddress.ip_address(str(value).split("%", 1)[0])
        except ValueError as exc:
            raise PublicUrlPolicyError(url, "DNS returned an invalid address") from exc
        mapped = getattr(address, "ipv4_mapped", None)
        rejected = (
            not address.is_global
            or address.is_multicast
            or address.is_unspecified
            or address.is_reserved
            or address.is_loopback
            or address.is_link_local
            or address.is_private
            or (mapped is not None and not mapped.is_global)
        )
        if rejected:
            raise PublicUrlPolicyError(url, "destination is not globally routable")
        return address.compressed

    def validate(self, url: str) -> ValidatedPublicUrl:
        raw_url = str(url or "").strip()
        try:
            parsed = urlsplit(raw_url)
        except ValueError as exc:
            raise PublicUrlPolicyError(raw_url, "URL could not be parsed") from exc

        scheme = parsed.scheme.lower()
        if scheme not in self._DEFAULT_PORTS:
            raise PublicUrlPolicyError(raw_url, "scheme must be http or https")
        if parsed.username is not None or parsed.password is not None:
            raise PublicUrlPolicyError(raw_url, "credentials are not allowed")

        host = parsed.hostname
        if not host:
            raise PublicUrlPolicyError(raw_url, "hostname is required")
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise PublicUrlPolicyError(raw_url, "hostname is invalid") from exc
        if host == "localhost" or host.endswith(".localhost"):
            raise PublicUrlPolicyError(raw_url, "localhost is not allowed")

        try:
            port = parsed.port or self._DEFAULT_PORTS[scheme]
        except ValueError as exc:
            raise PublicUrlPolicyError(raw_url, "port is invalid") from exc
        if port != self._DEFAULT_PORTS[scheme]:
            raise PublicUrlPolicyError(raw_url, "port is not allowed")

        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            try:
                raw_addresses = self._resolver(host, port)
            except Exception as exc:
                raise PublicUrlPolicyError(raw_url, "hostname resolution failed") from exc
        else:
            raw_addresses = [literal.compressed]

        resolved = tuple(
            dict.fromkeys(self._public_address(address, raw_url) for address in raw_addresses)
        )
        if not resolved:
            raise PublicUrlPolicyError(raw_url, "hostname did not resolve")

        display_host = f"[{host}]" if ":" in host else host
        explicit_port = f":{port}" if parsed.port is not None else ""
        normalized = urlunsplit(
            (scheme, f"{display_host}{explicit_port}", parsed.path or "/", parsed.query, "")
        )
        return ValidatedPublicUrl(normalized, host, port, resolved)

    def resolve_redirect(
        self,
        current: ValidatedPublicUrl,
        location: str,
    ) -> ValidatedPublicUrl:
        return self.validate(urljoin(current.url, str(location or "")))

    def verify_response_peer(
        self,
        response: object,
        validated: ValidatedPublicUrl,
    ) -> str:
        try:
            peer_value = self._peer_getter(response)
        except PublicUrlPolicyError:
            raise
        except Exception as exc:
            raise PublicUrlPolicyError(
                validated.url, "connected peer could not be verified"
            ) from exc
        return self.verify_peer_address(peer_value, validated)

    def verify_peer_address(
        self,
        peer_address: str,
        validated: ValidatedPublicUrl,
    ) -> str:
        peer = self._public_address(peer_address, validated.url)
        if peer not in validated.resolved_addresses:
            raise PublicUrlPolicyError(
                validated.url, "connected peer did not match validated DNS results"
            )
        return peer
