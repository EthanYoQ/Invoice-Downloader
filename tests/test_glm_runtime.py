import json
import logging
import threading
import time

import pytest
import requests

import app_api
from app_api import InvoiceAppAPI
from invoice_extractor import InvoiceExtractor


class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"ok": True}
        self.text = text

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _runtime_symbols():
    from glm_runtime import (
        DEFAULT_PROFILES,
        AdaptiveConcurrencyLimiter,
        GlmRequestError,
        GlmRuntime,
        ModelProfile,
        default_profiles,
    )

    return {
        "DEFAULT_PROFILES": DEFAULT_PROFILES,
        "AdaptiveConcurrencyLimiter": AdaptiveConcurrencyLimiter,
        "GlmRequestError": GlmRequestError,
        "GlmRuntime": GlmRuntime,
        "ModelProfile": ModelProfile,
        "default_profiles": default_profiles,
    }


def test_default_registry_is_immutable_and_preserves_current_models():
    symbols = _runtime_symbols()
    profiles = symbols["DEFAULT_PROFILES"]
    ModelProfile = symbols["ModelProfile"]

    assert profiles["ocr"] == ModelProfile(
        "glm-ocr",
        "https://open.bigmodel.cn/api/paas/v4/layout_parsing",
        2,
        90,
        "vision_quality",
    )
    assert profiles["text"].name == "glm-4-flash"
    assert profiles["text"].timeout_seconds == 60
    assert profiles["vision_quality"].name == "glm-4.5v"
    assert profiles["vision_quality"].timeout_seconds == 120
    with pytest.raises(TypeError):
        profiles["text"] = profiles["ocr"]
    with pytest.raises(Exception):
        profiles["text"].name = "glm-4.6v-flashx"


def test_local_limits_are_configurable_but_candidate_models_do_not_become_defaults():
    default_profiles = _runtime_symbols()["default_profiles"]
    profiles = default_profiles(
        {
            "glm_profile_limits": {"text": 4, "ocr": 3},
            "glm_model_candidates": {
                "text": ["glm-4.6v-flashx"],
                "vision_quality": ["glm-4.6v"],
            },
        }
    )

    assert profiles["text"].max_concurrency == 4
    assert profiles["ocr"].max_concurrency == 3
    assert profiles["text"].name == "glm-4-flash"
    assert profiles["vision_quality"].name == "glm-4.5v"


def test_limiter_allows_two_entries_and_releases_waiter_without_deadlock():
    limiter = _runtime_symbols()["AdaptiveConcurrencyLimiter"](2)
    entered = []
    release = threading.Event()

    def worker(index):
        limiter.acquire()
        try:
            entered.append(index)
            release.wait(1)
        finally:
            limiter.release()

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(3)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 1
    while len(entered) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    assert len(entered) == 2
    time.sleep(0.02)
    assert len(entered) == 2
    release.set()
    for thread in threads:
        thread.join(1)
        assert not thread.is_alive()
    assert sorted(entered) == [0, 1, 2]
    assert limiter.active == 0


def test_rate_limit_is_profile_local_and_success_restoration_is_cautious():
    symbols = _runtime_symbols()
    runtime = symbols["GlmRuntime"]("test-key", max_attempts=1, restore_after_successes=3)

    assert runtime.limiters["text"].record_limit(1302) is True
    assert runtime.limiters["text"].current_limit == 1
    assert runtime.limiters["ocr"].current_limit == 2
    for _ in range(2):
        runtime.limiters["text"].record_success()
        assert runtime.limiters["text"].current_limit == 1
    runtime.limiters["text"].record_success()
    assert runtime.limiters["text"].current_limit == 2
    for _ in range(20):
        runtime.limiters["text"].record_success()
    assert runtime.limiters["text"].current_limit == 2


