import json
import os
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
    with pytest.raises(RuntimeError, match="finalizing"):
        handle.advance(RunState.ARCHIVING)
    assert handle.state is RunState.FINALIZING
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
    snapshot = handle.snapshot
    assert snapshot.primary_failure is None
    assert [failure.callback for failure in snapshot.finalizer_failures] == ["report"]
    assert snapshot.finalizer_failures[0].reason_code == "FINALIZER_REPORT_FAILED"
    assert "FINALIZER_REPORT_FAILED" in handle.error
    assert "super-secret" not in handle.error


def test_primary_failure_stays_failed_after_successful_cleanup(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-1", tmp_path / "run-1")
    handle.advance(RunState.SCANNING)
    handle.fail(
        RuntimeError("mailbox password leaked here"),
        reason_code="PROCESSING_FAILED",
        user_message="处理过程中发生异常，请重试。",
    )

    assert handle.state is RunState.FINALIZING
    assert handle.finalize([]) is RunState.FAILED
    assert handle.snapshot.primary_failure.reason_code == "PROCESSING_FAILED"
    assert "处理过程中发生异常，请重试。" in handle.error
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


def test_advance_validation_and_mutation_are_atomic_under_interleaving(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    handle = RunLifecycle().begin("run-atomic", tmp_path / "run-atomic")
    released_once = threading.Event()
    resume_scanning = threading.Event()

    class PauseAfterScanningRelease:
        def __init__(self):
            self._inner = threading.RLock()
            self._paused = False

        def __enter__(self):
            self._inner.acquire()
            return self

        def __exit__(self, exc_type, exc, tb):
            self._inner.release()
            if threading.current_thread().name == "advance-scanning" and not self._paused:
                self._paused = True
                released_once.set()
                assert resume_scanning.wait(2)

    handle._lock = PauseAfterScanningRelease()
    scanning = threading.Thread(target=lambda: handle.advance(RunState.SCANNING), name="advance-scanning")
    scanning.start()
    assert released_once.wait(1)

    handle.advance(RunState.EXTRACTING)
    resume_scanning.set()
    scanning.join(1)

    assert not scanning.is_alive()
    assert handle.state is RunState.EXTRACTING


def test_snapshot_preserves_primary_and_ordered_finalizer_failures(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-combined", tmp_path / "run-combined")
    calls = []

    class ReportFailure(RuntimeError):
        reason_code = "TRUTH_AUDIT_TIMEOUT"
        user_message = "真值审计收尾超时。"

    handle.fail(
        RuntimeError("https://secret.example/path?token=KEY-SECRET"),
        reason_code="PROCESSING_FAILED",
        user_message="处理过程中发生异常，请重试。",
    )

    def report():
        calls.append("report")
        raise ReportFailure("token=REPORT-SECRET")

    def disconnect():
        calls.append("disconnect")
        raise RuntimeError("auth=MAIL-SECRET")

    def cleanup():
        calls.append("cleanup")

    assert handle.finalize([("report", report), ("disconnect", disconnect), ("cleanup", cleanup)]) is RunState.FAILED

    snapshot = handle.snapshot
    assert calls == ["report", "disconnect", "cleanup"]
    assert snapshot.primary_failure.reason_code == "PROCESSING_FAILED"
    assert [item.reason_code for item in snapshot.finalizer_failures] == [
        "TRUTH_AUDIT_TIMEOUT",
        "FINALIZER_DISCONNECT_FAILED",
    ]
    assert "处理过程中发生异常，请重试。" in handle.error
    assert "TRUTH_AUDIT_TIMEOUT" in handle.error
    assert "KEY-SECRET" not in handle.error
    assert "REPORT-SECRET" not in handle.error
    assert "MAIL-SECRET" not in handle.error


def test_staging_ownership_is_retired_only_after_finalization(tmp_path):
    RunLifecycle, RunState = lifecycle_types()
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("run-owned", tmp_path / "run-owned")
    entered = threading.Event()
    release = threading.Event()

    def blocked_cleanup():
        entered.set()
        assert release.wait(2)

    finalizer = threading.Thread(target=lambda: handle.finalize([blocked_cleanup]))
    finalizer.start()
    assert entered.wait(1)
    assert lifecycle.owned_staging_count == 1
    assert lifecycle.can_begin is False

    release.set()
    finalizer.join(1)
    assert handle.state is RunState.COMPLETED
    assert lifecycle.owned_staging_count == 0


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
    assert "FINALIZER_CLEANUP_FAILED" in api.last_error
    assert "临时文件清理失败" in api.last_error
    assert "top-secret" not in api.last_error
    assert terminal_events == ["failed"]


def test_app_api_combines_actionable_primary_and_cleanup_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    api._begin_run("running")
    terminal_events = []

    def cleanup(staging_dir=None, temp_dir=None):
        raise RuntimeError("https://secret.example/?api_key=TOP-SECRET")

    monkeypatch.setattr(api, "_cleanup_temp_folders", cleanup)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )

    api._fail_run(
        "处理失败",
        "https://secret.example/?token=PRIMARY-SECRET",
        reason_code="PROCESSING_FAILED",
        user_message="处理过程中发生异常，请重试；如持续失败请查看诊断报告。",
    )

    snapshot = api._active_run_handle.snapshot
    assert terminal_events == ["failed"]
    assert snapshot.primary_failure.reason_code == "PROCESSING_FAILED"
    assert [item.reason_code for item in snapshot.finalizer_failures] == ["FINALIZER_CLEANUP_FAILED"]
    assert "请重试" in api.last_error
    assert "临时文件清理失败" in api.last_error
    assert "secret.example" not in api.last_error
    assert "TOP-SECRET" not in api.last_error
    assert "PRIMARY-SECRET" not in api.last_error


def _controlled_context(run_root, run_id):
    run_root = Path(run_root)
    return {
        "enabled": True,
        "run_id": run_id,
        "run_root": str(run_root),
        "output_dir": str(run_root / "output"),
        "staging_dir": str(run_root / "staging"),
        "diagnostics_dir": str(run_root / "diagnostics"),
        "monitoring_dir": str(run_root / "monitoring"),
        "qc_dir": str(run_root / "monitoring" / "qc"),
        "debug_trace_path": str(run_root / "diagnostics" / "debug_trace.jsonl"),
    }


def test_truth_audit_timeout_is_bounded_and_late_worker_is_quarantined(tmp_path, monkeypatch):
    import audit_email_truth

    api = InvoiceAppAPI(truth_audit_timeout_seconds=0.02)
    old_root = tmp_path / "old-run"
    new_root = old_root
    api._run_context = _controlled_context(old_root, "old")
    api._current_run_id = "old"
    monkeypatch.setattr(api, "_refresh_run_context", lambda: api._run_context)
    audit_entered = threading.Event()
    release_audit = threading.Event()
    callbacks = []
    terminal_events = []

    def collect_truth_table(email, auth_code, date_from, date_to):
        audit_entered.set()
        assert release_audit.wait(2)
        return {"run": "old", "email_domain": email.split("@")[-1]}

    monkeypatch.setattr(audit_email_truth, "collect_truth_table", collect_truth_table)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )
    api._begin_run("running")
    old_handle = api._active_run_handle
    audit_thread = api._start_truth_audit_async("old@qq.com", "AUTH-SECRET")
    assert audit_entered.wait(1)

    class Fetcher:
        def disconnect(self):
            callbacks.append("disconnect")

    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **kwargs: callbacks.append("cleanup"))

    def finalize():
        api._mark_finalizing()
        api._start_async_finalizers(Fetcher())
        api._finish_run(True, "done")

    worker = threading.Thread(target=finalize, name="bounded-finalizer")
    api._worker_thread = worker
    worker.start()
    worker.join(0.25)
    bounded = not worker.is_alive()
    if not bounded:
        release_audit.set()
        worker.join(1)
    assert bounded, "truth-audit finalization exceeded its configured timeout"

    assert callbacks == ["disconnect", "cleanup"]
    assert api.run_state == "failed"
    assert terminal_events == ["failed"]
    assert api._worker_thread is None
    assert old_handle.snapshot.finalizer_failures[0].reason_code == "TRUTH_AUDIT_TIMEOUT"
    assert api.last_error.count("TRUTH_AUDIT_TIMEOUT") == 1

    api._run_context = _controlled_context(new_root, "new")
    api._current_run_id = "new"
    api._begin_run("new running")
    state_before = (api.run_state, api.status_text, list(api.logs))
    release_audit.set()
    audit_thread.join(1)

    assert not audit_thread.is_alive()
    quarantined_report = old_root / "monitoring" / "quarantined" / old_handle.run_id / "email_truth_audit.json"
    assert json.loads(quarantined_report.read_text(encoding="utf-8"))["run"] == "old"
    assert not (new_root / "monitoring" / "email_truth_audit.json").exists()
    assert (api.run_state, api.status_text, api.logs) == state_before


