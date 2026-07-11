from __future__ import annotations

import hashlib
import re
import threading
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


_STAGE_RANK = {
    RunState.CREATED: 0,
    RunState.SCANNING: 1,
    RunState.RECOVERING: 2,
    RunState.EXTRACTING: 3,
    RunState.ARCHIVING: 4,
    RunState.REPORTING: 5,
}
_TERMINAL_STATES = {RunState.COMPLETED, RunState.FAILED}


def _error_fingerprint(exc: BaseException) -> str:
    return hashlib.sha256(str(exc).encode("utf-8", errors="replace")).hexdigest()[:12]


def _safe_callback_name(value: object) -> str:
    text = str(value or "callback")
    return re.sub(r"[^A-Za-z0-9_]+", "_", text).strip("_") or "callback"


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
        self._error = ""
        self._finalize_started = False
        self._finalized = threading.Event()

    @property
    def state(self) -> RunState:
        with self._lock:
            return self._state

    @property
    def error(self) -> str:
        with self._lock:
            return self._error

    def _transition(self, state: RunState) -> None:
        callback = None
        with self._lock:
            previous = self._state
            if previous is state:
                return
            self._state = state
            callback = self._on_transition
        if callback is not None:
            callback(previous, state)

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
        self._transition(state)
        return state

    def fail(self, exc: BaseException) -> RunState:
        with self._lock:
            if self._state in _TERMINAL_STATES:
                return self._state
            if not self._error:
                exc_type = type(exc).__name__
                self._error = f"RUN_FAILED:{exc_type}:{_error_fingerprint(exc)}"
        self._transition(RunState.FINALIZING)
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
            else:
                self._finalize_started = True

        if should_wait:
            self._finalized.wait()
            return self.state

        self._transition(RunState.FINALIZING)
        callback_errors = []
        for item in callbacks:
            if isinstance(item, tuple):
                callback_name, callback = item
            else:
                callback = item
                callback_name = getattr(callback, "__name__", "callback")
            try:
                callback()
            except Exception as exc:
                callback_errors.append(
                    f"{_safe_callback_name(callback_name)}:{type(exc).__name__}:{_error_fingerprint(exc)}"
                )

        with self._lock:
            if callback_errors and not self._error:
                self._error = f"FINALIZER_FAILED:{callback_errors[0]}"
                if len(callback_errors) > 1:
                    self._error += f":plus_{len(callback_errors) - 1}"
            terminal = RunState.FAILED if self._error else RunState.COMPLETED

        self._transition(terminal)
        self._lifecycle._release(self)
        self._finalized.set()
        return terminal


class RunLifecycle:
    def __init__(self):
        self._lock = threading.RLock()
        self._active: RunHandle | None = None
        self._owned_staging_dirs: set[str] = set()

    @property
    def can_begin(self) -> bool:
        with self._lock:
            return self._active is None

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
            if staging_key in self._owned_staging_dirs:
                raise RuntimeError("staging directory was already owned by a prior run")
            staging_dir.mkdir(parents=True, exist_ok=False)
            handle = RunHandle(self, run_id, staging_dir, on_transition=on_transition)
            self._active = handle
            self._owned_staging_dirs.add(staging_key)
            return handle

    def _release(self, handle: RunHandle) -> None:
        with self._lock:
            if self._active is handle:
                self._active = None
