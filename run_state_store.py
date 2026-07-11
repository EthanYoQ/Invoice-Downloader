from __future__ import annotations

import copy
import threading
import time
from typing import Any, Callable, Mapping


_TERMINAL_STATES = {"completed", "failed"}


class RunStateStore:
    """Thread-safe owner of the frontend-visible state for one active run."""

    def __init__(self, event_sink: Callable[[dict[str, Any]], None] | None = None):
        self._lock = threading.RLock()
        self._event_sink = event_sink
        self._terminal = False
        self._state = self._initial_state("")

    @staticmethod
    def _initial_state(run_id: str) -> dict[str, Any]:
        return {
            "run_id": str(run_id or ""),
            "progress": 0,
            "status_text": "等待任务开始...",
            "logs": [],
            "new_categories": [],
            "stats": {"emails": 0, "invoices": 0, "errors": 0},
            "processed_invoices": [],
            "error_invoices": [],
            "is_running": False,
            "run_state": "idle",
            "last_error": "",
            "stop_requested": False,
            "can_stop": False,
            "quota_exhausted": False,
            "quota_message": "",
        }

    def _emit(self, snapshot: dict[str, Any]) -> None:
        callback = self._event_sink
        if callback is None:
            return
        try:
            callback(copy.deepcopy(snapshot))
        except Exception:
            return

    def reset(self, run_id: str) -> None:
        with self._lock:
            self._state = self._initial_state(run_id)
            self._terminal = False
            snapshot = copy.deepcopy(self._state)
        self._emit(snapshot)

    def update(
        self,
        *,
        progress: int | None = None,
        status_text: str | None = None,
        run_state: str | None = None,
        last_error: str | None = None,
        stop_requested: bool | None = None,
        quota_exhausted: bool | None = None,
        quota_message: str | None = None,
        statistics: Mapping[str, Any] | None = None,
        processed_invoices: list[Mapping[str, Any]] | None = None,
        error_invoices: list[Mapping[str, Any]] | None = None,
        categories: list[str] | set[str] | tuple[str, ...] | None = None,
        logs: list[Mapping[str, Any]] | None = None,
    ) -> None:
        with self._lock:
            if self._terminal:
                return
            if progress is not None:
                bounded = max(0, min(100, int(progress)))
                self._state["progress"] = max(int(self._state["progress"]), bounded)
            if status_text is not None:
                self._state["status_text"] = str(status_text)
            if run_state is not None:
                normalized = str(run_state)
                self._state["run_state"] = normalized
                self._state["is_running"] = normalized in {"running", "finalizing"}
            if last_error is not None:
                self._state["last_error"] = str(last_error)
            if stop_requested is not None:
                self._state["stop_requested"] = bool(stop_requested)
            if quota_exhausted is not None:
                self._state["quota_exhausted"] = bool(quota_exhausted)
            if quota_message is not None:
                self._state["quota_message"] = str(quota_message)
            if statistics is not None:
                self._state["stats"] = copy.deepcopy(dict(statistics))
            if processed_invoices is not None:
                self._state["processed_invoices"] = copy.deepcopy(list(processed_invoices))
            if error_invoices is not None:
                self._state["error_invoices"] = copy.deepcopy(list(error_invoices))
            if categories is not None:
                self._state["new_categories"] = sorted({str(item) for item in categories})
            if logs is not None:
                self._state["logs"] = copy.deepcopy(list(logs))
            self._state["can_stop"] = bool(
                self._state["run_state"] == "running"
                and self._state["is_running"]
                and not self._state["stop_requested"]
            )
            snapshot = copy.deepcopy(self._state)
        self._emit(snapshot)

    def append_log(self, level: str, message: str, color: str = "text-slate-700") -> None:
        with self._lock:
            if self._terminal:
                return
            self._state["logs"].append(
                {
                    "time": time.strftime("[%H:%M:%S]"),
                    "type": str(level),
                    "color": str(color),
                    "msg": str(message),
                }
            )
            snapshot = copy.deepcopy(self._state)
        self._emit(snapshot)

    def add_processed(self, item: Mapping[str, Any]) -> None:
        with self._lock:
            if self._terminal:
                return
            self._state["processed_invoices"].append(copy.deepcopy(dict(item)))
            snapshot = copy.deepcopy(self._state)
        self._emit(snapshot)

    def add_error(self, item: Mapping[str, Any]) -> None:
        with self._lock:
            if self._terminal:
                return
            self._state["error_invoices"].append(copy.deepcopy(dict(item)))
            snapshot = copy.deepcopy(self._state)
        self._emit(snapshot)

    def terminal(self, state: str, *, status_text: str, last_error: str = "") -> None:
        normalized = str(state)
        if normalized not in _TERMINAL_STATES:
            raise ValueError("terminal state must be completed or failed")
        with self._lock:
            if self._terminal:
                return
            self._state["run_state"] = normalized
            self._state["is_running"] = False
            self._state["can_stop"] = False
            self._state["status_text"] = str(status_text)
            self._state["last_error"] = str(last_error)
            if normalized == "completed":
                self._state["progress"] = 100
            elif self._state["progress"] >= 100:
                self._state["progress"] = 99
            self._terminal = True
            snapshot = copy.deepcopy(self._state)
        self._emit(snapshot)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._state)

    def frontend_snapshot(
        self,
        *,
        build_identity: Mapping[str, Any] | None = None,
        raw_date_range: str = "",
        imap_query_range: str = "",
    ) -> dict[str, Any]:
        snapshot = self.snapshot()
        snapshot["logs"] = snapshot["logs"][-20:]
        snapshot.pop("processed_invoices", None)
        snapshot.pop("error_invoices", None)
        snapshot.pop("run_id", None)
        snapshot["build_identity"] = copy.deepcopy(dict(build_identity or {}))
        snapshot["raw_date_range"] = str(raw_date_range or "")
        snapshot["imap_query_range"] = str(imap_query_range or "")
        return snapshot
