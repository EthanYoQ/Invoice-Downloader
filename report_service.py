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
    ):
        self._report_callback = report_callback
        self._disconnect_callback = disconnect_callback
        self._cleanup_callback = cleanup_callback
        self._evidence_writer = evidence_writer
        self._timeout_seconds = max(0.001, float(timeout_seconds))
        self._evidence_capture_timeout_seconds = max(
            0.001, float(evidence_capture_timeout_seconds)
        )

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
        if evidence_required:
            if self._evidence_writer is None:
                raise ValueError("production_evidence_writer_required")
            def capture_evidence() -> None:
                nonlocal evidence_captured
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

                thread = threading.Thread(
                    target=runner,
                    name=f"EvidenceCapture-{context.run_id}",
                    daemon=True,
                )
                try:
                    thread.start()
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