def test_truth_audit_deadline_does_not_wait_for_blocked_publication(tmp_path, monkeypatch):
    import audit_email_truth

    api = InvoiceAppAPI(truth_audit_timeout_seconds=0.02)
    shared_root = tmp_path / "shared-run-root"
    api._run_context = _controlled_context(shared_root, "old")
    api._current_run_id = "old"
    monkeypatch.setattr(api, "_refresh_run_context", lambda: api._run_context)
    monkeypatch.setattr(audit_email_truth, "collect_truth_table", lambda *args: {"run": "old"})
    publication_entered = threading.Event()
    release_publication = threading.Event()
    callbacks = []
    terminal_events = []
    original_write = api._diag_write_json

    def blocked_write(path, payload):
        publication_entered.set()
        assert release_publication.wait(2)
        original_write(path, payload)

    monkeypatch.setattr(api, "_diag_write_json", blocked_write)
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **kwargs: callbacks.append("cleanup"))
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )
    api._begin_run("running")
    old_handle = api._active_run_handle
    audit_thread = api._start_truth_audit_async("old@qq.com", "AUTH-SECRET")
    assert publication_entered.wait(1)

    def finalize():
        api._mark_finalizing()
        api._start_async_finalizers()
        api._finish_run(True, "done")

    worker = threading.Thread(target=finalize, name="hard-deadline-finalizer")
    api._worker_thread = worker
    worker.start()
    worker.join(0.20)
    bounded = not worker.is_alive()
    if not bounded:
        release_publication.set()
        worker.join(1)
    assert bounded, "timeout path waited on audit publication"

    assert api.run_state == "failed"
    assert terminal_events == ["failed"]
    assert api._worker_thread is None
    assert api._run_lifecycle.can_begin is True
    assert api._run_lifecycle.owned_staging_count == 0
    assert callbacks == ["cleanup"]
    assert old_handle.snapshot.finalizer_failures[0].reason_code == "TRUTH_AUDIT_TIMEOUT"

    api._run_context = _controlled_context(shared_root, "new")
    api._current_run_id = "new"
    api._begin_run("new running")
    state_before = (api.run_state, api.status_text, list(api.logs))
    release_publication.set()
    audit_thread.join(1)

    assert not audit_thread.is_alive()
    assert not (shared_root / "monitoring" / "email_truth_audit.json").exists()
    assert (api.run_state, api.status_text, api.logs) == state_before


