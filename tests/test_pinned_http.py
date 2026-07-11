from email.message import Message

import pytest
import requests
import urllib3
from urllib3._collections import HTTPHeaderDict

from pinned_http import PinnedHttpTransport
from url_security import PublicUrlPolicy


PUBLIC_A = "93.184.216.34"
PUBLIC_B = "142.250.72.14"


def direct_target(url="https://invoice.example/document?value=synthetic"):
    policy = PublicUrlPolicy(
        resolver=lambda host, port: [PUBLIC_A, PUBLIC_B],
        proxy_endpoint=None,
    )
    return policy.validate(url)


def proxy_target(url="https://invoice.example/document?value=synthetic"):
    policy = PublicUrlPolicy(
        resolver=lambda host, port: ["198.18.0.42"],
        public_resolver=lambda host, port: [PUBLIC_A, PUBLIC_B],
        proxy_endpoint=("127.0.0.1", 7897),
        proxy_bypass_checker=lambda host: False,
    )
    return policy.validate(url)


class FakeOriginalResponse:
    def __init__(self, set_cookie=None):
        self.msg = Message()
        if set_cookie:
            self.msg.add_header("Set-Cookie", set_cookie)


class FakeRawResponse:
    def __init__(self, status=200, data=b"body", headers=None, set_cookie=None):
        self.status = status
        self.data = data
        self.headers = HTTPHeaderDict(headers or {})
        self._original_response = FakeOriginalResponse(set_cookie)
        self.closed = 0
        self.released = 0
        self.read_calls = 0
        self._offset = 0

    def read(self, amount=None, decode_content=True):
        del decode_content
        self.read_calls += 1
        if self._offset >= len(self.data):
            return b""
        end = len(self.data) if amount is None else self._offset + amount
        chunk = self.data[self._offset:end]
        self._offset += len(chunk)
        return chunk

    def close(self):
        self.closed += 1

    def release_conn(self):
        self.released += 1


class FakeManager:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.response


def test_direct_plan_pins_wire_url_but_preserves_original_host_and_tls_name():
    target = direct_target()

    plan = PinnedHttpTransport.build_plan(target, PUBLIC_A)

    assert plan.original_url == target.url
    assert plan.transport_url == "https://93.184.216.34/document?value=synthetic"
    assert plan.host_header == "invoice.example"
    assert plan.server_hostname == "invoice.example"
    assert plan.assert_hostname == "invoice.example"
    assert plan.proxy_url is None


def test_proxy_plan_connects_proxy_to_public_ip_not_attacker_controlled_hostname():
    target = proxy_target()

    plan = PinnedHttpTransport.build_plan(target, PUBLIC_A)

    assert plan.transport_url == "https://93.184.216.34/document?value=synthetic"
    assert plan.proxy_url == "http://127.0.0.1:7897"
    assert plan.proxy_connect_authority == "93.184.216.34:443"
    assert "invoice.example" not in plan.transport_url


def test_transport_prepares_original_identity_and_uses_pinned_ip_for_wire_request():
    raw = FakeRawResponse(
        headers={"content-type": "application/pdf", "X-Mixed-Case": "yes"},
        set_cookie="provider_session=abc; Path=/; Secure",
    )
    manager = FakeManager(raw)
    manager_calls = []

    def pool_factory(**kwargs):
        manager_calls.append(kwargs)
        return manager

    transport = PinnedHttpTransport(pool_manager_factory=pool_factory)
    session = requests.Session()
    session.cookies.set("mail_cookie", "cookie-value", domain="invoice.example")
    target = direct_target()

    response = transport.request(
        session,
        "GET",
        target,
        headers={"X-Original": "yes"},
        timeout=3,
    )

    assert len(manager.calls) == 1
    assert manager_calls[0]["assert_hostname"] == "invoice.example"
    assert manager_calls[0]["server_hostname"] == "invoice.example"
    method, wire_url, kwargs = manager.calls[0]
    assert method == "GET"
    assert wire_url.startswith("https://93.184.216.34/")
    assert kwargs["headers"]["Host"] == "invoice.example"
    assert "mail_cookie=cookie-value" in kwargs["headers"]["Cookie"]
    assert response.url == target.url
    assert response.request.url == target.url
    assert response.headers["Content-Type"] == "application/pdf"
    assert response.headers["x-mixed-case"] == "yes"
    assert session.cookies.get("provider_session", domain="invoice.example") == "abc"
    assert raw.closed == 1
    assert raw.released == 1


