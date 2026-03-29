import os
import sys
from pathlib import Path


def _resolve_browser_path():
    if getattr(sys, "frozen", False):
        base_dir = Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent))
        return base_dir / "runtime" / "ms-playwright"

    repo_root = Path(__file__).resolve().parents[2]
    return repo_root / "build" / "runtime" / "ms-playwright"


browser_path = _resolve_browser_path()
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browser_path)
os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
