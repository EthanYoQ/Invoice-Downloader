import ipaddress
import json
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import requests
import urllib3
from requests.cookies import extract_cookies_to_jar
from requests.structures import CaseInsensitiveDict

from url_security import ValidatedPublicUrl


def _is_public_unicast(value: str) -> bool:
    address = ipaddress.ip_address(value)
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


def _ip_authority(address: str, port: int, include_port: bool) -> str:
    host = f"[{address}]" if ":" in address else address
    return f"{host}:{port}" if include_port else host


@dataclass(frozen=True)
class PinnedRequestPlan:
    original_url: str
    transport_url: str
    selected_ip: str
    host_header: str
    server_hostname: str
    assert_hostname: str
    proxy_url: str | None
    proxy_connect_authority: str | None


@dataclass(frozen=True)
class PinnedHttpResponse:
    url: str
    content: bytes
    headers: CaseInsensitiveDict
    status_code: int
    request: requests.PreparedRequest
    set_cookie_headers: tuple[str, ...] = ()

    @property
    def text(self):
        encoding = requests.utils.get_encoding_from_headers(self.headers) or "utf-8"
        return self.content.decode(encoding, errors="replace")

    def json(self):
        return json.loads(self.text)

    def close(self):
        return None


class PinnedHttpTransport:
    def __init__(
        self,
        pool_manager_factory=None,
        proxy_manager_factory=None,
    ):
        self._pool_manager_factory = pool_manager_factory or urllib3.PoolManager
        self._proxy_manager_factory = proxy_manager_factory or urllib3.ProxyManager
        self._managers = {}
        self._lock = threading.Lock()

    @staticmethod
    def build_plan(
        target: ValidatedPublicUrl,
        selected_ip: str | None = None,
        original_url: str | None = None,
    ) -> PinnedRequestPlan:
        address = ipaddress.ip_address(
            selected_ip or (target.public_addresses[0] if target.public_addresses else "")
        ).compressed
        if address not in target.public_addresses or not _is_public_unicast(address):
            raise ValueError("selected transport address is not publicly attested")

        original = str(original_url or target.url)
        parsed = urlsplit(original)
        default_port = {"http": 80, "https": 443}[parsed.scheme.lower()]
        transport_authority = _ip_authority(
            address,
            target.port,
            target.port != default_port,
        )
        transport_url = urlunsplit(
            (
                parsed.scheme.lower(),
                transport_authority,
                parsed.path or "/",
                parsed.query,
                "",
            )
        )
        original_host = f"[{target.host}]" if ":" in target.host else target.host
        host_header = (
            f"{original_host}:{target.port}"
            if parsed.port is not None
            else original_host
        )

        proxy_url = None
        proxy_connect_authority = None
        if target.proxy_endpoint is not None:
            proxy_address = target.proxy_endpoint.addresses[0]
            proxy_authority = _ip_authority(
                proxy_address,
                target.proxy_endpoint.port,
                True,
            )
            proxy_url = f"{target.proxy_endpoint.scheme}://{proxy_authority}"
            proxy_connect_authority = _ip_authority(address, target.port, True)

        return PinnedRequestPlan(
            original_url=original,
            transport_url=transport_url,
            selected_ip=address,
            host_header=host_header,
            server_hostname=target.host,
            assert_hostname=target.host,
            proxy_url=proxy_url,
            proxy_connect_authority=proxy_connect_authority,
        )

    def _manager_for(self, target, plan):
        key = (
            urlsplit(plan.original_url).scheme.lower(),
            target.host,
            target.port,
            plan.selected_ip,
            plan.proxy_url,
        )
        with self._lock:
            manager = self._managers.get(key)
            if manager is not None:
                return manager

            kwargs = {"num_pools": 4}
            if urlsplit(plan.original_url).scheme.lower() == "https":
                kwargs.update(
                    {
                        "cert_reqs": "CERT_REQUIRED",
                        "ca_certs": requests.certs.where(),
                        "assert_hostname": plan.assert_hostname,
                        "server_hostname": plan.server_hostname,
                    }
                )
            if plan.proxy_url is None:
                manager = self._pool_manager_factory(**kwargs)
            else:
                proxy = target.proxy_endpoint
                if proxy.scheme == "https":
                    kwargs["proxy_assert_hostname"] = proxy.host
                manager = self._proxy_manager_factory(
                    plan.proxy_url,
                    use_forwarding_for_https=False,
                    **kwargs,
                )
            self._managers[key] = manager
            return manager

    @staticmethod
    def _prepare_request(
        session,
        method,
        target,
        headers=None,
        body=None,
        data=None,
        json_body=None,
        files=None,
        params=None,
    ):
        request_data = body if body is not None else data
        request = requests.Request(
            method=str(method or "GET").upper(),
            url=target.url,
            headers=dict(headers or {}),
            data=request_data,
            json=json_body,
            files=files,
            params=params,
        )
        return session.prepare_request(request)

    def request(
        self,
        session,
        method,
        target,
        headers=None,
        body=None,
        data=None,
        json=None,
        files=None,
        params=None,
        timeout=20,
        suppress_auth=False,
        decode_content=True,
    ):
        prepared = self._prepare_request(
            session,
            method,
            target,
            headers=headers,
            body=body,
            data=data,
            json_body=json,
            files=files,
            params=params,
        )
        if suppress_auth:
            prepared.headers.pop("Authorization", None)
        plan = self.build_plan(target, original_url=prepared.url)
        manager = self._manager_for(target, plan)
        wire_headers = CaseInsensitiveDict(prepared.headers)
        wire_headers["Host"] = plan.host_header

        raw = None
        try:
            raw = manager.request(
                prepared.method,
                plan.transport_url,
                body=prepared.body,
                headers=dict(wire_headers),
                redirect=False,
                retries=False,
                preload_content=True,
                decode_content=decode_content,
                timeout=timeout,
            )
            response_headers = CaseInsensitiveDict(raw.headers.items())
            set_cookie_headers = tuple(raw.headers.getlist("Set-Cookie"))
            content = bytes(raw.data or b"")
            status = int(raw.status)
            if decode_content and "Content-Encoding" in response_headers:
                response_headers.pop("Content-Encoding", None)
                response_headers.pop("Content-Length", None)

            extract_cookies_to_jar(session.cookies, prepared, raw)

            return PinnedHttpResponse(
                url=target.url,
                content=content,
                headers=response_headers,
                status_code=status,
                request=prepared,
                set_cookie_headers=set_cookie_headers,
            )
        finally:
            if raw is not None:
                try:
                    raw.close()
                finally:
                    raw.release_conn()
