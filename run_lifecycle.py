from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Callable, Iterable


class RunState(str, Enum):
    CREATED = "created"
    SCANNING = "scanning"
    RECOVERING = "recovering"
    EXTRACTING = "extracting"
    ARCHIVING = "archiving"
    REPORTING = "reporting"
    FINALIZING = "finalizing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass(frozen=True)
class FailureDetail:
    reason_code: str
    user_message: str
    exception_type: str
    fingerprint: str
    callback: str = ""


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    staging_dir: Path
    state: RunState
    primary_failure: FailureDetail | None
    finalizer_failures: tuple[FailureDetail, ...]


_STAGE_RANK = {
    RunState.CREATED: 0,
    RunState.SCANNING: 1,
    RunState.RECOVERING: 2,
    RunState.EXTRACTING: 3,
    RunState.ARCHIVING: 4,
    RunState.REPORTING: 5,
}
_TERMINAL_STATES = {RunState.COMPLETED, RunState.FAILED}
_FINALIZER_MESSAGES = {
    "report": "运行报告收尾失败。",
    "disconnect": "邮箱连接关闭失败。",
    "cleanup": "临时文件清理失败。",
}


def _error_fingerprint(exc: BaseException) -> str:
    return hashlib.sha256(str(exc).encode("utf-8", errors="replace")).hexdigest()[:12]


def _safe_token(value: object, fallback: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "").upper()).strip("_")
    return text or fallback


def _safe_callback_name(value: object) -> str:
    text = re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "callback").lower()).strip("_")
    return text or "callback"


def _failure_detail(
    exc: BaseException,
    *,
    reason_code: str,
    user_message: str,
    callback: str = "",
) -> FailureDetail:
    return FailureDetail(
        reason_code=_safe_token(reason_code, "RUN_FAILED"),
        user_message=str(user_message or "运行失败，请重试。"),
        exception_type=type(exc).__name__,
        fingerprint=_error_fingerprint(exc),
        callback=_safe_callback_name(callback) if callback else "",
    )


