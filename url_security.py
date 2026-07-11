import ipaddress
import json
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import Request, getproxies, proxy_bypass, urlopen


Resolver = Callable[[str, int], Sequence[str]]
PeerGetter = Callable[[object], object]
_AUTO_PROXY = object()


def _default_resolver(host: str, port: int) -> Sequence[str]:
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [item[4][0] for item in results]


def _default_peer_getter(response: object) -> tuple[str, int]:
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
            if isinstance(peer, tuple) and len(peer) >= 2:
                return str(peer[0]), int(peer[1])
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
        return urlunsplit((scheme, host_text, "/<redacted>", "", ""))
    except Exception:
        return "<invalid-url>"


class PublicUrlPolicyError(ValueError):
    def __init__(self, url: str, reason: str):
        self.safe_url = _safe_url(url)
        self.reason = reason
        super().__init__(f"URL_POLICY_REJECTED: {reason}; url={self.safe_url}")


def _parse_address(value: str, url: str):
    try:
        return ipaddress.ip_address(str(value).split("%", 1)[0])
    except ValueError as exc:
        raise PublicUrlPolicyError(url, "DNS returned an invalid address") from exc


def _is_public_unicast(address) -> bool:
    mapped = getattr(address, "ipv4_mapped", None)
    return not (
        not address.is_global
        or address.is_multicast
        or address.is_unspecified
        or address.is_reserved
        or address.is_loopback
        or address.is_link_local
        or address.is_private
        or (mapped is not None and not mapped.is_global)
    )


@dataclass(frozen=True)
class ProxyEndpoint:
    host: str
    port: int
    addresses: tuple[str, ...]

    def matches(self, address: str, port: int | None) -> bool:
        return port == self.port and address in self.addresses


@dataclass(frozen=True)
class ValidatedPublicUrl:
    url: str
    host: str
    port: int
    resolved_addresses: tuple[str, ...]
    public_addresses: tuple[str, ...]
    proxy_endpoint: ProxyEndpoint | None = None

    @property
    def transport_mode(self) -> str:
        return "proxy" if self.proxy_endpoint is not None else "direct"


class CachedDohResolver:
    _cache = {}
    _lock = threading.Lock()

    def __init__(self, timeout_seconds=1.5, cache_seconds=300):
        self.timeout_seconds = max(0.2, min(float(timeout_seconds), 2.0))
        self.cache_seconds = max(30, int(cache_seconds))
        self.endpoints = (
            "https://cloudflare-dns.com/dns-query?name={name}&type={type}",
            "https://dns.google/resolve?name={name}&type={type}",
        )

    def __call__(self, host: str, port: int) -> Sequence[str]:
        cache_key = host.lower()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(cache_key)
            if cached and cached[0] > now:
                return cached[1]

        last_error = None
        for endpoint in self.endpoints:
            addresses = []
            endpoint_worked = False
            for record_type in ("A", "AAAA"):
                request = Request(
                    endpoint.format(name=quote(host, safe=""), type=record_type),
                    headers={"Accept": "application/dns-json"},
                )
                try:
                    with urlopen(request, timeout=self.timeout_seconds) as response:
                        payload = json.loads(response.read().decode("utf-8"))
                    endpoint_worked = int(payload.get("Status", -1)) == 0
                    for answer in payload.get("Answer", []) or []:
                        if int(answer.get("type", 0)) in {1, 28}:
                            addresses.append(str(answer.get("data", "")))
                except Exception as exc:
                    last_error = exc
            if endpoint_worked and addresses:
                result = tuple(dict.fromkeys(addresses))
                with self._lock:
                    self._cache[cache_key] = (now + self.cache_seconds, result)
                return result
        raise RuntimeError("public DNS attestation unavailable") from last_error


def _proxy_spec_mapping(proxy_endpoint):
    if proxy_endpoint is _AUTO_PROXY:
        detected = getproxies()
        return {
            scheme: detected.get(scheme)
            for scheme in ("http", "https")
            if detected.get(scheme)
        }
    if proxy_endpoint is None:
        return {}
    if isinstance(proxy_endpoint, Mapping):
        return dict(proxy_endpoint)
    return {"http": proxy_endpoint, "https": proxy_endpoint}


def _parse_proxy_spec(spec, resolver: Resolver) -> ProxyEndpoint | None:
    if spec is None:
        return None
    if isinstance(spec, ProxyEndpoint):
        return spec
    if isinstance(spec, (tuple, list)) and len(spec) == 2:
        host, port = str(spec[0]), int(spec[1])
    else:
        value = str(spec).strip()
        if not value:
            return None
        parsed = urlsplit(value if "://" in value else f"http://{value}")
        host = parsed.hostname or ""
        port = parsed.port or 80
    if not host or not (1 <= port <= 65535):
        return None
    try:
        literal = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        addresses = resolver(host, port)
    else:
        addresses = [literal.compressed]
    canonical = tuple(
        dict.fromkeys(ipaddress.ip_address(str(value).split("%", 1)[0]).compressed for value in addresses)
    )
    if not canonical:
        return None
    return ProxyEndpoint(host.lower(), port, canonical)


