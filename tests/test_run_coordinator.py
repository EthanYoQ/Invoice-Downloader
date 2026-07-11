from __future__ import annotations

from dataclasses import asdict, fields
import importlib
import inspect
import threading
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


def _run_reserved(coordinator, request, staging_dir):
    handle = coordinator._lifecycle.begin(request.run_id, staging_dir)
    coordinator._state.reset(request.run_id)
    return coordinator.run(request, handle=handle)


def test_coordinator_modules_do_not_import_pywebview():
    for name in ("run_coordinator", "run_state_store", "report_service"):
        source = inspect.getsource(importlib.import_module(name))
        assert "import webview" not in source
        assert "from app_api" not in source


def test_run_request_is_frozen_and_contains_no_secret_fields(tmp_path: Path):
    from run_coordinator import RunRequest

    request = RunRequest(
        run_id="run-1",
        date_from="2026-06-01",
        date_to="2026-06-13",
        save_path=str(tmp_path),
        rules_text="rules",
        account_id="q***@qq.com",
        channel_id="qq",
    )

    assert {field.name for field in fields(request)} == {
        "run_id",
        "date_from",
        "date_to",
        "save_path",
        "rules_text",
        "account_id",
        "channel_id",
    }
    serialized = repr(request) + repr(asdict(request))
    assert "auth_code" not in serialized
    assert "api_key" not in serialized
    with pytest.raises((AttributeError, TypeError)):
        request.run_id = "changed"


def test_process_only_dependencies_are_redacted_and_not_dataclasses():
    from run_coordinator import RunDependencies

    secret = "AUTH-SECRET-123"
    deps = RunDependencies(secrets={"auth_code": secret}, connect=lambda _request: object())

    assert secret not in repr(deps)
    assert "auth_code" not in repr(deps)
    with pytest.raises(TypeError):
        asdict(deps)


def _make_dependencies(calls, *, archive_report=None, cancel=lambda: False):
    from report_service import ReportService
    from run_coordinator import RunDependencies

    session = object()

    def connect(_request):
        calls.append("connect")
        return session

    def scan(active_session, _request):
        assert active_session is session
        calls.append("scan")
        return ["mail"]

    def candidates(messages, _request):
        assert messages == ["mail"]
        calls.append("candidate")
        return ["candidate"]

    def extract(items, _request):
        assert items == ["candidate"]
        calls.append("extract")
        return ["outcome"]

    def archive(outcomes, _request):
        assert outcomes == ["outcome"]
        calls.append("archive")
        return archive_report or SimpleNamespace(can_complete=True, archived_count=1)

    report_service = ReportService(
        report_callback=lambda _context, _result: calls.append("report"),
        disconnect_callback=lambda _context, _session: calls.append("disconnect"),
        cleanup_callback=lambda _context: calls.append("cleanup"),
        timeout_seconds=0.2,
    )
    return RunDependencies(
        connect=connect,
        scan=scan,
        candidate=candidates,
        extract=extract,
        archive=archive,
        report_service=report_service,
        cancel_requested=cancel,
    )