class RunHandle:
    def __init__(
        self,
        lifecycle: "RunLifecycle",
        run_id: str,
        staging_dir: Path,
        on_transition: Callable[[RunState, RunState], None] | None = None,
    ):
        self._lifecycle = lifecycle
        self.run_id = run_id
        self.staging_dir = staging_dir
        self._on_transition = on_transition
        self._lock = threading.RLock()
        self._state = RunState.CREATED
        self._primary_failure: FailureDetail | None = None
        self._finalizer_failures: tuple[FailureDetail, ...] = ()
        self._finalize_started = False
        self._finalized = threading.Event()

    @property
    def state(self) -> RunState:
        with self._lock:
            return self._state

    @property
    def snapshot(self) -> RunSnapshot:
        with self._lock:
            return RunSnapshot(
                run_id=self.run_id,
                staging_dir=self.staging_dir,
                state=self._state,
                primary_failure=self._primary_failure,
                finalizer_failures=self._finalizer_failures,
            )

    @property
    def error(self) -> str:
        snapshot = self.snapshot
        parts = []
        if snapshot.primary_failure is not None:
            primary = snapshot.primary_failure
            parts.append(f"{primary.user_message} [{primary.reason_code}]")
        if snapshot.finalizer_failures:
            rendered = ", ".join(
                f"{failure.user_message} [{failure.reason_code}]"
                for failure in snapshot.finalizer_failures
            )
            parts.append(f"收尾异常：{rendered}")
        return "；".join(parts)

    def _notify_transition(self, previous: RunState, state: RunState) -> None:
        if self._on_transition is None or previous is state:
            return
        try:
            self._on_transition(previous, state)
        except Exception:
            # State observers cannot keep a run from reaching its terminal barrier.
            return

    def advance(self, state: RunState) -> RunState:
        state = RunState(state)
        with self._lock:
            current = self._state
            if current in _TERMINAL_STATES or current is RunState.FINALIZING:
                raise RuntimeError(f"cannot advance terminal/finalizing run from {current.value}")
            if state not in _STAGE_RANK:
                raise RuntimeError(f"advance requires a stage state, got {state.value}")
            if _STAGE_RANK[state] < _STAGE_RANK[current]:
                raise RuntimeError(f"cannot move run backward from {current.value} to {state.value}")
            self._state = state
        self._notify_transition(current, state)
        return state

    def fail(
        self,
        exc: BaseException,
        *,
        reason_code: str = "RUN_FAILED",
        user_message: str = "运行失败，请重试。",
    ) -> RunState:
        with self._lock:
            if self._state in _TERMINAL_STATES:
                return self._state
            current = self._state
            if self._primary_failure is None:
                self._primary_failure = _failure_detail(
                    exc,
                    reason_code=reason_code,
                    user_message=user_message,
                )
            self._state = RunState.FINALIZING
        self._notify_transition(current, RunState.FINALIZING)
        return RunState.FINALIZING

    def finalize(
        self,
        callbacks: Iterable[Callable[[], None] | tuple[str, Callable[[], None]]],
    ) -> RunState:
        should_wait = False
        with self._lock:
            if self._state in _TERMINAL_STATES:
                return self._state
            if self._finalize_started:
                should_wait = True
                previous = self._state
            else:
                self._finalize_started = True
                previous = self._state
                self._state = RunState.FINALIZING

        if should_wait:
            self._finalized.wait()
            return self.state

        self._notify_transition(previous, RunState.FINALIZING)
        callback_failures = []
        for item in callbacks:
            if isinstance(item, tuple):
                callback_name, callback = item
            else:
                callback = item
                callback_name = getattr(callback, "__name__", "callback")
            safe_name = _safe_callback_name(callback_name)
            try:
                callback()
            except Exception as exc:
                reason_code = getattr(exc, "reason_code", "") or f"FINALIZER_{safe_name.upper()}_FAILED"
                user_message = getattr(exc, "user_message", "") or _FINALIZER_MESSAGES.get(
                    safe_name,
                    "运行收尾步骤失败。",
                )
                callback_failures.append(
                    _failure_detail(
                        exc,
                        reason_code=reason_code,
                        user_message=user_message,
                        callback=safe_name,
                    )
                )

        with self._lock:
            self._finalizer_failures = tuple(callback_failures)
            terminal = RunState.FAILED if self._primary_failure or callback_failures else RunState.COMPLETED
            previous = self._state
            self._state = terminal

        self._notify_transition(previous, terminal)
        self._lifecycle._release(self)
        self._finalized.set()
        return terminal


class RunLifecycle:
    def __init__(self):
        self._lock = threading.RLock()
        self._active: RunHandle | None = None
        self._owned_staging_dirs: dict[str, str] = {}

    @property
    def can_begin(self) -> bool:
        with self._lock:
            return self._active is None

    @property
    def owned_staging_count(self) -> int:
        with self._lock:
            return len(self._owned_staging_dirs)

    def begin(
        self,
        run_id: str,
        staging_dir: Path,
        on_transition: Callable[[RunState, RunState], None] | None = None,
    ) -> RunHandle:
        run_id = str(run_id or "").strip()
        if not run_id:
            raise ValueError("run_id is required")
        staging_dir = Path(staging_dir).resolve()
        staging_key = str(staging_dir).casefold()
        with self._lock:
            if self._active is not None:
                raise RuntimeError("a run is already active")
            if run_id in self._owned_staging_dirs or staging_key in self._owned_staging_dirs.values():
                raise RuntimeError("staging directory is already owned")
            staging_dir.mkdir(parents=True, exist_ok=False)
            handle = RunHandle(self, run_id, staging_dir, on_transition=on_transition)
            self._active = handle
            self._owned_staging_dirs[run_id] = staging_key
            return handle

    def _release(self, handle: RunHandle) -> None:
        with self._lock:
            if self._active is handle:
                self._active = None
            self._owned_staging_dirs.pop(handle.run_id, None)
