import json
import os
from pathlib import Path

_EXPLICIT_RUN_CONTEXT_PATH = ""
_RUN_CONTEXT_ENV_VAR = "INVOICEFLOWAI_RUN_CONTEXT_PATH"


def _is_truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def set_explicit_run_context_path(path):
    global _EXPLICIT_RUN_CONTEXT_PATH
    normalized = str(path or "").strip()
    _EXPLICIT_RUN_CONTEXT_PATH = normalized
    if normalized:
        os.environ[_RUN_CONTEXT_ENV_VAR] = normalized
    else:
        os.environ.pop(_RUN_CONTEXT_ENV_VAR, None)


def _resolve_run_context_path():
    explicit_path = str(_EXPLICIT_RUN_CONTEXT_PATH or "").strip()
    if explicit_path:
        return explicit_path
    return str(os.environ.get(_RUN_CONTEXT_ENV_VAR, "") or "").strip()


def _empty_run_context():
    return {
        "enabled": False,
        "explicit_run_context": False,
        "run_id": "",
        "run_root": "",
        "output_dir": "",
        "staging_dir": "",
        "diagnostics_dir": "",
        "monitoring_dir": "",
        "qc_dir": "",
        "debug_trace_path": "",
        "locked_date_from": "",
        "locked_date_to": "",
        "disable_auto_local_scan": False,
        "autostart_enabled": False,
        "autostart_mode": "",
        "autostart_delay_ms": 0,
        "autostart_token": "",
    }


def load_run_context():
    run_context_path = _resolve_run_context_path()
    if not run_context_path:
        return _empty_run_context()

    context_file = Path(run_context_path)
    if not context_file.exists():
        return _empty_run_context()

    try:
        file_context = json.loads(context_file.read_text(encoding="utf-8-sig"))
    except Exception:
        return _empty_run_context()

    if not isinstance(file_context, dict):
        return _empty_run_context()

    enabled = bool(file_context.get("enabled", True))
    if not enabled:
        return _empty_run_context()

    controlled_run = bool(file_context.get("controlled_run", True))
    run_root = str(file_context.get("run_root", "")).strip()
    locked_output_path = str(file_context.get("locked_output_path", "")).strip()
    staging_dir = str(file_context.get("staging_dir", "")).strip()
    diagnostics_dir = str(file_context.get("diagnostics_dir", "")).strip()
    monitoring_dir = str(file_context.get("monitoring_dir", "")).strip()
    qc_dir = str(file_context.get("qc_dir", "")).strip()
    debug_trace_path = str(file_context.get("debug_trace_path", "")).strip()

    if not run_root:
        for candidate in [locked_output_path, staging_dir, diagnostics_dir, monitoring_dir, qc_dir, debug_trace_path]:
            if candidate:
                candidate_path = Path(candidate)
                if candidate_path.name == "qc":
                    run_root = str(candidate_path.parent.parent)
                elif candidate_path.name == "monitoring":
                    run_root = str(candidate_path.parent)
                else:
                    run_root = str(candidate_path.parent)
                break

    if not run_root and context_file.parent.name in {"monitoring", "diagnostics"}:
        run_root = str(context_file.parent.parent)
    elif not run_root:
        run_root = str(context_file.parent)
    run_root = os.path.abspath(run_root) if run_root else ""

    run_id = str(file_context.get("run_id", "")).strip()
    if not run_id and run_root:
        run_id = os.path.basename(run_root)

    locked_date_from = str(file_context.get("locked_date_from", "")).strip()
    locked_date_to = str(file_context.get("locked_date_to", "")).strip()
    disable_auto_local_scan = bool(file_context.get("disable_auto_local_scan", False))
    autostart_enabled = bool(file_context.get("autostart_enabled", False))
    autostart_mode = str(file_context.get("autostart_mode", "")).strip() if autostart_enabled else ""
    autostart_delay_ms = 0
    try:
        autostart_delay_ms = max(0, int(file_context.get("autostart_delay_ms", 0) or 0))
    except (TypeError, ValueError):
        autostart_delay_ms = 0
    autostart_token = str(file_context.get("autostart_token", "")).strip() if autostart_enabled else ""

    return {
        "enabled": enabled,
        "explicit_run_context": True,
        "run_id": run_id,
        "run_root": run_root,
        "output_dir": locked_output_path or (os.path.join(run_root, "output") if run_root else ""),
        "staging_dir": staging_dir or (os.path.join(run_root, "staging") if run_root else ""),
        "diagnostics_dir": diagnostics_dir or (os.path.join(run_root, "diagnostics") if run_root else ""),
        "monitoring_dir": monitoring_dir or (os.path.join(run_root, "monitoring") if run_root else ""),
        "qc_dir": qc_dir or (os.path.join(run_root, "monitoring", "qc") if run_root else ""),
        "debug_trace_path": debug_trace_path or (os.path.join(run_root, "diagnostics", "debug_trace.jsonl") if run_root else ""),
        "locked_date_from": locked_date_from,
        "locked_date_to": locked_date_to,
        "disable_auto_local_scan": disable_auto_local_scan,
        "autostart_enabled": bool(autostart_enabled and controlled_run),
        "autostart_mode": autostart_mode if controlled_run else "",
        "autostart_delay_ms": autostart_delay_ms if controlled_run else 0,
        "autostart_token": autostart_token if controlled_run else "",
        "controlled_run": controlled_run,
    }


def ensure_run_context_dirs(context):
    if not context.get("enabled"):
        return

    for key in [
        "run_root",
        "output_dir",
        "staging_dir",
        "diagnostics_dir",
        "monitoring_dir",
        "qc_dir",
    ]:
        target = context.get(key)
        if target:
            os.makedirs(target, exist_ok=True)


def serialize_run_context(context):
    return {
        "enabled": bool(context.get("enabled", False)),
        "explicit_run_context": bool(context.get("explicit_run_context", False)),
        "controlled_run": bool(context.get("controlled_run", False)),
        "run_id": context.get("run_id", ""),
        "run_root": context.get("run_root", ""),
        "locked_output_path": context.get("output_dir", ""),
        "locked_date_from": context.get("locked_date_from", ""),
        "locked_date_to": context.get("locked_date_to", ""),
        "debug_trace_path": context.get("debug_trace_path", ""),
        "monitoring_dir": context.get("monitoring_dir", ""),
        "qc_dir": context.get("qc_dir", ""),
        "autostart_enabled": bool(context.get("autostart_enabled", False)),
        "autostart_mode": context.get("autostart_mode", ""),
        "autostart_delay_ms": int(context.get("autostart_delay_ms", 0) or 0),
        "autostart_token": context.get("autostart_token", ""),
    }
