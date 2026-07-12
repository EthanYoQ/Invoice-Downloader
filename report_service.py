from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import queue
import threading
from typing import Any, Callable


@dataclass(frozen=True)
class RunFinalizationContext:
    run_id: str
    staging_dir: Path
    output_dir: Path
    run_root: Path | None = None
    request: Any = None
    started_monotonic_seconds: str = ""
    started_at_utc: str = ""


@dataclass(frozen=True)
class ReportArtifacts:
    report_path: str = ""
    diagnostics_path: str = ""


class FinalizerTimeoutError(RuntimeError):
    def __init__(self, callback_name: str):
        safe_name = str(callback_name or "callback").upper()
        self.reason_code = f"FINALIZER_{safe_name}_TIMEOUT"
        self.user_message = "运行收尾步骤超时。"
        super().__init__(self.reason_code)


class FinalizerCallbackError(RuntimeError):
    def __init__(self, callback_name: str, exc: BaseException):
        safe_name = str(callback_name or "callback").upper()
        fingerprint = hashlib.sha256(
            f"{type(exc).__name__}:{exc}".encode("utf-8", errors="replace")
        ).hexdigest()[:12]
        self.reason_code = f"FINALIZER_{safe_name}_FAILED"
        self.user_message = "运行收尾步骤失败。"
        super().__init__(f"{self.reason_code}:{type(exc).__name__}:{fingerprint}")


class _EvidenceCaptureDispatcher:
    """Prestarted dispatcher keeps worker launch off the finalizer thread."""

    _SENTINEL = object()

    def __init__(self, launcher: Callable[[Callable[[], None]], Any] | None = None):
        self._launcher = launcher or self._launch_thread
        self._jobs: queue.Queue[Any] = queue.Queue()
        self._startup_error: BaseException | None = None
        self._close_lock = threading.Lock()
        self._close_requested = False
        self._closed = threading.Event()
        self._thread = threading.Thread(
            target=self._serve,
            name="EvidenceCaptureDispatcher",
            daemon=True,
        )
        try:
            self._thread.start()
        except BaseException as exc:
            self._startup_error = exc
            self._closed.set()

    @staticmethod
    def _launch_thread(target: Callable[[], None]) -> None:
        thread = threading.Thread(
            target=target,
            name="EvidenceCapture-Worker",
            daemon=True,
        )
        thread.start()

    def _serve(self) -> None:
        try:
            while True:
                item = self._jobs.get()
                if item is self._SENTINEL:
                    return
                target, failures, done = item
                try:
                    self._launcher(target)
                except BaseException as exc:
                    failures.append(exc)
                    done.set()
        finally:
            self._closed.set()

    def submit(
        self,
        target: Callable[[], None],
        failures: list[BaseException],
        done: threading.Event,
    ) -> None:
        if self._startup_error is not None:
            raise self._startup_error
        with self._close_lock:
            if self._close_requested:
                raise RuntimeError("evidence_capture_dispatcher_closed")
            self._jobs.put_nowait((target, failures, done))

    def close(self) -> None:
        with self._close_lock:
            if self._close_requested:
                return
            self._close_requested = True
            if self._startup_error is None:
                self._jobs.put_nowait(self._SENTINEL)

    def wait_closed(self, timeout: float | None = None) -> bool:
        return self._closed.wait(timeout)

    def __enter__(self) -> "_EvidenceCaptureDispatcher":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()


