import base64
import ctypes
import hashlib
import json
import os
from ctypes import wintypes


APP_DIR_NAME = "InvoiceFlowAI External Test"
SETTINGS_FILE_NAME = "user_settings.json"
SENSITIVE_KEYS = {"auth_code", "api_key"}
DEFAULT_OUTPUT_DIR_NAME = "发票整理"


class DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _appdata_root():
    return os.getenv("APPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Roaming")


def _localappdata_root():
    return os.getenv("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")


def get_settings_dir():
    settings_dir = os.path.join(_appdata_root(), APP_DIR_NAME)
    os.makedirs(settings_dir, exist_ok=True)
    return settings_dir


def get_settings_path():
    return os.path.join(get_settings_dir(), SETTINGS_FILE_NAME)


def get_runtime_data_dir():
    runtime_dir = os.path.join(_localappdata_root(), APP_DIR_NAME)
    os.makedirs(runtime_dir, exist_ok=True)
    return runtime_dir


def get_runtime_diagnostics_dir():
    diagnostics_dir = os.path.join(get_runtime_data_dir(), "diagnostics")
    os.makedirs(diagnostics_dir, exist_ok=True)
    return diagnostics_dir


def get_packaged_diagnostics_dir():
    packaged_dir = os.path.join(get_runtime_diagnostics_dir(), "packaged_5p")
    os.makedirs(packaged_dir, exist_ok=True)
    return packaged_dir


def get_default_debug_trace_path():
    return os.path.join(get_runtime_diagnostics_dir(), "debug_trace.jsonl")


def ensure_directory(path):
    resolved = os.path.abspath(str(path or "").strip())
    if not resolved:
        raise ValueError("Path is required")
    os.makedirs(resolved, exist_ok=True)
    return resolved


def get_default_save_path():
    desktop_path = os.path.join(os.path.expanduser("~"), "Desktop")
    return os.path.join(desktop_path, DEFAULT_OUTPUT_DIR_NAME)


def get_output_state_dir(output_path):
    normalized_output_path = os.path.normcase(
        os.path.abspath(str(output_path or get_default_save_path()).strip())
    )
    digest = hashlib.sha256(normalized_output_path.encode("utf-8")).hexdigest()[:16]
    return ensure_directory(os.path.join(get_settings_dir(), "state", "output_scoped", digest))


def _blob_from_bytes(raw_bytes):
    if raw_bytes is None:
        raw_bytes = b""
    buffer = ctypes.create_string_buffer(raw_bytes)
    blob = DATA_BLOB(len(raw_bytes), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char)))
    return blob, buffer


def _protect_bytes(raw_bytes):
    if os.name != "nt":
        return raw_bytes

    in_blob, in_buffer = _blob_from_bytes(raw_bytes)
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        APP_DIR_NAME,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


def _unprotect_bytes(raw_bytes):
    if os.name != "nt":
        return raw_bytes

    in_blob, in_buffer = _blob_from_bytes(raw_bytes)
    out_blob = DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise ctypes.WinError()

    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(out_blob.pbData, wintypes.HLOCAL))


def _protect_text(value):
    protected = _protect_bytes(str(value or "").encode("utf-8"))
    return base64.b64encode(protected).decode("ascii")


def _unprotect_text(value):
    if not value:
        return ""
    raw_bytes = base64.b64decode(value.encode("ascii"))
    return _unprotect_bytes(raw_bytes).decode("utf-8")


class UserSettingsStore:
    def __init__(self, settings_path=None):
        self.settings_path = settings_path or get_settings_path()

    def load(self):
        if not os.path.exists(self.settings_path):
            return {}

        try:
            with open(self.settings_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except Exception:
            return {}

        values = dict(payload.get("values") or {})
        protected = dict(payload.get("protected") or {})
        for key, encoded in protected.items():
            try:
                values[key] = _unprotect_text(encoded)
            except Exception:
                values[key] = ""
        return values

    def save(self, settings):
        os.makedirs(os.path.dirname(self.settings_path), exist_ok=True)
        payload = {"version": 1, "values": {}, "protected": {}}

        for key, value in dict(settings or {}).items():
            clean_value = value if value is not None else ""
            if key in SENSITIVE_KEYS:
                payload["protected"][key] = _protect_text(clean_value)
            else:
                payload["values"][key] = clean_value

        temp_path = f"{self.settings_path}.tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self.settings_path)
        return self.settings_path

    def clear(self):
        if os.path.exists(self.settings_path):
            os.remove(self.settings_path)
