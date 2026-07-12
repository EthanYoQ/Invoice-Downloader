import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlparse

from requests.structures import CaseInsensitiveDict

from app_api import InvoiceAppAPI, build_processing_history_key
import pdf_converter
from email_fetcher import _build_link_candidate_decision
from pdf_converter import PDFConverter
from provider_direct_invoice import infer_direct_invoice_family
from url_security import PublicUrlPolicy, PublicUrlPolicyError


PUBLIC_ADDRESS = "93.184.216.34"


def public_test_policy(peer_address=PUBLIC_ADDRESS):
    peer = peer_address if isinstance(peer_address, tuple) else (peer_address, 443)
    return PublicUrlPolicy(
        resolver=lambda host, port: [PUBLIC_ADDRESS],
        peer_getter=lambda response: peer,
        proxy_endpoint=None,
    )


def fake_ip_proxy_policy(peer=("127.0.0.1", 7897)):
    return PublicUrlPolicy(
        resolver=lambda host, port: ["198.18.0.42"],
        public_resolver=lambda host, port: [PUBLIC_ADDRESS],
        peer_getter=lambda response: peer,
        proxy_endpoint=("127.0.0.1", 7897),
    )


class FakeResponse:
    def __init__(self, url, content=b"", headers=None, status_code=200, json_data=None):
        self.url = url
        self._content = content
        self.headers = headers or {}
        self.status_code = status_code
        self._json_data = json_data
        self.content_reads = 0
        self.close_calls = 0
        self.text = content.decode("utf-8", errors="replace") if isinstance(content, bytes) else str(content)

    @property
    def content(self):
        self.content_reads += 1
        return self._content

    def json(self):
        if self._json_data is not None:
            return self._json_data
        return json.loads(self.text)

    def close(self):
        self.close_calls += 1


class FakeSession:
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def get(self, url, **kwargs):
        parsed = urlparse(url)
        if "sdapi.fpyun.com.cn" in parsed.netloc:
            return FakeResponse(
                url,
                headers={"Location": "https://fp.baiwang.com/format/d"},
                status_code=302,
            )
        if "fp.baiwang.com" in parsed.netloc:
            return FakeResponse(
                url,
                b"%PDF-1.5\nfpyun pdf",
                {"Content-Type": "application/pdf;charset=utf-8"},
            )
        if "files.pdd-fapiao.com" in parsed.netloc and "/pdf/" in parsed.path:
            return FakeResponse(url, b"%PDF-1.5\npdd pdf", {"Content-Type": "application/pdf"})
        if "eicore-invoice-" in parsed.netloc and parsed.path.endswith(".pdf"):
            return FakeResponse(url, b"%PDF-1.5\njd pdf", {"Content-Type": "application/pdf"})
        if "etd.kpbyd.com" in parsed.netloc and "fileCode=" in parsed.query and parsed.query.endswith("_pdf"):
            return FakeResponse(url, b"%PDF-1.5\nkpbyd pdf", {"Content-Type": "application/pdf"})
        if url == "https://nnfp.jss.com.cn/71ykyWlR=C-18aNx":
            return FakeResponse(
                url,
                headers={
                    "Location": "https://nnfp.jss.com.cn/scan-invoice/printQrcode?paramList=91430103MABWFD0J9B!!!26060100373102829862!false&aliView=true&shortLinkSource=1&wxApplet=0"
                },
                status_code=302,
            )
        if "nnfp.jss.com.cn/scan-invoice/printQrcode" in url:
            return FakeResponse(
                url,
                b"<html></html>",
                {"Content-Type": "text/html; charset=utf-8"},
            )
        if "nuonuo.pdf" in url:
            return FakeResponse(url, b"%PDF-1.5\nnuonuo pdf", {"Content-Type": "application/pdf"})
        if "nuonuo.xml" in url:
            return FakeResponse(url, NUONUO_XML.encode("utf-8"), {"Content-Type": "application/xml"})
        if "baiwang.com/bwmg/mix/bw/downloadFormat" in url and "formatType=PDF" in url:
            return FakeResponse(url, b"%PDF-1.5\nbaiwang pdf", {"Content-Type": "text/pdf;charset=utf8"})
        if "baiwang.com/bwmg/mix/bw/downloadFormat" in url and "formatType=XML" in url:
            return FakeResponse(url, BAIWANG_XML.encode("utf-8"), {"Content-Type": "text/xml;charset=utf8"})
        if "baiwang.com/bwmg/mix/bw/downloadFormat" in url and "formatType=OFD" in url:
            return FakeResponse(url, b"PK\x03\x04ofd", {"Content-Type": "text/ofd;charset=utf8"})
        return FakeResponse(url, b"<html></html>", {"Content-Type": "text/html"})

    def post(self, url, data=None, headers=None, **kwargs):
        if "getIvcDetailShow.do" in url:
            return FakeResponse(
                url,
                json.dumps({
                    "status": "0000",
                    "data": {
                        "invoiceSimpleVo": {
                            "fphm": "26432000001233579481",
                            "saleName": "长沙楼上餐饮管理有限公司",
                            "buyername": "辉瑞投资有限公司",
                            "orderTotal": 399.40,
                            "invoiceDate": "2026-06-01 00:37:12",
                            "url": "https://inv.jss.com.cn/nuonuo.pdf",
                            "xmlUrl": "https://storage.nuonuo.com/nuonuo.xml",
                        }
                    },
                }).encode("utf-8"),
                {"Content-Type": "application/json;charset=utf-8"},
            )
        if "previewInvoiceQd" in url:
            return FakeResponse(
                url,
                json.dumps({"success": True, "total": "1", "data": [{"invoiceNo": "26432000001239781576"}]}).encode("utf-8"),
                {"Content-Type": "application/json"},
                json_data={"success": True, "total": "1", "data": [{"invoiceNo": "26432000001239781576"}]},
            )
        return FakeResponse(url, b"{}", {"Content-Type": "application/json"})


class FakeRequests:
    @staticmethod
    def Session():
        return FakeSession()


class RecordingSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.responses.pop(0)


class FakeRoute:
    def __init__(self):
        self.action = None
        self.continue_calls = 0

    def continue_(self):
        self.continue_calls += 1
        self.action = "continue"

    def abort(self, reason=None):
        self.action = ("abort", reason)

    def fulfill(self, **kwargs):
        self.action = ("fulfill", kwargs)


class FakeBrowserRequest:
    def __init__(
        self,
        url,
        page=None,
        method="GET",
        headers=None,
        post_data_buffer=None,
        resource_type="document",
    ):
        self.url = url
        self.frame = FakeFrame(page) if page is not None else None
        self.method = method
        self.headers = headers or {}
        self.post_data_buffer = post_data_buffer
        self.resource_type = resource_type

    def all_headers(self):
        return dict(self.headers)


class FakeFrame:
    def __init__(self, page):
        self.page = page