def test_transport_preserves_duplicate_set_cookie_headers_for_browser_fulfillment():
    raw = FakeRawResponse()
    raw.headers.add("Set-Cookie", "a=1; Path=/; Secure")
    raw.headers.add("Set-Cookie", "b=2; Path=/; Secure")
    manager = FakeManager(raw)
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)

    response = transport.request(requests.Session(), "GET", direct_target())

    assert response.set_cookie_headers == (
        "a=1; Path=/; Secure",
        "b=2; Path=/; Secure",
    )


def test_proxy_transport_factory_receives_exact_proxy_and_pinned_connect_target():
    raw = FakeRawResponse()
    manager = FakeManager(raw)
    proxy_calls = []

    def proxy_factory(proxy_url, **kwargs):
        proxy_calls.append((proxy_url, kwargs))
        return manager

    transport = PinnedHttpTransport(proxy_manager_factory=proxy_factory)
    target = proxy_target()

    transport.request(requests.Session(), "GET", target, timeout=3)

    assert proxy_calls[0][0] == "http://127.0.0.1:7897"
    assert proxy_calls[0][1]["assert_hostname"] == "invoice.example"
    assert proxy_calls[0][1]["server_hostname"] == "invoice.example"
    _, wire_url, kwargs = manager.calls[0]
    assert wire_url.startswith("https://93.184.216.34/")
    assert kwargs["headers"]["Host"] == "invoice.example"


def test_transport_reuses_origin_ip_pool_and_closes_each_raw_response():
    responses = [FakeRawResponse(data=b"one"), FakeRawResponse(data=b"two")]
    factory_calls = []

    class SequencedManager(FakeManager):
        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            return responses.pop(0)

    manager = SequencedManager(None)

    def pool_factory(**kwargs):
        factory_calls.append(kwargs)
        return manager

    transport = PinnedHttpTransport(pool_manager_factory=pool_factory)
    target = direct_target()
    first_raw = responses[0]
    second_raw = responses[1]

    first = transport.request(requests.Session(), "GET", target)
    second = transport.request(requests.Session(), "GET", target)

    assert first.content == b"one"
    assert second.content == b"two"
    assert len(factory_calls) == 1
    assert first_raw.closed == first_raw.released == 1
    assert second_raw.closed == second_raw.released == 1


def test_transport_can_suppress_session_auth_after_cross_origin_redirect():
    raw = FakeRawResponse()
    manager = FakeManager(raw)
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)
    session = requests.Session()
    session.auth = ("synthetic-user", "synthetic-password")

    transport.request(
        session,
        "GET",
        direct_target(),
        suppress_auth=True,
    )

    wire_headers = manager.calls[0][2]["headers"]
    assert "Authorization" not in wire_headers


def test_transport_rejects_selected_address_not_in_validated_public_set():
    target = direct_target()

    with pytest.raises(ValueError):
        PinnedHttpTransport.build_plan(target, "10.0.0.9")


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_idempotent_request_fails_over_to_next_attested_ip_on_connect_failure(method):
    successful_raw = FakeRawResponse(data=b"second-ip")

    class FailFirstIpManager:
        def __init__(self):
            self.calls = []

        def request(self, request_method, url, **kwargs):
            self.calls.append((request_method, url, kwargs))
            if PUBLIC_A in url:
                raise urllib3.exceptions.ConnectTimeoutError(
                    None, "SYNTHETIC_CONNECT_DETAIL"
                )
            return successful_raw

    manager = FailFirstIpManager()
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)

    response = transport.request(
        requests.Session(),
        method,
        direct_target(),
        max_response_bytes=1024,
    )

    assert response.content == b"second-ip"
    assert [url for _, url, _ in manager.calls] == [
        "https://93.184.216.34/document?value=synthetic",
        "https://142.250.72.14/document?value=synthetic",
    ]
    assert successful_raw.closed == successful_raw.released == 1


