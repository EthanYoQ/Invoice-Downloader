# -*- mode: python ; coding: utf-8 -*-

import json
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules


ROOT = Path(SPECPATH).resolve().parents[1]
MANIFEST_PATH = ROOT / "build" / "windows" / "resources.manifest.json"
MANIFEST = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

datas = []
runtime_source_override = os.environ.get("INVOICEFLOW_RUNTIME_SOURCE", "").strip()
for item in MANIFEST["datas"]:
    source_path = ROOT / item["source"]
    if runtime_source_override and item["source"] == MANIFEST["chromium"]["stagingDir"]:
        source_path = Path(runtime_source_override)
    if source_path.exists():
        datas.append((str(source_path), item["target"]))
    elif not item.get("optional", False):
        raise SystemExit(f"Required data path not found: {source_path}")

hiddenimports = list(MANIFEST.get("hiddenImports", []))
hiddenimports.extend(collect_submodules("pyzbar"))
pyzbar_binaries = collect_dynamic_libs("pyzbar")

runtime_hooks = [str(ROOT / MANIFEST["runtimeHook"])]
version_path = ROOT / MANIFEST["version"]["rendered"]
version_file = str(version_path) if version_path.exists() else None
manifest_file = None
manifest_setting = (MANIFEST.get("windowsManifest") or "").strip()
if manifest_setting:
    manifest_path = ROOT / manifest_setting
    if not manifest_path.exists():
        raise SystemExit(f"Configured Windows manifest not found: {manifest_path}")
    manifest_file = str(manifest_path)
icon_value = None
icon_setting = ((MANIFEST.get("icon") or {}).get("exeIconPath") or "").strip()
if icon_setting:
    icon_path = ROOT / icon_setting
    if not icon_path.exists():
        raise SystemExit(f"Configured EXE icon not found: {icon_path}")
    icon_value = str(icon_path)


a = Analysis(
    [str(ROOT / MANIFEST["entryScript"])],
    pathex=[str(ROOT)],
    binaries=pyzbar_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=runtime_hooks,
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=MANIFEST["appName"],
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    version=version_file,
    icon=icon_value,
    manifest=manifest_file,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name=MANIFEST["appName"],
)