@pytest.mark.parametrize("code", [1305, 1312])
def test_overload_codes_retry_without_being_misclassified_as_concurrency(code):
    symbols = _runtime_symbols()
    session = FakeSession([FakeResponse(payload={"code": code}), FakeResponse(payload={"value": 7})])
    delays = []
    runtime = symbols["GlmRuntime"](
        "test-key",
        session=session,
        max_attempts=2,
        sleep=delays.append,
        random_source=lambda: 0.25,
    )

    assert runtime.request("text", {}, lambda body: body["value"]) == 7
    assert runtime.limiters["text"].current_limit == 2
    assert delays == [pytest.approx(1.125)]


def test_http_429_and_1302_reduce_limit_and_use_bounded_backoff():
    symbols = _runtime_symbols()
    session = FakeSession(
        [
            FakeResponse(status_code=429, payload={"error": {"code": 429}}),
            FakeResponse(payload={"code": 1302}),
            FakeResponse(payload={"value": "ok"}),
        ]
    )
    delays = []
    runtime = symbols["GlmRuntime"](
        "test-key",
        session=session,
        max_attempts=3,
        sleep=delays.append,
        random_source=lambda: 1.0,
    )

    assert runtime.request("text", {}, lambda body: body["value"]) == "ok"
    assert runtime.limiters["text"].current_limit == 1
    assert delays == [1.5, 2.5]
    assert all(0 < delay <= 10 for delay in delays)


@pytest.mark.parametrize("status", [401, 402, 403])
def test_auth_quota_and_entitlement_failures_do_not_retry_or_reduce_concurrency(status):
    symbols = _runtime_symbols()
    session = FakeSession([FakeResponse(status_code=status, payload={"message": "secret-body"})])
    runtime = symbols["GlmRuntime"]("test-key", session=session, max_attempts=3, sleep=lambda _: None)

    with pytest.raises(symbols["GlmRequestError"]) as caught:
        runtime.request("text", {"document": "secret-payload"}, lambda body: body)

    assert caught.value.http_status == status
    assert len(session.calls) == 1
    assert runtime.limiters["text"].current_limit == 2
    assert runtime.limiters["text"].active == 0


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404])
def test_nonretryable_http_status_wins_even_when_response_is_not_json(status):
    symbols = _runtime_symbols()
    session = FakeSession([FakeResponse(status_code=status, payload=ValueError("secret body"))])
    runtime = symbols["GlmRuntime"]("test-key", session=session, max_attempts=3, sleep=lambda _: None)

    with pytest.raises(symbols["GlmRequestError"]) as caught:
        runtime.request("ocr", {}, lambda body: body)

    assert caught.value.reason == "http_error"
    assert caught.value.http_status == status
    assert len(session.calls) == 1
    assert runtime.limiters["ocr"].current_limit == 2


@pytest.mark.parametrize("profile_name", ["text", "vision_quality"])
@pytest.mark.parametrize("status", [400, 404])
def test_text_and_vision_keep_legacy_retry_count_before_pipeline_fallback(profile_name, status):
    symbols = _runtime_symbols()
    session = FakeSession([FakeResponse(status_code=status) for _ in range(3)])
    runtime = symbols["GlmRuntime"](
        "test-key", session=session, max_attempts=3, sleep=lambda _: None
    )

    with pytest.raises(symbols["GlmRequestError"]):
        runtime.request(profile_name, {}, lambda body: body)

    assert len(session.calls) == 3
    assert runtime.limiters[profile_name].current_limit == 2


def test_transient_transport_parser_and_5xx_failures_never_leak_permits():
    symbols = _runtime_symbols()
    session = FakeSession(
        [
            requests.ConnectionError("transport secret"),
            FakeResponse(status_code=500, payload={"message": "body secret"}),
            FakeResponse(payload={"value": 3}),
        ]
    )
    runtime = symbols["GlmRuntime"](
        "test-key",
        session=session,
        max_attempts=3,
        sleep=lambda _: None,
        random_source=lambda: 0,
    )

    assert runtime.request("ocr", {}, lambda body: body["value"]) == 3
    assert runtime.limiters["ocr"].active == 0

    parser_runtime = symbols["GlmRuntime"](
        "test-key", session=FakeSession([FakeResponse(payload={})]), max_attempts=1
    )
    with pytest.raises(symbols["GlmRequestError"]):
        parser_runtime.request("text", {}, lambda _body: (_ for _ in ()).throw(ValueError("payload secret")))
    assert parser_runtime.limiters["text"].active == 0


