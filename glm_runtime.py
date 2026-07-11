from __future__ import annotations

import copy
import hashlib
import logging
import math
import random
import threading
import time
from collections import deque
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Callable, Mapping

import requests


LAYOUT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/layout_parsing"
CHAT_ENDPOINT = "https://open.bigmodel.cn/api/paas/v4/chat/completions"
CONCURRENCY_LIMIT_CODES = frozenset({429, 1302})
TRANSIENT_OVERLOAD_CODES = frozenset({1305, 1312})
SUCCESS_BUSINESS_CODES = frozenset({0, 200})
MAX_REQUEST_TIMEOUT_SECONDS = 600.0


def _validate_timeout_seconds(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError("timeout_seconds must be a real number")
    try:
        normalized = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("timeout_seconds is unsafe") from exc
    if (
        not math.isfinite(normalized)
        or normalized <= 0
        or normalized > MAX_REQUEST_TIMEOUT_SECONDS
    ):
        raise ValueError("timeout_seconds is outside the safe range")
    return normalized


@dataclass(frozen=True)
class ModelProfile:
    name: str
    endpoint: str
    max_concurrency: int = 2
    timeout_seconds: float = 60
    fallback_name: str | None = None

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("model name is required")
        if not isinstance(self.endpoint, str) or not self.endpoint.startswith("https://"):
            raise ValueError("HTTPS model endpoint is required")
        if isinstance(self.max_concurrency, bool) or not isinstance(self.max_concurrency, int):
            raise TypeError("max_concurrency must be an integer")
        if self.max_concurrency < 1:
            raise ValueError("max_concurrency must be positive")
        _validate_timeout_seconds(self.timeout_seconds)


DEFAULT_PROFILES = MappingProxyType(
    {
        "ocr": ModelProfile("glm-ocr", LAYOUT_ENDPOINT, 2, 90, "vision_quality"),
        "text": ModelProfile("glm-4-flash", CHAT_ENDPOINT, 2, 60, "vision_quality"),
        "vision_quality": ModelProfile("glm-4.5v", CHAT_ENDPOINT, 2, 120, None),
    }
)


def default_profiles(settings: Mapping | None = None) -> Mapping[str, ModelProfile]:
    """Return immutable production profiles with optional local concurrency ceilings.

    Candidate model names are intentionally not applied here. A candidate can only
    become a production default through a separately reviewed calibration change.
    """

    configured = dict(DEFAULT_PROFILES)
    limits = (settings or {}).get("glm_profile_limits")
    if isinstance(limits, Mapping):
        for alias, value in limits.items():
            if alias not in configured:
                continue
            ceiling = _bounded_int(
                value,
                configured[alias].max_concurrency,
                minimum=1,
                maximum=configured[alias].max_concurrency,
            )
            configured[alias] = replace(configured[alias], max_concurrency=ceiling)
    return MappingProxyType(configured)


def _bounded_int(value, default, *, minimum, maximum):
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return min(maximum, max(minimum, parsed))


class AdaptiveConcurrencyLimiter:
    def __init__(self, configured_ceiling=2, *, restore_after_successes=8):
        if isinstance(configured_ceiling, bool) or not isinstance(configured_ceiling, int):
            raise TypeError("configured_ceiling must be an integer")
        if configured_ceiling < 1:
            raise ValueError("configured_ceiling must be positive")
        if isinstance(restore_after_successes, bool) or not isinstance(restore_after_successes, int):
            raise TypeError("restore_after_successes must be an integer")
        if restore_after_successes < 1:
            raise ValueError("restore_after_successes must be positive")
        self.configured_ceiling = configured_ceiling
        self.restore_after_successes = restore_after_successes
        self._current_limit = configured_ceiling
        self._active = 0
        self._successes_since_limit = 0
        self._condition = threading.Condition()
        self._waiters = deque()
        self._closed = False

    @property
    def current_limit(self):
        with self._condition:
            return self._current_limit

    @property
    def active(self):
        with self._condition:
            return self._active

    @property
    def waiting_count(self):
        with self._condition:
            return len(self._waiters)

    def acquire(self):
        token = object()
        acquired = False
        with self._condition:
            if self._closed:
                raise LimiterClosedError("concurrency limiter is closed")
            self._waiters.append(token)
            try:
                while True:
                    if self._closed:
                        raise LimiterClosedError("concurrency limiter is closed")
                    if self._waiters[0] is token and self._active < self._current_limit:
                        self._waiters.popleft()
                        self._active += 1
                        acquired = True
                        self._condition.notify_all()
                        return
                    self._condition.wait()
            finally:
                if not acquired:
                    try:
                        self._waiters.remove(token)
                    except ValueError:
                        pass
                    self._condition.notify_all()

    def release(self):
        with self._condition:
            if self._active <= 0:
                raise RuntimeError("limiter release without acquire")
            self._active -= 1
            self._condition.notify_all()

    def record_limit(self, error_code):
        if _normalize_code(error_code) not in CONCURRENCY_LIMIT_CODES:
            return False
        with self._condition:
            self._current_limit = 1
            self._successes_since_limit = 0
            self._condition.notify_all()
        return True

    def record_success(self):
        with self._condition:
            if self._closed:
                return
            if self._current_limit >= self.configured_ceiling:
                self._successes_since_limit = 0
                return
            self._successes_since_limit += 1
            if self._successes_since_limit < self.restore_after_successes:
                return
            self._current_limit = min(self.configured_ceiling, self._current_limit + 1)
            self._successes_since_limit = 0
            self._condition.notify_all()

    def close(self):
        with self._condition:
            if self._closed:
                return
            self._closed = True
            self._condition.notify_all()


class LimiterClosedError(RuntimeError):
    pass


class GlmRuntimeClosedError(RuntimeError):
    pass


class GlmRequestError(RuntimeError):
    """A deliberately data-poor GLM failure safe for diagnostics and UI mapping."""

    def __init__(self, profile, *, http_status=None, business_code=None, reason="request_failed"):
        self.profile = str(profile)
        self.http_status = _normalize_code(http_status)
        self.business_code = _normalize_code(business_code)
        self.reason = str(reason or "request_failed")
        fields = [f"profile={self.profile}", f"reason={self.reason}"]
        if self.http_status is not None:
            fields.append(f"http_status={self.http_status}")
        if self.business_code is not None:
            fields.append(f"business_code={self.business_code}")
        super().__init__("GLM request failed; " + "; ".join(fields))


@dataclass(frozen=True)
class _AttemptResult:
    parsed: object = None
    error: GlmRequestError | None = None
    retryable: bool = False
    http_status: int | None = None
    business_code: int | None = None


class GlmRuntime:
    def __init__(
        self,
        api_key,
        *,
        profiles: Mapping[str, ModelProfile] | None = None,
        settings: Mapping | None = None,
        session=None,
        max_attempts=3,
        restore_after_successes=8,
        sleep: Callable[[float], None] = time.sleep,
        random_source: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.perf_counter,
        diagnostic_callback: Callable[[dict], None] | None = None,
    ):
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int):
            raise TypeError("max_attempts must be an integer")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self._api_key = str(api_key or "")
        self.profiles = MappingProxyType(dict(profiles or default_profiles(settings)))
        self.session = session if session is not None else requests.Session()
        self.max_attempts = max_attempts
        self.sleep = sleep
        self.random_source = random_source
        self.clock = clock
        self.diagnostic_callback = diagnostic_callback
        self.limiters = {
            alias: AdaptiveConcurrencyLimiter(
                profile.max_concurrency,
                restore_after_successes=restore_after_successes,
            )
            for alias, profile in self.profiles.items()
        }
        self.last_trace = {}
        self._trace_lock = threading.Lock()
        self._state_condition = threading.Condition()
        self._closing = False
        self._closed = False
        self._active_requests = 0

    def request(
        self,
        profile_name,
        payload,
        parser,
        *,
        attempts=None,
        timeout_seconds=None,
    ):
        if profile_name not in self.profiles:
            raise KeyError(f"unknown GLM profile: {profile_name}")
        if not callable(parser):
            raise TypeError("parser must be callable")
        attempt_limit = self.max_attempts if attempts is None else attempts
        if isinstance(attempt_limit, bool) or not isinstance(attempt_limit, int) or attempt_limit < 1:
            raise ValueError("attempts must be a positive integer")

        profile = self.profiles[profile_name]
        request_timeout = (
            float(profile.timeout_seconds)
            if timeout_seconds is None
            else _validate_timeout_seconds(timeout_seconds)
        )
        request_payload = copy.deepcopy(dict(payload or {}))
        request_payload["model"] = profile.name
        self._enter_request()
        try:
            last_error = None
            for attempt in range(1, attempt_limit + 1):
                if attempt > 1:
                    self._raise_if_closing()
                started = self.clock()
                result = self._request_once(
                    profile_name,
                    profile,
                    request_payload,
                    parser,
                    request_timeout,
                )
                outcome = "success" if result.error is None else result.error.reason
                self._record_trace(
                    profile_name,
                    profile,
                    attempt,
                    result.http_status,
                    result.business_code,
                    outcome,
                    started,
                )
                if result.error is None:
                    self.limiters[profile_name].record_success()
                    return result.parsed
                last_error = result.error
                if not result.retryable or attempt >= attempt_limit:
                    raise last_error
                self.sleep(self.retry_delay(attempt))
            raise last_error or GlmRequestError(profile_name)
        finally:
            self._leave_request()

    def _request_once(self, profile_name, profile, payload, parser, timeout_seconds):
        limiter = self.limiters[profile_name]
        try:
            limiter.acquire()
        except LimiterClosedError as exc:
            raise GlmRuntimeClosedError("GLM runtime is closing") from exc
        response = None
        try:
            try:
                response = self._send_immutable_request(
                    profile.endpoint,
                    payload,
                    timeout_seconds,
                )
            except requests.Timeout:
                return self._failure(profile_name, reason="timeout", retryable=True)
            except requests.ConnectionError:
                return self._failure(profile_name, reason="connection_error", retryable=True)
            except Exception:
                return self._failure(profile_name, reason="transport_error", retryable=True)

            http_status = _normalize_code(getattr(response, "status_code", None))
            if http_status == 429:
                limiter.record_limit(http_status)
                return self._failure(
                    profile_name,
                    http_status=http_status,
                    reason="rate_limited",
                    retryable=True,
                )
            if http_status is None or not 200 <= http_status < 300:
                return self._failure(
                    profile_name,
                    http_status=http_status,
                    reason="http_error",
                    retryable=_is_retryable_http(profile_name, http_status),
                )
            try:
                body = response.json()
            except Exception:
                return self._failure(
                    profile_name,
                    http_status=http_status,
                    reason="invalid_json",
                    retryable=True,
                )
            business_code = _extract_business_code(body)

            if business_code in CONCURRENCY_LIMIT_CODES:
                limiter.record_limit(business_code or http_status)
                return self._failure(
                    profile_name,
                    http_status=http_status,
                    business_code=business_code,
                    reason="rate_limited",
                    retryable=True,
                )
            if business_code in TRANSIENT_OVERLOAD_CODES:
                return self._failure(
                    profile_name,
                    http_status=http_status,
                    business_code=business_code,
                    reason="overloaded",
                    retryable=True,
                )
            if business_code is not None and business_code not in SUCCESS_BUSINESS_CODES:
                return self._failure(
                    profile_name,
                    http_status=http_status,
                    business_code=business_code,
                    reason="business_error",
                    retryable=False,
                )
            try:
                parsed = parser(body)
            except Exception:
                return self._failure(
                    profile_name,
                    http_status=http_status,
                    business_code=business_code,
                    reason="parser_error",
                    retryable=True,
                )
            return _AttemptResult(
                parsed=parsed,
                http_status=http_status,
                business_code=business_code,
            )
        finally:
            try:
                if response is not None:
                    try:
                        close_response = getattr(response, "close", None)
                        if callable(close_response):
                            close_response()
                    except Exception as exc:
                        self._record_response_close_failure(exc)
            finally:
                limiter.release()

    @staticmethod
    def _failure(
        profile_name,
        *,
        http_status=None,
        business_code=None,
        reason,
        retryable,
    ):
        return _AttemptResult(
            error=GlmRequestError(
                profile_name,
                http_status=http_status,
                business_code=business_code,
                reason=reason,
            ),
            retryable=retryable,
            http_status=_normalize_code(http_status),
            business_code=_normalize_code(business_code),
        )

    def retry_delay(self, attempt):
        base = min(8.0, max(2.0, 2.0 ** max(0, int(attempt) - 1)))
        try:
            random_value = float(self.random_source())
        except (TypeError, ValueError, OverflowError):
            random_value = 0.0
        jitter = min(0.5, max(0.0, random_value * 0.5))
        return min(10.0, base + jitter)

    def _send_immutable_request(self, endpoint, payload, timeout_seconds):
        """Send through the sole Session's adapter pool without mutable Session.request state."""
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        if isinstance(self.session, requests.Session):
            prepared = requests.Request(
                "POST",
                endpoint,
                headers=headers,
                json=payload,
            ).prepare()
            environment = self.session.merge_environment_settings(
                prepared.url,
                {},
                None,
                None,
                None,
            )
            adapter = self.session.get_adapter(prepared.url)
            return adapter.send(prepared, timeout=timeout_seconds, **environment)
        return self.session.post(
            endpoint,
            headers=headers,
            json=payload,
            timeout=timeout_seconds,
        )

    def _enter_request(self):
        with self._state_condition:
            if self._closing or self._closed:
                raise GlmRuntimeClosedError("GLM runtime is closing")
            self._active_requests += 1

    @property
    def active_requests(self):
        with self._state_condition:
            return self._active_requests

    def _leave_request(self):
        with self._state_condition:
            if self._active_requests <= 0:
                raise RuntimeError("runtime request accounting underflow")
            self._active_requests -= 1
            self._state_condition.notify_all()

    def _raise_if_closing(self):
        with self._state_condition:
            if self._closing or self._closed:
                raise GlmRuntimeClosedError("GLM runtime is closing")

    def close(self):
        with self._state_condition:
            if self._closed:
                return
            if self._closing:
                while not self._closed:
                    self._state_condition.wait()
                return
            self._closing = True
            for limiter in self.limiters.values():
                limiter.close()
            while self._active_requests:
                self._state_condition.wait()
        try:
            self.session.close()
        except Exception:
            logging.debug("GLM session close failed", exc_info=False)
        finally:
            with self._state_condition:
                self._closed = True
                self._state_condition.notify_all()

    def __enter__(self):
        self._raise_if_closing()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        del exc_type, exc_value, traceback
        self.close()
        return False

    def _record_trace(
        self,
        profile_name,
        profile,
        attempt,
        http_status,
        business_code,
        outcome,
        started,
    ):
        try:
            elapsed_ms = round(max(0.0, self.clock() - started) * 1000.0, 1)
        except Exception:
            elapsed_ms = 0.0
        trace = {
            "profile": profile_name,
            "model": profile.name,
            "attempt": attempt,
            "http_status": http_status,
            "business_code": business_code,
            "outcome": outcome,
            "elapsed_ms": elapsed_ms,
            "current_limit": self.limiters[profile_name].current_limit,
        }
        with self._trace_lock:
            self.last_trace = trace
        if self.diagnostic_callback is not None:
            try:
                self.diagnostic_callback(dict(trace))
            except Exception:
                logging.debug("GLM diagnostic callback failed", exc_info=False)

    def _record_response_close_failure(self, exc):
        exception_class = type(exc)
        exception_type = exception_class.__name__
        type_identity = f"{exception_class.__module__}.{exception_class.__qualname__}"
        fingerprint = hashlib.sha256(type_identity.encode("utf-8", errors="replace")).hexdigest()[:16]
        diagnostic = {
            "event": "response_close_failed",
            "exception_type": exception_type,
            "exception_fingerprint": fingerprint,
        }
        if self.diagnostic_callback is not None:
            try:
                self.diagnostic_callback(dict(diagnostic))
            except Exception:
                logging.debug("GLM diagnostic callback failed", exc_info=False)
        logging.debug(
            "GLM response close failed; exception_type=%s; exception_fingerprint=%s",
            exception_type,
            fingerprint,
        )


def _normalize_code(value):
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _extract_business_code(body):
    if not isinstance(body, Mapping):
        return None
    candidates = [body.get("code")]
    error = body.get("error")
    if isinstance(error, Mapping):
        candidates.append(error.get("code"))
    for candidate in candidates:
        normalized = _normalize_code(candidate)
        if normalized is not None:
            return normalized
    return None


def _is_retryable_http(profile_name, http_status):
    if http_status is None:
        return True
    if http_status in {401, 402, 403}:
        return False
    if profile_name == "ocr" and http_status in {400, 404}:
        return False
    return http_status >= 400