def test_normal_path_promotion_stall_fails_closed_without_late_visibility(tmp_path, monkeypatch):
    import audit_email_truth

    api = InvoiceAppAPI(truth_audit_timeout_seconds=0.02)
    shared_root = tmp_path / "promotion-stall"
    api._run_context = _controlled_context(shared_root, "old")
    api._current_run_id = "old"
    monkeypatch.setattr(api, "_refresh_run_context", lambda: api._run_context)
    monkeypatch.setattr(audit_email_truth, "collect_truth_table", lambda *args: {"run": "old"})
    normal_path = (shared_root / "monitoring" / "email_truth_audit.json").resolve()
    promotion_entered = threading.Event()
    release_promotion = threading.Event()
    terminal_events = []
    original_replace = os.replace

    def blocked_normal_replace(source, destination):
        if Path(destination).resolve() == normal_path:
            promotion_entered.set()
            assert release_promotion.wait(2)
        return original_replace(source, destination)

    monkeypatch.setattr(os, "replace", blocked_normal_replace)
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **kwargs: None)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )
    api._begin_run("running")
    old_handle = api._active_run_handle
    audit_thread = api._start_truth_audit_async("old@qq.com", "AUTH-SECRET")
    assert old_handle.run_id
    assert api._truth_audit_job.ready.wait(1)

    def finalize():
        api._mark_finalizing()
        api._start_async_finalizers()
        api._finish_run(True, "done")

    worker = threading.Thread(target=finalize, name="promotion-stall-finalizer")
    api._worker_thread = worker
    worker.start()
    worker.join(0.20)
    bounded = not worker.is_alive()
    if not bounded:
        release_promotion.set()
        worker.join(1)
    assert bounded, "lifecycle waited for blocked normal-path promotion"
    assert promotion_entered.is_set() is False

    assert api.run_state == "completed"
    assert terminal_events == ["completed"]
    assert api._worker_thread is None
    assert api._run_lifecycle.can_begin is True
    assert api._run_lifecycle.owned_staging_count == 0
    assert old_handle.snapshot.finalizer_failures == ()
    immutable_artifact = (
        shared_root
        / "monitoring"
        / "quarantined"
        / old_handle.run_id
        / "email_truth_audit.json"
    )
    assert immutable_artifact.exists()

    api._run_context = _controlled_context(shared_root, "new")
    api._current_run_id = "new"
    api._begin_run("new running")
    state_before = (api.run_state, api.status_text, list(api.logs))
    release_promotion.set()
    audit_thread.join(1)

    assert not normal_path.exists()
    assert (api.run_state, api.status_text, api.logs) == state_before