def test_coordinator_runs_real_stages_and_finalizers_before_completed(tmp_path: Path):
    from run_coordinator import RunCoordinator, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    calls = []
    lifecycle = RunLifecycle()
    store = RunStateStore()
    coordinator = RunCoordinator(lifecycle, store, _make_dependencies(calls))
    request = RunRequest("run-1", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")

    result = _run_reserved(coordinator, request, tmp_path / "staging")

    assert calls == ["connect", "scan", "candidate", "extract", "archive", "report", "disconnect", "cleanup"]
    assert result.state is RunState.COMPLETED
    assert result.email_count == 1
    assert result.candidate_count == 1
    assert result.archive_report.archived_count == 1
    snapshot = store.snapshot()
    assert snapshot["run_state"] == "completed"
    assert snapshot["progress"] == 100
    assert lifecycle.can_begin is True


def test_zero_mail_skips_pipeline_but_still_finalizes(tmp_path: Path):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    calls = []
    deps = RunDependencies(
        connect=lambda _request: calls.append("connect") or object(),
        scan=lambda _session, _request: calls.append("scan") or [],
        candidate=lambda *_args: pytest.fail("candidate must be skipped"),
        extract=lambda *_args: pytest.fail("extract must be skipped"),
        archive=lambda *_args: pytest.fail("archive must be skipped"),
        report_service=ReportService(
            report_callback=lambda *_args: calls.append("report"),
            disconnect_callback=lambda *_args: calls.append("disconnect"),
            cleanup_callback=lambda *_args: calls.append("cleanup"),
        ),
    )
    lifecycle = RunLifecycle()
    coordinator = RunCoordinator(lifecycle, RunStateStore(), deps)
    request = RunRequest("zero", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    result = _run_reserved(coordinator, request, tmp_path / "staging")

    assert result.state is RunState.COMPLETED
    assert result.email_count == 0
    assert calls == ["connect", "scan", "report", "disconnect", "cleanup"]


@pytest.mark.parametrize("stop_after", ["connect", "scan", "candidate", "extract"])
def test_stop_at_each_boundary_prevents_later_side_effects(tmp_path: Path, stop_after: str):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    calls = []
    stopped = threading.Event()

    def stage(name, result):
        def invoke(*_args):
            calls.append(name)
            if name == stop_after:
                stopped.set()
            return result
        return invoke

    deps = RunDependencies(
        connect=stage("connect", object()),
        scan=stage("scan", ["mail"]),
        candidate=stage("candidate", ["candidate"]),
        extract=stage("extract", ["outcome"]),
        archive=stage("archive", SimpleNamespace(can_complete=True, archived_count=1)),
        report_service=ReportService(
            report_callback=lambda *_args: calls.append("report"),
            disconnect_callback=lambda *_args: calls.append("disconnect"),
            cleanup_callback=lambda *_args: calls.append("cleanup"),
        ),
        cancel_requested=stopped.is_set,
    )
    lifecycle = RunLifecycle()
    coordinator = RunCoordinator(lifecycle, RunStateStore(), deps)
    request = RunRequest("stop", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    result = _run_reserved(coordinator, request, tmp_path / "staging")

    assert result.state is RunState.COMPLETED
    assert result.cancelled is True
    stage_order = ["connect", "scan", "candidate", "extract", "archive"]
    assert calls[: stage_order.index(stop_after) + 1] == stage_order[: stage_order.index(stop_after) + 1]
    assert not any(name in calls for name in stage_order[stage_order.index(stop_after) + 1 :])
    assert calls[-3:] == ["report", "disconnect", "cleanup"]


@pytest.mark.parametrize("broken_stage", ["connect", "scan", "candidate", "extract", "archive"])
def test_stage_exception_maps_to_one_failed_without_completed(tmp_path: Path, broken_stage: str):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    events = []

    def stage(name, result):
        def invoke(*_args):
            if name == broken_stage:
                raise RuntimeError("SECRET failure payload")
            return result
        return invoke

    store = RunStateStore(event_sink=lambda event: events.append(event))
    deps = RunDependencies(
        connect=stage("connect", object()),
        scan=stage("scan", ["mail"]),
        candidate=stage("candidate", ["candidate"]),
        extract=stage("extract", ["outcome"]),
        archive=stage("archive", SimpleNamespace(can_complete=True, archived_count=1)),
        report_service=ReportService(),
    )
    lifecycle = RunLifecycle()
    coordinator = RunCoordinator(lifecycle, store, deps)
    request = RunRequest("failed", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    result = _run_reserved(coordinator, request, tmp_path / "staging")

    assert result.state is RunState.FAILED
    assert "SECRET" not in repr(result)
    terminal = [event for event in events if event.get("run_state") in {"completed", "failed"}]
    assert [event["run_state"] for event in terminal] == ["failed"]


def test_archive_incomplete_fails_closed(tmp_path: Path):
    from run_coordinator import RunCoordinator, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    report = SimpleNamespace(can_complete=False, archived_count=0, unresolved_count=1)
    lifecycle = RunLifecycle()
    coordinator = RunCoordinator(lifecycle, RunStateStore(), _make_dependencies([], archive_report=report))
    request = RunRequest("incomplete", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    result = _run_reserved(coordinator, request, tmp_path / "staging")
    assert result.state is RunState.FAILED
    assert result.reason_code == "ARCHIVE_INCOMPLETE"


def test_finalizer_timeout_continues_order_and_fails_closed(tmp_path: Path):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    calls = []
    blocked = threading.Event()
    release = threading.Event()

    def report(*_args):
        calls.append("report")
        blocked.set()
        release.wait(1)

    deps = RunDependencies(
        connect=lambda _request: object(),
        scan=lambda *_args: [],
        report_service=ReportService(
            report_callback=report,
            disconnect_callback=lambda *_args: calls.append("disconnect"),
            cleanup_callback=lambda *_args: calls.append("cleanup"),
            timeout_seconds=0.02,
        ),
    )
    lifecycle = RunLifecycle()
    coordinator = RunCoordinator(lifecycle, RunStateStore(), deps)
    request = RunRequest("timeout", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    result = _run_reserved(coordinator, request, tmp_path / "staging")
    release.set()

    assert blocked.is_set()
    assert calls[:3] == ["report", "disconnect", "cleanup"]
    assert result.state is RunState.FAILED
    assert "FINALIZER_REPORT_TIMEOUT" in result.error


def test_finalizer_thread_start_failure_is_sanitized_and_fails_closed(tmp_path: Path, monkeypatch):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    monkeypatch.setattr(
        "report_service.threading.Thread.start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("START-SECRET")),
    )
    deps = RunDependencies(
        connect=lambda _request: object(),
        scan=lambda *_args: [],
        report_service=ReportService(report_callback=lambda *_args: None),
    )
    lifecycle = RunLifecycle()
    coordinator = RunCoordinator(lifecycle, RunStateStore(), deps)
    request = RunRequest("start-fail", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    result = _run_reserved(coordinator, request, tmp_path / "staging")

    assert result.state is RunState.FAILED
    assert "START-SECRET" not in result.error
    assert "FINALIZER_REPORT_FAILED" in result.error
    snapshot = coordinator._state.snapshot()
    assert snapshot["status_text"] == "处理失败"
    assert "FINALIZER_REPORT_FAILED" in snapshot["logs"][-1]["msg"]
    assert "START-SECRET" not in repr(snapshot)


def test_one_active_run_race_allows_only_one(tmp_path: Path):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle
    from run_state_store import RunStateStore

    entered = threading.Event()
    release = threading.Event()
    lifecycle = RunLifecycle()
    coordinator = RunCoordinator(
        lifecycle,
        RunStateStore(),
        RunDependencies(
            connect=lambda _request: object(),
            scan=lambda *_args: entered.set() or release.wait(1) or [],
            report_service=ReportService(),
        ),
    )
    first = RunRequest("first", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    second = RunRequest("second", "2026-06-01", "2026-06-13", str(tmp_path), "", "q***", "qq")
    first_handle = lifecycle.begin(first.run_id, tmp_path / "one")
    coordinator._state.reset(first.run_id)
    worker = threading.Thread(target=lambda: coordinator.run(first, handle=first_handle))
    worker.start()
    assert entered.wait(1)
    with pytest.raises(RuntimeError, match="already active"):
        coordinator.run(second, handle=first_handle)
    release.set()
    worker.join(1)


def test_state_store_snapshots_are_deep_independent_monotonic_and_terminal(tmp_path: Path):
    from run_state_store import RunStateStore

    events = []
    store = RunStateStore(event_sink=lambda event: events.append(event))
    store.reset("run-1")
    store.update(progress=50, status_text="half", statistics={"emails": 2, "nested": {"x": [1]}})
    store.update(progress=10)
    store.add_processed({"path": str(tmp_path), "meta": {"items": [1]}})
    snapshot = store.snapshot()
    snapshot["stats"]["nested"]["x"].append(2)
    snapshot["processed_invoices"][0]["meta"]["items"].append(2)

    assert store.snapshot()["progress"] == 50
    assert store.snapshot()["stats"]["nested"]["x"] == [1]
    assert store.snapshot()["processed_invoices"][0]["meta"]["items"] == [1]
    store.terminal("completed", status_text="done")
    event_count = len(events)
    store.update(progress=99, status_text="late")
    store.append_log("late", "must be ignored")
    assert len(events) == event_count
    assert store.snapshot()["status_text"] == "done"


def test_state_store_callback_failure_is_isolated_and_sanitized():
    from run_state_store import RunStateStore

    store = RunStateStore(event_sink=lambda _event: (_ for _ in ()).throw(RuntimeError("CALLBACK-SECRET")))
    store.reset("run")
    store.update(progress=20)
    snapshot = store.snapshot()
    assert snapshot["progress"] == 20
    assert "CALLBACK-SECRET" not in repr(snapshot)


def test_secret_dependencies_never_enter_state_events_or_result(tmp_path: Path):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle
    from run_state_store import RunStateStore

    events = []
    secret = "AUTH-AND-API-SECRET"
    lifecycle = RunLifecycle()
    store = RunStateStore(event_sink=events.append)
    coordinator = RunCoordinator(
        lifecycle,
        store,
        RunDependencies(
            connect=lambda _request: (_ for _ in ()).throw(RuntimeError(secret)),
            report_service=ReportService(),
            secrets={"auth_code": secret, "api_key": secret},
        ),
    )
    request = RunRequest("secret", "2026-06-01", "2026-06-13", str(tmp_path), "", "account-hash", "qq")
    result = _run_reserved(coordinator, request, tmp_path / "staging")

    rendered = json.dumps(events, ensure_ascii=False, default=str) + repr(result)
    assert secret not in rendered
    assert "auth_code" not in rendered
    assert "api_key" not in rendered


def test_app_api_progress_snapshot_is_independent_and_keeps_exact_frontend_shape():
    from app_api import InvoiceAppAPI

    api = InvoiceAppAPI()
    api._is_running = True
    api.run_state = "running"
    api.progress = 30
    api.logs = [{"time": "[00:00:00]", "type": "信息", "color": "blue", "msg": "safe"}]
    api.stats = {"emails": 1, "invoices": 2, "errors": 3}
    first = api.get_progress()
    first["logs"][0]["msg"] = "mutated"
    first["stats"]["emails"] = 999
    second = api.get_progress()

    assert set(second) == {
        "progress",
        "status_text",
        "logs",
        "new_categories",
        "stats",
        "is_running",
        "run_state",
        "last_error",
        "stop_requested",
        "can_stop",
        "quota_exhausted",
        "quota_message",
        "build_identity",
        "raw_date_range",
        "imap_query_range",
    }
    assert second["logs"][0]["msg"] == "safe"
    assert second["stats"]["emails"] == 1


def test_app_api_worker_delegates_to_coordinator_not_legacy_whole_run():
    from app_api import InvoiceAppAPI

    facade_source = inspect.getsource(InvoiceAppAPI._processing_worker)
    source = inspect.getsource(InvoiceAppAPI._run_coordinator_worker)
    assert "_run_coordinator_worker" in facade_source
    assert "RunCoordinator" in source
    assert ".run(" in source
    assert "fetch_emails_by_date" not in source
    assert "extract_attachments" not in source
    assert "_run_processing_loop(" not in source


def test_app_api_coordinator_setup_failure_releases_run_without_secret(tmp_path: Path, monkeypatch):
    from app_api import InvoiceAppAPI
    from run_lifecycle import RunState

    api = InvoiceAppAPI()
    monkeypatch.setattr(
        api,
        "_build_run_dependencies",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("SETUP-SECRET")),
    )
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **_kwargs: None)
    handle = api._prepare_run_lifecycle()
    api._run_state_store.reset(handle.run_id)

    api._processing_worker(
        "",
        str(tmp_path / "output"),
        email_address="a@qq.com",
        auth_code="auth",
        api_key="key",
        run_handle=handle,
    )

    assert api._active_run_handle.state is RunState.FAILED
    assert api._run_lifecycle.can_begin is True
    assert api.run_state == "failed"
    assert "SETUP-SECRET" not in api.last_error


def test_prepare_run_lifecycle_rejects_any_existing_handle(tmp_path: Path, monkeypatch):
    from app_api import InvoiceAppAPI

    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    first = api._prepare_run_lifecycle()

    with pytest.raises(RuntimeError, match="already assigned"):
        api._prepare_run_lifecycle()

    assert api._active_run_handle is first


def test_start_processing_admission_is_atomic_under_real_thread_race(tmp_path: Path, monkeypatch):
    from app_api import InvoiceAppAPI

    monkeypatch.chdir(tmp_path)
    api = InvoiceAppAPI()
    entered = threading.Event()
    release = threading.Event()
    worker_calls = []

    def blocked_worker(*args):
        worker_calls.append(args)
        entered.set()
        release.wait(2)

    monkeypatch.setattr(api, "_processing_worker", blocked_worker)
    monkeypatch.setattr(api, "_start_truth_audit_async", lambda *_args: None)
    monkeypatch.setattr(api, "save_user_settings", lambda *_args, **_kwargs: {"success": True})
    barrier = threading.Barrier(3)
    results = []

    def caller():
        barrier.wait()
        results.append(
            api.start_processing(
                "",
                str(tmp_path / "output"),
                "2026-06-01",
                "2026-06-13",
                "race-user@qq.com",
                "AUTH-RACE-SECRET",
                "API-RACE-SECRET",
            )
        )

    callers = [threading.Thread(target=caller) for _ in range(2)]
    for caller_thread in callers:
        caller_thread.start()
    barrier.wait()
    assert entered.wait(1)
    for caller_thread in callers:
        caller_thread.join(1)
    release.set()
    if api._worker_thread is not None:
        api._worker_thread.join(1)

    assert sorted(result["success"] for result in results) == [False, True]
    assert sum(result.get("message") == "任务已在运行中" for result in results) == 1
    assert len(worker_calls) == 1
    assert api._run_lifecycle.owned_staging_count == 1


def test_state_store_terminalize_commits_reason_log_and_status_in_one_event():
    from run_state_store import RunStateStore

    events = []
    store = RunStateStore(event_sink=events.append)
    store.reset("terminal")
    store.update(run_state="running", progress=30)
    before = len(events)
    store.terminalize(
        "failed",
        status_text="邮箱登录失败",
        last_error="邮箱登录失败，请检查授权码和 IMAP 设置。 [IMAP_LOGIN_FAILED]",
        reason_code="IMAP_LOGIN_FAILED",
        logs=[
            {
                "time": "[00:00:00]",
                "type": "错误",
                "color": "text-red-400",
                "msg": "邮箱登录失败，请检查授权码和 IMAP 设置。",
            }
        ],
    )

    assert len(events) == before + 1
    snapshot = store.snapshot()
    assert snapshot["run_state"] == "failed"
    assert snapshot["status_text"] == "邮箱登录失败"
    assert snapshot["logs"][-1]["msg"] == "邮箱登录失败，请检查授权码和 IMAP 设置。"
    assert store.terminal_reason == "IMAP_LOGIN_FAILED"
    store.append_log("late", "forbidden")
    assert store.snapshot() == snapshot


@pytest.mark.parametrize(
    ("reason_code", "user_message", "expected_status", "expected_log"),
    [
        ("IMAP_LOGIN_FAILED", "邮箱登录失败，请检查授权码和 IMAP 设置。", "邮箱登录失败", "检查授权码"),
        ("QUOTA_EXHAUSTED", "GLM API 额度已耗尽，请充值或更换可用的 API Key。", "GLM API 额度不足", "充值"),
        ("PROCESSING_FAILED", "处理过程中发生异常，请重试；如持续失败请查看诊断报告。", "处理失败", "请重试"),
    ],
)
def test_coordinator_is_single_terminal_presentation_owner(
    tmp_path: Path,
    reason_code: str,
    user_message: str,
    expected_status: str,
    expected_log: str,
):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle
    from run_state_store import RunStateStore

    class StageFailure(RuntimeError):
        pass

    failure = StageFailure("PRIVATE-FAILURE-PAYLOAD")
    failure.reason_code = reason_code
    failure.user_message = user_message
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("terminal-owner", tmp_path / "staging")
    events = []
    store = RunStateStore(event_sink=events.append)
    store.reset(handle.run_id)
    result = RunCoordinator(
        lifecycle,
        store,
        RunDependencies(
            connect=lambda _request: (_ for _ in ()).throw(failure),
            report_service=ReportService(),
        ),
    ).run(
        RunRequest(handle.run_id, "2026-06-01", "2026-06-13", str(tmp_path), "", "account", "qq"),
        handle=handle,
    )

    snapshot = store.snapshot()
    assert snapshot["status_text"] == expected_status
    assert expected_log in snapshot["logs"][-1]["msg"]
    assert "PRIVATE-FAILURE-PAYLOAD" not in repr(snapshot) + repr(result)
    assert [event["run_state"] for event in events if event["run_state"] in {"failed", "completed"}] == ["failed"]


def test_missing_credentials_keeps_idle_legacy_and_frontend_parity(tmp_path: Path):
    from app_api import InvoiceAppAPI

    api = InvoiceAppAPI()
    result = api.start_processing("", str(tmp_path), email_address="", auth_code="", api_key="")
    frontend = api.get_progress()

    assert result == {"success": False, "message": "缺少必要凭证，请填写邮箱、授权码和 API Key"}
    assert api.run_state == frontend["run_state"] == "idle"
    assert frontend["logs"] == api.logs == []
    assert api._run_lifecycle.can_begin is True


def test_pipeline_close_is_independent_and_precedes_report_start_failure(tmp_path: Path, monkeypatch):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    calls = []
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("close-order", tmp_path / "staging")
    store = RunStateStore()
    store.reset(handle.run_id)
    monkeypatch.setattr(
        "report_service.threading.Thread.start",
        lambda _thread: (_ for _ in ()).throw(RuntimeError("REPORT-START-SECRET")),
    )
    resources = {"runtime_open": True, "session_open": True}

    def close_pipeline():
        calls.append("pipeline_close")
        resources["runtime_open"] = False

    def disconnect(*_args):
        calls.append("disconnect")
        resources["session_open"] = False

    deps = RunDependencies(
        connect=lambda _request: object(),
        scan=lambda *_args: [],
        pipeline_close=close_pipeline,
        report_service=ReportService(
            report_callback=lambda *_args: calls.append("report"),
            disconnect_callback=disconnect,
            cleanup_callback=lambda *_args: calls.append("cleanup"),
        ),
    )
    result = RunCoordinator(lifecycle, store, deps).run(
        RunRequest(handle.run_id, "2026-06-01", "2026-06-13", str(tmp_path), "", "account", "qq"),
        handle=handle,
    )

    assert calls == ["pipeline_close", "disconnect", "cleanup"]
    assert result.state is RunState.FAILED
    assert lifecycle.can_begin is True
    assert "REPORT-START-SECRET" not in result.error
    assert resources == {"runtime_open": False, "session_open": False}


def test_pipeline_close_failure_aggregates_and_other_finalizers_continue(tmp_path: Path):
    from report_service import ReportService
    from run_coordinator import RunCoordinator, RunDependencies, RunRequest
    from run_lifecycle import RunLifecycle, RunState
    from run_state_store import RunStateStore

    calls = []
    lifecycle = RunLifecycle()
    handle = lifecycle.begin("close-fail", tmp_path / "staging")
    store = RunStateStore()
    store.reset(handle.run_id)

    def close_pipeline():
        calls.append("pipeline_close")
        raise RuntimeError("PIPELINE-CLOSE-SECRET")

    result = RunCoordinator(
        lifecycle,
        store,
        RunDependencies(
            connect=lambda _request: object(),
            scan=lambda *_args: [],
            pipeline_close=close_pipeline,
            report_service=ReportService(
                report_callback=lambda *_args: calls.append("report"),
                disconnect_callback=lambda *_args: calls.append("disconnect"),
                cleanup_callback=lambda *_args: calls.append("cleanup"),
            ),
        ),
    ).run(
        RunRequest(handle.run_id, "2026-06-01", "2026-06-13", str(tmp_path), "", "account", "qq"),
        handle=handle,
    )

    assert calls == ["pipeline_close", "report", "disconnect", "cleanup"]
    assert result.state is RunState.FAILED
    assert "FINALIZER_PIPELINE_CLOSE_FAILED" in result.error
    assert "PIPELINE-CLOSE-SECRET" not in result.error
    snapshot = store.snapshot()
    assert snapshot["status_text"] == "处理失败"
    assert snapshot["last_error"] == result.error


def test_full_account_and_secrets_never_enter_runtime_surfaces(tmp_path: Path, monkeypatch):
    from app_api import InvoiceAppAPI

    email = "distinctive.private.user+invoice@qq.com"
    auth = "DISTINCTIVE-AUTH-SECRET"
    api_key = "DISTINCTIVE-API-SECRET"
    api = InvoiceAppAPI()
    events = []
    diagnostics = []

    class Fetcher:
        def __init__(self, *args, progress_callback=None, **kwargs):
            self.progress_callback = progress_callback

        def connect(self):
            self.progress_callback(f"connected account {email} using {auth}")
            return False

        def disconnect(self):
            pass

    monkeypatch.setattr("email_fetcher.EmailFetcher", Fetcher)
    monkeypatch.setattr(api, "_start_truth_audit_async", lambda *_args: None)
    monkeypatch.setattr(api, "_cleanup_temp_folders", lambda **_kwargs: None)
    monkeypatch.setattr(api, "_safe_emit_stage_event", lambda stage, event, extra=None: events.append((stage, event, extra)))
    monkeypatch.setattr(api, "_packaged_diag_reset", lambda summary=None: diagnostics.append(("reset", summary)))
    monkeypatch.setattr(api, "_packaged_diag_write", lambda *args, **kwargs: diagnostics.append((args, kwargs)))
    monkeypatch.setattr(api, "_safe_write_run_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(api, "save_user_settings", lambda *_args, **_kwargs: {"success": True})
    start_result = api.start_processing(
        "",
        str(tmp_path),
        "2026-06-01",
        "2026-06-13",
        email,
        auth,
        api_key,
    )
    for _ in range(100):
        if api.run_state in {"completed", "failed"}:
            break
        threading.Event().wait(0.01)
    assert start_result["success"] is True
    assert api.run_state == "failed"
    surfaces = {
        "legacy": {
            "status": api.status_text,
            "logs": api.logs,
            "last_error": api.last_error,
        },
        "frontend": api.get_progress(),
        "store": api._run_state_store.snapshot(),
        "events": events,
        "diagnostics": diagnostics,
        "handle": api._active_run_handle.snapshot,
    }
    rendered = json.dumps(surfaces, ensure_ascii=False, default=str) + repr(surfaces)

    assert email not in rendered
    assert auth not in rendered
    assert api_key not in rendered
    assert "qq" in rendered.lower()