def test_proxy_get_failover_pins_each_connect_target_with_original_host_and_tls_name():
    successful_raw = FakeRawResponse(data=b"proxy-second-ip")

    class FailFirstProxyManager:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            if PUBLIC_A in url:
                raise urllib3.exceptions.ProxyError(
                    "proxy connect failed", OSError("synthetic proxy failure")
                )
            return successful_raw

    manager = FailFirstProxyManager()
    proxy_factories = []

    def proxy_factory(proxy_url, **kwargs):
        proxy_factories.append((proxy_url, kwargs))
        return manager

    transport = PinnedHttpTransport(proxy_manager_factory=proxy_factory)

    response = transport.request(
        requests.Session(), "GET", proxy_target(), max_response_bytes=1024
    )

    assert response.content == b"proxy-second-ip"
    assert [url for _, url, _ in manager.calls] == [
        "https://93.184.216.34/document?value=synthetic",
        "https://142.250.72.14/document?value=synthetic",
    ]
    assert all(call[2]["headers"]["Host"] == "invoice.example" for call in manager.calls)
    assert len(proxy_factories) == 2
    assert all(proxy_url == "http://127.0.0.1:7897" for proxy_url, _ in proxy_factories)
    assert all(options["assert_hostname"] == "invoice.example" for _, options in proxy_factories)
    assert all(options["server_hostname"] == "invoice.example" for _, options in proxy_factories)


def test_post_connect_failure_is_not_replayed_to_second_ip_and_error_is_sanitized():
    class AlwaysFailManager:
        def __init__(self):
            self.calls = []

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            raise urllib3.exceptions.ConnectTimeoutError(
                None, "SYNTHETIC_CONNECT_DETAIL"
            )

    manager = AlwaysFailManager()
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)

    with pytest.raises(Exception) as caught:
        transport.request(
            requests.Session(),
            "POST",
            direct_target(),
            data=b"non-idempotent",
            max_response_bytes=1024,
        )

    assert len(manager.calls) == 1
    assert PUBLIC_A in manager.calls[0][1]
    assert PUBLIC_B not in manager.calls[0][1]
    assert "SYNTHETIC_CONNECT_DETAIL" not in str(caught.value)
    assert "document" not in str(caught.value)


def test_http_error_status_does_not_trigger_ip_failover():
    raw = FakeRawResponse(status=503, data=b"provider unavailable")
    manager = FakeManager(raw)
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)

    response = transport.request(
        requests.Session(), "GET", direct_target(), max_response_bytes=1024
    )

    assert response.status_code == 503
    assert len(manager.calls) == 1
    assert PUBLIC_A in manager.calls[0][1]


def test_content_length_over_limit_rejects_before_body_read_and_releases_connection():
    raw = FakeRawResponse(
        data=b"oversized",
        headers={"Content-Length": "9"},
    )
    manager = FakeManager(raw)
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)

    with pytest.raises(Exception, match="response exceeds configured size limit"):
        transport.request(
            requests.Session(), "GET", direct_target(), max_response_bytes=8
        )

    assert raw.read_calls == 0
    assert raw.closed == raw.released == 1


def test_unknown_length_body_rejects_when_streamed_bytes_cross_limit_and_closes():
    raw = FakeRawResponse(data=b"123456789")
    manager = FakeManager(raw)
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)

    with pytest.raises(Exception) as caught:
        transport.request(
            requests.Session(), "GET", direct_target(), max_response_bytes=8
        )

    assert "response exceeds configured size limit" in str(caught.value)
    assert "document" not in str(caught.value)
    assert raw.read_calls >= 1
    assert raw.closed == raw.released == 1


def test_streamed_body_at_limit_is_buffered_and_connection_is_released():
    raw = FakeRawResponse(data=b"12345678", headers={"Content-Length": "8"})
    manager = FakeManager(raw)
    transport = PinnedHttpTransport(pool_manager_factory=lambda **kwargs: manager)

    response = transport.request(
        requests.Session(), "GET", direct_target(), max_response_bytes=8
    )

    assert response.content == b"12345678"
    assert manager.calls[0][2]["preload_content"] is False
    assert raw.read_calls >= 1
    assert raw.closed == raw.released == 1