def test_session_is_reused_and_model_endpoint_timeout_are_profile_owned():
    symbols = _runtime_symbols()
    session = FakeSession([FakeResponse(payload={"value": 1}), FakeResponse(payload={"value": 2})])
    runtime = symbols["GlmRuntime"]("test-key", session=session, max_attempts=1)

    assert runtime.request("text", {"model": "wrong"}, lambda body: body["value"]) == 1
    assert runtime.request("text", {}, lambda body: body["value"]) == 2
    assert len(session.calls) == 2
    assert all(call[0].endswith("/chat/completions") for call in session.calls)
    assert all(call[1]["json"]["model"] == "glm-4-flash" for call in session.calls)
    assert all(call[1]["timeout"] == 60 for call in session.calls)


def test_errors_and_telemetry_never_expose_key_authorization_payload_or_response(caplog):
    symbols = _runtime_symbols()
    key = "API-KEY-MUST-NOT-LEAK"
    document = "BASE64-DOCUMENT-MUST-NOT-LEAK"
    response_secret = "RESPONSE-BODY-MUST-NOT-LEAK"
    events = []
    runtime = symbols["GlmRuntime"](
        key,
        session=FakeSession([FakeResponse(status_code=500, payload={"message": response_secret}, text=response_secret)]),
        max_attempts=1,
        diagnostic_callback=events.append,
    )

    with caplog.at_level(logging.DEBUG), pytest.raises(symbols["GlmRequestError"]) as caught:
        runtime.request("vision_quality", {"file": document}, lambda body: body)

    rendered = repr((caught.value, caught.value.__dict__, events, runtime.last_trace, caplog.text))
    for secret in (key, document, response_secret, f"Bearer {key}"):
        assert secret not in rendered
    assert runtime.last_trace["profile"] == "vision_quality"
    assert runtime.last_trace["outcome"] == "http_error"


def _valid_invoice():
    return {
        "is_invoice": True,
        "Date": "20260610",
        "Purchaser": "辉瑞投资有限公司",
        "Seller": "测试商户",
        "Amount": "10.00",
        "InvoiceCode": "",
        "InvoiceNumber": "12345678",
        "Type": "餐饮",
        "category": "餐饮",
        "Departure_Date": "",
        "Departure_City": "",
        "Destination_City": "",
    }


class StubRuntime:
    def __init__(self, failed_profiles=()):
        self.failed_profiles = set(failed_profiles)
        self.calls = []
        self.last_trace = {}

    def request(self, profile_name, payload, parser, **kwargs):
        self.calls.append((profile_name, payload, kwargs))
        if profile_name in self.failed_profiles:
            from glm_runtime import GlmRequestError

            raise GlmRequestError(profile_name, http_status=500, reason="http_error")
        body = (
            {"md_results": "invoice text long enough"}
            if profile_name == "ocr"
            else {"choices": [{"message": {"content": json.dumps(_valid_invoice())}}]}
        )
        self.last_trace = {"profile": profile_name, "outcome": "success"}
        return parser(body)


def test_extractor_routes_track_a_through_runtime_with_original_attempt_counts(tmp_path):
    source = tmp_path / "invoice.png"
    source.write_bytes(b"x" * 1001)
    runtime = StubRuntime()
    extractor = InvoiceExtractor("local-key", str(tmp_path / "output"), glm_runtime=runtime)

    result = extractor.extract_info_via_llm(["image"], pdf_path=str(source))

    assert result["InvoiceNumber"] == "12345678"
    assert [item[0] for item in runtime.calls] == ["ocr", "text"]
    assert [item[2]["attempts"] for item in runtime.calls] == [2, 3]
    assert extractor.last_extraction_trace["engine"] == "track_a"


