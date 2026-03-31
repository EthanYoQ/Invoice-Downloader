import argparse
import ctypes
import json
import os
import sys
import time
from pathlib import Path

from frontend_run_context import set_explicit_run_context_path
from user_settings import get_runtime_diagnostics_dir


WINDOW_BASELINE_WIDTH = 1360
WINDOW_BASELINE_HEIGHT = 840
WINDOW_MARGIN = 24
WINDOW_MIN_WIDTH = 900
WINDOW_MIN_HEIGHT = 620
WINDOW_TITLE = "InvoiceFlowAI"
WEBVIEW2_RUNTIME_RELATIVE_DIR = Path("runtime") / "webview2-fixed"
STARTUP_DIAGNOSTIC_CONTEXT = {}
PACKAGED_UNBLOCK_SUFFIXES = {".dll", ".pyd", ".exe", ".json"}


def _coalesce_run_context_path(argv):
    if not argv:
        return ""

    for index, token in enumerate(argv):
        if token.startswith("--run-context="):
            return token.split("=", 1)[1].strip()
        if token != "--run-context":
            continue

        collected_tokens = []
        for candidate in argv[index + 1 :]:
            if candidate.startswith("--"):
                break
            collected_tokens.append(candidate)
        return " ".join(collected_tokens).strip()

    return ""


def _parse_args():
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument(
        "--run-context",
        dest="run_context_path",
        default="",
        help="Internal QA run-context JSON path.",
    )
    args, unknown_args = parser.parse_known_args()
    coalesced_run_context_path = _coalesce_run_context_path(sys.argv[1:])
    if coalesced_run_context_path:
        args.run_context_path = coalesced_run_context_path
    elif unknown_args and str(args.run_context_path or "").strip():
        args.run_context_path = " ".join([str(args.run_context_path).strip(), *unknown_args]).strip()
    return args


def _set_windows_dpi_awareness():
    if os.name != "nt":
        return "not_windows"

    try:
        user32 = ctypes.windll.user32
    except Exception:
        return "unavailable"

    try:
        set_context = user32.SetProcessDpiAwarenessContext
        set_context.argtypes = [ctypes.c_void_p]
        set_context.restype = ctypes.c_bool
        if set_context(ctypes.c_void_p(-4)):
            return "per_monitor_v2"
    except Exception:
        pass

    try:
        shcore = ctypes.windll.shcore
        set_awareness = shcore.SetProcessDpiAwareness
        set_awareness.argtypes = [ctypes.c_int]
        set_awareness.restype = ctypes.c_long
        result = set_awareness(2)
        if result in (0, 0x80070005):
            return "per_monitor"
    except Exception:
        pass

    try:
        set_aware = user32.SetProcessDPIAware
        set_aware.restype = ctypes.c_bool
        if set_aware():
            return "system_aware"
    except Exception:
        pass

    return "fallback"


def _show_startup_error(message, title="InvoiceFlowAI Startup Error"):
    if os.name != "nt":
        return

    try:
        ctypes.windll.user32.MessageBoxW(None, str(message), title, 0x10)
    except Exception:
        pass


def _startup_diagnostic_path():
    try:
        return Path(get_runtime_diagnostics_dir()) / "webview_startup.json"
    except Exception:
        return Path.cwd() / "webview_startup.json"


def _update_startup_diagnostic_context(**payload):
    STARTUP_DIAGNOSTIC_CONTEXT.update(payload)


