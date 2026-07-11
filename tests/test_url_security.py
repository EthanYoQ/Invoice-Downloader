import json
from urllib.parse import parse_qs, urlsplit

import pytest

from url_security import CachedDohResolver, PublicUrlPolicy, PublicUrlPolicyError


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def resolver_for(mapping):
    def _resolve(host, port):
        return mapping.get(host, [PUBLIC_V4])

    return _resolve


def direct_policy(**kwargs):
    return PublicUrlPolicy(proxy_endpoint=None, **kwargs)


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost/invoice.pdf",
        "http://127.0.0.1/invoice.pdf",
        "http://[::1]/invoice.pdf",
        "http://10.1.2.3/invoice.pdf",
        "http://172.16.2.3/invoice.pdf",
        "http://172.31.255.255/invoice.pdf",
        "http://192.168.1.2/invoice.pdf",
        "http://100.64.1.2/invoice.pdf",
        "http://169.254.1.2/invoice.pdf",
        "http://[fc00::1]/invoice.pdf",
        "http://[fe80::1]/invoice.pdf",
        "http://224.0.0.1/invoice.pdf",
        "http://[ff02::1]/invoice.pdf",
        "http://0.0.0.0/invoice.pdf",
        "http://[::]/invoice.pdf",
        "http://192.0.2.1/invoice.pdf",
        "http://[2001:db8::1]/invoice.pdf",
    ],
)
def test_rejects_non_public_literal_addresses(url):
    with pytest.raises(PublicUrlPolicyError):
        direct_policy().validate(url)


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "ftp://public.example/invoice.pdf",
        "javascript:alert(1)",
        "https://user:password@public.example/invoice.pdf",
        "https://token@public.example/invoice.pdf",
        "https://public.example:444/invoice.pdf",
    ],
)
def test_rejects_non_http_credentials_and_disallowed_ports(url):
    policy = direct_policy(resolver=resolver_for({}))

    with pytest.raises(PublicUrlPolicyError):
        policy.validate(url)


def test_rejects_hostname_when_any_dns_result_is_non_public():
    policy = direct_policy(
        resolver=resolver_for({"mixed.example": [PUBLIC_V4, "10.0.0.8"]})
    )

    with pytest.raises(PublicUrlPolicyError):
        policy.validate("https://mixed.example/invoice.pdf")


def test_accepts_public_https_hostname_and_freezes_all_addresses():
    policy = direct_policy(
        resolver=resolver_for({"invoice.example": [PUBLIC_V4, PUBLIC_V6]})
    )

    validated = policy.validate("HTTPS://INVOICE.EXAMPLE/invoice.pdf?token=secret#page")

    assert validated.url == "https://invoice.example/invoice.pdf?token=secret"
    assert validated.host == "invoice.example"
    assert validated.port == 443
    assert validated.resolved_addresses == (PUBLIC_V4, PUBLIC_V6)


def test_idna_hostname_is_canonicalized_before_resolution_and_sanitization():
    resolved_hosts = []

    def resolver(host, port):
        resolved_hosts.append((host, port))
        return [PUBLIC_V4]

    policy = direct_policy(resolver=resolver)
    validated = policy.validate("https://b\u00fccher.example/invoice?token=secret")

    assert resolved_hosts == [("xn--bcher-kva.example", 443)]
    assert validated.host == "xn--bcher-kva.example"
    assert validated.url == "https://xn--bcher-kva.example/invoice?token=secret"
    assert policy.sanitize(validated.url) == "https://xn--bcher-kva.example/<redacted>"


def test_trailing_dot_hostname_is_normalized_before_policy_and_logging():
    resolved_hosts = []

    def resolver(host, port):
        resolved_hosts.append((host, port))
        return [PUBLIC_V4]

    policy = direct_policy(resolver=resolver)
    validated = policy.validate("https://PUBLIC.EXAMPLE./invoice?token=secret")

    assert resolved_hosts == [("public.example", 443)]
    assert validated.host == "public.example"
    assert validated.url == "https://public.example/invoice?token=secret"
    assert policy.sanitize("https://PUBLIC.EXAMPLE./secret") == (
        "https://public.example/<redacted>"
    )


def test_rejects_private_redirect_destination():
    policy = direct_policy(resolver=resolver_for({"invoice.example": [PUBLIC_V4]}))
    current = policy.validate("https://invoice.example/start")

    with pytest.raises(PublicUrlPolicyError):
        policy.resolve_redirect(current, "http://127.0.0.1/admin?token=secret")


