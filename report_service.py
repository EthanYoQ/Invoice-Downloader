from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import hashlib
import threading
from typing import Any, Callable


@dataclass(frozen=True)
class RunFinalizationContext:
    run_id: str
    staging_dir: Path
    output_dir: Path


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


class ReportService:
    """Owns ordered, bounded report/disconnect/cleanup finalization."""

    def __init__(
        self,
        *,
        report_callback: Callable[[RunFinalizationContext, Any], Any] | None = None,
        disconnect_callback: Callable[[RunFinalizationContext, Any], Any] | None = None,
        cleanup_callback: Callable[[RunFinalizationContext], Any] | None = None,
        timeout_seconds: float = 120.0,
    ):
        self._report_callback = report_callback
        self._disconnect_callback = disconnect_callback
        self._cleanup_callback = cleanup_callback
        self._timeout_seconds = max(0.001, float(timeout_seconds))

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
            callbacks.append(
                ("cleanup", lambda: self._cleanup_callback(context))
            )
        return callbacks

    def finalize(self, run_result: Any) -> ReportArtifacts:
        del run_result
        return ReportArtifacts()
