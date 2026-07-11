from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
from pathlib import Path
import threading
from typing import Any, Callable

from report_service import ReportService, RunFinalizationContext
from run_lifecycle import RunHandle, RunLifecycle, RunState
from run_state_store import RunStateStore


@dataclass(frozen=True)
class RunRequest:
    run_id: str
    date_from: str
    date_to: str
    save_path: str
    rules_text: str
    account_id: str
    channel_id: str


@dataclass(frozen=True)
class RunResult:
    run_id: str
    state: RunState
    cancelled: bool = False
    email_count: int = 0
    candidate_count: int = 0
    outcome_count: int = 0
    archive_report: Any = None
    reason_code: str = ""
    error: str = ""


class RunDependencies:
    """Process-only dependency container. It is deliberately not serializable."""

    __slots__ = (
        "connect",
        "scan",
        "candidate",
        "extract",
        "archive",
        "report_service",
        "cancel_requested",
        "secrets",
        "stage_callback",
        "finalizer_session",
    )

    def __init__(
        self,
        *,
        connect: Callable[[RunRequest], Any],
        scan: Callable[[Any, RunRequest], Any] | None = None,
        candidate: Callable[[Any, RunRequest], Any] | None = None,
        extract: Callable[[Any, RunRequest], Any] | None = None,
        archive: Callable[[Any, RunRequest], Any] | None = None,
        report_service: ReportService | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        secrets: Any = None,
        stage_callback: Callable[[RunState, dict[str, Any]], None] | None = None,
        finalizer_session: Callable[[], Any] | None = None,
    ):
        self.connect = connect
        self.scan = scan or (lambda _session, _request: [])
        self.candidate = candidate or (lambda values, _request: values)
        self.extract = extract or (lambda values, _request: values)
        self.archive = archive or (
            lambda _values, _request: type(
                "EmptyArchiveReport", (), {"can_complete": True, "archived_count": 0}
            )()
        )
        self.report_service = report_service or ReportService()
        self.cancel_requested = cancel_requested or (lambda: False)
        self.secrets = secrets
        self.stage_callback = stage_callback
        self.finalizer_session = finalizer_session or (lambda: None)

    def __repr__(self) -> str:
        return "RunDependencies(<process-only redacted dependencies>)"


class ArchiveIncompleteError(RuntimeError):
    reason_code = "ARCHIVE_INCOMPLETE"
    user_message = "归档结果不完整，本次任务已失败。"