def test_extractor_preserves_track_b_fallback_through_runtime(tmp_path):
    source = tmp_path / "invoice.png"
    source.write_bytes(b"x" * 1001)
    runtime = StubRuntime({"ocr"})
    extractor = InvoiceExtractor("local-key", str(tmp_path / "output"), glm_runtime=runtime)

    result = extractor.extract_info_via_llm(["image"], pdf_path=str(source))

    assert result["InvoiceNumber"] == "12345678"
    assert [item[0] for item in runtime.calls] == ["ocr", "vision_quality"]
    assert [item[2]["attempts"] for item in runtime.calls] == [2, 3]
    assert extractor.last_extraction_trace["engine"] == "track_b"


def test_extractor_does_not_log_invalid_model_response_content(tmp_path, caplog):
    source = tmp_path / "invoice.png"
    source.write_bytes(b"x" * 1001)
    response_secret = "MODEL-RESPONSE-MUST-NOT-LEAK"

    class Runtime:
        last_trace = {}

        def request(self, profile_name, _payload, parser, **_kwargs):
            if profile_name == "ocr":
                return parser({"md_results": "invoice text long enough"})
            return parser({"choices": [{"message": {"content": response_secret}}]})

    extractor = InvoiceExtractor("local-key", str(tmp_path / "output"), glm_runtime=Runtime())
    with caplog.at_level(logging.WARNING):
        result = extractor.extract_info_via_llm(["image"], pdf_path=str(source))

    assert result["Type"] == "解析失败"
    assert response_secret not in caplog.text


def test_connection_uses_text_runtime_and_keeps_success_shape(monkeypatch):
    captured = {}

    class Runtime:
        def __init__(self, api_key, **kwargs):
            captured["api_key"] = api_key
            captured["settings"] = kwargs.get("settings")

        def request(self, profile_name, payload, parser, **kwargs):
            captured["request"] = (profile_name, payload, kwargs)
            return parser({"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(app_api, "GlmRuntime", Runtime)
    api = InvoiceAppAPI()
    monkeypatch.setattr(api._settings_store, "load", lambda: {"glm_profile_limits": {"text": 3}})

    result = api.test_connection("", "", "valid-local-key")

    assert result == {"success": True, "message": "连接成功 - 智谱 GLM 服务已就绪"}
    assert captured["request"][0] == "text"
    assert captured["request"][2]["attempts"] == 1


@pytest.mark.parametrize(
    ("http_status", "business_code", "reason", "message"),
    [
        (401, None, "http_error", "连接失败 - API Key 鉴权未通过或无效"),
        (402, None, "http_error", "连接失败 - GLM API 额度已耗尽，请充值或更换 API Key"),
        (429, None, "rate_limited", "连接失败 - 触发限流，请求并发过高"),
        (200, 1302, "rate_limited", "连接失败 - 触发限流，请求并发过高"),
        (None, None, "timeout", "API连接失败 - 请求超时，请检查您的网络连接"),
        (None, None, "connection_error", "API连接失败 - 无法连接到智谱 API 服务器"),
    ],
)
def test_connection_maps_sanitized_runtime_failures(
    monkeypatch, http_status, business_code, reason, message
):
    symbols = _runtime_symbols()

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            raise symbols["GlmRequestError"](
                "text", http_status=http_status, business_code=business_code, reason=reason
            )

    monkeypatch.setattr(app_api, "GlmRuntime", Runtime)
    assert InvoiceAppAPI().test_connection("", "", "valid-local-key") == {
        "success": False,
        "message": message,
    }


def test_connection_does_not_echo_unexpected_runtime_exception(monkeypatch):
    secret = "UNEXPECTED-GLM-SECRET"

    class Runtime:
        def __init__(self, *_args, **_kwargs):
            pass

        def request(self, *_args, **_kwargs):
            raise RuntimeError(f"Bearer {secret} response={secret}")

    monkeypatch.setattr(app_api, "GlmRuntime", Runtime)
    result = InvoiceAppAPI().test_connection("", "", "valid-local-key")

    assert result == {"success": False, "message": "网络或API未知异常: GLM 请求失败"}
    assert secret not in repr(result)
