#!/usr/bin/env bash
set -euo pipefail

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <release-tag>" >&2
  exit 2
fi

release_tag="$1"
version="${release_tag#v}"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
arch="$(uname -m)"
app_name="InvoiceFlowAI"
dist_root="$repo_root/dist/macos/$arch"
work_root="$repo_root/build/work/macos/$arch"
runtime_root="$repo_root/build/runtime/macos/$arch/ms-playwright"
identity_path="$repo_root/build/macos/build-identity.generated.json"
iconset_path="$work_root/$app_name.iconset"
icon_path="$work_root/$app_name.icns"

rm -rf "$dist_root" "$work_root" "$runtime_root"
mkdir -p "$dist_root" "$work_root" "$runtime_root" "$iconset_path"

export PLAYWRIGHT_BROWSERS_PATH="$runtime_root"
python -m playwright install chromium

RELEASE_TAG="$release_tag" ARCH="$arch" IDENTITY_PATH="$identity_path" python - <<'PY'
import json
import os
import subprocess
from pathlib import Path

identity_path = Path(os.environ["IDENTITY_PATH"])
identity_path.parent.mkdir(parents=True, exist_ok=True)
identity_path.write_text(
    json.dumps(
        {
            "build_time": subprocess.check_output(
                ["git", "show", "-s", "--format=%cI", "HEAD"], text=True
            ).strip(),
            "baseline": "InvoiceFlowAI",
            "source_revision": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True
            ).strip(),
            "build_label": f"macos-{os.environ['ARCH']}",
            "release_tag": os.environ["RELEASE_TAG"],
        },
        ensure_ascii=False,
        indent=2,
    )
    + "\n",
    encoding="utf-8",
)
PY

for size in 16 32 128 256 512; do
  rsvg-convert --width "$size" --height "$size" \
    "$repo_root/build/windows/assets/invoiceflowai-icon.svg" \
    --output "$iconset_path/icon_${size}x${size}.png"
done
for size in 16 32 128 256 512; do
  doubled_size="$((size * 2))"
  rsvg-convert --width "$doubled_size" --height "$doubled_size" \
    "$repo_root/build/windows/assets/invoiceflowai-icon.svg" \
    --output "$iconset_path/icon_${size}x${size}@2x.png"
done
iconutil --convert icns "$iconset_path" --output "$icon_path"

INVOICEFLOW_ICON_PATH="$icon_path" \
python -m PyInstaller \
  --noconfirm \
  --clean \
  --distpath "$dist_root" \
  --workpath "$work_root/pyinstaller" \
  "$repo_root/build/macos/InvoiceFlowAI.spec"

app_path="$dist_root/$app_name.app"
app_executable="$app_path/Contents/MacOS/$app_name"
runtime_target="$app_path/Contents/Frameworks/runtime/ms-playwright"
test -d "$app_path"
test -x "$app_executable"

# Chromium contains a nested .app bundle. Copying it only after PyInstaller has
# completed prevents PyInstaller from trying to ad-hoc-sign that nested bundle.
mkdir -p "$(dirname "$runtime_target")"
ditto "$runtime_root" "$runtime_target"
test -d "$runtime_target"
chromium_executable="$(find "$runtime_target" -type f -path '*Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing' -perm -111 -print -quit)"
test -n "$chromium_executable"
INVOICEFLOWAI_PLAYWRIGHT_RUNTIME_SMOKE=1 "$app_executable" --help | grep --quiet -- '--run-context'

dmg_path="$dist_root/$app_name-v$version-macos-$arch.dmg"
sha_path="$dist_root/$app_name-v$version-macos-$arch.SHA256"
hdiutil create \
  -volname "$app_name" \
  -srcfolder "$app_path" \
  -ov \
  -format UDZO \
  "$dmg_path"
shasum -a 256 "$dmg_path" > "$sha_path"

printf '%s\n' "Built $dmg_path"
