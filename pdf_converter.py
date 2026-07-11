import json
import logging
import os
import re
import shutil
from urllib.parse import parse_qs, quote, urljoin, urlparse

import fitz  # PyMuPDF

try:
    import requests
    REQUESTS_IMPORT_ERROR = None
except ImportError as exc:
    requests = None
    REQUESTS_IMPORT_ERROR = exc

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
    PLAYWRIGHT_IMPORT_ERROR = None
except ImportError as exc:
    PlaywrightError = RuntimeError
    PlaywrightTimeoutError = RuntimeError
    sync_playwright = None
    PLAYWRIGHT_IMPORT_ERROR = exc

from provider_baiwang import (
    collect_baiwang_candidate_urls,
    extract_fields_from_pdf_text,
    infer_baiwang_download_kind,
    is_baiwang_family_url,
    looks_like_baiwang_wrapper_text,
    match_baiwang_expected_fields,
    merge_expected_fields,
    parse_baiwang_xml_fields,
)
from provider_direct_invoice import (
    DIRECT_INVOICE_FAMILIES,
    build_direct_invoice_group_key,
    collect_direct_invoice_candidate_urls,
    extract_direct_invoice_fields_from_pdf_text,
    infer_direct_download_kind,
    infer_direct_invoice_family,
    is_direct_invoice_family_url,
    normalize_token as normalize_direct_token,
    parse_direct_invoice_xml_fields,
)
from pinned_http import PinnedHttpTransport
from url_security import PublicUrlPolicy, PublicUrlPolicyError

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")