def _write_startup_diagnostic(stage, **payload):
    diagnostic_path = _startup_diagnostic_path()
    diagnostic_path.parent.mkdir(parents=True, exist_ok=True)

    content = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "stage": stage,
        **STARTUP_DIAGNOSTIC_CONTEXT,
        **payload,
    }

    diagnostic_path.write_text(
        json.dumps(content, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return diagnostic_path


def _resolve_packaged_app_root(current_dir):
    current_dir = Path(current_dir).resolve()
    if current_dir.name.lower() == "_internal":
        return current_dir.parent
    if (current_dir / "_internal").is_dir():
        return current_dir
    return current_dir


def _iter_packaged_unblock_targets(app_root):
    try:
        for candidate in app_root.rglob("*"):
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue

            if candidate.suffix.lower() not in PACKAGED_UNBLOCK_SUFFIXES:
                continue
            yield candidate
    except OSError:
        return


def _has_zone_identifier(path_value):
    zone_identifier_path = f"{path_value}:Zone.Identifier"
    try:
        with open(zone_identifier_path, "rb"):
            return True
    except FileNotFoundError:
        return False
    except OSError:
        return False


def _clear_packaged_download_markers(current_dir):
    report = {
        "download_marker_cleanup_enabled": False,
        "download_marker_app_root": "",
        "download_marker_scanned": 0,
        "download_marker_detected": 0,
        "download_marker_cleared": 0,
        "download_marker_failed": 0,
        "download_marker_failed_paths": [],
    }

    if os.name != "nt" or not (getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")):
        return report

    app_root = _resolve_packaged_app_root(current_dir)
    report["download_marker_cleanup_enabled"] = True
    report["download_marker_app_root"] = str(app_root)

    if not app_root.exists():
        return report

    for candidate in _iter_packaged_unblock_targets(app_root):
        report["download_marker_scanned"] += 1
        if not _has_zone_identifier(candidate):
            continue

        report["download_marker_detected"] += 1
        zone_identifier_path = f"{candidate}:Zone.Identifier"
        try:
            os.remove(zone_identifier_path)
            report["download_marker_cleared"] += 1
        except OSError as exc:
            report["download_marker_failed"] += 1
            if len(report["download_marker_failed_paths"]) < 12:
                report["download_marker_failed_paths"].append(
                    {
                        "path": str(candidate),
                        "error": str(exc),
                    }
                )

    return report


def _resolve_webview2_runtime_dir(current_dir):
    candidates = []
    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        candidates.append(current_dir / WEBVIEW2_RUNTIME_RELATIVE_DIR)
    candidates.append(current_dir / "build" / WEBVIEW2_RUNTIME_RELATIVE_DIR)

    for candidate in candidates:
        if candidate.is_dir():
            return candidate.resolve()

    return candidates[0].resolve() if candidates else (current_dir / WEBVIEW2_RUNTIME_RELATIVE_DIR).resolve()


def _validate_webview2_runtime_dir(runtime_dir):
    required_entries = [
        runtime_dir / "msedgewebview2.exe",
        runtime_dir / "msedge.dll",
        runtime_dir / "resources.pak",
        runtime_dir / "Locales",
    ]
    missing = [str(entry) for entry in required_entries if not entry.exists()]
    if missing:
        raise FileNotFoundError(
            "Bundled WebView2 runtime is missing required files: " + "; ".join(missing)
        )


def _configure_webview_runtime(webview_module, current_dir):
    if os.name != "nt":
        return None

    runtime_dir = _resolve_webview2_runtime_dir(current_dir)
    _validate_webview2_runtime_dir(runtime_dir)
    webview_module.settings["WEBVIEW2_RUNTIME_PATH"] = str(runtime_dir)
    return runtime_dir


def _resolve_working_area(screen):
    frame = getattr(screen, "frame", None)
    if frame is not None:
        width = int(getattr(frame, "Width", 0) or 0)
        height = int(getattr(frame, "Height", 0) or 0)
        if width > 0 and height > 0:
            return {
                "x": int(getattr(frame, "X", getattr(screen, "x", 0)) or 0),
                "y": int(getattr(frame, "Y", getattr(screen, "y", 0)) or 0),
                "width": width,
                "height": height,
                "screen": screen,
            }

    width = int(getattr(screen, "width", 0) or 0)
    height = int(getattr(screen, "height", 0) or 0)
    if width > 0 and height > 0:
        return {
            "x": int(getattr(screen, "x", 0) or 0),
            "y": int(getattr(screen, "y", 0) or 0),
            "width": width,
            "height": height,
            "screen": screen,
        }

    return None


def _resolve_window_geometry(webview_module):
    screens = list(getattr(webview_module, "screens", []) or [])
    working_area = _resolve_working_area(screens[0]) if screens else None
    if not working_area:
        return {
            "width": WINDOW_BASELINE_WIDTH,
            "height": WINDOW_BASELINE_HEIGHT,
            "min_size": (WINDOW_MIN_WIDTH, WINDOW_MIN_HEIGHT),
        }

    available_width = max(working_area["width"] - WINDOW_MARGIN, 760)
    available_height = max(working_area["height"] - WINDOW_MARGIN, 560)

    width = min(WINDOW_BASELINE_WIDTH, available_width)
    height = min(WINDOW_BASELINE_HEIGHT, available_height)
    min_width = min(WINDOW_MIN_WIDTH, width)
    min_height = min(WINDOW_MIN_HEIGHT, height)
    x = working_area["x"] + max((working_area["width"] - width) // 2, 0)
    y = working_area["y"] + max((working_area["height"] - height) // 2, 0)

    return {
        "screen": working_area["screen"],
        "x": int(x),
        "y": int(y),
        "width": int(width),
        "height": int(height),
        "min_size": (int(min_width), int(min_height)),
    }


def _ensure_expected_frontend_assets(current_dir):
    html_path = current_dir / "templates" / "index.html"
    js_path = current_dir / "templates" / "index_app.js"

    if not html_path.is_file():
        raise FileNotFoundError(f"Frontend HTML not found: {html_path}")
    if not js_path.is_file():
        raise FileNotFoundError(f"Frontend app bundle not found: {js_path}")

    html_text = html_path.read_text(encoding="utf-8")
    js_text = js_path.read_text(encoding="utf-8")

    expected_html_markers = [
        "<title>InvoiceFlowAI</title>",
        "color-scheme: dark;",
    ]
    expected_js_markers = [
        'name: "InvoiceFlowAI"',
        'subtitle: "AI发票管家',
        "const APP_VISIBLE_COPY = {",
    ]
    unexpected_js_markers = []

    missing_markers = [
        marker
        for marker in [*expected_html_markers, *expected_js_markers]
        if marker not in html_text and marker not in js_text
    ]
    unexpected_markers = [marker for marker in unexpected_js_markers if marker in js_text]

    if missing_markers or unexpected_markers:
        problems = []
        if missing_markers:
            problems.append(f"missing markers: {', '.join(missing_markers)}")
        if unexpected_markers:
            problems.append(f"unexpected markers: {', '.join(unexpected_markers)}")
        raise RuntimeError(
            "Frontend asset verification failed. The application stopped because the packaged interface files "
            f"are incomplete or inconsistent ({'; '.join(problems)})."
        )

    return html_path


def main():
    args = _parse_args()
    run_context_path = str(args.run_context_path or "").strip()
    if run_context_path:
        resolved_run_context_path = Path(run_context_path).expanduser().resolve()
        if not resolved_run_context_path.is_file():
            raise FileNotFoundError(f"Run context file not found: {resolved_run_context_path}")
        os.environ["INVOICEFLOWAI_RUN_CONTEXT_PATH"] = str(resolved_run_context_path)
        set_explicit_run_context_path(str(resolved_run_context_path))
    else:
        os.environ.pop("INVOICEFLOWAI_RUN_CONTEXT_PATH", None)
        set_explicit_run_context_path("")

    _set_windows_dpi_awareness()
    current_dir = Path(__file__).resolve().parent
    download_marker_report = _clear_packaged_download_markers(current_dir)
    _update_startup_diagnostic_context(**download_marker_report)

    import webview

    from app_api import InvoiceAppAPI

    webview.settings["DRAG_REGION_SELECTOR"] = ".window-drag-region"

    runtime_dir = _configure_webview_runtime(webview, current_dir)
    _write_startup_diagnostic(
        "preflight_ok",
        runtime_dir=str(runtime_dir) if runtime_dir else "",
        packaged=bool(getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS")),
    )
    html_path = _ensure_expected_frontend_assets(current_dir)

    api = InvoiceAppAPI()
    window_geometry = _resolve_window_geometry(webview)

    webview.create_window(
        title=WINDOW_TITLE,
        url=str(html_path),
        js_api=api,
        width=window_geometry["width"],
        height=window_geometry["height"],
        x=window_geometry.get("x"),
        y=window_geometry.get("y"),
        screen=window_geometry.get("screen"),
        min_size=window_geometry["min_size"],
        resizable=True,
        frameless=True,
        easy_drag=False,
    )

    def _record_renderer():
        _write_startup_diagnostic(
            "ui_started",
            runtime_dir=str(runtime_dir) if runtime_dir else "",
            renderer=str(getattr(webview, "renderer", "") or ""),
        )

    webview.start(func=_record_renderer, debug=False, http_server=True)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        diagnostic_path = _write_startup_diagnostic(
            "startup_failed",
            error=str(exc),
        )
        _show_startup_error(
            f"{exc}\n\nDiagnostic file:\n{diagnostic_path}",
        )
        sys.exit(1)