def test_controlled_run_config_and_audit_share_confined_canonical_locator(tmp_path, monkeypatch):
    import audit_email_truth

    api = InvoiceAppAPI(truth_audit_timeout_seconds=0.05)
    run_root = tmp_path / "confined-run"
    external_monitoring = tmp_path / "external-monitoring"
    context = _controlled_context(run_root, "confined")
    context["monitoring_dir"] = str(external_monitoring)
    api._run_context = context
    api._current_run_id = "confined"
    monkeypatch.setattr(api, "_refresh_run_context", lambda: api._run_context)
    monkeypatch.setattr(audit_email_truth, "collect_truth_table", lambda *args: {"status": "ok", "rows": 1})
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **kwargs: None)

    api._begin_run("running")
    handle = api._active_run_handle
    api._safe_write_run_config("test@qq.com", auth_code="AUTH", api_key="API")
    api._start_truth_audit_async("test@qq.com", "AUTH")
    assert api._truth_audit_job.ready.wait(1)
    api._mark_finalizing()
    api._start_async_finalizers()
    api._finish_run(True, "done")

    confined_monitoring = run_root / "monitoring"
    run_config_path = confined_monitoring / "run_config.json"
    assert api.run_state == "completed"
    assert run_config_path.exists()
    run_config = json.loads(run_config_path.read_text(encoding="utf-8"))
    assert Path(run_config["monitoring_dir"]) == confined_monitoring
    index_path = Path(run_config["truth_audit_index_path"])
    assert index_path.exists()
    index = json.loads(index_path.read_text(encoding="utf-8"))
    artifact_path = Path(index["artifact_path"])
    assert artifact_path.exists()
    assert artifact_path == (
        confined_monitoring
        / "quarantined"
        / handle.run_id
        / "email_truth_audit.json"
    )
    assert Path(run_config["truth_audit_artifact_dir"]) == artifact_path.parent
    assert not (external_monitoring / "run_config.json").exists()
    assert not list(external_monitoring.rglob("email_truth_audit*.json"))
    assert not list(external_monitoring.rglob("truth_audit_index.json"))


def test_timely_valid_truth_audit_keeps_run_completed(tmp_path, monkeypatch):
    import audit_email_truth

    api = InvoiceAppAPI(truth_audit_timeout_seconds=0.05)
    run_root = tmp_path / "timely-success"
    api._run_context = _controlled_context(run_root, "timely")
    api._current_run_id = "timely"
    monkeypatch.setattr(api, "_refresh_run_context", lambda: api._run_context)
    monkeypatch.setattr(audit_email_truth, "collect_truth_table", lambda *args: {"status": "ok", "rows": 2})
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **kwargs: None)
    terminal_events = []
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )

    api._begin_run("running")
    api._safe_write_run_config("test@qq.com", auth_code="AUTH", api_key="API")
    api._start_truth_audit_async("test@qq.com", "AUTH")
    assert api._truth_audit_job.ready.wait(1)
    api._mark_finalizing()
    api._start_async_finalizers()
    api._finish_run(True, "done")

    run_config = json.loads((run_root / "monitoring" / "run_config.json").read_text(encoding="utf-8"))
    assert api.run_state == "completed"
    assert terminal_events == ["completed"]
    assert "TRUTH_AUDIT_COMPATIBILITY_PATH_DISABLED" not in api.last_error
    assert Path(run_config["truth_audit_index_path"]).exists()


