# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
RUNTIME_SOURCE = Path(os.environ["INVOICEFLOW_RUNTIME_SOURCE"]).resolve()
IDENTITY_PATH = ROOT / "build" / "macos" / "build-identity.generated.json"
ICON_PATH = Path(os.environ["INVOICEFLOW_ICON_PATH"]).resolve()

for required_path in (RUNTIME_SOURCE, IDENTITY_PATH, ICON_PATH):
    if not required_path.exists():
        raise SystemExit(f"Required macOS packaging input not found: {required_path}")

datas = [
    (str(ROOT / "templates"), "templates"),
    (str(RUNTIME_SOURCE), "runtime/ms-playwright"),
    (str(IDENTITY_PATH), "build_meta"),
]

hiddenimports = [
    "audit_email_truth",
    "webview",
    "webview.platforms.cocoa",
    "pythonnet",
    "clr_loader",
    "openpyxl",
    "fitz",
    "PIL",
    "playwright",
    "playwright.sync_api",
    "pyzbar",
    "tenacity",
]
hiddenimports.extend(collect_submodules("keyring"))
hiddenimports.extend(collect_submodules("pyzbar"))
pyzbar_binaries = collect_dynamic_libs("pyzbar")

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=pyzbar_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / "build" / "windows" / "runtime_hook_playwright.py")],
    excludes=[],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="InvoiceFlowAI",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    icon=str(ICON_PATH),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="InvoiceFlowAI",
)

app = BUNDLE(
    coll,
    name="InvoiceFlowAI.app",
    icon=str(ICON_PATH),
    bundle_identifier="com.ethanyoq.invoiceflowai",
)