class PDFConverter:
    DIRECT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
    BROWSER_DOCUMENT_MAX_RESPONSE_BYTES = 64 * 1024 * 1024
    BROWSER_PASSIVE_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
    BROWSER_PASSIVE_RESOURCE_TYPES = frozenset(
        {"script", "stylesheet", "image", "media", "font"}
    )
    BAIWANG_CLICK_TARGETS = {
        "pdf": [
            "a:has-text('下载PDF')",
            "a:has-text('下载 PDF')",
            "button:has-text('下载PDF')",
            "button:has-text('下载 PDF')",
            "text=/下载\\s*PDF/i",
            "text=/download\\s*pdf/i",
            "a.primary",
        ],
        "xml": [
            "a:has-text('下载XML')",
            "a:has-text('下载 XML')",
            "button:has-text('下载XML')",
            "button:has-text('下载 XML')",
            "text=/下载\\s*XML/i",
            "text=/download\\s*xml/i",
        ],
        "ofd": [
            "a:has-text('下载OFD')",
            "a:has-text('下载 OFD')",
            "button:has-text('下载OFD')",
            "button:has-text('下载 OFD')",
            "text=/下载\\s*OFD/i",
            "text=/download\\s*ofd/i",
        ],
    }

    def __init__(
        self,
        staging_dir="staging",
        timeout_ms=30000,
        url_policy=None,
        pinned_transport=None,
    ):
        self.staging_dir = os.path.abspath(staging_dir)
        self.timeout_ms = timeout_ms
        self.generic_timeout_ms = max(8000, min(int(timeout_ms or 0) or 30000, 12000))
        self.provider_settle_timeout_ms = max(1500, min(int(timeout_ms or 0) or 30000, 4000))
        self.url_policy = url_policy or PublicUrlPolicy()
        self.pinned_transport = pinned_transport or PinnedHttpTransport()
        self._browser_request_contexts = {}
        self._browser_effective_request_contexts = {}
        self._browser_fulfilled_requests = set()
        self._browser_http_session = None
        self._browser_page_compromises = {}
        self._browser_pages = {}
        self._browser_page_redirect_targets = {}
        self._browser_page_effective_urls = {}
        self._browser_global_compromise = None
        self._browser_policy_rejections = []

    @staticmethod
    def _elapsed_ms(started_at):
        import time
        return round((time.perf_counter() - started_at) * 1000.0, 1)

    @staticmethod
    def _baiwang_url_priority(url):
        normalized = str(url or "").lower()
        if "previewinvoice" in normalized or "previewinvoiceallele" in normalized or "smkp-vue" in normalized:
            return 0
        if "ad.efapiao.com/api/affair/maillink" in normalized:
            return 1
        if "pis.baiwang.com" in normalized:
            return 2
        if "www.baiwang.com" in normalized or "official_website" in normalized:
            return 9
        return 5

    @staticmethod
    def _pdf_bytes_look_valid(pdf_bytes):
        return bool(pdf_bytes and pdf_bytes.startswith(b"%PDF"))

    @staticmethod
    def _compact_text(value):
        return re.sub(r"\s+", "", str(value or "")).strip().lower()

    @staticmethod
    def _read_body_text(page):
        try:
            return page.locator("body").inner_text(timeout=5000)
        except Exception:
            return ""

    @staticmethod
    def _safe_page_title(page):
        try:
            return page.title() or ""
        except Exception:
            return ""

    @staticmethod
    def _normalized_page_text(*parts):
        return " ".join(str(part or "") for part in parts).strip().lower()

    @staticmethod
    def _has_invoice_keywords(text):
        keywords = (
            "invoice",
            "receipt",
            "fapiao",
            "tax invoice",
            "发票",
            "电子发票",
            "账单",
            "行程单",
            "开票",
            "票据",
        )
        normalized = str(text or "").lower()
        return any(keyword in normalized for keyword in keywords)

    def _classify_generic_page(self, url, title_text, body_text, response_status=None):
        normalized = self._normalized_page_text(url, title_text, body_text)
        normalized_lower = normalized.lower()
        auth_tokens = (
            "登录",
            "login",
            "sign in",
            "sign-in",
            "验证码",
            "captcha",
            "access denied",
            "authenticate",
            "authentication",
            "身份验证",
            "verify",
        )
        non_invoice_tokens = (
            "reward",
            "rewards",
            "会员",
            "积分",
            "portal",
            "account",
            "privacy",
            "terms",
            "unsubscribe",
            "promotion",
            "campaign",
            "support",
            "help center",
            "loyalty",
            "hotel",
            "banking",
            "credit card",
        )
        nuonuo_platform_tokens = (
            "本软件由浙江诺诺网络科技有限公司开发运营",
            "企业税务数智化协同管理平台",
            "诺税通-税务共享平台",
            "全链路票据解决方案",
            "立即体验",
        )
        has_invoice_signal = self._has_invoice_keywords(normalized_lower)
        nuonuo_platform_hits = sum(1 for token in nuonuo_platform_tokens if token.lower() in normalized_lower)
        if response_status in {401, 403} or any(token in normalized_lower for token in auth_tokens):
            return (
                "URL_AUTH_WALL_DETECTED",
                "URL stopped at an authentication wall or captcha before a usable invoice document was available.",
            )
        if nuonuo_platform_hits >= 2:
            return (
                "URL_NON_INVOICE_PAGE_SKIPPED",
                "URL resolved to a generic Nuonuo product or platform page and was safely skipped before PDF generation.",
            )
        if not has_invoice_signal and any(token in normalized_lower for token in non_invoice_tokens):
            return (
                "URL_NON_INVOICE_PAGE_SKIPPED",
                "URL resolved to a non-invoice page and was safely skipped before PDF generation.",
            )
        return "", ""

    @staticmethod
    def _header_value(headers, name):
        target = str(name or "").lower()
        for key, value in dict(headers or {}).items():
            if str(key).lower() == target:
                return value
        return ""

    @staticmethod
    def _policy_rejection_log(exc, source):
        return {
            "kind": "URL_POLICY_REJECTED",
            "url": exc.safe_url,
            "message": exc.reason,
            "source": source,
        }

    @staticmethod
    def _origin(validated):
        parsed = urlparse(validated.url)
        return parsed.scheme.lower(), validated.host, validated.port

    @staticmethod
    def _url_origin(url):
        try:
            parsed = urlparse(str(url or ""))
            if parsed.username is not None or parsed.password is not None:
                return None
            scheme = parsed.scheme.lower()
            host = (parsed.hostname or "").lower()
            default_port = {"http": 80, "https": 443}.get(scheme)
            port = parsed.port or default_port
            if not host or port is None:
                return None
            return scheme, host, port
        except ValueError:
            return None

    @staticmethod
    def _redirect_headers(headers, cross_origin, method_changed_to_get):
        credential_headers = {"authorization", "cookie", "proxy-authorization"}
        entity_headers = {
            "content-encoding",
            "content-language",
            "content-length",
            "content-location",
            "content-type",
            "transfer-encoding",
        }
        blocked = set()
        if cross_origin:
            blocked.update(credential_headers)
        if method_changed_to_get:
            blocked.update(entity_headers)
        return {
            key: value
            for key, value in dict(headers or {}).items()
            if str(key).lower() not in blocked
        }

    @staticmethod
    def _redirect_method(method, status):
        if status == 303 and method != "HEAD":
            return "GET"
        if status == 302 and method != "HEAD":
            return "GET"
        if status == 301 and method == "POST":
            return "GET"
        return method

    def _request_public(self, session, method, url, max_redirects=10, **kwargs):
        current = self.url_policy.validate(url)
        request_method = str(method or "GET").upper()
        request_kwargs = dict(kwargs)
        request_kwargs.pop("allow_redirects", None)
        request_kwargs.pop("stream", None)
        timeout = request_kwargs.pop("timeout", 20)
        max_response_bytes = request_kwargs.pop(
            "max_response_bytes", self.DIRECT_MAX_RESPONSE_BYTES
        )
        suppress_auth = False

        for redirect_count in range(max_redirects + 1):
            response = self.pinned_transport.request(
                session,
                request_method,
                current,
                headers=request_kwargs.get("headers"),
                data=request_kwargs.get("data"),
                json=request_kwargs.get("json"),
                files=request_kwargs.get("files"),
                params=request_kwargs.get("params"),
                timeout=timeout,
                suppress_auth=suppress_auth,
                max_response_bytes=max_response_bytes,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            response_headers = getattr(response, "headers", {}) or {}
            location = self._header_value(response_headers, "location")
            if status not in {301, 302, 303, 307, 308} or not location:
                return response
            if redirect_count >= max_redirects:
                raise PublicUrlPolicyError(current.url, "too many redirects")
            next_target = self.url_policy.resolve_redirect(current, location)
            next_method = self._redirect_method(request_method, status)
            changed_to_get = request_method != next_method and next_method == "GET"
            cross_origin = self._origin(current) != self._origin(next_target)
            headers = self._redirect_headers(
                request_kwargs.get("headers", {}),
                cross_origin=cross_origin,
                method_changed_to_get=changed_to_get,
            )
            suppress_auth = suppress_auth or cross_origin

            request_kwargs["headers"] = headers
            request_kwargs.pop("params", None)
            if changed_to_get:
                request_kwargs.pop("data", None)
                request_kwargs.pop("json", None)
                request_kwargs.pop("files", None)
            request_method = next_method
            current = next_target

        raise PublicUrlPolicyError(current.url, "too many redirects")

    @staticmethod
    def _page_from_request(request):
        try:
            frame = request.frame
            return frame.page if frame is not None else None
        except Exception:
            return None

    def _mark_browser_page_compromised(self, page, exc):
        if page is None:
            self._browser_global_compromise = exc
            return
        self._browser_pages[id(page)] = page
        self._browser_page_compromises[id(page)] = exc

    def _ensure_browser_page_secure(self, page):
        if self._browser_global_compromise is not None:
            raise self._browser_global_compromise
        compromise = self._browser_page_compromises.get(id(page))
        if compromise is not None:
            raise compromise

    def _prepare_browser_page(self, page):
        self._browser_pages[id(page)] = page
        self._browser_page_compromises.pop(id(page), None)
        self._browser_page_redirect_targets.pop(id(page), None)
        self._browser_page_effective_urls.pop(id(page), None)

    @staticmethod
    def _browser_is_navigation_request(request):
        marker = getattr(request, "is_navigation_request", None)
        if callable(marker):
            try:
                return bool(marker())
            except Exception:
                return False
        return str(getattr(request, "resource_type", "") or "").lower() == "document"

    def _browser_is_main_navigation(self, request, page):
        if page is None or not self._browser_is_navigation_request(request):
            return False
        try:
            main_frame = getattr(page, "main_frame", None)
            return main_frame is None or request.frame == main_frame
        except Exception:
            return False

    def _browser_pinned_response(self, request, initial_target):
        method = str(getattr(request, "method", "GET") or "GET").upper()
        headers = self._browser_request_headers(request)
        body = getattr(request, "post_data_buffer", None)
        current = initial_target
        suppress_auth = False
        max_response_bytes = self._browser_response_limit(request)

        for redirect_count in range(11):
            response = self.pinned_transport.request(
                self._browser_http_session,
                method,
                current,
                headers=headers,
                body=body,
                timeout=max(1, self.timeout_ms / 1000.0),
                suppress_auth=suppress_auth,
                decode_content=True,
                max_response_bytes=max_response_bytes,
            )
            status = int(getattr(response, "status_code", 0) or 0)
            location = self._header_value(getattr(response, "headers", {}), "location")
            if status not in {301, 302, 303, 307, 308} or not location:
                return response, current, None
            if redirect_count >= 10:
                raise PublicUrlPolicyError(current.url, "too many redirects")

            next_target = self.url_policy.resolve_redirect(current, location)
            next_method = self._redirect_method(method, status)
            if (
                self._browser_is_navigation_request(request)
                and method == "GET"
                and next_method == "GET"
            ):
                return response, current, next_target

            changed_to_get = method != next_method and next_method == "GET"
            cross_origin = self._origin(current) != self._origin(next_target)
            headers = self._redirect_headers(
                headers,
                cross_origin=cross_origin,
                method_changed_to_get=changed_to_get,
            )
            suppress_auth = suppress_auth or cross_origin
            if changed_to_get:
                body = None
            method = next_method
            current = next_target
            response.close()

        raise PublicUrlPolicyError(current.url, "too many redirects")

    @staticmethod
    def _browser_redirect_shim(target_url):
        target_literal = json.dumps(str(target_url), ensure_ascii=True).replace(
            "</", "<\\/"
        )
        return (
            "<!doctype html><meta charset=\"utf-8\">"
            f"<script>location.replace({target_literal})</script>"
        ).encode("ascii")

    def _guard_browser_request(self, route, request):
        page = self._page_from_request(request)
        try:
            validated = self.url_policy.validate(request.url)
            request_key = self._browser_request_key(request)
            self._browser_request_contexts[request_key] = validated
            if self._browser_http_session is None:
                raise PublicUrlPolicyError(
                    request.url, "browser pinned transport session is unavailable"
                )
            response, effective_target, browser_redirect = self._browser_pinned_response(
                request, validated
            )
            self._browser_effective_request_contexts[request_key] = effective_target
            main_navigation = self._browser_is_main_navigation(request, page)
            if browser_redirect is not None:
                if main_navigation:
                    self._browser_page_redirect_targets[id(page)] = browser_redirect.url
                    self._browser_page_effective_urls[id(page)] = effective_target.url
                response_headers = self._browser_fulfill_headers(
                    {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store"},
                    getattr(response, "set_cookie_headers", ()),
                )
                fulfill_status = 200
                fulfill_body = self._browser_redirect_shim(browser_redirect.url)
            else:
                if main_navigation:
                    self._browser_page_redirect_targets.pop(id(page), None)
                    self._browser_page_effective_urls[id(page)] = effective_target.url
                response_headers = self._browser_fulfill_headers(
                    response.headers,
                    getattr(response, "set_cookie_headers", ()),
                )
                fulfill_status = response.status_code
                fulfill_body = response.content
            self._browser_fulfilled_requests.add(request_key)
            try:
                route.fulfill(
                    status=fulfill_status,
                    headers=response_headers,
                    body=fulfill_body,
                )
            except Exception:
                self._browser_fulfilled_requests.discard(request_key)
                raise
            return
        except Exception as caught:
            exc = (
                caught
                if isinstance(caught, PublicUrlPolicyError)
                else PublicUrlPolicyError(request.url, "pinned browser transport failed")
            )
            self._mark_browser_page_compromised(page, exc)
            self._browser_policy_rejections.append(
                self._policy_rejection_log(exc, "browser_request")
            )
            logging.warning("%s", exc)
            route.abort("blockedbyclient")
            return

    @staticmethod
    def _browser_request_key(request):
        impl = getattr(request, "_impl_obj", None)
        return getattr(impl, "_guid", None) or id(request)

    @staticmethod
    def _browser_request_headers(request):
        all_headers = getattr(request, "all_headers", None)
        headers = dict(
            all_headers() if callable(all_headers) else getattr(request, "headers", {}) or {}
        )
        blocked = {"host", "content-length", "connection", "proxy-connection"}
        return {
            key: value
            for key, value in headers.items()
            if str(key).lower() not in blocked
        }

    def _browser_response_limit(self, request):
        resource_type = str(getattr(request, "resource_type", "") or "").lower()
        if resource_type in self.BROWSER_PASSIVE_RESOURCE_TYPES:
            return self.BROWSER_PASSIVE_MAX_RESPONSE_BYTES
        return self.BROWSER_DOCUMENT_MAX_RESPONSE_BYTES

    @staticmethod
    def _browser_fulfill_headers(headers, set_cookie_headers=()):
        blocked = {"connection", "proxy-connection", "transfer-encoding", "content-length"}
        result = {
            key: value
            for key, value in dict(headers or {}).items()
            if str(key).lower() not in blocked
        }
        if set_cookie_headers:
            result["Set-Cookie"] = "\n".join(set_cookie_headers)
        return result

    def _block_browser_websocket(self, web_socket):
        exc = PublicUrlPolicyError(
            getattr(web_socket, "url", ""), "browser WebSockets are blocked"
        )
        self._browser_global_compromise = exc
        self._browser_policy_rejections.append(
            self._policy_rejection_log(exc, "browser_websocket")
        )
        web_socket.close(code=1008, reason="blocked by URL policy")

    def _attach_browser_response_guard(self, page):
        def _guard(response):
            try:
                self._validate_browser_response(response)
            except PublicUrlPolicyError as exc:
                self._browser_policy_rejections.append(
                    self._policy_rejection_log(exc, "browser_response")
                )

        page.on("response", _guard)

    def _discard_browser_artifacts(self, artifacts, start_index):
        discarded = list(artifacts[start_index:])
        del artifacts[start_index:]
        staging_root = os.path.realpath(self.staging_dir)
        for artifact in discarded:
            path = os.path.realpath(str(artifact.get("path") or ""))
            if not path or not os.path.isfile(path):
                continue
            try:
                inside_staging = os.path.commonpath([staging_root, path]) == staging_root
            except ValueError:
                inside_staging = False
            if inside_staging:
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _validate_browser_response(self, response):
        response_url = str(getattr(response, "url", "") or "")
        request = getattr(response, "request", None)
        page = self._page_from_request(request)
        try:
            request_key = self._browser_request_key(request)
            validated = self._browser_request_contexts.get(request_key)
            if (
                validated is None
                or request_key not in self._browser_fulfilled_requests
            ):
                raise PublicUrlPolicyError(
                    response_url, "browser response was not produced by pinned fulfillment"
                )
            if self._origin(validated) != self._url_origin(response_url):
                raise PublicUrlPolicyError(
                    response_url, "browser response target changed unexpectedly"
                )
            return True
        except PublicUrlPolicyError as exc:
            self._mark_browser_page_compromised(page, exc)
            raise

    def _browser_effective_response_url(self, response):
        request = getattr(response, "request", None)
        if request is not None:
            target = self._browser_effective_request_contexts.get(
                self._browser_request_key(request)
            )
            if target is not None:
                return target.url
        return str(getattr(response, "url", "") or "")

    def _browser_page_url(self, page):
        return self._browser_page_effective_urls.get(
            id(page), str(getattr(page, "url", "") or "")
        )

    def _wait_for_browser_redirects(self, page, timeout_ms):
        wait_for_url = getattr(page, "wait_for_url", None)
        if not callable(wait_for_url):
            return
        import time

        deadline = time.monotonic() + max(0.001, timeout_ms / 1000.0)
        while True:
            target_url = self._browser_page_redirect_targets.get(id(page))
            if not target_url:
                return
            remaining_ms = max(1, int((deadline - time.monotonic()) * 1000))
            if remaining_ms <= 1 and time.monotonic() >= deadline:
                raise PlaywrightTimeoutError("guarded browser redirect timed out")
            wait_for_url(target_url, timeout=remaining_ms, wait_until="domcontentloaded")

    def _goto_public_page(self, page, url, timeout_ms):
        self._prepare_browser_page(page)
        validated = self.url_policy.validate(url)
        navigation_responses = []

        def _capture_navigation(response):
            request = getattr(response, "request", None)
            if not self._browser_is_main_navigation(request, page):
                return
            navigation_responses.append(response)
            try:
                self._validate_browser_response(response)
            except PublicUrlPolicyError as exc:
                self._browser_policy_rejections.append(
                    self._policy_rejection_log(exc, "browser_navigation_response")
                )

        listening = False
        try:
            page.on("response", _capture_navigation)
            listening = True
        except Exception:
            pass
        try:
            response = page.goto(
                validated.url, timeout=timeout_ms, wait_until="domcontentloaded"
            )
            self._wait_for_browser_redirects(page, timeout_ms)
            if navigation_responses:
                response = navigation_responses[-1]
            if response:
                self._validate_browser_response(response)
            self._ensure_browser_page_secure(page)
            return response
        finally:
            if listening:
                try:
                    page.remove_listener("response", _capture_navigation)
                except Exception:
                    pass

    def _goto_provider_page(self, page, url):
        response = self._goto_public_page(page, url, self.timeout_ms)
        try:
            page.wait_for_load_state("networkidle", timeout=self.provider_settle_timeout_ms)
        except Exception:
            pass
        self._ensure_browser_page_secure(page)
        return response

    def _read_pdf_text(self, pdf_path, max_pages=2):
        if not pdf_path or not os.path.exists(pdf_path):
            return ""
        try:
            texts = []
            with fitz.open(pdf_path) as doc:
                for page_index in range(min(max_pages, len(doc))):
                    texts.append(doc.load_page(page_index).get_text("text") or "")
            return "\n".join(texts).strip()
        except Exception:
            return ""

    def _safe_write_bytes(self, path, payload):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(payload)

    def _response_headers(self, response):
        try:
            return response.headers or {}
        except Exception:
            return {}

    @staticmethod
    def _require_requests():
        if requests is None:
            message = (
                "PDFConverter requires the 'requests' package at runtime. "
                "Install project dependencies before running invoice recovery."
            )
            if REQUESTS_IMPORT_ERROR is not None:
                raise RuntimeError(message) from REQUESTS_IMPORT_ERROR
            raise RuntimeError(message)

    @staticmethod
    def _require_playwright():
        if sync_playwright is None:
            message = (
                "PDFConverter requires Playwright at runtime. "
                "Install project dependencies before running Playwright-backed invoice recovery."
            )
            if PLAYWRIGHT_IMPORT_ERROR is not None:
                raise RuntimeError(message) from PLAYWRIGHT_IMPORT_ERROR
            raise RuntimeError(message)

    @staticmethod
    def _installed_browser_executables():
        candidates = []
        for command in ("chrome.exe", "msedge.exe", "chrome", "microsoft-edge"):
            resolved = shutil.which(command)
            if resolved:
                candidates.append(resolved)

        roots = [
            os.environ.get("PROGRAMFILES"),
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("LOCALAPPDATA"),
        ]
        relative_paths = (
            os.path.join("Google", "Chrome", "Application", "chrome.exe"),
            os.path.join("Microsoft", "Edge", "Application", "msedge.exe"),
        )
        for root in roots:
            if not root:
                continue
            for relative_path in relative_paths:
                candidate = os.path.join(root, relative_path)
                if os.path.isfile(candidate):
                    candidates.append(candidate)
        return list(dict.fromkeys(candidates))

    @staticmethod
    def _launch_chromium_browser(playwright, context_label):
        launch_error = None
        try:
            return playwright.chromium.launch(headless=True)
        except Exception as exc:
            launch_error = exc

        for executable_path in PDFConverter._installed_browser_executables():
            try:
                return playwright.chromium.launch(
                    headless=True,
                    executable_path=executable_path,
                )
            except Exception as exc:
                launch_error = exc

        message = (
            f"{context_label}: no production-supported Chromium, Chrome, or Edge "
            "browser could be started. See release_prep/chromium_build_contract.md."
        )
        raise RuntimeError(message) from launch_error

    def _new_browser_context(self, browser):
        self._browser_request_contexts.clear()
        self._browser_effective_request_contexts.clear()
        self._browser_fulfilled_requests.clear()
        self._browser_page_compromises.clear()
        self._browser_pages.clear()
        self._browser_page_redirect_targets.clear()
        self._browser_page_effective_urls.clear()
        self._browser_global_compromise = None
        self._require_requests()
        self._browser_http_session = requests.Session()
        self._browser_http_session.trust_env = False
        browser_http_session = self._browser_http_session
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            accept_downloads=True,
            service_workers="block",
        )
        context.route("**/*", self._guard_browser_request)
        context.route_web_socket("**/*", self._block_browser_websocket)
        try:
            context.on("close", lambda: browser_http_session.close())
        except Exception:
            pass
        return context

    def _build_candidate_urls(self, text_content, subject, candidate_info):
        if candidate_info and candidate_info.get("provider_family") == "baiwang":
            return collect_baiwang_candidate_urls(
                candidate_info.get("source_url") or text_content,
                candidate_info.get("provider_candidate_urls") or [],
            )
        if candidate_info and candidate_info.get("provider_family") in DIRECT_INVOICE_FAMILIES:
            return collect_direct_invoice_candidate_urls(
                candidate_info.get("source_url") or text_content,
                candidate_info.get("provider_candidate_urls") or [],
            )

        url_pattern = re.compile(r'https?://[^\s<>"]+')
        if str(text_content).startswith(("http://", "https://")):
            return [str(text_content).strip()]

        urls = url_pattern.findall(f"{text_content} {subject}")
        return [
            url
            for url in sorted(set(urls))
            if any(
                kw in url.lower()
                for kw in ["invoice", "fapiao", "fp", "pdf", "download", "bill", "receipt", "tax", "e-invoice", "jd"]
            )
        ]

    def _probe_direct_artifact(self, session, url, artifact_prefix):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        }
        try:
            response = self._request_public(session, "GET", url, timeout=20, headers=headers)
        except PublicUrlPolicyError as exc:
            return [], [self._policy_rejection_log(exc, "direct_probe")]
        except Exception as exc:
            return [], [{"kind": "direct_probe_error", "url": url, "message": str(exc)}]

        content_type = response.headers.get("Content-Type", "")
        content_disposition = response.headers.get("Content-Disposition", "")
        kind = infer_baiwang_download_kind(
            response.url,
            content_type=content_type,
            content_disposition=content_disposition,
        )
        if not kind:
            return [], []

        artifact_path = f"{artifact_prefix}_direct.{kind}"
        if kind == "pdf":
            if not self._pdf_bytes_look_valid(response.content):
                return [], [{"kind": "direct_probe_invalid_pdf", "url": response.url}]
            self._safe_write_bytes(artifact_path, response.content)
            return [self._describe_baiwang_artifact(artifact_path, "pdf", response.url, "direct_request", {})], []

        self._safe_write_bytes(artifact_path, response.content)
        fields = parse_baiwang_xml_fields(response.content) if kind == "xml" else {}
        return [self._describe_baiwang_artifact(artifact_path, kind, response.url, "direct_request", fields)], []

    def _probe_baiwang_preview_api_artifacts(self, session, candidate_urls, artifact_prefix):
        artifacts = []
        logs = []
        for url in candidate_urls:
            parsed = urlparse(str(url or ""))
            if "pis.baiwang.com" not in (parsed.hostname or "").lower():
                continue
            param = (parse_qs(parsed.query).get("param") or [""])[0].strip()
            if not param:
                continue
            api_url = "https://pis.baiwang.com/bwmg/mix/bw/previewInvoiceQd"
            try:
                response = self._request_public(
                    session,
                    "POST",
                    api_url,
                    data=param.encode("utf-8"),
                    headers={"Content-Type": "application/json; charset=utf-8"},
                    timeout=20,
                )
                data = response.json()
            except PublicUrlPolicyError as exc:
                logs.append(self._policy_rejection_log(exc, "baiwang_preview_api"))
                continue
            except Exception as exc:
                logs.append({"kind": "baiwang_preview_api_error", "url": api_url, "message": str(exc)})
                continue
            if not data.get("success"):
                logs.append({"kind": "baiwang_preview_api_unsuccessful", "url": api_url, "message": data.get("message", "")})
                continue
            for fmt in ("XML", "PDF", "OFD"):
                download_url = (
                    "https://pis.baiwang.com/bwmg/mix/bw/downloadFormat"
                    f"?param={quote(param)}&formatType={fmt}"
                )
                direct_artifacts, direct_logs = self._probe_direct_artifact(
                    session,
                    download_url,
                    f"{artifact_prefix}_preview_api_{fmt.lower()}",
                )
                artifacts.extend(direct_artifacts)
                logs.extend(direct_logs)
        return artifacts, logs

    def _collect_dom_urls(self, page):
        urls = []
        try:
            hrefs = page.locator("a[href]").evaluate_all(
                "els => els.map(el => el.href || el.getAttribute('href') || '').filter(Boolean)"
            )
            urls.extend(hrefs or [])
        except Exception:
            pass

        try:
            html = page.content()
            urls.extend(re.findall(r'https?://[^\s"\'<>]+', html))
        except Exception:
            pass

        unique_urls = []
        seen = set()
        for candidate in urls:
            normalized = str(candidate or "").strip()
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            unique_urls.append(normalized)
        return unique_urls

    def _validated_dom_urls(self, page, base_url, logs):
        self._ensure_browser_page_secure(page)
        public_urls = []
        for dom_url in self._collect_dom_urls(page):
            candidate = urljoin(base_url, dom_url)
            try:
                public_urls.append(self.url_policy.validate(candidate).url)
            except PublicUrlPolicyError as exc:
                logs.append(self._policy_rejection_log(exc, "dom_url"))
        self._ensure_browser_page_secure(page)
        return public_urls

    def _append_process_log(self, level, reason_code, url, subject=None):
        del subject
        safe_url = self.url_policy.sanitize(url)
        with open(
            os.path.join(self.staging_dir, "process_log.txt"),
            "a",
            encoding="utf-8",
        ) as handle:
            handle.write(f"[{level}] {reason_code}: {safe_url}\n")

    def _describe_baiwang_artifact(self, path, kind, source_url, mode, fields):
        artifact = {
            "path": path,
            "kind": kind,
            "source_url": source_url,
            "download_mode": mode,
            "fields": fields or {},
            "wrapper_detected": False,
        }
        if kind == "pdf":
            preview_text = self._read_pdf_text(path)
            artifact["preview_text_excerpt"] = preview_text[:800]
            artifact["wrapper_detected"] = looks_like_baiwang_wrapper_text(preview_text)
            artifact["fields"] = merge_expected_fields(fields or {}, extract_fields_from_pdf_text(preview_text))
        return artifact

    def _describe_direct_invoice_artifact(self, path, kind, source_url, resolved_url, mode, fields):
        artifact = {
            "path": path,
            "kind": kind,
            "source_url": source_url,
            "resolved_url": resolved_url or source_url,
            "download_mode": mode,
            "fields": dict(fields or {}),
        }
        if kind == "pdf":
            preview_text = self._read_pdf_text(path)
            artifact["preview_text_excerpt"] = preview_text[:800]
            artifact["fields"] = {
                **dict(fields or {}),
                **extract_direct_invoice_fields_from_pdf_text(preview_text),
            }
        return artifact

    def _probe_direct_invoice_artifact(self, session, url, artifact_prefix):
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
            )
        }
        try:
            response = self._request_public(
                session, "GET", url, timeout=20, headers=headers, stream=True
            )
        except PublicUrlPolicyError as exc:
            return [], [self._policy_rejection_log(exc, "direct_invoice_probe")]
        except Exception as exc:
            return [], [{"kind": "direct_probe_error", "url": url, "message": str(exc)}]

        try:
            content = response.content
            content_type = response.headers.get("Content-Type", "")
            content_disposition = response.headers.get("Content-Disposition", "")
            kind = infer_direct_download_kind(
                response.url,
                content_type=content_type,
                content_disposition=content_disposition,
            )
            if not kind:
                return [], []

            artifact_path = f"{artifact_prefix}_direct.{kind}"
            if kind == "pdf":
                if not self._pdf_bytes_look_valid(content):
                    return [], [{"kind": "direct_probe_invalid_pdf", "url": response.url}]
                self._safe_write_bytes(artifact_path, content)
                return [
                    self._describe_direct_invoice_artifact(
                        artifact_path,
                        "pdf",
                        url,
                        response.url,
                        "direct_request",
                        {},
                    )
                ], []

            self._safe_write_bytes(artifact_path, content)
            fields = parse_direct_invoice_xml_fields(content) if kind == "xml" else {}
            return [
                self._describe_direct_invoice_artifact(
                    artifact_path,
                    kind,
                    url,
                    response.url,
                    "direct_request",
                    fields,
                )
            ], []
        finally:
            response.close()

    def _probe_nuonuo_scan_invoice_artifacts(self, session, url, artifact_prefix):
        artifacts = []
        logs = []
        try:
            response = self._request_public(
                session,
                "GET",
                url,
                timeout=20,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
                    )
                },
            )
        except PublicUrlPolicyError as exc:
            return [], [self._policy_rejection_log(exc, "nuonuo_shortlink")]
        except Exception as exc:
            return [], [{"kind": "nuonuo_shortlink_error", "url": url, "message": str(exc)}]

        final_url = getattr(response, "url", "") or url
        query = parse_qs(urlparse(final_url).query)
        param_list = (query.get("paramList") or [""])[0]
        if not param_list:
            logs.append({"kind": "nuonuo_missing_param_list", "url": final_url})
            return artifacts, logs

        endpoint = "https://nnfp.jss.com.cn/sapi/scan2/getIvcDetailShow.do"
        if (query.get("isOuterPageReq") or [""])[0].lower() == "true":
            endpoint = "https://nnfp.jss.com.cn/sapi/invoice/scan/IvcDetail.do"
        payload = {
            "paramList": param_list,
            "code": (query.get("code") or [""])[0],
            "aliView": (query.get("aliView") or [""])[0],
            "invoiceDetailMiddleUri": "printQrcode",
            "shortLinkSource": (query.get("shortLinkSource") or [""])[0],
        }
        try:
            detail_response = self._request_public(
                session, "POST", endpoint, data=payload, timeout=20
            )
            detail = detail_response.json()
        except PublicUrlPolicyError as exc:
            logs.append(self._policy_rejection_log(exc, "nuonuo_detail_api"))
            return artifacts, logs
        except Exception as exc:
            logs.append({"kind": "nuonuo_detail_api_error", "url": endpoint, "message": str(exc)})
            return artifacts, logs

        if detail.get("status") != "0000":
            logs.append({"kind": "nuonuo_detail_api_unsuccessful", "url": endpoint, "message": detail.get("msg", "")})
            return artifacts, logs

        invoice_info = ((detail.get("data") or {}).get("invoiceSimpleVo") or {})
        for key in ("xmlUrl", "url", "ofdDownloadUrl"):
            target_url = str(invoice_info.get(key) or "").strip()
            if not target_url:
                continue
            direct_artifacts, direct_logs = self._probe_direct_invoice_artifact(
                session,
                target_url,
                f"{artifact_prefix}_nuonuo_{key.lower()}",
            )
            artifacts.extend(direct_artifacts)
            logs.extend(direct_logs)
        return artifacts, logs

    def _capture_direct_invoice_response_artifact(self, response, artifact_prefix, artifact_index, source_url):
        self._validate_browser_response(response)
        headers = self._response_headers(response)
        effective_url = self._browser_effective_response_url(response)
        kind = infer_direct_download_kind(
            effective_url,
            content_type=headers.get("content-type", ""),
            content_disposition=headers.get("content-disposition", ""),
        )
        if kind not in {"pdf", "xml", "ofd"}:
            return None
        try:
            payload = response.body()
        except Exception:
            return None
        if kind == "pdf" and not self._pdf_bytes_look_valid(payload):
            return None
        artifact_path = f"{artifact_prefix}_network_{artifact_index}.{kind}"
        self._safe_write_bytes(artifact_path, payload)
        fields = parse_direct_invoice_xml_fields(payload) if kind == "xml" else {}
        return self._describe_direct_invoice_artifact(
            artifact_path,
            kind,
            source_url,
            effective_url,
            "network_capture",
            fields,
        )

    def _match_direct_invoice_pdf_artifact(self, expected_fields, artifact, xml_fields=None):
        expected = dict(expected_fields or {})
        fields = {
            **dict(xml_fields or {}),
            **dict(artifact.get("fields", {}) or {}),
        }
        expected_number = str(expected.get("invoice_number") or "").strip()
        actual_number = str(fields.get("invoice_number") or "").strip()
        if expected_number:
            if actual_number:
                if actual_number == expected_number:
                    return True, "invoice_number", False
                return False, "invoice_number_mismatch", True
            if expected_number in str(artifact.get("resolved_url") or "") or expected_number in str(artifact.get("source_url") or ""):
                return True, "invoice_number_from_url", False

        expected_seller = normalize_direct_token(expected.get("seller") or "")
        actual_seller = normalize_direct_token(fields.get("seller") or "")
        if expected_seller and actual_seller and expected_seller not in actual_seller and actual_seller not in expected_seller:
            return False, "seller_mismatch", True

        return False, "no_explicit_match", False

    def _select_direct_invoice_recovery_result(self, artifacts, expected_fields):
        xml_artifacts = []
        pdf_artifacts = []
        hard_mismatch = False

        for artifact in artifacts:
            if artifact.get("kind") == "xml":
                xml_artifacts.append(artifact)
            elif artifact.get("kind") == "pdf":
                pdf_artifacts.append(artifact)

        best_xml_fields = {}
        for artifact in xml_artifacts:
            xml_fields = artifact.get("fields", {}) or {}
            if not best_xml_fields:
                best_xml_fields = dict(xml_fields)
            matched, matched_on, mismatch = self._match_direct_invoice_pdf_artifact(
                expected_fields,
                {"fields": xml_fields, "resolved_url": artifact.get("resolved_url", ""), "source_url": artifact.get("source_url", "")},
            )
            if matched:
                best_xml_fields = dict(xml_fields)
                break
            hard_mismatch = hard_mismatch or mismatch

        for artifact in pdf_artifacts:
            matched, matched_on, mismatch = self._match_direct_invoice_pdf_artifact(expected_fields, artifact, best_xml_fields)
            artifact["fields"] = {**best_xml_fields, **dict(artifact.get("fields", {}) or {})}
            artifact["expected_match"] = matched
            artifact["matched_on"] = matched_on
            if matched:
                return artifact, matched_on
            hard_mismatch = hard_mismatch or mismatch

        if len(pdf_artifacts) == 1 and not hard_mismatch:
            artifact = pdf_artifacts[0]
            artifact["fields"] = {**best_xml_fields, **dict(artifact.get("fields", {}) or {})}
            artifact["expected_match"] = True
            artifact["matched_on"] = "single_pdf_without_conflict"
            return artifact, "single_pdf_without_conflict"

        if hard_mismatch:
            return None, "pdf_entity_mismatch"
        if pdf_artifacts:
            return None, "multiple_pdf_candidates_without_match"
        if xml_artifacts:
            return None, "xml_only_no_pdf"
        return None, "no_valid_pdf"

    def _capture_response_artifact(self, response, artifact_prefix, artifact_index):
        self._validate_browser_response(response)
        headers = self._response_headers(response)
        effective_url = self._browser_effective_response_url(response)
        kind = infer_baiwang_download_kind(
            effective_url,
            content_type=headers.get("content-type", ""),
            content_disposition=headers.get("content-disposition", ""),
        )
        if kind not in {"pdf", "xml", "ofd"}:
            return None
        try:
            payload = response.body()
        except Exception:
            return None
        if kind == "pdf" and not self._pdf_bytes_look_valid(payload):
            return None

        artifact_path = f"{artifact_prefix}_network_{artifact_index}.{kind}"
        self._safe_write_bytes(artifact_path, payload)
        fields = parse_baiwang_xml_fields(payload) if kind == "xml" else {}
        return self._describe_baiwang_artifact(artifact_path, kind, effective_url, "network_capture", fields)

    def _attempt_click_downloads(self, page, artifact_prefix):
        artifacts = []
        for kind, selectors in self.BAIWANG_CLICK_TARGETS.items():
            for selector in selectors:
                try:
                    trigger = page.locator(selector).first
                    if trigger.count() == 0:
                        continue
                    download_path = f"{artifact_prefix}_click_{kind}.{kind}"
                    with page.expect_download(timeout=self.timeout_ms) as download_info:
                        trigger.click()
                    download = download_info.value
                    suggested = download.suggested_filename or ""
                    actual_kind = infer_baiwang_download_kind(
                        suggested,
                        filename=suggested,
                    ) or kind
                    download_path = f"{artifact_prefix}_click_{kind}.{actual_kind}"
                    download.save_as(download_path)
                    fields = {}
                    if actual_kind == "xml":
                        with open(download_path, "rb") as fh:
                            fields = parse_baiwang_xml_fields(fh.read())
                    artifacts.append(
                        self._describe_baiwang_artifact(
                            download_path,
                            actual_kind,
                            self._browser_page_url(page),
                            f"playwright_click_{kind}",
                            fields,
                        )
                    )
                    break
                except Exception:
                    continue
        return artifacts

    def _select_baiwang_recovery_result(self, artifacts, expected_fields):
        matched_xml = []
        candidate_pdfs = []
        matched_pdfs = []
        for artifact in artifacts:
            matched, matched_on = match_baiwang_expected_fields(expected_fields, artifact.get("fields", {}))
            artifact["expected_match"] = matched
            artifact["matched_on"] = matched_on
            if artifact.get("kind") in {"xml", "ofd"} and matched:
                matched_xml.append(artifact)
            if artifact.get("kind") == "pdf" and not artifact.get("wrapper_detected"):
                candidate_pdfs.append(artifact)
                if matched:
                    matched_pdfs.append(artifact)

        if matched_pdfs:
            selected = matched_pdfs[0]
            return selected, "matched_pdf"

        if matched_xml:
            preferred_sources = {item.get("source_url") for item in matched_xml if item.get("source_url")}
            for artifact in candidate_pdfs:
                if artifact.get("source_url") in preferred_sources:
                    artifact["fields"] = merge_expected_fields(matched_xml[0].get("fields", {}), artifact.get("fields", {}))
                    artifact["expected_match"] = True
                    artifact["matched_on"] = "xml_then_pdf_same_source"
                    return artifact, "xml_then_pdf_same_source"
            if len(candidate_pdfs) == 1:
                artifact = candidate_pdfs[0]
                artifact["fields"] = merge_expected_fields(matched_xml[0].get("fields", {}), artifact.get("fields", {}))
                artifact["expected_match"] = True
                artifact["matched_on"] = "xml_then_single_pdf"
                return artifact, "xml_then_single_pdf"
            return None, "xml_only_no_confirmed_pdf"

        if candidate_pdfs and not any(str(value or "").strip() for value in (expected_fields or {}).values()):
            return candidate_pdfs[0], "pdf_without_expected_fields"

        if candidate_pdfs:
            return None, "pdf_entity_mismatch"

        return None, "no_valid_pdf"

    def _recover_baiwang_group(self, candidate_urls, subject, email_id, email_staging_path, candidate_info):
        expected_fields = merge_expected_fields(candidate_info.get("provider_expected_fields", {}))
        recovery_started_at = __import__("time").perf_counter()
        recovery_meta = {
            "source_url": candidate_info.get("source_url") or (candidate_urls[0] if candidate_urls else ""),
            "candidate_urls": list(candidate_urls),
            "resolved_urls": [],
            "pdf_path": "",
            "provider_family": "baiwang",
            "provider_group_key": candidate_info.get("provider_group_key", ""),
            "download_mode": "",
            "wrapper_detected": False,
            "body_excerpt": "",
            "page_title": "",
            "status": "provider_recovery_failed",
            "failure_stage": "",
            "reason_code": "",
            "expected_fields": expected_fields,
            "selected_fields": {},
            "captured_network": [],
            "captured_artifacts": [],
            "timing_ms": {},
        }
        artifacts = []
        network_logs = []
        ordered_candidate_urls = sorted(list(candidate_urls), key=self._baiwang_url_priority)

        self._require_requests()
        with requests.Session() as session:
            for index, url in enumerate(ordered_candidate_urls, start=1):
                artifact_prefix = os.path.join(email_staging_path, f"baiwang_{index}")
                api_artifacts, api_logs = self._probe_baiwang_preview_api_artifacts(
                    session,
                    [url],
                    artifact_prefix,
                )
                artifacts.extend(api_artifacts)
                network_logs.extend(api_logs)
                direct_artifacts, direct_logs = self._probe_direct_artifact(session, url, artifact_prefix)
                artifacts.extend(direct_artifacts)
                network_logs.extend(direct_logs)
                selected_artifact, _ = self._select_baiwang_recovery_result(artifacts, expected_fields)
                if selected_artifact:
                    recovery_meta["timing_ms"] = {"total_ms": self._elapsed_ms(recovery_started_at)}
                    recovery_meta["captured_network"] = list(network_logs[:200])
                    recovery_meta["captured_artifacts"] = [
                        {
                            "path": artifact.get("path", ""),
                            "kind": artifact.get("kind", ""),
                            "source_url": artifact.get("source_url", ""),
                            "download_mode": artifact.get("download_mode", ""),
                            "wrapper_detected": artifact.get("wrapper_detected", False),
                            "fields": artifact.get("fields", {}),
                            "expected_match": artifact.get("expected_match"),
                            "matched_on": artifact.get("matched_on", ""),
                        }
                        for artifact in artifacts
                    ]
                    recovery_meta.update(
                        {
                            "pdf_path": selected_artifact.get("path", ""),
                            "download_mode": selected_artifact.get("download_mode", ""),
                            "status": "downloaded",
                            "reason_code": "",
                            "failure_stage": "",
                            "selected_fields": selected_artifact.get("fields", {}),
                            "matched_on": selected_artifact.get("matched_on", ""),
                        }
                    )
                    return recovery_meta

        self._require_playwright()
        with requests.Session() as session:
            with sync_playwright() as playwright:
                browser = self._launch_chromium_browser(playwright, "baiwang recovery")
                context = self._new_browser_context(browser)
                try:
                    for index, url in enumerate(ordered_candidate_urls, start=1):
                        artifact_prefix = os.path.join(email_staging_path, f"baiwang_{index}")
                        direct_artifacts, direct_logs = self._probe_direct_artifact(session, url, artifact_prefix)
                        artifacts.extend(direct_artifacts)
                        network_logs.extend(direct_logs)
                        selected_artifact, _ = self._select_baiwang_recovery_result(artifacts, expected_fields)
                        if selected_artifact:
                            break

                        browser_artifact_start = len(artifacts)
                        page = context.new_page()
                        captured_responses = []

                        def _on_response(response):
                            try:
                                self._validate_browser_response(response)
                            except PublicUrlPolicyError as exc:
                                network_logs.append(
                                    self._policy_rejection_log(exc, "browser_response")
                                )
                                return
                            headers = self._response_headers(response)
                            captured_responses.append(
                                {
                                    "response": response,
                                    "url": self._browser_effective_response_url(response),
                                    "status": getattr(response, "status", None),
                                    "content_type": headers.get("content-type", ""),
                                    "content_disposition": headers.get("content-disposition", ""),
                                }
                            )

                        page.on("response", _on_response)
                        try:
                            response = self._goto_provider_page(page, url)
                            if not response:
                                network_logs.append({"kind": "navigation_no_response", "url": url})
                                continue

                            body_text = self._read_body_text(page)
                            effective_page_url = self._browser_page_url(page)
                            recovery_meta["resolved_urls"].append(effective_page_url)
                            recovery_meta["body_excerpt"] = body_text[:800] or recovery_meta["body_excerpt"]
                            try:
                                recovery_meta["page_title"] = page.title() or recovery_meta["page_title"]
                            except Exception:
                                pass

                            wrapper_detected = looks_like_baiwang_wrapper_text(body_text) or (
                                is_baiwang_family_url(effective_page_url)
                                and "/fp/detail" in urlparse(effective_page_url).path.lower()
                            )
                            recovery_meta["wrapper_detected"] = recovery_meta["wrapper_detected"] or wrapper_detected

                            for candidate_dom_url in self._validated_dom_urls(
                                page, effective_page_url, network_logs
                            ):
                                if not is_baiwang_family_url(candidate_dom_url, sender_addr="", subject=subject):
                                    continue
                                dom_artifacts, dom_logs = self._probe_direct_artifact(
                                    session,
                                    candidate_dom_url,
                                    f"{artifact_prefix}_dom",
                                )
                                artifacts.extend(dom_artifacts)
                                network_logs.extend(dom_logs)

                            if wrapper_detected:
                                artifacts.extend(self._attempt_click_downloads(page, artifact_prefix))

                            for response_item in captured_responses:
                                try:
                                    artifact = self._capture_response_artifact(
                                        response_item["response"],
                                        artifact_prefix,
                                        len(artifacts) + 1,
                                    )
                                except PublicUrlPolicyError as exc:
                                    network_logs.append(
                                        self._policy_rejection_log(exc, "captured_response")
                                    )
                                    continue
                                if artifact:
                                    artifacts.append(artifact)
                                else:
                                    network_logs.append(
                                        {
                                            "kind": "network_seen",
                                            "url": response_item["url"],
                                            "status": response_item["status"],
                                            "content_type": response_item["content_type"],
                                        }
                                    )
                            self._ensure_browser_page_secure(page)
                            selected_artifact, _ = self._select_baiwang_recovery_result(artifacts, expected_fields)
                            if selected_artifact:
                                break
                        except PublicUrlPolicyError as exc:
                            self._discard_browser_artifacts(
                                artifacts, browser_artifact_start
                            )
                            network_logs.append(
                                self._policy_rejection_log(exc, "browser_navigation")
                            )
                        except PlaywrightTimeoutError:
                            network_logs.append({"kind": "navigation_timeout", "url": url})
                        except Exception as exc:
                            network_logs.append({"kind": "navigation_error", "url": url, "message": str(exc)})
                        finally:
                            if not page.is_closed():
                                page.close()
                finally:
                    browser.close()

        selected_artifact, select_reason = self._select_baiwang_recovery_result(artifacts, expected_fields)
        recovery_meta["timing_ms"] = {"total_ms": self._elapsed_ms(recovery_started_at)}
        recovery_meta["captured_network"] = list(network_logs[:200])
        recovery_meta["captured_artifacts"] = [
            {
                "path": artifact.get("path", ""),
                "kind": artifact.get("kind", ""),
                "source_url": artifact.get("source_url", ""),
                "download_mode": artifact.get("download_mode", ""),
                "wrapper_detected": artifact.get("wrapper_detected", False),
                "fields": artifact.get("fields", {}),
                "expected_match": artifact.get("expected_match"),
                "matched_on": artifact.get("matched_on", ""),
            }
            for artifact in artifacts
        ]

        if selected_artifact:
            recovery_meta.update(
                {
                    "pdf_path": selected_artifact.get("path", ""),
                    "download_mode": selected_artifact.get("download_mode", ""),
                    "status": "downloaded",
                    "reason_code": "",
                    "failure_stage": "",
                    "selected_fields": selected_artifact.get("fields", {}),
                    "matched_on": selected_artifact.get("matched_on", ""),
                }
            )
            return recovery_meta

        recovery_meta["reason_code"] = f"BAIWANG_{select_reason.upper()}"
        recovery_meta["failure_stage"] = "provider_recovery"
        if select_reason == "xml_only_no_confirmed_pdf":
            recovery_meta["reason_code"] = "BAIWANG_XML_ONLY_NO_CONFIRMED_PDF"
        elif select_reason == "pdf_entity_mismatch":
            recovery_meta["reason_code"] = "BAIWANG_RECOVERED_PDF_ENTITY_MISMATCH"
        elif select_reason == "no_valid_pdf":
            recovery_meta["reason_code"] = "BAIWANG_NO_VALID_PDF_RECOVERED"
        return recovery_meta

    def _recover_direct_invoice_group(self, candidate_urls, subject, email_id, email_staging_path, candidate_info):
        provider_family = str(candidate_info.get("provider_family") or infer_direct_invoice_family(candidate_urls[0] if candidate_urls else "")).strip()
        expected_fields = dict(candidate_info.get("provider_expected_fields", {}) or {})
        recovery_started_at = __import__("time").perf_counter()
        recovery_meta = {
            "source_url": candidate_info.get("source_url") or (candidate_urls[0] if candidate_urls else ""),
            "candidate_urls": list(candidate_urls),
            "resolved_url": "",
            "resolved_urls": [],
            "pdf_path": "",
            "provider_family": provider_family,
            "provider_group_key": candidate_info.get("provider_group_key", "") or build_direct_invoice_group_key(
                family=provider_family,
                email_id=email_id,
                expected_fields=expected_fields,
                candidate_urls=candidate_urls,
            ),
            "download_mode": "",
            "wrapper_detected": False,
            "body_excerpt": "",
            "page_title": "",
            "status": "provider_recovery_failed",
            "failure_stage": "",
            "reason_code": "",
            "expected_fields": expected_fields,
            "selected_fields": {},
            "captured_network": [],
            "captured_artifacts": [],
            "retention_bucket_suffix": "direct_invoice_url",
            "provider_recovery_message": "Direct invoice recovery exhausted all restoration paths without a confirmed invoice PDF.",
            "timing_ms": {},
        }
        artifacts = []
        network_logs = []
        seen_urls = set()

        self._require_requests()
        with requests.Session() as session:
            for index, url in enumerate(candidate_urls, start=1):
                if url in seen_urls:
                    continue
                seen_urls.add(url)
                artifact_prefix = os.path.join(email_staging_path, f"direct_invoice_{index}")
                if provider_family == "nuonuo_scan_invoice":
                    nuonuo_artifacts, nuonuo_logs = self._probe_nuonuo_scan_invoice_artifacts(
                        session,
                        url,
                        artifact_prefix,
                    )
                    artifacts.extend(nuonuo_artifacts)
                    network_logs.extend(nuonuo_logs)
                direct_artifacts, direct_logs = self._probe_direct_invoice_artifact(session, url, artifact_prefix)
                artifacts.extend(direct_artifacts)
                network_logs.extend(direct_logs)

        selected_artifact, select_reason = self._select_direct_invoice_recovery_result(artifacts, expected_fields)
        if selected_artifact:
            recovery_meta["timing_ms"] = {"total_ms": self._elapsed_ms(recovery_started_at)}
            recovery_meta["captured_network"] = list(network_logs[:200])
            recovery_meta["captured_artifacts"] = [
                {
                    "path": artifact.get("path", ""),
                    "kind": artifact.get("kind", ""),
                    "source_url": artifact.get("source_url", ""),
                    "resolved_url": artifact.get("resolved_url", ""),
                    "download_mode": artifact.get("download_mode", ""),
                    "fields": artifact.get("fields", {}),
                    "expected_match": artifact.get("expected_match"),
                    "matched_on": artifact.get("matched_on", ""),
                }
                for artifact in artifacts
            ]
            recovery_meta.update(
                {
                    "resolved_url": self.url_policy.sanitize(
                        selected_artifact.get("resolved_url", "")
                    ),
                    "resolved_urls": [
                        self.url_policy.sanitize(
                            selected_artifact.get("resolved_url", "")
                        )
                    ] if selected_artifact.get("resolved_url") else recovery_meta["resolved_urls"],
                    "pdf_path": selected_artifact.get("path", ""),
                    "download_mode": selected_artifact.get("download_mode", ""),
                    "status": "downloaded",
                    "reason_code": "",
                    "failure_stage": "",
                    "selected_fields": selected_artifact.get("fields", {}),
                    "matched_on": selected_artifact.get("matched_on", ""),
                }
            )
            return recovery_meta
        if provider_family != "bwjf_signed_invoice":
            recovery_meta["timing_ms"] = {"total_ms": self._elapsed_ms(recovery_started_at)}
            recovery_meta["captured_network"] = list(network_logs[:200])
            recovery_meta["captured_artifacts"] = [
                {
                    "path": artifact.get("path", ""),
                    "kind": artifact.get("kind", ""),
                    "source_url": artifact.get("source_url", ""),
                    "resolved_url": artifact.get("resolved_url", ""),
                    "download_mode": artifact.get("download_mode", ""),
                    "fields": artifact.get("fields", {}),
                    "expected_match": artifact.get("expected_match"),
                    "matched_on": artifact.get("matched_on", ""),
                }
                for artifact in artifacts
            ]
            recovery_meta["reason_code"] = f"DIRECT_INVOICE_{select_reason.upper()}"
            recovery_meta["failure_stage"] = "provider_recovery"
            return recovery_meta

        seen_urls = set()
        self._require_playwright()
        with requests.Session() as session:
            with sync_playwright() as playwright:
                browser = self._launch_chromium_browser(playwright, "direct invoice recovery")
                context = self._new_browser_context(browser)
                try:
                    for index, url in enumerate(candidate_urls, start=1):
                        if url in seen_urls:
                            continue
                        seen_urls.add(url)
                        artifact_prefix = os.path.join(email_staging_path, f"direct_invoice_{index}")
                        direct_artifacts, direct_logs = self._probe_direct_invoice_artifact(session, url, artifact_prefix)
                        artifacts.extend(direct_artifacts)
                        network_logs.extend(direct_logs)
                        selected_artifact, _ = self._select_direct_invoice_recovery_result(artifacts, expected_fields)
                        if selected_artifact:
                            break

                        if direct_artifacts and any(item.get("kind") == "pdf" for item in direct_artifacts):
                            continue

                        if provider_family != "bwjf_signed_invoice":
                            continue

                        browser_artifact_start = len(artifacts)
                        page = context.new_page()
                        captured_responses = []

                        def _on_response(response):
                            try:
                                self._validate_browser_response(response)
                            except PublicUrlPolicyError as exc:
                                network_logs.append(
                                    self._policy_rejection_log(exc, "browser_response")
                                )
                                return
                            headers = self._response_headers(response)
                            captured_responses.append(
                                {
                                    "response": response,
                                    "url": self._browser_effective_response_url(response),
                                    "status": getattr(response, "status", None),
                                    "content_type": headers.get("content-type", ""),
                                    "content_disposition": headers.get("content-disposition", ""),
                                }
                            )

                        page.on("response", _on_response)
                        try:
                            response = self._goto_provider_page(page, url)
                            if not response:
                                network_logs.append({"kind": "navigation_no_response", "url": url})
                                continue
                            effective_page_url = self._browser_page_url(page)
                            recovery_meta["resolved_urls"].append(effective_page_url)
                            if not recovery_meta["resolved_url"]:
                                recovery_meta["resolved_url"] = effective_page_url
                            recovery_meta["body_excerpt"] = (self._read_body_text(page) or "")[:800]
                            try:
                                recovery_meta["page_title"] = page.title() or recovery_meta["page_title"]
                            except Exception:
                                pass

                            resolved_family = infer_direct_invoice_family(effective_page_url)
                            if resolved_family:
                                resolved_artifacts, resolved_logs = self._probe_direct_invoice_artifact(
                                    session,
                                    effective_page_url,
                                    f"{artifact_prefix}_resolved",
                                )
                                artifacts.extend(resolved_artifacts)
                                network_logs.extend(resolved_logs)

                            for candidate_dom_url in self._validated_dom_urls(
                                page, effective_page_url, network_logs
                            ):
                                if not is_direct_invoice_family_url(candidate_dom_url):
                                    continue
                                dom_artifacts, dom_logs = self._probe_direct_invoice_artifact(
                                    session,
                                    candidate_dom_url,
                                    f"{artifact_prefix}_dom",
                                )
                                artifacts.extend(dom_artifacts)
                                network_logs.extend(dom_logs)

                            for response_item in captured_responses:
                                try:
                                    artifact = self._capture_direct_invoice_response_artifact(
                                        response_item["response"],
                                        artifact_prefix,
                                        len(artifacts) + 1,
                                        url,
                                    )
                                except PublicUrlPolicyError as exc:
                                    network_logs.append(
                                        self._policy_rejection_log(exc, "captured_response")
                                    )
                                    continue
                                if artifact:
                                    artifacts.append(artifact)
                                else:
                                    network_logs.append(
                                        {
                                            "kind": "network_seen",
                                            "url": response_item["url"],
                                            "status": response_item["status"],
                                            "content_type": response_item["content_type"],
                                        }
                                    )
                            self._ensure_browser_page_secure(page)
                            selected_artifact, _ = self._select_direct_invoice_recovery_result(artifacts, expected_fields)
                            if selected_artifact:
                                break
                        except PublicUrlPolicyError as exc:
                            self._discard_browser_artifacts(
                                artifacts, browser_artifact_start
                            )
                            network_logs.append(
                                self._policy_rejection_log(exc, "browser_navigation")
                            )
                        except PlaywrightTimeoutError:
                            network_logs.append({"kind": "navigation_timeout", "url": url})
                        except Exception as exc:
                            network_logs.append({"kind": "navigation_error", "url": url, "message": str(exc)})
                        finally:
                            if not page.is_closed():
                                page.close()
                finally:
                    browser.close()

        selected_artifact, select_reason = self._select_direct_invoice_recovery_result(artifacts, expected_fields)
        recovery_meta["timing_ms"] = {"total_ms": self._elapsed_ms(recovery_started_at)}
        recovery_meta["captured_network"] = list(network_logs[:200])
        recovery_meta["captured_artifacts"] = [
            {
                "path": artifact.get("path", ""),
                "kind": artifact.get("kind", ""),
                "source_url": artifact.get("source_url", ""),
                "resolved_url": artifact.get("resolved_url", ""),
                "download_mode": artifact.get("download_mode", ""),
                "fields": artifact.get("fields", {}),
                "expected_match": artifact.get("expected_match"),
                "matched_on": artifact.get("matched_on", ""),
            }
            for artifact in artifacts
        ]

        if selected_artifact:
            recovery_meta.update(
                {
                    "pdf_path": selected_artifact.get("path", ""),
                    "resolved_url": self.url_policy.sanitize(
                        selected_artifact.get("resolved_url", "")
                    ),
                    "download_mode": selected_artifact.get("download_mode", ""),
                    "status": "downloaded",
                    "reason_code": "",
                    "failure_stage": "",
                    "selected_fields": selected_artifact.get("fields", {}),
                    "matched_on": selected_artifact.get("matched_on", ""),
                }
            )
            return recovery_meta

        reason_map = {
            "pdf_entity_mismatch": "DIRECT_INVOICE_PDF_ENTITY_MISMATCH",
            "multiple_pdf_candidates_without_match": "DIRECT_INVOICE_MULTIPLE_PDF_CANDIDATES",
            "xml_only_no_pdf": "DIRECT_INVOICE_XML_ONLY_NO_PDF",
            "no_valid_pdf": "DIRECT_INVOICE_NO_VALID_PDF_RECOVERED",
        }
        recovery_meta["reason_code"] = reason_map.get(select_reason, "DIRECT_INVOICE_PROVIDER_RECOVERY_FAILED")
        recovery_meta["failure_stage"] = "provider_recovery"
        return recovery_meta

    def process_invoice_links(self, text_content, subject, email_id, return_metadata=False, candidate_info=None):
        invoice_urls = self._build_candidate_urls(text_content, subject, candidate_info or {})
        if not invoice_urls:
            return []

        downloaded_items = []
        safe_subject = re.sub(r'[\\/:*?"<>|]', "_", subject)[:50]
        email_folder_name = f"{email_id}_{safe_subject}"
        email_staging_path = os.path.join(self.staging_dir, email_folder_name)
        os.makedirs(email_staging_path, exist_ok=True)

        provider_family = (candidate_info or {}).get("provider_family", "")
        if provider_family == "baiwang" or any(is_baiwang_family_url(url, subject=subject) for url in invoice_urls):
            recovery_meta = self._recover_baiwang_group(
                invoice_urls,
                subject,
                email_id,
                email_staging_path,
                candidate_info or {},
            )
            if return_metadata:
                return [dict(recovery_meta)]
            if recovery_meta.get("pdf_path"):
                return [recovery_meta["pdf_path"]]
            return []
        if provider_family in DIRECT_INVOICE_FAMILIES or any(is_direct_invoice_family_url(url) for url in invoice_urls):
            recovery_meta = self._recover_direct_invoice_group(
                invoice_urls,
                subject,
                email_id,
                email_staging_path,
                candidate_info or {},
            )
            if return_metadata:
                return [dict(recovery_meta)]
            if recovery_meta.get("pdf_path"):
                return [recovery_meta["pdf_path"]]
            return []

        self._require_playwright()
        with sync_playwright() as playwright:
            browser = self._launch_chromium_browser(playwright, "web invoice conversion")
            context = self._new_browser_context(browser)

            for index, url in enumerate(invoice_urls):
                safe_log_url = self.url_policy.sanitize(url)
                logging.info("Visiting URL: %s", safe_log_url)
                page = context.new_page()
                self._attach_browser_response_guard(page)
                link_started_at = __import__("time").perf_counter()
                pdf_path = os.path.join(email_staging_path, f"web_invoice_{index + 1}.pdf")
                link_meta = {
                    "source_url": str(url),
                    "resolved_url": str(url),
                    "pdf_path": pdf_path,
                    "provider_family": "",
                    "download_mode": "",
                    "wrapper_detected": False,
                    "body_excerpt": "",
                    "page_title": "",
                    "timing_ms": {},
                    "status": "started",
                    "reason_code": "",
                    "message": "",
                }

                def _append_link_result():
                    link_meta["timing_ms"] = {"total_ms": self._elapsed_ms(link_started_at)}
                    downloaded_items.append(dict(link_meta))

                try:
                    response = self._goto_public_page(page, url, self.generic_timeout_ms)
                    if not response:
                        logging.warning("No response from URL: %s", safe_log_url)
                        link_meta.update(
                            {
                                "status": "failed",
                                "reason_code": "URL_NO_RESPONSE",
                                "message": "URL did not return a response before processing stopped.",
                            }
                        )
                        if return_metadata:
                            _append_link_result()
                        continue

                    body_text = self._read_body_text(page)
                    effective_page_url = self._browser_page_url(page)
                    link_meta["resolved_url"] = effective_page_url
                    link_meta["body_excerpt"] = body_text[:800]
                    link_meta["page_title"] = self._safe_page_title(page)
                    self._ensure_browser_page_secure(page)

                    reason_code, reason_message = self._classify_generic_page(
                        effective_page_url,
                        link_meta["page_title"],
                        body_text,
                        getattr(response, "status", None),
                    )
                    if reason_code:
                        log_level = logging.error if reason_code == "URL_AUTH_WALL_DETECTED" else logging.info
                        log_level("%s at %s", reason_code, safe_log_url)
                        self._append_process_log("INFO", reason_code, url)
                        link_meta.update(
                            {
                                "status": "skipped" if reason_code == "URL_NON_INVOICE_PAGE_SKIPPED" else "failed",
                                "reason_code": reason_code,
                                "message": reason_message,
                            }
                        )
                        if return_metadata:
                            _append_link_result()
                        continue

                    logging.info("Generating PDF from webpage...")
                    self._ensure_browser_page_secure(page)
                    page.pdf(path=pdf_path, format="A4", print_background=True)
                    self._ensure_browser_page_secure(page)
                    if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 1024:
                        link_meta.update({"status": "downloaded", "download_mode": "page_pdf"})
                        _append_link_result()
                    else:
                        logging.warning("Generated PDF is empty or missing: %s", pdf_path)
                        link_meta.update(
                            {
                                "status": "failed",
                                "reason_code": "URL_DOWNLOAD_FAILED",
                                "message": "Generated webpage PDF was empty or missing after page render.",
                            }
                        )
                        if return_metadata:
                            _append_link_result()
                except PublicUrlPolicyError as exc:
                    if os.path.isfile(pdf_path):
                        try:
                            os.remove(pdf_path)
                        except OSError:
                            pass
                    logging.error("%s", exc)
                    link_meta.update(
                        {
                            "source_url": exc.safe_url,
                            "resolved_url": exc.safe_url,
                            "status": "failed",
                            "reason_code": "URL_POLICY_REJECTED",
                            "message": exc.reason,
                        }
                    )
                    if return_metadata:
                        _append_link_result()
                except PlaywrightTimeoutError:
                    logging.error("Timeout (%sms) while loading %s", self.generic_timeout_ms, safe_log_url)
                    self._append_process_log("ERROR", "URL_PAGE_TIMEOUT", url)
                    link_meta.update(
                        {
                            "status": "failed",
                            "reason_code": "URL_PAGE_TIMEOUT",
                            "message": f"URL timed out after {self.generic_timeout_ms}ms before a usable invoice page was available.",
                        }
                    )
                    if return_metadata:
                        _append_link_result()
                except Exception as exc:
                    logging.error(
                        "Error processing URL %s (%s)",
                        safe_log_url,
                        type(exc).__name__,
                    )
                    link_meta.update(
                        {
                            "status": "failed",
                            "reason_code": "URL_DOWNLOAD_FAILED",
                            "message": "URL processing failed before a usable invoice PDF was available.",
                        }
                    )
                    if return_metadata:
                        _append_link_result()
                finally:
                    link_meta["timing_ms"] = {"total_ms": self._elapsed_ms(link_started_at)}
                    if not page.is_closed():
                        page.close()

            browser.close()

        if return_metadata:
            return downloaded_items
        return [item["pdf_path"] for item in downloaded_items]

    def process_all_in_staging(self, email_results):
        logging.info("Starting PDF standardization and link processing (Module 2)")
        all_processed_files = []

        for email_data in email_results:
            email_id = email_data["email_id"]
            subject = email_data["subject"]
            attachments = email_data.get("attachments", [])
            processed_attachments = []

            for attach in attachments:
                ext = os.path.splitext(attach)[1].lower()
                if ext in [".jpg", ".jpeg", ".png"]:
                    pdf_result = self.convert_image_to_pdf(attach)
                    if pdf_result:
                        processed_attachments.append(pdf_result)
                elif ext == ".pdf":
                    processed_attachments.append(attach)

            body_text = email_data.get("body_text", "")
            if body_text:
                processed_attachments.extend(self.process_invoice_links(body_text, subject, email_id))

            email_data["standardized_pdfs"] = processed_attachments
            all_processed_files.extend(processed_attachments)

        logging.info("Module 2 complete. Total standardized PDFs: %s", len(all_processed_files))
        return email_results

    def convert_image_to_pdf(self, image_path):
        if not os.path.exists(image_path):
            return None

        ext = os.path.splitext(image_path)[1].lower()
        if ext not in [".jpg", ".jpeg", ".png"]:
            return image_path

        try:
            pdf_path = os.path.splitext(image_path)[0] + ".pdf"
            doc = fitz.open()
            img = fitz.open(image_path)
            rect = img[0].rect
            pdfbytes = img.convert_to_pdf()
            img.close()

            img_pdf = fitz.open("pdf", pdfbytes)
            page = doc.new_page(width=rect.width, height=rect.height)
            page.show_pdf_page(rect, img_pdf, 0)
            doc.save(pdf_path)
            doc.close()

            logging.info("Successfully converted image to PDF: %s", os.path.basename(pdf_path))
            return pdf_path
        except Exception as exc:
            logging.error("Failed to convert image %s to PDF: %s", image_path, exc)
            return image_path


if __name__ == "__main__":
    converter = PDFConverter()
    os.makedirs("staging", exist_ok=True)
    test_img = os.path.join("staging", "test_receipt.jpg")
    try:
        from PIL import Image

        img = Image.new("RGB", (100, 100), color="white")
        img.save(test_img)
        print("Testing image to PDF conversion...")
        print(converter.convert_image_to_pdf(test_img))
    except Exception as exc:
        print(f"Skipping image to PDF test: {exc}")

    print("\nTesting Playwright URL extraction...")
    sample_subject = "滴滴出行行程单与发票"
    sample_text = "您好，这里是发票下载链接：https://invoice.didiglobal.com/receipt/download?id=dummy123"
    print(converter.process_invoice_links(sample_text, sample_subject, "test_mail_001"))