class RunCoordinator:
    def __init__(
        self,
        lifecycle: RunLifecycle,
        state_store: RunStateStore,
        dependencies: RunDependencies,
    ):
        self._lifecycle = lifecycle
        self._state = state_store
        self._dependencies = dependencies
        self._run_lock = threading.Lock()

    @staticmethod
    def _count(values: Any) -> int:
        try:
            return len(values)
        except TypeError:
            return sum(1 for _item in values)

    def _stage(self, handle: RunHandle, state: RunState, **payload: Any) -> None:
        handle.advance(state)
        progress = {
            RunState.SCANNING: 10,
            RunState.RECOVERING: 30,
            RunState.EXTRACTING: 45,
            RunState.ARCHIVING: 90,
            RunState.REPORTING: 95,
        }[state]
        status_text = {
            RunState.SCANNING: "正在扫描邮件...",
            RunState.RECOVERING: "正在提取候选文档...",
            RunState.EXTRACTING: "正在识别发票...",
            RunState.ARCHIVING: "正在归档发票...",
            RunState.REPORTING: "正在生成运行报告...",
        }[state]
        self._state.update(
            run_state="running",
            progress=progress,
            status_text=status_text,
        )
        callback = self._dependencies.stage_callback
        if callback is not None:
            try:
                callback(state, dict(payload))
            except Exception:
                pass

    def _cancelled(self) -> bool:
        try:
            return bool(self._dependencies.cancel_requested())
        except Exception:
            return True

    @staticmethod
    def _safe_failure(exc: BaseException) -> tuple[str, str, str]:
        reason_code = str(getattr(exc, "reason_code", "") or "PROCESSING_FAILED")
        user_message = str(
            getattr(exc, "user_message", "")
            or "处理过程中发生异常，请重试；如持续失败请查看诊断报告。"
        )
        fingerprint = hashlib.sha256(
            f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        return reason_code, user_message, f"{user_message} [{reason_code}:{fingerprint}]"

    def run(
        self,
        request: RunRequest,
        callbacks: Any = None,
        *,
        staging_dir: str | Path | None = None,
        handle: RunHandle | None = None,
    ) -> RunResult:
        del callbacks
        if not self._run_lock.acquire(blocking=False):
            raise RuntimeError("a run is already active")
        session = None
        result = RunResult(run_id=request.run_id, state=RunState.CREATED)
        acquired_handle = handle
        try:
            if acquired_handle is None:
                if staging_dir is None:
                    staging_dir = Path(request.save_path) / ".invoiceflow-staging" / request.run_id
                acquired_handle = self._lifecycle.begin(request.run_id, Path(staging_dir))
            elif acquired_handle.run_id != request.run_id:
                raise RuntimeError("run handle does not match request")

            self._state.reset(request.run_id)
            self._state.update(run_state="running", status_text="正在初始化任务...")
            cancelled = self._cancelled()
            emails: Any = []
            candidates: Any = []
            outcomes: Any = []
            archive_report = None
            try:
                if not cancelled:
                    self._stage(acquired_handle, RunState.SCANNING)
                    session = self._dependencies.connect(request)
                    cancelled = self._cancelled()
                if not cancelled:
                    emails = self._dependencies.scan(session, request)
                    email_count = self._count(emails)
                    self._state.update(statistics={"emails": email_count, "invoices": 0, "errors": 0})
                    cancelled = self._cancelled()
                else:
                    email_count = 0

                if not cancelled and email_count:
                    self._stage(acquired_handle, RunState.RECOVERING, emails=email_count)
                    candidates = self._dependencies.candidate(emails, request)
                    candidate_count = self._count(candidates)
                    cancelled = self._cancelled()
                else:
                    candidate_count = 0

                if not cancelled and email_count:
                    self._stage(acquired_handle, RunState.EXTRACTING, candidates=candidate_count)
                    outcomes = self._dependencies.extract(candidates, request)
                    outcome_count = self._count(outcomes)
                    cancelled = self._cancelled()
                else:
                    outcome_count = 0

                if not cancelled and email_count:
                    self._stage(acquired_handle, RunState.ARCHIVING, outcomes=outcome_count)
                    archive_report = self._dependencies.archive(outcomes, request)
                    if not bool(getattr(archive_report, "can_complete", False)):
                        raise ArchiveIncompleteError("ARCHIVE_INCOMPLETE")
                    cancelled = self._cancelled()

                result = RunResult(
                    run_id=request.run_id,
                    state=acquired_handle.state,
                    cancelled=cancelled,
                    email_count=email_count,
                    candidate_count=candidate_count,
                    outcome_count=outcome_count,
                    archive_report=archive_report,
                )
            except Exception as exc:
                reason_code, user_message, safe_error = self._safe_failure(exc)
                acquired_handle.fail(
                    exc,
                    reason_code=reason_code,
                    user_message=user_message,
                )
                result = replace(
                    result,
                    state=RunState.FINALIZING,
                    reason_code=reason_code,
                    error=safe_error,
                )

            if acquired_handle.state is not RunState.FINALIZING:
                self._stage(acquired_handle, RunState.REPORTING)
            self._state.update(
                run_state="finalizing",
                progress=99,
                status_text="正在收尾...",
            )
            context = RunFinalizationContext(
                run_id=request.run_id,
                staging_dir=acquired_handle.staging_dir,
                output_dir=Path(request.save_path).resolve(),
            )
            if session is None:
                try:
                    session = self._dependencies.finalizer_session()
                except Exception:
                    session = None
            terminal = acquired_handle.finalize(
                self._dependencies.report_service.callbacks(context, result, session)
            )
            lifecycle_error = acquired_handle.error
            if terminal is RunState.FAILED:
                reason_code = result.reason_code
                if not reason_code and acquired_handle.snapshot.finalizer_failures:
                    reason_code = acquired_handle.snapshot.finalizer_failures[0].reason_code
                result = replace(
                    result,
                    state=terminal,
                    reason_code=reason_code or "FINALIZATION_FAILED",
                    error=lifecycle_error or result.error,
                )
                self._state.terminal("failed", status_text="处理失败", last_error=result.error)
            else:
                result = replace(result, state=terminal)
                status = "已安全停止" if result.cancelled else "处理完成"
                self._state.terminal("completed", status_text=status)
            return result
        finally:
            self._run_lock.release()