class ReportService:
    """Owns ordered, bounded report/disconnect/cleanup finalization."""

    def __init__(
        self,
        *,
        report_callback: Callable[[RunFinalizationContext, Any], Any] | None = None,
        disconnect_callback: Callable[[RunFinalizationContext, Any], Any] | None = None,
        cleanup_callback: Callable[[RunFinalizationContext], Any] | None = None,
        evidence_writer: Any = None,
        timeout_seconds: float = 120.0,
        evidence_capture_timeout_seconds: float = 120.0,
        evidence_capture_launcher: Callable[[Callable[[], None]], Any] | None = None,
        evidence_required: bool = False,
    ):
        self._report_callback = report_callback
        self._disconnect_callback = disconnect_callback
        self._cleanup_callback = cleanup_callback
        self._evidence_writer = evidence_writer
        self._timeout_seconds = max(0.001, float(timeout_seconds))
        self._evidence_capture_timeout_seconds = max(
            0.001, float(evidence_capture_timeout_seconds)
        )
        self._evidence_capture_dispatcher = (
            _EvidenceCaptureDispatcher(evidence_capture_launcher)
            if evidence_writer is not None and evidence_required
            else None
        )

    def _capture_dispatcher(self) -> _EvidenceCaptureDispatcher:
        if self._evidence_capture_dispatcher is None:
            raise ValueError("production_evidence_dispatcher_required")
        return self._evidence_capture_dispatcher

    def close(self) -> None:
        dispatcher = self._evidence_capture_dispatcher
        if dispatcher is not None:
            dispatcher.close()

    def __enter__(self) -> "ReportService":
        return self

    def __exit__(self, *_args: Any) -> None:
        self.close()

    def _bounded(self, name: str, callback: Callable[[], Any]) -> Callable[[], None]:
        def invoke() -> None:
            done = threading.Event()
            failure: list[BaseException] = []

            def runner() -> None:
                try:
                    callback()
                except BaseException as exc:
                    failure.append(exc)
                finally:
                    done.set()

            thread = threading.Thread(
                target=runner,
                name=f"RunFinalizer-{name}",
                daemon=True,
            )
            thread.start()
            if not done.wait(self._timeout_seconds):
                raise FinalizerTimeoutError(name)
            if failure:
                raise FinalizerCallbackError(name, failure[0])

        return invoke

    def callbacks(
        self,
        context: RunFinalizationContext,
        result: Any,
        session: Any,
    ) -> list[tuple[str, Callable[[], None]]]:
        callbacks: list[tuple[str, Callable[[], None]]] = []
        evidence_captured = False
        evidence_required = bool(
            getattr(getattr(context, "request", None), "evidence_required", False)
        )
        if not evidence_required:
            self.close()
        if evidence_required:
            if self._evidence_writer is None:
                raise ValueError("production_evidence_writer_required")
            def capture_evidence() -> None:
                nonlocal evidence_captured
                dispatcher = self._capture_dispatcher()
                done = threading.Event()
                abandoned = threading.Event()
                failure: list[BaseException] = []
                promoted: list[bool] = []

                def runner() -> None:
                    try:
                        promoted.append(
                            bool(
                                self._evidence_writer.capture(
                                    context,
                                    result,
                                    authorization=lambda: not abandoned.is_set(),
                                )
                            )
                        )
                    except BaseException as exc:
                        failure.append(exc)
                    finally:
                        done.set()

                try:
                    try:
                        dispatcher.submit(runner, failure, done)
                    except BaseException as exc:
                        abandoned.set()
                        self._evidence_writer.abandon(context)
                        raise FinalizerCallbackError("evidence_capture", exc) from None
                    if not done.wait(self._evidence_capture_timeout_seconds):
                        abandoned.set()
                        self._evidence_writer.abandon(context)
                        raise FinalizerTimeoutError("evidence_capture")
                    if failure:
                        self._evidence_writer.abandon(context)
                        raise failure[0]
                    if promoted != [True]:
                        self._evidence_writer.abandon(context)
                        raise ValueError("evidence_capture_not_promoted")
                    evidence_captured = True
                finally:
                    dispatcher.close()

            callbacks.append(("evidence_capture", capture_evidence))
        if self._report_callback is not None:
            callbacks.append(
                ("report", self._bounded("report", lambda: self._report_callback(context, result)))
            )
        if self._disconnect_callback is not None and session is not None:
            callbacks.append(
                (
                    "disconnect",
                    lambda: self._disconnect_callback(context, session),
                )
            )
        if self._cleanup_callback is not None:
            def cleanup() -> Any:
                if evidence_required and not evidence_captured:
                    raise ValueError("lineage_capture_required_before_cleanup")
                return self._cleanup_callback(context)

            callbacks.append(
                ("cleanup", cleanup)
            )
        if evidence_required:
            callbacks.append(
                (
                    "evidence_finalize",
                    self._bounded(
                        "evidence_finalize",
                        lambda: self._evidence_writer.finalize(context, result),
                    ),
                )
            )
        return callbacks

    def finalize(self, run_result: Any) -> ReportArtifacts:
        del run_result
        return ReportArtifacts()