def test_truth_audit_error_fails_with_truthful_sanitized_reason(tmp_path, monkeypatch):
    import audit_email_truth

    api = InvoiceAppAPI(truth_audit_timeout_seconds=0.05)
    run_root = tmp_path / "audit-error"
    api._run_context = _controlled_context(run_root, "audit-error")
    api._current_run_id = "audit-error"
    monkeypatch.setattr(api, "_refresh_run_context", lambda: api._run_context)

    def failed_audit(*args):
        raise RuntimeError("https://secret.example/?api_key=AUDIT-SECRET")

    monkeypatch.setattr(audit_email_truth, "collect_truth_table", failed_audit)
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **kwargs: None)

    api._begin_run("running")
    api._safe_write_run_config("test@qq.com", auth_code="AUTH", api_key="API")
    api._start_truth_audit_async("test@qq.com", "AUTH")
    assert api._truth_audit_job.ready.wait(1)
    api._mark_finalizing()
    api._start_async_finalizers()
    api._finish_run(True, "done")

    run_config = json.loads((run_root / "monitoring" / "run_config.json").read_text(encoding="utf-8"))
    index = json.loads(Path(run_config["truth_audit_index_path"]).read_text(encoding="utf-8"))
    assert api.run_state == "failed"
    assert "TRUTH_AUDIT_FAILED" in api.last_error
    assert "TRUTH_AUDIT_COMPATIBILITY_PATH_DISABLED" not in api.last_error
    assert "secret.example" not in api.last_error
    assert "AUDIT-SECRET" not in api.last_error
    assert index["status"] == "failed"
    assert index["reason_code"] == "TRUTH_AUDIT_FAILED"
    assert Path(index["artifact_path"]).exists()


def test_start_processing_thread_start_failure_releases_lifecycle_once(tmp_path, monkeypatch):
    api = InvoiceAppAPI(truth_audit_timeout_seconds=0.01)
    run_root = tmp_path / "start-failure"
    api._run_context = _controlled_context(run_root, "start-failure")
    api._current_run_id = "start-failure"
    monkeypatch.setattr(api, "_refresh_run_context", lambda: api._run_context)
    monkeypatch.setattr(api, "save_user_settings", lambda settings: {"success": True})
    monkeypatch.setattr(api, "_safe_write_run_config", lambda *args, **kwargs: None)
    cleanup_calls = []
    terminal_events = []
    original_cleanup = api._cleanup_temp_folders

    def cleanup(**kwargs):
        cleanup_calls.append("cleanup")
        return original_cleanup(**kwargs)

    def failed_start(thread):
        raise RuntimeError("https://secret.example/?api_key=THREAD-START-SECRET")

    monkeypatch.setattr(api, "_cleanup_temp_folders", cleanup)
    monkeypatch.setattr(threading.Thread, "start", failed_start)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )

    result = api.start_processing(
        "",
        str(run_root / "output"),
        email_address="test@qq.com",
        auth_code="AUTH-SECRET",
        api_key="API-SECRET",
    )

    assert result["success"] is False
    assert "THREAD-START-SECRET" not in str(result)
    assert api.run_state == "failed"
    assert terminal_events == ["failed"]
    assert cleanup_calls == ["cleanup"]
    assert api._worker_thread is None
    assert api._run_lifecycle.can_begin is True
    assert api._run_lifecycle.owned_staging_count == 0
    assert api._active_run_handle.state.name == "FAILED"
    assert not api._active_run_handle.staging_dir.exists()
    assert "WORKER_START_FAILED" in api.last_error
    assert "THREAD-START-SECRET" not in api.last_error