def test_direct_mode_rejects_private_connected_peer():
    policy = direct_policy(
        resolver=resolver_for({"invoice.example": [PUBLIC_V4]}),
        peer_getter=lambda response: ("10.0.0.9", 443),
    )
    validated = policy.validate("https://invoice.example/invoice.pdf")

    with pytest.raises(PublicUrlPolicyError):
        policy.verify_response_peer(object(), validated)


def test_direct_mode_accepts_public_cdn_peer_outside_dns_snapshot():
    policy = direct_policy(
        resolver=resolver_for({"invoice.example": [PUBLIC_V4]}),
        peer_getter=lambda response: ("142.250.72.14", 443),
    )
    validated = policy.validate("https://invoice.example/invoice.pdf")

    assert policy.verify_response_peer(object(), validated) == "142.250.72.14"


def test_fake_ip_succeeds_only_with_explicit_proxy_and_public_attestation():
    policy = PublicUrlPolicy(
        resolver=resolver_for({"invoice.example": ["198.18.0.42"]}),
        public_resolver=resolver_for({"invoice.example": [PUBLIC_V4, PUBLIC_V6]}),
        proxy_endpoint=("127.0.0.1", 7897),
        peer_getter=lambda response: ("127.0.0.1", 7897),
    )

    validated = policy.validate("https://invoice.example/invoice.pdf")

    assert validated.transport_mode == "proxy"
    assert validated.public_addresses == (PUBLIC_V4, PUBLIC_V6)
    assert policy.verify_response_peer(object(), validated) == "127.0.0.1"


def test_fake_ip_without_explicit_proxy_is_rejected():
    policy = direct_policy(
        resolver=resolver_for({"invoice.example": ["198.18.0.42"]}),
        public_resolver=resolver_for({"invoice.example": [PUBLIC_V4]}),
    )

    with pytest.raises(PublicUrlPolicyError):
        policy.validate("https://invoice.example/invoice.pdf")


def test_proxy_mode_fails_closed_when_public_attestation_is_unavailable():
    def unavailable(host, port):
        raise TimeoutError("attestation unavailable")

    policy = PublicUrlPolicy(
        resolver=resolver_for({"invoice.example": ["198.18.0.42"]}),
        public_resolver=unavailable,
        proxy_endpoint=("127.0.0.1", 7897),
    )

    with pytest.raises(PublicUrlPolicyError, match="attestation"):
        policy.validate("https://invoice.example/invoice.pdf")


def test_proxy_bypass_target_uses_direct_rules_and_rejects_fake_ip():
    policy = PublicUrlPolicy(
        resolver=resolver_for({"invoice.example": ["198.18.0.42"]}),
        public_resolver=resolver_for({"invoice.example": [PUBLIC_V4]}),
        proxy_endpoint=("127.0.0.1", 7897),
        proxy_bypass_checker=lambda host: True,
    )

    with pytest.raises(PublicUrlPolicyError):
        policy.validate("https://invoice.example/invoice.pdf")


def test_proxy_mode_rejects_wrong_transport_peer_even_when_loopback():
    policy = PublicUrlPolicy(
        resolver=resolver_for({"invoice.example": ["198.18.0.42"]}),
        public_resolver=resolver_for({"invoice.example": [PUBLIC_V4]}),
        proxy_endpoint=("127.0.0.1", 7897),
        peer_getter=lambda response: ("127.0.0.1", 7898),
    )
    validated = policy.validate("https://invoice.example/invoice.pdf")

    with pytest.raises(PublicUrlPolicyError, match="configured proxy"):
        policy.verify_response_peer(object(), validated)


def test_browser_missing_peer_is_never_accepted_as_transport_proof():
    proxy_policy = PublicUrlPolicy(
        resolver=resolver_for({"invoice.example": ["198.18.0.42"]}),
        public_resolver=resolver_for({"invoice.example": [PUBLIC_V4]}),
        proxy_endpoint=("127.0.0.1", 7897),
    )
    proxy_target = proxy_policy.validate("https://invoice.example/invoice.pdf")
    with pytest.raises(PublicUrlPolicyError):
        proxy_policy.verify_browser_peer(None, proxy_target)

    direct = direct_policy(
        resolver=resolver_for({"invoice.example": [PUBLIC_V4]})
    )
    direct_target = direct.validate("https://invoice.example/invoice.pdf")
    with pytest.raises(PublicUrlPolicyError):
        direct.verify_browser_peer(None, direct_target)


