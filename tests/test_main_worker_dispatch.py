import importlib
import sys
import types


def test_worker_dispatch_lazily_imports_worker_without_ui(monkeypatch, tmp_path):
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "url_recovery_worker", raising=False)
    monkeypatch.delitem(sys.modules, "pdf_converter", raising=False)
    monkeypatch.setitem(sys.modules, "webview", None)

    main = importlib.import_module("main")

    assert "url_recovery_worker" not in sys.modules
    assert "pdf_converter" not in sys.modules

    called = []
    worker = types.ModuleType("url_recovery_worker")
    worker.run_url_recovery_job = lambda path: called.append(path) or 0
    monkeypatch.setitem(sys.modules, "url_recovery_worker", worker)

    assert main.main(["--url-recovery-worker", str(tmp_path / "job.json")]) == 0
    assert called == [str(tmp_path / "job.json")]


def test_worker_dispatch_failure_is_stderr_only_and_returns_nonzero(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.delitem(sys.modules, "main", raising=False)
    monkeypatch.delitem(sys.modules, "url_recovery_worker", raising=False)
    monkeypatch.setitem(sys.modules, "webview", None)
    main = importlib.import_module("main")

    worker = types.ModuleType("url_recovery_worker")

    def fail_worker(_path):
        raise RuntimeError("worker exploded")

    worker.run_url_recovery_job = fail_worker
    monkeypatch.setitem(sys.modules, "url_recovery_worker", worker)
    shown_errors = []
    monkeypatch.setattr(main, "_show_startup_error", shown_errors.append)

    result = main.main(["--url-recovery-worker", str(tmp_path / "job.json")])

    captured = capsys.readouterr()
    assert result != 0
    assert "URL recovery worker failed" in captured.err
    assert captured.out == ""
    assert shown_errors == []
