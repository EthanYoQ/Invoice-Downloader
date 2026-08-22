"""DSH-owned bridge from the frozen invoice engine to its current coordinator."""

from __future__ import annotations

import os
import socket
import threading
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from invoice_extractor import InvoiceExtractor


class ImapFetchTimeout(RuntimeError):
    """Report a bounded IMAP read that cannot safely provide a complete mailbox scope."""

    reason_code = "IMAP_FETCH_TIMEOUT"
    user_message = "邮箱读取超时，本次扫描已停止；请稍后重试。"

    def __init__(self, message: str) -> None:
        super().__init__(f"{self.reason_code}: {message}")


def _positive_timeout(config: dict[str, Any]) -> float:
    """Read the DSH-owned IMAP read limit from the IPC job configuration."""
    try:
        seconds = float(config["imapFetchTimeoutSeconds"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("imapFetchTimeoutSeconds must be a positive number") from exc
    if seconds <= 0:
        raise ValueError("imapFetchTimeoutSeconds must be a positive number")
    return seconds


def _server_search_window(date_from: str, date_to: str) -> tuple[str, str]:
    """Return an IMAP date window that keeps a one-day margin for local date filtering."""
    first_day = date.fromisoformat(date_from)
    last_day = date.fromisoformat(date_to)
    if last_day < first_day:
        raise ValueError("dateTo must not be earlier than dateFrom")
    return (
        (first_day - timedelta(days=1)).strftime("%d-%b-%Y"),
        (last_day + timedelta(days=2)).strftime("%d-%b-%Y"),
    )


def _bound_imap_reads(
    fetcher: Any,
    timeout_seconds: float,
    date_from: str,
    date_to: str,
) -> dict[str, bool]:
    """Bound IMAP reads and replace all-mail search with a server-narrowed candidate window."""
    mail = getattr(fetcher, "mail", None)
    socket_handle = getattr(mail, "sock", None)
    set_timeout = getattr(socket_handle, "settimeout", None)
    uid = getattr(mail, "uid", None)
    if not callable(set_timeout) or not callable(uid):
        raise ImapFetchTimeout("IMAP read timeout cannot be configured")
    try:
        set_timeout(timeout_seconds)
    except OSError as exc:
        raise ImapFetchTimeout("IMAP read timeout cannot be configured") from exc

    state = {"timed_out": False}
    search_start, search_end = _server_search_window(date_from, date_to)

    def close_on_deadline() -> None:
        state["timed_out"] = True
        shutdown = getattr(socket_handle, "shutdown", None)
        if callable(shutdown):
            try:
                shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        close = getattr(socket_handle, "close", None)
        if callable(close):
            try:
                close()
            except OSError:
                pass

    def bounded_uid(*args: Any, **kwargs: Any) -> Any:
        if state["timed_out"]:
            raise ImapFetchTimeout("IMAP read timed out")
        if args == ("SEARCH", None, "ALL"):
            args = ("SEARCH", None, "SINCE", search_start, "BEFORE", search_end)
        deadline = threading.Timer(timeout_seconds, close_on_deadline)
        deadline.daemon = True
        deadline.start()
        try:
            return uid(*args, **kwargs)
        except (socket.timeout, TimeoutError) as exc:
            state["timed_out"] = True
            raise ImapFetchTimeout("IMAP read timed out") from exc
        except OSError as exc:
            if state["timed_out"]:
                raise ImapFetchTimeout("IMAP read timed out") from exc
            raise
        finally:
            deadline.cancel()

    mail.uid = bounded_uid
    return state


class DshRecognitionExtractor(InvoiceExtractor):
    """Present DSH's recognition chain through the engine's extraction and archive interface."""

    def __init__(self, recognition_chain: Any, output_dir: str) -> None:
        self._recognition_chain = recognition_chain
        self.output_dir = os.path.abspath(output_dir)
        os.makedirs(self.output_dir, exist_ok=True)
        self.processed_records_file = os.path.join(self.output_dir, "processed_records.json")
        self.glm_runtime = None
        self.last_extraction_trace: dict[str, Any] = {}
        self.last_route_trace: dict[str, Any] = {}
        self.last_timing_trace: dict[str, Any] = {}

    def pdf_to_base64_image(self, pdf_path: str) -> list[str]:
        """Supply a truthy placeholder because the DSH chain reads the source path directly."""
        return [pdf_path] if os.path.isfile(pdf_path) else []

    def extract_remote_only(
        self,
        _prepared_source: Any,
        custom_rules: str = "",
        pdf_path: str | None = None,
        document_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Run local OCR followed by the DSH DeepSeek extractor for one artifact."""
        del custom_rules
        if not pdf_path:
            raise RuntimeError("document path is required for DSH extraction")
        result = self._recognition_chain.extract(pdf_path, document_context or {})
        self.last_extraction_trace = {
            "engine": "local_ocr_dsh_deepseek",
            "reason_code": "",
        }
        self.last_timing_trace = {}
        return result

    def close(self) -> None:
        """Match the engine extractor lifecycle without owning a GLM runtime."""
        return None


def _run_processing_loop(
    app: Any,
    recognition_chain: Any,
    attachments: list[dict[str, Any]],
    save_path: str,
    date_from: str,
    date_to: str,
) -> Any:
    """Reuse the engine's candidate, archive, and reporting implementation with DSH extraction."""
    owner = DshRecognitionExtractor(recognition_chain, save_path)
    return app._run_processing_loop_with_extractor(
        attachments,
        "dsh-managed",
        save_path,
        date_from,
        date_to,
        _extractor=owner,
        _worker_extractor_factory=lambda _runtime: DshRecognitionExtractor(
            recognition_chain, save_path
        ),
    )


def run_scan(*, config: dict[str, Any], recognition_chain: Any) -> dict[str, int | str]:
    """Execute one DSH scan with the current engine coordinator."""
    from app_api import InvoiceAppAPI
    from run_coordinator import RunCoordinator, RunRequest
    from run_lifecycle import RunLifecycle, RunState

    job_id = str(config["jobId"])
    email = str(config["email"])
    auth_code = str(config["authCode"])
    date_from = str(config["dateFrom"])
    date_to = str(config["dateTo"])
    company = str(config.get("company") or "")
    save_path = str(Path(str(config["savePath"])).resolve())
    imap_fetch_timeout_seconds = _positive_timeout(config)
    staging_dir = Path(save_path) / ".dsh-staging" / f"{job_id}-{uuid.uuid4().hex}"

    app = InvoiceAppAPI()
    lifecycle = RunLifecycle()
    handle = lifecycle.begin(job_id, staging_dir)
    app._run_lifecycle = lifecycle
    app._active_run_handle = handle
    app._run_context = {
        "enabled": False,
        "run_id": job_id,
        "run_root": save_path,
        "staging_dir": str(staging_dir),
    }
    app._current_run_id = job_id
    app._requested_save_path = save_path
    app._effective_save_path = save_path
    app._effective_date_from = date_from
    app._effective_date_to = date_to
    app._active_run_config = {
        "company": company,
        "save_path": save_path,
        "date_from": date_from,
        "date_to": date_to,
    }

    request = RunRequest(
        run_id=job_id,
        date_from=date_from,
        date_to=date_to,
        save_path=save_path,
        rules_text="",
        account_id="dsh",
        channel_id="dsh-ipc",
        before_exclusive=date_to,
        target_identifier=company,
        run_root=save_path,
    )

    def processing_loop(
        attachments: list[dict[str, Any]],
        _legacy_api_key: str,
        output_dir: str,
        since_date: str,
        before_date: str,
        _rules_text: str,
    ) -> Any:
        return _run_processing_loop(
            app,
            recognition_chain,
            attachments,
            output_dir,
            since_date,
            before_date,
        )

    app._run_processing_loop = processing_loop
    dependencies = app._build_run_dependencies(
        request,
        email_address=email,
        auth_code=auth_code,
        api_key="dsh-managed",
    )
    connect = dependencies.connect
    scan = dependencies.scan
    imap_state: dict[str, bool] | None = None

    def bounded_connect(run_request: RunRequest) -> Any:
        nonlocal imap_state
        fetcher = connect(run_request)
        imap_state = _bound_imap_reads(
            fetcher,
            imap_fetch_timeout_seconds,
            date_from,
            date_to,
        )
        return fetcher

    def bounded_scan(fetcher: Any, run_request: RunRequest) -> Any:
        result = scan(fetcher, run_request)
        if imap_state is not None and imap_state["timed_out"]:
            raise ImapFetchTimeout("IMAP read timed out")
        return result

    dependencies.connect = bounded_connect
    dependencies.scan = bounded_scan
    result = RunCoordinator(lifecycle, app._run_state_store, dependencies).run(
        request, handle=handle
    )
    app._active_run_handle = None
    if result.state is not RunState.COMPLETED:
        reason_code = str(result.reason_code or "PROCESSING_FAILED")
        raise RuntimeError(f"invoice scan failed: {reason_code}")

    report = result.archive_report
    completed = {
        "status": "completed",
        "invoicesProcessed": int(result.outcome_count),
        "successCount": int(getattr(report, "archived_count", 0)),
        "retainedCount": int(getattr(report, "retained_count", 0)),
        "manualReviewCount": int(getattr(report, "manual_count", 0)),
    }
    export_result = app.export_run_summary(save_path)
    export_path = export_result.get("path")
    if export_result.get("success") and isinstance(export_path, str) and Path(export_path).is_file():
        completed["exportPath"] = export_path
    return completed