class PublicUrlPolicy:
    _DEFAULT_PORTS = {"http": 80, "https": 443}

    def __init__(
        self,
        resolver: Resolver | None = None,
        public_resolver: Resolver | None = None,
        peer_getter: PeerGetter | None = None,
        proxy_endpoint=_AUTO_PROXY,
        proxy_resolver: Resolver | None = None,
        proxy_bypass_checker=None,
    ):
        self._resolver = resolver or _default_resolver
        self._public_resolver = public_resolver or CachedDohResolver()
        self._peer_getter = peer_getter or _default_peer_getter
        self._proxy_bypass_checker = proxy_bypass_checker or proxy_bypass
        endpoint_resolver = proxy_resolver or _default_resolver
        self._proxy_endpoints = {
            scheme: endpoint
            for scheme, spec in _proxy_spec_mapping(proxy_endpoint).items()
            if (endpoint := _parse_proxy_spec(spec, endpoint_resolver)) is not None
        }

    @staticmethod
    def sanitize(url: str) -> str:
        return _safe_url(url)

    @staticmethod
    def _canonical_addresses(values, url, require_public):
        addresses = []
        for value in values:
            address = _parse_address(value, url)
            if require_public and not _is_public_unicast(address):
                raise PublicUrlPolicyError(
                    url, "public DNS attestation returned a non-public address"
                )
            addresses.append(address.compressed)
        result = tuple(dict.fromkeys(addresses))
        if not result:
            raise PublicUrlPolicyError(url, "hostname did not resolve")
        return result

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

        proxy = self._proxy_endpoints.get(scheme)
        if proxy is not None:
            try:
                bypassed = bool(self._proxy_bypass_checker(host))
            except Exception:
                bypassed = True
            if bypassed:
                proxy = None
        try:
            literal = ipaddress.ip_address(host.split("%", 1)[0])
        except ValueError:
            literal = None

        if literal is not None:
            if not _is_public_unicast(literal):
                raise PublicUrlPolicyError(
                    raw_url, "literal destination is not globally routable"
                )
            resolved = (literal.compressed,)
            public_addresses = resolved
        else:
            try:
                resolved = self._canonical_addresses(
                    self._resolver(host, port), raw_url, require_public=False
                )
            except Exception as exc:
                if proxy is None:
                    if isinstance(exc, PublicUrlPolicyError):
                        raise
                    raise PublicUrlPolicyError(
                        raw_url, "hostname resolution failed"
                    ) from exc
                resolved = ()

            if proxy is None:
                for value in resolved:
                    if not _is_public_unicast(ipaddress.ip_address(value)):
                        raise PublicUrlPolicyError(
                            raw_url, "destination is not globally routable"
                        )
                public_addresses = resolved
            else:
                try:
                    public_addresses = self._canonical_addresses(
                        self._public_resolver(host, port),
                        raw_url,
                        require_public=True,
                    )
                except Exception as exc:
                    if isinstance(exc, PublicUrlPolicyError):
                        raise
                    raise PublicUrlPolicyError(
                        raw_url, "public DNS attestation unavailable"
                    ) from exc

        display_host = f"[{host}]" if ":" in host else host
        explicit_port = f":{port}" if parsed.port is not None else ""
        normalized = urlunsplit(
            (scheme, f"{display_host}{explicit_port}", parsed.path or "/", parsed.query, "")
        )
        return ValidatedPublicUrl(
            normalized,
            host,
            port,
            resolved,
            public_addresses,
            proxy,
        )

    def resolve_redirect(
        self,
        current: ValidatedPublicUrl,
        location: str,
    ) -> ValidatedPublicUrl:
        return self.validate(urljoin(current.url, str(location or "")))

    @staticmethod
    def _split_peer(peer) -> tuple[str, int | None]:
        if isinstance(peer, Mapping):
            address = peer.get("ipAddress") or peer.get("address") or ""
            port = peer.get("port")
        elif isinstance(peer, (tuple, list)):
            address = peer[0] if peer else ""
            port = peer[1] if len(peer) > 1 else None
        else:
            address = peer or ""
            port = None
        return str(address), int(port) if port is not None else None

    def verify_response_peer(
        self,
        response: object,
        validated: ValidatedPublicUrl,
    ) -> str:
        try:
            peer = self._peer_getter(response)
        except Exception as exc:
            raise PublicUrlPolicyError(
                validated.url, "connected peer could not be verified"
            ) from exc
        return self.verify_peer_address(peer, validated)

    def verify_peer_address(self, peer, validated: ValidatedPublicUrl) -> str:
        peer_value, peer_port = self._split_peer(peer)
        address = _parse_address(peer_value, validated.url)
        canonical = address.compressed
        if validated.proxy_endpoint is not None:
            if not validated.public_addresses:
                raise PublicUrlPolicyError(
                    validated.url, "public DNS attestation is missing"
                )
            if not validated.proxy_endpoint.matches(canonical, peer_port):
                raise PublicUrlPolicyError(
                    validated.url, "connected peer is not the configured proxy endpoint"
                )
            return canonical
        if not _is_public_unicast(address):
            raise PublicUrlPolicyError(
                validated.url, "direct connected peer is not globally routable"
            )
        return canonical

    def verify_browser_peer(self, peer, validated: ValidatedPublicUrl):
        if peer is None:
            if validated.proxy_endpoint is not None and validated.public_addresses:
                return None
            raise PublicUrlPolicyError(
                validated.url, "browser connected peer could not be verified"
            )
        return self.verify_peer_address(peer, validated)
