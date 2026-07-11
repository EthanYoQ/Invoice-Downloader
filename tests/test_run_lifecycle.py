import threading
from pathlib import Path

import pytest

from app_api import InvoiceAppAPI


def lifecycle_types():
    from run_lifecycle import RunLifecycle, RunState

    return RunLifecycle, RunState


def test_blocked_cleanup_keeps_run_finalizing_and_rejects_second_run(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-1", tmp_path / "run-1")
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()

    def blocked_cleanup():
        cleanup_entered.set()
        assert release_cleanup.wait(2)

    finalizer = threading.Thread(target=lambda: handle.finalize([blocked_cleanup]))
    finalizer.start()
    assert cleanup_entered.wait(1)

    assert handle.state is RunState.FINALIZING
    assert lifecycle.can_begin is False
    with pytest.raises(RuntimeError, match="already active"):
        lifecycle.begin("run-2", tmp_path / "run-2")

    release_cleanup.set()
    finalizer.join(1)
    assert not finalizer.is_alive()
    assert handle.state is RunState.COMPLETED
    assert lifecycle.can_begin is True


def test_finalizers_run_in_order_continue_after_failure_and_sanitize_error(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-1", tmp_path / "run-1")
    calls = []

    def report():
        calls.append("report")
        raise RuntimeError("authorization=super-secret")

    def disconnect():
        calls.append("disconnect")

    def cleanup():
        calls.append("cleanup")

    state = handle.finalize([report, disconnect, cleanup])

    assert calls == ["report", "disconnect", "cleanup"]
    assert state is RunState.FAILED
    assert handle.error.startswith("FINALIZER_FAILED:report:RuntimeError:")
    assert "super-secret" not in handle.error


def test_primary_failure_stays_failed_after_successful_cleanup(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-1", tmp_path / "run-1")
    handle.advance(RunState.SCANNING)
    handle.fail(RuntimeError("mailbox password leaked here"))

    assert handle.state is RunState.FINALIZING
    assert handle.finalize([]) is RunState.FAILED
    assert handle.error.startswith("RUN_FAILED:RuntimeError:")
    assert "password" not in handle.error


def test_terminal_transition_happens_exactly_once(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    lifecycle = RunLifecycle()
    transitions = []
    handle = lifecycle.begin("run-1", tmp_path / "run-1", on_transition=lambda old, new: transitions.append((old, new)))

    assert handle.finalize([]) is RunState.COMPLETED
    assert handle.finalize([]) is RunState.COMPLETED
    handle.fail(RuntimeError("late failure"))

    terminals = [new for _, new in transitions if new in {RunState.COMPLETED, RunState.FAILED}]
    assert terminals == [RunState.COMPLETED]


def test_app_api_does_not_complete_or_release_worker_while_cleanup_is_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    api._begin_run("running")
    cleanup_entered = threading.Event()
    release_cleanup = threading.Event()
    terminal_events = []

    def blocked_cleanup(staging_dir=None, temp_dir=None):
        cleanup_entered.set()
        assert release_cleanup.wait(2)

    monkeypatch.setattr(api, "_cleanup_temp_folders", blocked_cleanup)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )

    def finish():
        api._mark_finalizing()
        api._start_async_finalizers()
        api._finish_run(True, "done")

    worker = threading.Thread(target=finish)
    api._worker_thread = worker
    worker.start()
    assert cleanup_entered.wait(1)

    assert api.run_state == "finalizing"
    assert api._worker_thread is worker
    assert terminal_events == []
    rejected = api.start_processing("", str(tmp_path / "output"), email_address="a@qq.com", auth_code="x", api_key="y")
    assert rejected["success"] is False

    release_cleanup.set()
    worker.join(1)
    assert not worker.is_alive()
    assert api.run_state == "completed"
    assert terminal_events == ["completed"]
    assert api._worker_thread is None


def test_app_api_finalizer_failure_reports_sanitized_failed_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    api._begin_run("running")
    calls = []
    terminal_events = []

    class Fetcher:
        def disconnect(self):
            calls.append("disconnect")

    def cleanup(staging_dir=None, temp_dir=None):
        calls.append("cleanup")
        raise RuntimeError("token=top-secret")

    monkeypatch.setattr(api, "_await_truth_audit", lambda: calls.append("report"))
    monkeypatch.setattr(api, "_cleanup_temp_folders", cleanup)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )

    api._mark_finalizing()
    api._start_async_finalizers(Fetcher())
    api._finish_run(True, "done")

    assert calls == ["report", "disconnect", "cleanup"]
    assert api.run_state == "failed"
    assert api.last_error.startswith("FINALIZER_FAILED:cleanup:RuntimeError:")
    assert "top-secret" not in api.last_error
    assert terminal_events == ["failed"]


def test_worker_finalizer_failure_removes_completed_facing_log(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    terminal_events = []

    class Fetcher:
        def __init__(self, *args, staging_dir, **kwargs):
            self.staging_dir = Path(staging_dir)
            self.staging_dir.mkdir(parents=True, exist_ok=True)

        def connect(self):
            return True

        def fetch_emails_by_date(self, **kwargs):
            return []

        def disconnect(self):
            return None

    def failed_cleanup(staging_dir=None, temp_dir=None):
        raise RuntimeError("credential=must-not-surface")

    monkeypatch.setattr("email_fetcher.EmailFetcher", Fetcher)
    monkeypatch.setattr(api, "_cleanup_temp_folders", failed_cleanup)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )

    api._processing_worker("", str(tmp_path / "output"), email_address="a@qq.com", auth_code="x", api_key="y")

    assert api.run_state == "failed"
    assert terminal_events == ["failed"]
    assert all(entry.get("type") != "完成" for entry in api.logs)
    assert "must-not-surface" not in api.last_error


@pytest.mark.parametrize(
    ("mode", "expected_state", "expected_status"),
    [
        ("cancel", "completed", "已安全停止"),
        ("zero", "completed", "处理完成"),
        ("success", "completed", "处理完成"),
        ("login_error", "failed", "邮箱登录失败"),
        ("error", "failed", "处理失败"),
    ],
)
def test_worker_paths_finalize_before_one_terminal_event(tmp_path, monkeypatch, mode, expected_state, expected_status):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    events = []
    disconnects = []

    class Fetcher:
        def __init__(self, *args, staging_dir, **kwargs):
            self.staging_dir = Path(staging_dir)
            self.staging_dir.mkdir(parents=True, exist_ok=True)

        def connect(self):
            return mode != "login_error"

        def fetch_emails_by_date(self, **kwargs):
            if mode == "error":
                raise RuntimeError("mailbox capability secret")
            if mode == "cancel":
                api._stop_requested = True
            return [] if mode in {"cancel", "zero"} else [b"1"]

        def extract_attachments(self, email_ids):
            return []

        def disconnect(self):
            disconnects.append((self.staging_dir, self.staging_dir.exists()))

    monkeypatch.setattr("email_fetcher.EmailFetcher", Fetcher)
    monkeypatch.setattr(api, "_start_truth_audit_async", lambda *args: None)
    monkeypatch.setattr(api, "_run_processing_loop", lambda *args: None)
    monkeypatch.setattr(api, "_cwt_cancellation_matching", lambda *args: None)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: events.append(new) if new in {"completed", "failed"} else None,
    )

    api._processing_worker("", str(tmp_path / "output"), email_address="a@qq.com", auth_code="x", api_key="y")

    assert api.run_state == expected_state
    assert api.status_text == expected_status
    assert events == [expected_state]
    assert len(disconnects) == 1
    staging_dir, existed_at_disconnect = disconnects[0]
    assert existed_at_disconnect is True
    assert not staging_dir.exists()


def test_each_run_owns_a_unique_staging_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()

    api._begin_run("first")
    first = api._active_run_handle.staging_dir
    api._mark_finalizing()
    api._start_async_finalizers()
    api._finish_run(True, "done")

    api._begin_run("second")
    second = api._active_run_handle.staging_dir
    marker = second / "owned-by-second-run.txt"
    marker.write_text("keep", encoding="utf-8")
    api._cleanup_temp_folders(staging_dir=first)

    assert first != second
    assert marker.exists()
