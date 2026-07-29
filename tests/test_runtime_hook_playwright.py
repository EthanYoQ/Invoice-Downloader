import os
import runpy
import sys
import types
from pathlib import Path


HOOK_PATH = Path(__file__).resolve().parents[1] / "build" / "windows" / "runtime_hook_playwright.py"


def test_runtime_hook_uses_frozen_runtime_and_runs_requested_browser_smoke(monkeypatch, tmp_path):
    events = []

    class Browser:
        def close(self):
            events.append("close")

    class Chromium:
        def launch(self, *, headless):
            events.append(("launch", headless))
            return Browser()

    class PlaywrightContext:
        chromium = Chromium()

        def __enter__(self):
            events.append("enter")
            return self

        def __exit__(self, exc_type, exc, traceback):
            events.append("exit")

    sync_api = types.ModuleType("playwright.sync_api")
    sync_api.sync_playwright = PlaywrightContext
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    monkeypatch.setenv("INVOICEFLOWAI_PLAYWRIGHT_RUNTIME_SMOKE", "1")

    runpy.run_path(str(HOOK_PATH))

    assert events == ["enter", ("launch", True), "close", "exit"]
    assert Path(os.environ["PLAYWRIGHT_BROWSERS_PATH"]) == tmp_path / "runtime" / "ms-playwright"
