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
        self.close_count = 0

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def close(self):
        self.close_count += 1


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
        GlmRuntimeClosedError,
        LimiterClosedError,
        ModelProfile,
        default_profiles,
    )

    return {
        "DEFAULT_PROFILES": DEFAULT_PROFILES,
        "AdaptiveConcurrencyLimiter": AdaptiveConcurrencyLimiter,
        "GlmRequestError": GlmRequestError,
        "GlmRuntime": GlmRuntime,
        "GlmRuntimeClosedError": GlmRuntimeClosedError,
        "LimiterClosedError": LimiterClosedError,
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


@pytest.mark.parametrize("configured", [3, 16, 10**400])
def test_unverified_local_limits_cannot_exceed_production_default(configured):
    default_profiles = _runtime_symbols()["default_profiles"]
    profiles = default_profiles(
        {
            "glm_profile_limits": {"text": configured, "ocr": configured},
            "glm_model_candidates": {
                "text": ["glm-4.6v-flashx"],
                "vision_quality": ["glm-4.6v"],
            },
        }
    )

    assert profiles["text"].max_concurrency == 2
    assert profiles["ocr"].max_concurrency == 2
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


def test_limiter_fifo_survives_reduction_restoration_and_new_arrivals():
    limiter = _runtime_symbols()["AdaptiveConcurrencyLimiter"](2, restore_after_successes=1)
    assert hasattr(limiter, "waiting_count")
    limiter.acquire()
    limiter.acquire()
    entered = []
    entered_events = {name: threading.Event() for name in "ABCDE"}
    release_events = {name: threading.Event() for name in "ABCDE"}

    def worker(name):
        limiter.acquire()
        try:
            entered.append(name)
            entered_events[name].set()
            assert release_events[name].wait(2)
        finally:
            limiter.release()

    threads = {}
    for expected_waiters, name in enumerate("ABC", 1):
        threads[name] = threading.Thread(target=worker, args=(name,))
        threads[name].start()
        deadline = time.monotonic() + 1
        while limiter.waiting_count < expected_waiters and time.monotonic() < deadline:
            time.sleep(0.002)
        assert limiter.waiting_count == expected_waiters

    limiter.record_limit(429)
    limiter.release()
    assert not entered_events["A"].wait(0.03)
    threads["D"] = threading.Thread(target=worker, args=("D",))
    threads["D"].start()
    deadline = time.monotonic() + 1
    while limiter.waiting_count < 4 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert limiter.waiting_count == 4
    limiter.release()
    assert entered_events["A"].wait(1)

    limiter.record_success()
    assert entered_events["B"].wait(1)
    threads["E"] = threading.Thread(target=worker, args=("E",))
    threads["E"].start()
    deadline = time.monotonic() + 1
    while limiter.waiting_count < 3 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert limiter.waiting_count == 3

    release_events["A"].set()
    assert entered_events["C"].wait(1)
    release_events["B"].set()
    assert entered_events["D"].wait(1)
    release_events["C"].set()
    assert entered_events["E"].wait(1)
    release_events["D"].set()
    release_events["E"].set()
    for thread in threads.values():
        thread.join(1)
        assert not thread.is_alive()
    assert entered == list("ABCDE")
    assert limiter.active == 0
    assert limiter.waiting_count == 0


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
    assert delays == [pytest.approx(2.125)]


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
    assert delays == [2.5, 2.5]
    assert all(0 < delay <= 10 for delay in delays)


def test_legacy_retry_floor_is_two_seconds_with_zero_jitter():
    symbols = _runtime_symbols()
    session = FakeSession(
        [FakeResponse(status_code=500), FakeResponse(status_code=500), FakeResponse(payload={"value": "ok"})]
    )
    delays = []
    runtime = symbols["GlmRuntime"](
        "test-key",
        session=session,
        max_attempts=3,
        sleep=delays.append,
        random_source=lambda: 0.0,
    )

    assert runtime.request(
        "text", {}, lambda body: body["value"], timeout_seconds=45
    ) == "ok"
    assert delays == [2.0, 2.0]
    assert [call[1]["timeout"] for call in session.calls] == [45.0, 45.0, 45.0]


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


def test_every_http_response_is_closed_to_release_adapter_pool_connection():
    symbols = _runtime_symbols()
    failed_response = FakeResponse(status_code=500)
    success_response = FakeResponse(payload={"value": 1})
    failed_runtime = symbols["GlmRuntime"](
        "test-key", session=FakeSession([failed_response]), max_attempts=1
    )
    success_runtime = symbols["GlmRuntime"](
        "test-key", session=FakeSession([success_response]), max_attempts=1
    )

    with pytest.raises(symbols["GlmRequestError"]):
        failed_runtime.request("text", {}, lambda body: body)
    assert success_runtime.request("text", {}, lambda body: body["value"]) == 1

    assert failed_response.close_count == 1
    assert success_response.close_count == 1


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


@pytest.mark.parametrize("timeout_seconds", [False, True, 0, -1, float("nan"), float("inf"), 601, 10**400, "15"])
def test_request_timeout_override_rejects_unsafe_values_before_transport(timeout_seconds):
    symbols = _runtime_symbols()
    session = FakeSession([])
    runtime = symbols["GlmRuntime"]("test-key", session=session)

    with pytest.raises((TypeError, ValueError)):
        runtime.request("text", {}, lambda body: body, timeout_seconds=timeout_seconds)

    assert session.calls == []


def test_request_timeout_override_reaches_transport_exactly():
    symbols = _runtime_symbols()
    session = FakeSession([FakeResponse(payload={"value": 1})])
    runtime = symbols["GlmRuntime"]("test-key", session=session, max_attempts=1)

    assert runtime.request(
        "text", {}, lambda body: body["value"], timeout_seconds=45
    ) == 1
    assert session.calls[0][1]["timeout"] == 45.0


class PoolOnlySession(requests.Session):
    def __init__(self, adapter):
        super().__init__()
        self.close_count = 0
        self.mount("https://", adapter)

    def request(self, *args, **kwargs):
        raise AssertionError("mutable Session.request path must not be used")

    def close(self):
        self.close_count += 1
        super().close()


class ConcurrentAdapter(requests.adapters.HTTPAdapter):
    def __init__(self):
        super().__init__()
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.entered = threading.Barrier(2)
        self.release = threading.Event()

    def send(self, request, **kwargs):
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.entered.wait(2)
            assert self.release.wait(2)
            response = requests.Response()
            response.status_code = 200
            response._content = b'{"value":"ok"}'
            response.headers = {}
            response.request = request
            return response
        finally:
            with self.lock:
                self.active -= 1


def test_production_session_uses_adapter_pool_for_concurrent_immutable_requests():
    symbols = _runtime_symbols()
    adapter = ConcurrentAdapter()
    session = PoolOnlySession(adapter)
    session.headers["X-Session-Marker"] = "unchanged"
    session.cookies.set("session-cookie", "unchanged")
    headers_before = dict(session.headers)
    cookies_before = session.cookies.get_dict()
    runtime = symbols["GlmRuntime"]("test-key", session=session, max_attempts=1)
    results = []

    def call_runtime():
        try:
            results.append(runtime.request("text", {}, lambda body: body["value"]))
        except Exception as exc:
            results.append(type(exc).__name__)

    threads = [threading.Thread(target=call_runtime) for _ in range(2)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 2
    while adapter.max_active < 2 and time.monotonic() < deadline:
        time.sleep(0.002)
    adapter.release.set()
    for thread in threads:
        thread.join(2)
        assert not thread.is_alive()

    assert results == ["ok", "ok"]
    assert adapter.max_active == 2
    assert dict(session.headers) == headers_before
    assert session.cookies.get_dict() == cookies_before


def test_runtime_close_rejects_waiters_waits_for_active_and_closes_once():
    symbols = _runtime_symbols()
    assert hasattr(symbols["GlmRuntime"], "close")
    ModelProfile = symbols["ModelProfile"]

    class BlockingSession:
        def __init__(self):
            self.entered = threading.Event()
            self.release = threading.Event()
            self.close_count = 0

        def post(self, *_args, **_kwargs):
            self.entered.set()
            assert self.release.wait(2)
            return FakeResponse(payload={"value": "ok"})

        def close(self):
            self.close_count += 1

    session = BlockingSession()
    runtime = symbols["GlmRuntime"](
        "test-key",
        session=session,
        profiles={"text": ModelProfile("glm-4-flash", "https://example.test", 1, 60)},
        max_attempts=1,
    )
    results = []

    def request_once(label):
        try:
            value = runtime.request("text", {}, lambda body: body["value"])
            results.append((label, value))
        except Exception as exc:
            results.append((label, type(exc).__name__))

    first = threading.Thread(target=request_once, args=("first",))
    second = threading.Thread(target=request_once, args=("second",))
    first.start()
    assert session.entered.wait(1)
    second.start()
    deadline = time.monotonic() + 1
    while runtime.limiters["text"].waiting_count < 1 and time.monotonic() < deadline:
        time.sleep(0.002)
    assert runtime.limiters["text"].waiting_count == 1

    closer = threading.Thread(target=runtime.close)
    second_closer = threading.Thread(target=runtime.close)
    closer.start()
    second_closer.start()
    second.join(1)
    assert not second.is_alive()
    assert ("second", "GlmRuntimeClosedError") in results
    assert closer.is_alive()
    with pytest.raises(symbols["GlmRuntimeClosedError"]):
        runtime.request("text", {}, lambda body: body)
    session.release.set()
    first.join(1)
    closer.join(1)
    second_closer.join(1)
    assert not first.is_alive() and not closer.is_alive() and not second_closer.is_alive()
    assert ("first", "ok") in results
    runtime.close()
    assert session.close_count == 1
    assert runtime.limiters["text"].active == 0
    assert runtime.limiters["text"].waiting_count == 0
    with pytest.raises(symbols["GlmRuntimeClosedError"]):
        runtime.request("text", {}, lambda body: body)


def test_runtime_context_manager_closes_exactly_once():
    symbols = _runtime_symbols()

    class ClosableSession(FakeSession):
        def __init__(self):
            super().__init__([])
            self.close_count = 0

        def close(self):
            self.close_count += 1

    session = ClosableSession()
    with symbols["GlmRuntime"]("test-key", session=session) as runtime:
        assert runtime is not None
    runtime.close()
    assert session.close_count == 1


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
        self.close_count = 0

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

    def close(self):
        self.close_count += 1


def test_extractor_routes_track_a_through_runtime_with_original_attempt_counts(tmp_path):
    source = tmp_path / "invoice.png"
    source.write_bytes(b"x" * 1001)
    runtime = StubRuntime()
    extractor = InvoiceExtractor("local-key", str(tmp_path / "output"), glm_runtime=runtime)

    result = extractor.extract_info_via_llm(["image"], pdf_path=str(source))

    assert result["InvoiceNumber"] == "12345678"
    assert [item[0] for item in runtime.calls] == ["ocr", "text"]
    assert [item[2]["attempts"] for item in runtime.calls] == [2, 3]
    assert [item[2]["timeout_seconds"] for item in runtime.calls] == [90, 45]
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
    assert [item[2]["timeout_seconds"] for item in runtime.calls] == [90, 60]
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

        def close(self):
            captured["close_count"] = captured.get("close_count", 0) + 1

    monkeypatch.setattr(app_api, "GlmRuntime", Runtime)
    api = InvoiceAppAPI()
    monkeypatch.setattr(api._settings_store, "load", lambda: {"glm_profile_limits": {"text": 3}})

    result = api.test_connection("", "", "valid-local-key")

    assert result == {"success": True, "message": "连接成功 - 智谱 GLM 服务已就绪"}
    assert captured["request"][0] == "text"
    assert captured["request"][2]["attempts"] == 1
    assert captured["request"][2]["timeout_seconds"] == 15
    assert captured["close_count"] == 1


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

        def close(self):
            pass

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

        def close(self):
            pass

    monkeypatch.setattr(app_api, "GlmRuntime", Runtime)
    result = InvoiceAppAPI().test_connection("", "", "valid-local-key")

    assert result == {"success": False, "message": "网络或API未知异常: GLM 请求失败"}
    assert secret not in repr(result)


def test_invoice_extractor_close_and_context_close_runtime_once(tmp_path):
    runtime = StubRuntime()
    extractor = InvoiceExtractor("local-key", str(tmp_path / "one"), glm_runtime=runtime)
    extractor.close()
    extractor.close()
    assert runtime.close_count == 1

    second_runtime = StubRuntime()
    with InvoiceExtractor(
        "local-key", str(tmp_path / "two"), glm_runtime=second_runtime
    ) as managed:
        assert managed.glm_runtime is second_runtime
    assert second_runtime.close_count == 1