class FakeBrowserPage:
    def __init__(self):
        self.closed = False

    def is_closed(self):
        return self.closed


class FakeNavigationPage(FakeBrowserPage):
    def __init__(self, response):
        super().__init__()
        self.response = response

    def goto(self, url, **kwargs):
        self.response.request = FakeBrowserRequest(url, self)
        return self.response


class FakeEventPage(FakeBrowserPage):
    def __init__(self):
        super().__init__()
        self.handlers = {}

    def on(self, event, callback):
        self.handlers[event] = callback

    def emit(self, event, value):
        self.handlers[event](value)


class FakeWebSocketRoute:
    def __init__(self, url="wss://public.example/socket"):
        self.url = url
        self.closed = None

    def close(self, **kwargs):
        self.closed = kwargs


class FakeBrowserContext:
    def __init__(self, options):
        self.options = options
        self.routes = []

    def route(self, pattern, callback):
        self.routes.append(("http", pattern, callback))

    def route_web_socket(self, pattern, callback):
        self.routes.append(("websocket", pattern, callback))

    def on(self, event, callback):
        return None


class FakeBrowser:
    def __init__(self):
        self.context = None

    def new_context(self, **kwargs):
        self.context = FakeBrowserContext(kwargs)
        return self.context


class FakeDomPage:
    def locator(self, selector):
        return self

    def evaluate_all(self, script):
        return [
            "https://public.example/invoice.pdf?token=public-secret",
            "http://127.0.0.1/admin?token=private-secret",
        ]

    def content(self):
        return ""


class FakeBrowserResponse(FakeResponse):
    def __init__(self, url, peer_address, peer_port=443, request=None, **kwargs):
        super().__init__(url, **kwargs)
        self._peer_address = peer_address
        self._peer_port = peer_port
        self.request = request

    def server_addr(self):
        if self._peer_address is None:
            return None
        return {"ipAddress": self._peer_address, "port": self._peer_port}

    def body(self):
        return self.content


class ExplodingBodyResponse(FakeResponse):
    @property
    def content(self):
        self.content_reads += 1
        raise RuntimeError("body read failed")


class ExplodingServerAddressResponse(FakeBrowserResponse):
    def server_addr(self):
        raise RuntimeError("server address unavailable")


class FakePinnedResponse:
    def __init__(
        self,
        url,
        status_code=200,
        headers=None,
        content=b"",
        set_cookie_headers=(),
    ):
        self.url = url
        self.status_code = status_code
        self.headers = headers or {}
        self.content = content
        self.set_cookie_headers = tuple(set_cookie_headers)

    @property
    def text(self):
        return self.content.decode("utf-8", errors="replace")

    def json(self):
        return json.loads(self.text)

    def close(self):
        return None


class RecordingPinnedTransport:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.calls = []

    def request(self, session, method, target, **kwargs):
        self.calls.append((session, method, target, kwargs))
        if self.error is not None:
            raise self.error
        return self.response


class SessionFakePinnedTransport:
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
        max_response_bytes=None,
        allow_direct_source_fallback=False,
    ):
        del (
            suppress_auth,
            decode_content,
            max_response_bytes,
            allow_direct_source_fallback,
        )
        kwargs = {
            "headers": dict(headers or {}),
            "allow_redirects": False,
            "stream": True,
            "timeout": timeout,
        }
        request_data = body if body is not None else data
        if request_data is not None:
            kwargs["data"] = request_data
        if json is not None:
            kwargs["json"] = json
        if files is not None:
            kwargs["files"] = files
        if params is not None:
            kwargs["params"] = params
        source = getattr(session, method.lower())(target.url, **kwargs)
        try:
            status = int(getattr(source, "status_code", 0) or 0)
            content = b"" if status in {301, 302, 303, 307, 308} else source.content
            return FakePinnedResponse(
                target.url,
                status_code=status,
                headers=CaseInsensitiveDict(getattr(source, "headers", {}) or {}),
                content=content,
                set_cookie_headers=(
                    (getattr(source, "headers", {}) or {}).get("Set-Cookie"),
                ) if (getattr(source, "headers", {}) or {}).get("Set-Cookie") else (),
            )
        finally:
            source.close()


NUONUO_XML = """<?xml version="1.0" encoding="utf-8"?>
<EInvoice><SellerName>长沙楼上餐饮管理有限公司</SellerName><BuyerName>辉瑞投资有限公司</BuyerName>
<TotalTax-includedAmount>399.40</TotalTax-includedAmount><InvoiceNumber>26432000001233579481</InvoiceNumber>
<IssueTime>2026-06-01</IssueTime></EInvoice>"""

BAIWANG_XML = """<?xml version="1.0" encoding="utf-8"?>
<EInvoice><SellerName>湖南运达酒店管理有限公司长沙运达喜来登酒店</SellerName><BuyerName>辉瑞投资有限公司</BuyerName>
<TotalTax-includedAmount>1950.00</TotalTax-includedAmount><InvoiceNumber>26432000001239781576</InvoiceNumber>
<IssueTime>2026-06-01</IssueTime></EInvoice>"""


