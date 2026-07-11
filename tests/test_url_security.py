import pytest

from url_security import PublicUrlPolicy, PublicUrlPolicyError


PUBLIC_V4 = "93.184.216.34"
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def resolver_for(mapping):
    def _resolve(host, port):
        return mapping.get(host, [PUBLIC_V4])

    return _resolve


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
        PublicUrlPolicy().validate(url)


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
    policy = PublicUrlPolicy(resolver=resolver_for({}))

    with pytest.raises(PublicUrlPolicyError):
        policy.validate(url)


def test_rejects_hostname_when_any_dns_result_is_non_public():
    policy = PublicUrlPolicy(
        resolver=resolver_for({"mixed.example": [PUBLIC_V4, "10.0.0.8"]})
    )

    with pytest.raises(PublicUrlPolicyError):
        policy.validate("https://mixed.example/invoice.pdf")


def test_accepts_public_https_hostname_and_freezes_all_addresses():
    policy = PublicUrlPolicy(
        resolver=resolver_for({"invoice.example": [PUBLIC_V4, PUBLIC_V6]})
    )

    validated = policy.validate("HTTPS://INVOICE.EXAMPLE/invoice.pdf?token=secret#page")

    assert validated.url == "https://invoice.example/invoice.pdf?token=secret"
    assert validated.host == "invoice.example"
    assert validated.port == 443
    assert validated.resolved_addresses == (PUBLIC_V4, PUBLIC_V6)


def test_rejects_private_redirect_destination():
    policy = PublicUrlPolicy(resolver=resolver_for({"invoice.example": [PUBLIC_V4]}))
    current = policy.validate("https://invoice.example/start")

    with pytest.raises(PublicUrlPolicyError):
        policy.resolve_redirect(current, "http://127.0.0.1/admin?token=secret")


def test_rejects_connected_peer_outside_validated_dns_results():
    policy = PublicUrlPolicy(
        resolver=resolver_for({"invoice.example": [PUBLIC_V4]}),
        peer_getter=lambda response: "10.0.0.9",
    )
    validated = policy.validate("https://invoice.example/invoice.pdf")

    with pytest.raises(PublicUrlPolicyError):
        policy.verify_response_peer(object(), validated)


def test_policy_error_safe_url_removes_credentials_query_and_fragment():
    policy = PublicUrlPolicy(resolver=resolver_for({}))

    with pytest.raises(PublicUrlPolicyError) as caught:
        policy.validate("https://user:password@public.example/path/invoice.pdf?token=secret#frag")

    rendered = str(caught.value)
    assert "URL_POLICY_REJECTED" in rendered
    assert "https://public.example/path/invoice.pdf" in rendered
    assert "user" not in rendered
    assert "password" not in rendered
    assert "token" not in rendered
    assert "secret" not in rendered