def test_stop_during_finalizing_does_not_claim_success_or_change_status(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    api._begin_run("running")
    entered = threading.Event()
    release = threading.Event()

    def blocked_cleanup(staging_dir=None, temp_dir=None):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(api, "_cleanup_temp_folders", blocked_cleanup)

    def finalize():
        api._mark_finalizing()
        api._start_async_finalizers()
        api._finish_run(True, "done")

    worker = threading.Thread(target=finalize)
    api._worker_thread = worker
    worker.start()
    assert entered.wait(1)
    status_before = api.status_text

    result = api.stop_processing()

    assert result["success"] is False
    assert api.run_state == "finalizing"
    assert api.status_text == status_before
    assert api._stop_requested is False
    progress = api.get_progress()
    assert progress["can_stop"] is False
    assert progress["run_state"] == "finalizing"
    release.set()
    worker.join(1)


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
    ("mode", "expected_state", "expected_status", "error_code", "actionable_text"),
    [
        ("cancel", "completed", "已安全停止", "", ""),
        ("zero", "completed", "处理完成", "", ""),
        ("success", "completed", "处理完成", "", ""),
        ("login_error", "failed", "邮箱登录失败", "IMAP_LOGIN_FAILED", "检查授权码"),
        ("quota", "failed", "GLM API 额度不足", "QUOTA_EXHAUSTED", "充值"),
        ("error", "failed", "处理失败", "PROCESSING_FAILED", "请重试"),
    ],
)
def test_worker_paths_finalize_before_one_terminal_event(
    tmp_path,
    monkeypatch,
    mode,
    expected_state,
    expected_status,
    error_code,
    actionable_text,
):
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
                raise RuntimeError("https://secret.example/path?token=API-KEY-SECRET")
            if mode == "cancel":
                api._stop_requested = True
            return [] if mode in {"cancel", "zero"} else [b"1"]

        def extract_attachments(self, email_ids):
            return []

        def disconnect(self):
            disconnects.append((self.staging_dir, self.staging_dir.exists()))

    monkeypatch.setattr("email_fetcher.EmailFetcher", Fetcher)
    monkeypatch.setattr(api, "_start_truth_audit_async", lambda *args: None)
    def processing_loop(*args):
        if mode == "quota":
            api.quota_exhausted = True
            api.quota_message = "GLM API 额度已耗尽，请充值或更换可用的 API Key。"

    monkeypatch.setattr(api, "_run_processing_loop", processing_loop)
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
    if error_code:
        assert error_code in api.last_error
        assert actionable_text in api.last_error
        assert "secret.example" not in api.last_error
        assert "API-KEY-SECRET" not in api.last_error


def test_processing_loop_internal_exception_reaches_worker_failed_terminal_once(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    terminal_events = []
    stage_events = []
    finalizers = []

    class RaisingLogList(list):
        def append(self, entry):
            if "Phase 2 撮合全部完成" in str(entry.get("msg", "")):
                raise RuntimeError("https://secret.example/?token=INNER-SECRET")
            super().append(entry)

    class Fetcher:
        def __init__(self, *args, staging_dir, **kwargs):
            self.staging_dir = Path(staging_dir)
            self.staging_dir.mkdir(parents=True, exist_ok=True)

        def connect(self):
            return True

        def fetch_emails_by_date(self, **kwargs):
            return [b"1"]

        def extract_attachments(self, email_ids):
            api._is_running = False
            api.logs = RaisingLogList(api.logs)
            return [
                {
                    "filepath": str(tmp_path / "missing-file.pdf"),
                    "file_name": "missing-file.pdf",
                    "source_url": "https://secret.example/?token=KEY",
                }
            ]

        def disconnect(self):
            finalizers.append("disconnect")

    class FakeExtractor:
        def __init__(self, api_key, output_dir):
            self.processed_records_file = ""

        def load_processed_records(self):
            return {}

    monkeypatch.setattr("email_fetcher.EmailFetcher", Fetcher)
    monkeypatch.setattr("invoice_extractor.InvoiceExtractor", FakeExtractor)
    monkeypatch.setattr(api, "_output_state_dir", lambda save_path: str(tmp_path / "state"))
    monkeypatch.setattr(api, "_load_output_run_state", lambda state_dir: {})
    monkeypatch.setattr(api, "_load_committed_history", lambda state_dir: set())
    monkeypatch.setattr(api, "_mark_output_run_state", lambda *args, **kwargs: None)
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **kwargs: finalizers.append("cleanup"))
    original_processing_loop = api._run_processing_loop
    propagated = []

    def observed_processing_loop(*args, **kwargs):
        try:
            return original_processing_loop(*args, **kwargs)
        except Exception as exc:
            propagated.append(type(exc).__name__)
            raise

    monkeypatch.setattr(api, "_run_processing_loop", observed_processing_loop)
    monkeypatch.setattr(
        api,
        "_safe_emit_run_state_event",
        lambda old, new: terminal_events.append(new) if new in {"completed", "failed"} else None,
    )
    monkeypatch.setattr(
        api,
        "_safe_emit_stage_event",
        lambda stage, event, extra=None: stage_events.append((stage, event, dict(extra or {}))),
    )

    api._processing_worker("", str(tmp_path / "output"), email_address="a@qq.com", auth_code="x", api_key="y")

    assert api.run_state == "failed"
    assert terminal_events == ["failed"]
    assert not any(extra.get("result") == "completed" for stage, event, extra in stage_events if stage == "frontend_processing_worker")
    assert finalizers == ["disconnect", "cleanup"]
    assert propagated == ["ProcessingLoopFailure"]
    assert "PROCESSING_FAILED" in api.last_error
    assert "请重试" in api.last_error
    assert "secret.example" not in api.last_error


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