class ProviderUrlRecoveryTests(unittest.TestCase):
    def setUp(self):
        self._original_pinned_transport = pdf_converter.PinnedHttpTransport
        pdf_converter.PinnedHttpTransport = SessionFakePinnedTransport

    def tearDown(self):
        pdf_converter.PinnedHttpTransport = self._original_pinned_transport

    def test_private_direct_candidate_is_rejected_before_session_connection(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        session = RecordingSession([])

        artifacts, logs = converter._probe_direct_invoice_artifact(
            session,
            "http://127.0.0.1/invoice.pdf?token=private-secret",
            str(Path(converter.staging_dir) / "private"),
        )

        self.assertEqual(artifacts, [])
        self.assertEqual(session.calls, [])
        self.assertEqual(logs[0]["kind"], "URL_POLICY_REJECTED")
        self.assertNotIn("private-secret", json.dumps(logs))

    def test_public_request_rejects_private_redirect_without_following_it(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        redirect_response = FakeResponse(
            "https://public.example/start",
            status_code=302,
            headers={"Location": "http://127.0.0.1/admin?value=private-secret"},
        )
        session = RecordingSession([redirect_response])

        with self.assertRaises(PublicUrlPolicyError) as caught:
            converter._request_public(session, "GET", "https://public.example/start")

        self.assertEqual(len(session.calls), 1)
        self.assertFalse(session.calls[0][2]["allow_redirects"])
        self.assertNotIn("private-secret", str(caught.exception))
        self.assertEqual(session.responses, [])
        self.assertEqual(session.calls[0][0], "GET")
        self.assertEqual(redirect_response.close_calls, 1)
        self.assertEqual(redirect_response.content_reads, 0)

    def test_public_request_uses_pinned_transport_without_session_network_method(self):
        response = FakePinnedResponse(
            "https://public.example/invoice.pdf",
            content=b"%PDF-1.5\npublic",
        )
        transport = RecordingPinnedTransport(response=response)
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        result = converter._request_public(
            object(), "GET", "https://public.example/invoice.pdf"
        )

        self.assertEqual(result.content, b"%PDF-1.5\npublic")
        self.assertEqual(len(transport.calls), 1)

    def test_direct_source_fallback_is_scoped_to_fpyun_download_host(self):
        response = FakePinnedResponse(
            "https://sdapi.fpyun.com.cn/invoice.pdf",
            content=b"%PDF-1.5\npublic",
            headers={"Content-Type": "application/pdf"},
        )
        transport = RecordingPinnedTransport(response=response)
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        converter._probe_direct_invoice_artifact(
            object(),
            "https://sdapi.fpyun.com.cn/invoice/qd/download/"
            "getInvoiceFile?fptqm=opaque&type=1",
            str(Path(converter.staging_dir) / "fpyun"),
        )
        converter._probe_direct_invoice_artifact(
            object(),
            "https://public.example/invoice.pdf",
            str(Path(converter.staging_dir) / "ordinary"),
        )

        assert transport.calls[0][3]["allow_direct_source_fallback"] is True
        assert transport.calls[1][3]["allow_direct_source_fallback"] is False

    def test_direct_source_fallback_requires_exact_secure_fpyun_entry(self):
        response = FakePinnedResponse(
            "https://sdapi.fpyun.com.cn/other",
            content=b"<html></html>",
            headers={"Content-Type": "text/html"},
        )
        transport = RecordingPinnedTransport(response=response)
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        converter._probe_direct_invoice_artifact(
            object(),
            "https://sdapi.fpyun.com.cn/other?fptqm=opaque&type=1",
            str(Path(converter.staging_dir) / "wrong-path"),
        )

        assert transport.calls[0][3]["allow_direct_source_fallback"] is False

    def test_http_fpyun_entry_is_not_a_direct_invoice_provider(self):
        assert infer_direct_invoice_family(
            "http://sdapi.fpyun.com.cn/invoice/qd/download/"
            "getInvoiceFile?fptqm=opaque&type=1"
        ) == ""

    def test_fpyun_legacy_redirect_is_pinned_and_final_download_is_upgraded(self):
        class RedirectTransport:
            def __init__(self):
                self.targets = []

            def request(self, session, method, target, **kwargs):
                self.targets.append(target)
                if len(self.targets) == 1:
                    return FakePinnedResponse(
                        target.url,
                        status_code=302,
                        headers={
                            "Location": (
                                "http://93.184.216.34:7100/qd/download/"
                                "getInvoiceFile?fptqm=opaque&type=1"
                            )
                        },
                    )
                if len(self.targets) == 2:
                    return FakePinnedResponse(
                        target.url,
                        status_code=302,
                        headers={
                            "Location": (
                                "http://fp.baiwang.com/format/d?param=opaque"
                            )
                        },
                    )
                return FakePinnedResponse(
                    target.url,
                    content=b"%PDF-1.5\ninvoice",
                    headers={"Content-Type": "application/pdf"},
                )

        transport = RedirectTransport()
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        artifacts, logs = converter._probe_direct_invoice_artifact(
            object(),
            "https://sdapi.fpyun.com.cn/invoice/qd/download/"
            "getInvoiceFile?fptqm=opaque&type=1",
            str(Path(converter.staging_dir) / "fpyun"),
        )

        assert logs == []
        assert len(artifacts) == 1
        assert transport.targets[1].host == PUBLIC_ADDRESS
        assert transport.targets[1].port == 7100
        assert transport.targets[2].url.startswith(
            "https://fp.baiwang.com/format/d?"
        )

    def test_fpyun_direct_source_fallback_is_limited_to_validated_chain_hops(self):
        class RedirectTransport:
            def __init__(self):
                self.calls = []

            def request(self, session, method, target, **kwargs):
                self.calls.append((target, kwargs))
                if len(self.calls) == 1:
                    return FakePinnedResponse(
                        target.url,
                        status_code=302,
                        headers={
                            "Location": (
                                "http://93.184.216.34:7100/qd/download/"
                                "getInvoiceFile?fptqm=opaque&type=1"
                            )
                        },
                    )
                if len(self.calls) == 2:
                    return FakePinnedResponse(
                        target.url,
                        status_code=302,
                        headers={"Location": "http://fp.baiwang.com/format/d?param=opaque"},
                    )
                return FakePinnedResponse(
                    target.url,
                    content=b"%PDF-1.5\ninvoice",
                    headers={"Content-Type": "application/pdf"},
                )

        transport = RedirectTransport()
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        artifacts, logs = converter._probe_direct_invoice_artifact(
            object(),
            "https://sdapi.fpyun.com.cn/invoice/qd/download/"
            "getInvoiceFile?fptqm=opaque&type=1",
            str(Path(converter.staging_dir) / "fpyun"),
        )

        assert logs == []
        assert len(artifacts) == 1
        assert transport.calls[0][1]["allow_direct_source_fallback"] is True
        assert transport.calls[1][1]["allow_direct_source_fallback"] is True
        assert transport.calls[2][1]["allow_direct_source_fallback"] is True

    def test_fpyun_final_baiwang_download_is_terminal(self):
        class RedirectTransport:
            def __init__(self):
                self.calls = []

            def request(self, session, method, target, **kwargs):
                self.calls.append((target, kwargs))
                if len(self.calls) == 1:
                    return FakePinnedResponse(
                        target.url,
                        status_code=302,
                        headers={"Location": "https://fp.baiwang.com/format/d?param=opaque"},
                    )
                return FakePinnedResponse(
                    target.url,
                    status_code=302,
                    headers={"Location": "https://unrelated.example/invoice.pdf"},
                )

        transport = RedirectTransport()
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        artifacts, logs = converter._probe_direct_invoice_artifact(
            object(),
            "https://sdapi.fpyun.com.cn/invoice/qd/download/"
            "getInvoiceFile?fptqm=opaque&type=1",
            str(Path(converter.staging_dir) / "fpyun"),
        )

        assert artifacts == []
        assert logs[0]["kind"] == "URL_POLICY_REJECTED"
        assert len(transport.calls) == 2

    def test_fpyun_legacy_redirect_rejects_changed_query(self):
        transport = RecordingPinnedTransport(
            response=FakePinnedResponse(
                "https://sdapi.fpyun.com.cn/start",
                status_code=302,
                headers={
                    "Location": (
                        "http://93.184.216.34:7100/qd/download/"
                        "getInvoiceFile?fptqm=changed&type=1"
                    )
                },
            )
        )
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        artifacts, logs = converter._probe_direct_invoice_artifact(
            object(),
            "https://sdapi.fpyun.com.cn/invoice/qd/download/"
            "getInvoiceFile?fptqm=opaque&type=1",
            str(Path(converter.staging_dir) / "fpyun"),
        )

        assert artifacts == []
        assert logs[0]["kind"] == "URL_POLICY_REJECTED"

    def test_public_request_buffers_body_then_closes_source_response(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        source_response = FakeResponse(
            "https://public.example/document",
            content=b"public body",
            headers={"Content-Type": "application/octet-stream"},
        )

        buffered = converter._request_public(
            RecordingSession([source_response]),
            "GET",
            "https://public.example/document",
        )

        self.assertEqual(buffered.content, b"public body")
        self.assertEqual(source_response.content_reads, 1)
        self.assertEqual(source_response.close_calls, 1)

    def test_public_request_closes_response_when_body_read_fails(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        source_response = ExplodingBodyResponse("https://public.example/document")

        with self.assertRaisesRegex(RuntimeError, "body read failed"):
            converter._request_public(
                RecordingSession([source_response]),
                "GET",
                "https://public.example/document",
            )

        self.assertEqual(source_response.close_calls, 1)

    def test_redirect_matrix_preserves_requests_method_semantics_and_closes_hops(self):
        for method in ("GET", "POST"):
            for status in (301, 302, 303, 307, 308):
                with self.subTest(method=method, status=status):
                    first = FakeResponse(
                        "https://public.example/start",
                        status_code=status,
                        headers={"Location": "https://cdn.example/final"},
                    )
                    second = FakeResponse(
                        "https://cdn.example/final", content=b"finished"
                    )
                    session = RecordingSession([first, second])
                    converter = PDFConverter(
                        staging_dir=tempfile.mkdtemp(),
                        url_policy=public_test_policy(
                            peer_address=("142.250.72.14", 443)
                        ),
                    )
                    kwargs = {
                        "headers": {
                            "Authorization": "Bearer synthetic-secret",
                            "Cookie": "session=synthetic-secret",
                            "Content-Type": "application/json",
                            "Content-Length": "2",
                            "X-Keep": "yes",
                        }
                    }
                    if method == "POST":
                        kwargs["data"] = b"{}"

                    result = converter._request_public(
                        session, method, "https://public.example/start", **kwargs
                    )

                    expected_second_method = (
                        "GET"
                        if method == "POST" and status in {301, 302, 303}
                        else method
                    )
                    self.assertEqual(
                        [call[0] for call in session.calls],
                        [method, expected_second_method],
                    )
                    redirected_headers = session.calls[1][2]["headers"]
                    expected_headers = {"X-Keep": "yes"}
                    if expected_second_method == method:
                        expected_headers.update(
                            {
                                "Content-Type": "application/json",
                                "Content-Length": "2",
                            }
                        )
                    self.assertEqual(redirected_headers, expected_headers)
                    if expected_second_method == "GET":
                        self.assertNotIn("data", session.calls[1][2])
                    else:
                        self.assertEqual(session.calls[1][2].get("data"), kwargs.get("data"))
                    self.assertEqual(result.content, b"finished")
                    self.assertEqual(first.close_calls, 1)
                    self.assertEqual(second.close_calls, 1)

    def test_same_origin_post_to_get_redirect_strips_only_entity_headers(self):
        first = FakeResponse(
            "https://public.example/start",
            status_code=302,
            headers={"Location": "/final"},
        )
        second = FakeResponse("https://public.example/final", content=b"finished")
        session = RecordingSession([first, second])
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )

        converter._request_public(
            session,
            "POST",
            "https://public.example/start",
            data=b"{}",
            headers={
                "Authorization": "Bearer synthetic-secret",
                "Cookie": "session=synthetic-secret",
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
        )

        self.assertEqual(
            session.calls[1][2]["headers"],
            {
                "Authorization": "Bearer synthetic-secret",
                "Cookie": "session=synthetic-secret",
            },
        )

    def test_redirect_uses_fresh_validation_context_for_each_hop(self):
        resolved_hosts = []

        def resolver(host, port):
            resolved_hosts.append(host)
            return [PUBLIC_ADDRESS]

        policy = PublicUrlPolicy(
            resolver=resolver,
            peer_getter=lambda response: ("142.250.72.14", 443),
            proxy_endpoint=None,
        )
        converter = PDFConverter(staging_dir=tempfile.mkdtemp(), url_policy=policy)
        session = RecordingSession(
            [
                FakeResponse(
                    "https://public.example/start",
                    status_code=307,
                    headers={"Location": "https://cdn.example/final"},
                ),
                FakeResponse("https://cdn.example/final", content=b"finished"),
            ]
        )

        converter._request_public(session, "GET", "https://public.example/start")

        self.assertEqual(resolved_hosts, ["public.example", "cdn.example"])

    def test_fake_ip_provider_request_succeeds_through_attested_explicit_proxy(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=fake_ip_proxy_policy()
        )
        response = FakeResponse(
            "https://pis.baiwang.com/document",
            content=b"%PDF-1.5\npublic",
            headers={"Content-Type": "application/pdf"},
        )

        artifacts, logs = converter._probe_direct_artifact(
            RecordingSession([response]),
            "https://pis.baiwang.com/document",
            str(Path(converter.staging_dir) / "proxy"),
        )

        self.assertEqual(logs, [])
        self.assertEqual(len(artifacts), 1)
        self.assertEqual(response.close_calls, 1)

    def test_browser_route_aborts_private_document_or_resource_requests(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        for url in (
            "http://127.0.0.1/document",
            "http://10.0.0.5/private-resource.js?token=private-secret",
        ):
            with self.subTest(url=url):
                route = FakeRoute()
                converter._guard_browser_request(route, FakeBrowserRequest(url))
                self.assertEqual(route.action[0], "abort")

        route = FakeRoute()
        converter._browser_http_session = FakeSession()
        converter._guard_browser_request(
            route, FakeBrowserRequest("https://public.example/invoice")
        )
        self.assertEqual(route.action[0], "fulfill")
        self.assertEqual(route.continue_calls, 0)

    def test_browser_route_fulfills_from_pinned_transport_without_browser_origin_network(self):
        response = FakePinnedResponse(
            "https://public.example/document",
            status_code=200,
            headers={
                "Content-Type": "text/html; charset=utf-8",
                "Set-Cookie": "provider_session=abc; Path=/; Secure",
                "X-Provider": "yes",
            },
            content=b"<html>public</html>",
            set_cookie_headers=(
                "provider_session=abc; Path=/; Secure",
                "provider_second=two; Path=/; Secure",
            ),
        )
        transport = RecordingPinnedTransport(response=response)
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )
        converter._browser_http_session = object()
        page = FakeBrowserPage()
        request = FakeBrowserRequest(
            "https://public.example/document",
            page,
            method="POST",
            headers={"Cookie": "browser_cookie=one", "X-Request": "yes"},
            post_data_buffer=b"request-body",
        )
        route = FakeRoute()

        converter._guard_browser_request(route, request)

        self.assertEqual(route.action[0], "fulfill")
        fulfilled = route.action[1]
        self.assertEqual(fulfilled["status"], 200)
        self.assertEqual(fulfilled["body"], b"<html>public</html>")
        self.assertEqual(
            fulfilled["headers"]["Set-Cookie"],
            "provider_session=abc; Path=/; Secure\nprovider_second=two; Path=/; Secure",
        )
        self.assertEqual(route.continue_calls, 0)
        _, method, target, kwargs = transport.calls[0]
        self.assertEqual(method, "POST")
        self.assertEqual(target.host, "public.example")
        self.assertEqual(kwargs["body"], b"request-body")
        self.assertEqual(kwargs["headers"]["Cookie"], "browser_cookie=one")

    def test_browser_and_direct_requests_apply_explicit_resource_size_bounds(self):
        response = FakePinnedResponse(
            "https://public.example/document",
            headers={"Content-Type": "text/html"},
            content=b"ok",
        )
        transport = RecordingPinnedTransport(response=response)
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )
        converter._browser_http_session = object()

        for resource_type in ("document", "xhr", "script", "image", "font"):
            converter._guard_browser_request(
                FakeRoute(),
                FakeBrowserRequest(
                    f"https://public.example/{resource_type}",
                    resource_type=resource_type,
                ),
            )

        limits = {
            call[2].url.rsplit("/", 1)[-1]: call[3]["max_response_bytes"]
            for call in transport.calls
        }
        self.assertGreater(limits["document"], limits["script"])
        self.assertEqual(limits["document"], limits["xhr"])
        self.assertEqual(limits["script"], limits["image"])
        self.assertEqual(limits["image"], limits["font"])

        transport.calls.clear()
        converter._request_public(object(), "GET", "https://public.example/invoice.pdf")
        self.assertGreater(transport.calls[0][3]["max_response_bytes"], 0)

    def test_browser_route_pinned_failure_aborts_and_compromises_page(self):
        policy = public_test_policy()
        error = PublicUrlPolicyError(
            "https://public.example/private-capability?value=synthetic",
            "pinned transport failed",
        )
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=policy,
            pinned_transport=RecordingPinnedTransport(error=error),
        )
        converter._browser_http_session = object()
        page = FakeBrowserPage()
        route = FakeRoute()

        converter._guard_browser_request(
            route,
            FakeBrowserRequest("https://public.example/document", page),
        )

        self.assertEqual(route.action[0], "abort")
        with self.assertRaises(PublicUrlPolicyError):
            converter._ensure_browser_page_secure(page)

    def test_private_target_is_rejected_before_pinned_transport_receives_request(self):
        transport = RecordingPinnedTransport()
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
            pinned_transport=transport,
        )

        with self.assertRaises(PublicUrlPolicyError):
            converter._request_public(
                object(), "GET", "http://127.0.0.1/private-capability"
            )

        self.assertEqual(transport.calls, [])

    def test_unfulfilled_browser_response_marks_page_compromised_before_body_read(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        page = FakeBrowserPage()
        request = FakeBrowserRequest("https://public.example/document", page)
        response = FakeBrowserResponse(
            "https://public.example/document",
            "127.0.0.1",
            peer_port=7897,
            request=request,
            content=b"private response",
        )

        with self.assertRaises(PublicUrlPolicyError):
            converter._validate_browser_response(response)

        with self.assertRaises(PublicUrlPolicyError):
            converter._ensure_browser_page_secure(page)
        self.assertEqual(response.content_reads, 0)

    def test_generic_browser_navigation_discards_compromised_page(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        response = FakeBrowserResponse(
            "https://public.example/document", "10.0.0.8"
        )
        page = FakeNavigationPage(response)

        with self.assertRaises(PublicUrlPolicyError):
            converter._goto_public_page(page, "https://public.example/document", 1000)

        with self.assertRaises(PublicUrlPolicyError):
            converter._ensure_browser_page_secure(page)

    def test_generic_response_guard_marks_unfulfilled_subresource_compromised(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        page = FakeEventPage()
        request = FakeBrowserRequest("https://cdn.example/resource.js", page)
        converter._attach_browser_response_guard(page)

        page.emit(
            "response",
            FakeBrowserResponse(
                "https://cdn.example/resource.js",
                "127.0.0.1",
                peer_port=7897,
                request=request,
            ),
        )

        with self.assertRaises(PublicUrlPolicyError):
            converter._ensure_browser_page_secure(page)

    def test_compromised_browser_artifacts_are_removed_from_disk_and_candidates(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        retained = {"path": str(Path(converter.staging_dir) / "direct.pdf")}
        discarded_path = Path(converter.staging_dir) / "dom.pdf"
        discarded_path.write_bytes(b"%PDF-1.5\nsynthetic")
        artifacts = [retained, {"path": str(discarded_path)}]

        converter._discard_browser_artifacts(artifacts, 1)

        self.assertEqual(artifacts, [retained])
        self.assertFalse(discarded_path.exists())

    def test_browser_none_server_addr_is_accepted_only_as_fulfilled_response_metadata(self):
        response = FakePinnedResponse(
            "https://pis.baiwang.com/document",
            content=b"<html>public</html>",
        )
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=fake_ip_proxy_policy(),
            pinned_transport=RecordingPinnedTransport(response=response),
        )
        converter._browser_http_session = object()
        page = FakeBrowserPage()
        request = FakeBrowserRequest("https://pis.baiwang.com/document", page)
        converter._guard_browser_request(FakeRoute(), request)
        browser_response = FakeBrowserResponse(
            "https://pis.baiwang.com/document",
            None,
            request=request,
        )

        self.assertTrue(converter._validate_browser_response(browser_response))
        converter._ensure_browser_page_secure(page)

    def test_fulfilled_browser_response_never_calls_server_addr(self):
        pinned_response = FakePinnedResponse(
            "https://pis.baiwang.com/document",
            content=b"<html>public</html>",
        )
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=fake_ip_proxy_policy(),
            pinned_transport=RecordingPinnedTransport(response=pinned_response),
        )
        converter._browser_http_session = object()
        page = FakeBrowserPage()
        request = FakeBrowserRequest("https://pis.baiwang.com/document", page)
        converter._guard_browser_request(FakeRoute(), request)
        response = ExplodingServerAddressResponse(
            "https://pis.baiwang.com/document",
            None,
            request=request,
        )

        self.assertTrue(converter._validate_browser_response(response))

    def test_browser_websockets_are_blocked(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        route = FakeWebSocketRoute()

        converter._block_browser_websocket(route)

        self.assertIsNotNone(route.closed)
        self.assertEqual(route.closed.get("code"), 1008)

    def test_browser_context_keeps_network_enabled_but_installs_only_guarded_routes(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        browser = FakeBrowser()

        context = converter._new_browser_context(browser)

        self.assertNotIn("offline", context.options)
        self.assertEqual([kind for kind, _, _ in context.routes], ["http", "websocket"])

    def test_production_browser_launch_falls_back_to_supported_installed_browser(self):
        sentinel = object()

        class FakeChromium:
            def __init__(self):
                self.calls = []

            def launch(self, **kwargs):
                self.calls.append(kwargs)
                if "executable_path" not in kwargs:
                    raise pdf_converter.PlaywrightError("bundled browser missing")
                return sentinel

        class FakePlaywright:
            chromium = FakeChromium()

        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        with mock.patch.object(
            PDFConverter,
            "_installed_browser_executables",
            return_value=["installed-chrome.exe"],
            create=True,
        ):
            browser = converter._launch_chromium_browser(
                FakePlaywright(), "browser fallback regression"
            )

        self.assertIs(browser, sentinel)
        self.assertEqual(
            FakePlaywright.chromium.calls,
            [
                {"headless": True},
                {"headless": True, "executable_path": "installed-chrome.exe"},
            ],
        )

    def test_real_chrome_follows_fulfilled_document_and_subresource_redirects_without_origin_network(self):
        start_url = "https://route-proof.example/start"
        final_url = "https://route-proof.example/final"
        script_start_url = "https://assets-proof.example/script-start.js"
        script_final_url = "https://assets-proof.example/script-final.js"
        expected_chain = {
            start_url,
            final_url,
            script_start_url,
            script_final_url,
        }
        payloads = {
            start_url: FakePinnedResponse(
                start_url,
                status_code=302,
                headers={"Location": final_url},
            ),
            final_url: FakePinnedResponse(
                final_url,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=(
                    f"<html><body>final"
                    f"<script src='{script_start_url}'></script>"
                    "</body></html>"
                ).encode("utf-8"),
            ),
            script_start_url: FakePinnedResponse(
                script_start_url,
                status_code=302,
                headers={"Location": script_final_url},
            ),
            script_final_url: FakePinnedResponse(
                script_final_url,
                headers={"Content-Type": "application/javascript"},
                content=(
                    b"document.documentElement.dataset.redirectScript = 'executed';"
                ),
            ),
        }

        class MappingPinnedTransport:
            def __init__(self):
                self.calls = []

            def request(self, session, method, target, **kwargs):
                del session
                self.calls.append((method, target.url, kwargs))
                if target.url not in payloads:
                    raise AssertionError(f"unexpected browser request: {target.url}")
                return payloads[target.url]

        class AuditedRoute:
            def __init__(self, route, request_url, actions):
                self._route = route
                self._request_url = request_url
                self._actions = actions

            def fulfill(self, **kwargs):
                self._actions["fulfill"].append(self._request_url)
                return self._route.fulfill(**kwargs)

            def abort(self, reason=None):
                self._actions["abort"].append(self._request_url)
                return self._route.abort(reason)

            def continue_(self, **kwargs):
                self._actions["continue"].append(self._request_url)
                return self._route.continue_(**kwargs)

            def fallback(self, **kwargs):
                self._actions["fallback"].append(self._request_url)
                return self._route.fallback(**kwargs)

        validation_calls = []
        policy = public_test_policy()
        original_validate = policy.validate

        def recording_validate(url):
            validation_calls.append(str(url))
            return original_validate(url)

        policy.validate = recording_validate
        transport = MappingPinnedTransport()
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            timeout_ms=10000,
            url_policy=policy,
            pinned_transport=transport,
        )
        actions = {name: [] for name in ("fulfill", "abort", "continue", "fallback")}
        original_guard = converter._guard_browser_request

        def audited_guard(route, request):
            return original_guard(AuditedRoute(route, request.url, actions), request)

        converter._guard_browser_request = audited_guard

        with pdf_converter.sync_playwright() as playwright:
            try:
                browser = converter._launch_chromium_browser(
                    playwright, "real Chrome redirect regression"
                )
            except RuntimeError:
                self.skipTest("no production-supported Chrome or Edge is installed")
            context = converter._new_browser_context(browser)
            page = context.new_page()
            try:
                response = converter._goto_public_page(page, start_url, 10000)
                page.wait_for_function(
                    "document.documentElement.dataset.redirectScript === 'executed'",
                    timeout=10000,
                )
                self.assertEqual(response.url, final_url)
                self.assertEqual(page.url, final_url)
                converter._ensure_browser_page_secure(page)
            except Exception as exc:
                raise AssertionError(
                    f"chrome redirect failure; actions={actions}; "
                    f"pinned_calls={transport.calls}; "
                    f"rejections={converter._browser_policy_rejections}"
                ) from exc
            finally:
                context.close()
                browser.close()

        pinned_urls = [url for _, url, _ in transport.calls]
        self.assertEqual(set(pinned_urls), expected_chain)
        self.assertEqual(
            set(actions["fulfill"]),
            {start_url, final_url, script_start_url},
        )
        self.assertEqual(actions["continue"], [])
        self.assertEqual(actions["fallback"], [])
        self.assertEqual(actions["abort"], [])
        self.assertTrue(expected_chain.issubset(set(validation_calls)))

    def test_dom_private_urls_are_removed_before_provider_probe(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        logs = []

        urls = converter._validated_dom_urls(
            FakeDomPage(), "https://public.example/start", logs
        )

        self.assertEqual(
            urls, ["https://public.example/invoice.pdf?token=public-secret"]
        )
        self.assertEqual(logs[0]["kind"], "URL_POLICY_REJECTED")
        self.assertNotIn("private-secret", json.dumps(logs))

    def test_captured_private_response_url_is_rejected_before_body_read(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        response = FakeBrowserResponse(
            "http://127.0.0.1/invoice.pdf?token=private-secret",
            "127.0.0.1",
            content=b"%PDF-1.5\nprivate",
            headers={"content-type": "application/pdf"},
        )

        with self.assertRaises(PublicUrlPolicyError):
            converter._capture_direct_invoice_response_artifact(
                response,
                str(Path(converter.staging_dir) / "capture"),
                1,
                "https://public.example/start",
            )
        self.assertEqual(response.content_reads, 0)

    def test_unattributed_unfulfilled_browser_response_compromises_context(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        response = FakeBrowserResponse(
            "https://public.example/invoice.pdf", "10.0.0.9"
        )

        with self.assertRaises(PublicUrlPolicyError):
            converter._validate_browser_response(response)
        with self.assertRaises(PublicUrlPolicyError):
            converter._ensure_browser_page_secure(FakeBrowserPage())

    def test_policy_and_process_logs_redact_path_query_and_subject(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy()
        )
        rejected_url = "http://127.0.0.1/private-capability?value=synthetic-secret"
        with self.assertRaises(PublicUrlPolicyError) as caught:
            converter.url_policy.validate(rejected_url)
        trace = json.dumps(converter._policy_rejection_log(caught.exception, "test"))
        self.assertNotIn("private-capability", trace)
        self.assertNotIn("synthetic-secret", trace)

        converter._append_process_log(
            "INFO",
            "URL_POLICY_REJECTED",
            rejected_url,
            subject="SYNTHETIC_MAILBOX_SUBJECT",
        )
        process_log = (Path(converter.staging_dir) / "process_log.txt").read_text(
            encoding="utf-8"
        )
        self.assertIn("http://127.0.0.1/<redacted>", process_log)
        self.assertNotIn("private-capability", process_log)
        self.assertNotIn("synthetic-secret", process_log)
        self.assertNotIn("SYNTHETIC_MAILBOX_SUBJECT", process_log)

    def test_provider_result_survives_exact_appapi_dict_copy_and_pdf_handoff(self):
        staging_dir = tempfile.mkdtemp()
        converter = PDFConverter(
            staging_dir=staging_dir, url_policy=public_test_policy()
        )
        pdf_path = str(Path(staging_dir) / "provider-result.pdf")
        Path(pdf_path).write_bytes(b"%PDF-1.5\nsynthetic provider result")
        runtime_metadata = {
            "status": "downloaded",
            "reason_code": "",
            "provider_family": "fpyun_direct_invoice",
            "pdf_path": pdf_path,
            "source_url": "https://public.example/capability?token=runtime-token",
            "resolved_url": "https://cdn.example/invoice.pdf?token=resolved-token",
            "selected_fields": {
                "buyer": "RUNTIME-BUYER",
                "seller": "RUNTIME-SELLER",
                "invoice_number": "RUNTIME-INVOICE-NUMBER",
            },
        }
        converter._recover_direct_invoice_group = (
            lambda *args, **kwargs: runtime_metadata
        )

        link_results = converter.process_invoice_links(
            "https://public.example/invoice",
            "RUNTIME-MAILBOX-SUBJECT",
            "synthetic-email-id",
            return_metadata=True,
            candidate_info={"provider_family": "fpyun_direct_invoice"},
        )

        # This is the exact production conversion at app_api.py:2585.
        app_link_result = dict(link_results[0] or {})
        self.assertEqual(app_link_result["pdf_path"], pdf_path)
        self.assertEqual(app_link_result["selected_fields"], runtime_metadata["selected_fields"])
        self.assertEqual(app_link_result["source_url"], runtime_metadata["source_url"])
        self.assertEqual(app_link_result["resolved_url"], runtime_metadata["resolved_url"])
        self.assertIs(type(link_results[0]), dict)

        handoff_paths = []
        api = InvoiceAppAPI()
        api._inspect_pdf_health = lambda path: handoff_paths.append(path) or {
            "pdf_health_class": "ok"
        }
        api._inspect_pdf_health(app_link_result["pdf_path"])
        self.assertEqual(handoff_paths, [pdf_path])
        self.assertTrue(Path(handoff_paths[0]).is_file())

    def test_direct_invoice_acceptance_normalizes_seller_parentheses(self):
        api = InvoiceAppAPI()
        api._extract_pdf_preview_text = lambda *args, **kwargs: ""

        result = api._evaluate_document_acceptance(
            {
                "provider_family": "pdd_direct_invoice",
                "provider_expected_fields": {
                    "invoice_number": "25322000000555648119",
                    "seller": "姑苏区平阊园苏式面馆（个体工商户）",
                },
            },
            {},
            {
                "InvoiceNumber": "25322000000555648119",
                "Seller": "姑苏区平阊园苏式面馆(个体工商户)",
            },
            {"pdf_health_class": "ok"},
            "unused.pdf",
        )

        self.assertTrue(result["accepted"], result)

    def test_fpyun_and_nuonuo_links_are_provider_candidates_not_controlled_run_non_provider_urls(self):
        cases = [
            (
                "https://sdapi.fpyun.com.cn/invoice/qd/download/getInvoiceFile?fptqm=LQ26T4BJR950&type=1",
                "下载pdf文件(推荐)",
                "【发票云】尊敬的【辉瑞投资投资有限公司】客户,您收到1张来自【杭州联郡餐饮管理有限公司】为您开具的电子发票【取票码:LQ26T4BJR950】【发票号码:26337000000517112500】",
                "fpyun_direct_invoice",
            ),
            (
                "https://nnfp.jss.com.cn/71ykyWlR=C-18aNx",
                "下载发票",
                "您收到一张【长沙楼上餐饮管理有限公司】开具的发票【发票号码：26432000001233579481】",
                "nuonuo_scan_invoice",
            ),
            (
                "https://files.pdd-fapiao.com/invoice/92320508MADJX0LR2E/pdf/2025/11/25/25322000000555648119_a798.pdf",
                "下载PDF",
                "您收到来自姑苏区平阊园苏式面馆（个体工商户）的电子发票【发票号25322000000555648119】",
                "pdd_direct_invoice",
            ),
            (
                "https://eicore-invoice-25.s3.cn-north-1.jdcloud-oss.com/digital-invoice/digital_25117000000953853334.pdf?AWSAccessKeyId=JDC_8007&Expires=2702626081&Signature=x",
                "发票PDF",
                "您的京东订单电子发票已开具",
                "jdcloud_direct_invoice",
            ),
            (
                "https://etd.kpbyd.com/hub/files/download?code=abc&fileCode=shandong_0_26372000002439975871_20260525_8LQ3a3vnsNoEfsb_pdf",
                "下载发票",
                "您收到来自【济南历下小螺号海鲜店】的电子发票【发票号码26372000002439975871】",
                "kpbyd_direct_invoice",
            ),
        ]
        for url, anchor, subject, expected_family in cases:
            with self.subTest(url=url):
                decision = _build_link_candidate_decision(
                    url,
                    anchor,
                    tier=2,
                    sender_addr="",
                    subject=subject,
                    body_text="",
                )
                self.assertEqual(decision["candidate_action"], "main_chain")
                self.assertEqual(decision["provider_family"], expected_family)
                self.assertEqual(infer_direct_invoice_family(url), expected_family)

    def test_direct_file_invoice_recovery_downloads_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        cases = [
            (
                "https://files.pdd-fapiao.com/invoice/92320508MADJX0LR2E/pdf/2025/11/25/25322000000555648119_a798.pdf",
                "pdd_direct_invoice",
            ),
            (
                "https://eicore-invoice-25.s3.cn-north-1.jdcloud-oss.com/digital-invoice/digital_25117000000953853334.pdf?AWSAccessKeyId=JDC_8007&Expires=2702626081&Signature=x",
                "jdcloud_direct_invoice",
            ),
            (
                "https://etd.kpbyd.com/hub/files/download?code=abc&fileCode=shandong_0_26372000002439975871_20260525_8LQ3a3vnsNoEfsb_pdf",
                "kpbyd_direct_invoice",
            ),
        ]
        try:
            for url, family in cases:
                with self.subTest(url=url):
                    result = converter._recover_direct_invoice_group(
                        [url],
                        "电子发票",
                        "email-id",
                        str(Path(converter.staging_dir) / family),
                        {"provider_family": family, "provider_expected_fields": {}},
                    )
                    self.assertEqual(result["status"], "downloaded")
                    self.assertTrue(result["pdf_path"].endswith(".pdf"))
        finally:
            pdf_converter.requests = original_requests

    def test_url_history_key_distinguishes_body_identified_provider_invoices(self):
        first = build_processing_history_key(
            {
                "is_url": True,
                "email_id": "2446",
                "subject": "电子发票下载",
                "tier": 2,
                "source_url": "https://www.baiwang.com",
                "provider_family": "baiwang",
                "provider_expected_fields": {"invoice_number": "26112000000474524341"},
            },
            "www.baiwang.com",
            "https://www.baiwang.com",
        )
        second = build_processing_history_key(
            {
                "is_url": True,
                "email_id": "7001",
                "subject": "电子发票下载",
                "tier": 2,
                "source_url": "https://www.baiwang.com",
                "provider_family": "baiwang",
                "provider_expected_fields": {"invoice_number": "26332000003359187226"},
            },
            "www.baiwang.com",
            "https://www.baiwang.com",
        )

        self.assertNotEqual(first, second)
        self.assertRegex(first, r"^url:[0-9a-f]{64}$")
        self.assertRegex(second, r"^url:[0-9a-f]{64}$")
        self.assertNotIn("www.baiwang.com", first)
        self.assertNotIn("2446", first)
        self.assertNotIn("26112000000474524341", first)

    def test_direct_invoice_recovery_downloads_fpyun_pdf_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        try:
            result = converter._recover_direct_invoice_group(
                ["https://sdapi.fpyun.com.cn/invoice/qd/download/getInvoiceFile?fptqm=LQ26T4BJR950&type=1"],
                "【发票云】发票号码:26337000000517112500",
                "7063",
                str(Path(converter.staging_dir) / "7063"),
                {"provider_family": "fpyun_direct_invoice", "provider_expected_fields": {"invoice_number": "26337000000517112500"}},
            )
        finally:
            pdf_converter.requests = original_requests
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["pdf_path"].endswith(".pdf"))

    def test_direct_invoice_recovery_retries_transient_transport_failure(self):
        attempts = []

        class FlakySession(FakeSession):
            def get(self, url, **kwargs):
                attempts.append(url)
                if len(attempts) < 3:
                    raise ConnectionError("transient transport failure")
                return FakeResponse(
                    url,
                    b"%PDF-1.5\nrecovered invoice",
                    {"Content-Type": "application/pdf"},
                )

        class FlakyRequests:
            @staticmethod
            def Session():
                return FlakySession()

        original_requests = pdf_converter.requests
        pdf_converter.requests = FlakyRequests
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
        )
        try:
            result = converter._recover_direct_invoice_group(
                [
                    "https://sdapi.fpyun.com.cn/invoice/qd/download/"
                    "getInvoiceFile?fptqm=opaque&type=1"
                ],
                "发票号码:26110000000000000001",
                "101",
                str(Path(converter.staging_dir) / "101"),
                {
                    "provider_family": "fpyun_direct_invoice",
                    "provider_expected_fields": {
                        "invoice_number": "26110000000000000001"
                    },
                },
            )
        finally:
            pdf_converter.requests = original_requests

        self.assertEqual(result["status"], "downloaded")
        self.assertEqual(len(attempts), 3)

    def test_direct_invoice_recovery_retries_empty_provider_response(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
        )
        calls = []

        def probe(_session, _url, _artifact_prefix):
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                return [], [{"kind": "direct_probe_empty_response"}]
            return [{"kind": "pdf", "path": "recovered.pdf"}], []

        converter._probe_direct_invoice_artifact = probe

        artifacts, _logs = converter._probe_direct_invoice_artifact_with_retry(
            object(),
            "https://provider.example/invoice",
            "artifact",
            retry_delays=(0, 0),
        )

        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(artifacts[0]["path"], "recovered.pdf")

    def test_direct_invoice_recovery_does_not_retry_policy_rejection(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
        )
        calls = []

        def probe(_session, _url, _artifact_prefix):
            calls.append(len(calls) + 1)
            return [], [{"kind": "URL_POLICY_REJECTED"}]

        converter._probe_direct_invoice_artifact = probe

        artifacts, _logs = converter._probe_direct_invoice_artifact_with_retry(
            object(),
            "https://provider.example/invoice",
            "artifact",
            retry_delays=(0, 0),
        )

        self.assertEqual(calls, [1])
        self.assertEqual(artifacts, [])

    def test_nuonuo_recovery_retries_empty_provider_response(self):
        converter = PDFConverter(
            staging_dir=tempfile.mkdtemp(),
            url_policy=public_test_policy(),
        )
        calls = []

        def probe(_session, _url, _artifact_prefix):
            calls.append(len(calls) + 1)
            if len(calls) < 3:
                return [], [{"kind": "nuonuo_detail_api_unsuccessful"}]
            return [{"kind": "pdf", "path": "nuonuo.pdf"}], []

        converter._probe_nuonuo_scan_invoice_artifacts = probe

        artifacts, _logs = converter._probe_nuonuo_scan_invoice_artifacts_with_retry(
            object(),
            "https://nnfp.jss.com.cn/short-link",
            "artifact",
            retry_delays=(0, 0),
        )

        self.assertEqual(calls, [1, 2, 3])
        self.assertEqual(artifacts[0]["path"], "nuonuo.pdf")

    def test_baiwang_preview_invoice_downloads_pdf_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        try:
            result = converter._recover_baiwang_group(
                ["https://pis.baiwang.com/smkp-vue/previewInvoiceAllEle?param=A79B8219096507C9"],
                "电子发票下载",
                "7051",
                str(Path(converter.staging_dir) / "7051"),
                {"provider_family": "baiwang", "provider_expected_fields": {"invoice_number": "26432000001239781576"}},
            )
        finally:
            pdf_converter.requests = original_requests
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["pdf_path"].endswith(".pdf"))

    def test_nuonuo_shortlink_recovers_invoice_pdf_without_chromium(self):
        original_requests = pdf_converter.requests
        pdf_converter.requests = FakeRequests
        converter = PDFConverter(staging_dir=tempfile.mkdtemp(), url_policy=public_test_policy())
        converter._require_playwright = lambda: (_ for _ in ()).throw(AssertionError("Chromium should not be required"))
        try:
            result = converter._recover_direct_invoice_group(
                ["https://nnfp.jss.com.cn/71ykyWlR=C-18aNx"],
                "您收到一张【长沙楼上餐饮管理有限公司】开具的发票【发票号码：26432000001233579481】",
                "7048",
                str(Path(converter.staging_dir) / "7048"),
                {"provider_family": "nuonuo_scan_invoice", "provider_expected_fields": {"invoice_number": "26432000001233579481"}},
            )
        finally:
            pdf_converter.requests = original_requests
        self.assertEqual(result["status"], "downloaded")
        self.assertTrue(result["pdf_path"].endswith(".pdf"))


if __name__ == "__main__":
    unittest.main()