def test_policy_error_safe_url_removes_credentials_query_and_fragment():
    policy = direct_policy(resolver=resolver_for({}))

    with pytest.raises(PublicUrlPolicyError) as caught:
        policy.validate("https://user:password@public.example/path/invoice.pdf?token=secret#frag")

    rendered = str(caught.value)
    assert "URL_POLICY_REJECTED" in rendered
    assert "https://public.example/<redacted>" in rendered
    assert "user" not in rendered
    assert "password" not in rendered
    assert "token" not in rendered
    assert "secret" not in rendered


def test_sanitized_url_never_emits_capability_path_query_or_fragment():
    sanitized = direct_policy().sanitize(
        "https://public.example/private-capability/opaque-value?key=query-value#fragment-value"
    )

    assert sanitized == "https://public.example/<redacted>"
    assert "capability" not in sanitized
    assert "opaque" not in sanitized
    assert "query-value" not in sanitized
    assert "fragment-value" not in sanitized


class FakeDohResponse:
    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


class FakeDohOpener:
    def __init__(self, behavior):
        self.behavior = behavior
        self.calls = []

    def __call__(self, request, timeout):
        parsed = urlsplit(request.full_url)
        record_type = (parse_qs(parsed.query).get("type") or [""])[0]
        key = (parsed.netloc, record_type)
        self.calls.append((key, timeout))
        result = self.behavior[key]
        if isinstance(result, Exception):
            raise result
        answers = [
            {"type": 1 if ":" not in address else 28, "data": address}
            for address in result
        ]
        return FakeDohResponse({"Status": 0, "Answer": answers})


DOH_ENDPOINTS = (
    "https://primary.test/resolve?name={name}&type={type}",
    "https://fallback.test/resolve?name={name}&type={type}",
)


def test_doh_primary_results_are_cached_and_timeouts_are_bounded():
    opener = FakeDohOpener(
        {
            ("primary.test", "A"): [PUBLIC_V4],
            ("primary.test", "AAAA"): [PUBLIC_V6],
        }
    )
    resolver = CachedDohResolver(
        opener=opener,
        endpoints=DOH_ENDPOINTS,
        timeout_seconds=0.5,
        cache_seconds=60,
    )

    first = resolver("invoice.example", 443)
    second = resolver("invoice.example", 443)

    assert first == second == (PUBLIC_V4, PUBLIC_V6)
    assert len(opener.calls) == 2
    assert all(timeout <= 0.5 for _, timeout in opener.calls)


def test_doh_falls_back_to_second_tls_resolver():
    opener = FakeDohOpener(
        {
            ("primary.test", "A"): TimeoutError("primary down"),
            ("primary.test", "AAAA"): TimeoutError("primary down"),
            ("fallback.test", "A"): [PUBLIC_V4],
            ("fallback.test", "AAAA"): [],
        }
    )
    resolver = CachedDohResolver(opener=opener, endpoints=DOH_ENDPOINTS)

    assert resolver("invoice.example", 443) == (PUBLIC_V4,)
    assert [key[0] for key, _ in opener.calls] == [
        "primary.test",
        "primary.test",
        "fallback.test",
        "fallback.test",
    ]


@pytest.mark.parametrize(
    "answers",
    [
        ["10.0.0.8"],
        [PUBLIC_V4, "10.0.0.8"],
    ],
)
def test_doh_rejects_all_private_or_mixed_answers(answers):
    opener = FakeDohOpener(
        {
            ("primary.test", "A"): answers,
            ("primary.test", "AAAA"): [],
        }
    )
    resolver = CachedDohResolver(
        opener=opener,
        endpoints=(DOH_ENDPOINTS[0],),
    )

    with pytest.raises(RuntimeError, match="non-public"):
        resolver("invoice.example", 443)


def test_doh_all_resolvers_unavailable_is_explicit_fail_closed():
    opener = FakeDohOpener(
        {
            ("primary.test", "A"): TimeoutError("down"),
            ("primary.test", "AAAA"): TimeoutError("down"),
            ("fallback.test", "A"): TimeoutError("down"),
            ("fallback.test", "AAAA"): TimeoutError("down"),
        }
    )
    resolver = CachedDohResolver(opener=opener, endpoints=DOH_ENDPOINTS)

    with pytest.raises(RuntimeError, match="all public DNS attestors unavailable"):
        resolver("invoice.example", 443)


def test_https_proxy_without_explicit_port_defaults_to_443():
    policy = PublicUrlPolicy(
        resolver=lambda host, port: ["198.18.0.42"],
        public_resolver=lambda host, port: [PUBLIC_V4],
        proxy_endpoint="https://127.0.0.1",
        proxy_bypass_checker=lambda host: False,
    )

    target = policy.validate("https://invoice.example/document")

    assert target.proxy_endpoint.scheme == "https"
    assert target.proxy_endpoint.port == 443
