import json
import os
import sys
from pathlib import Path

from document_types import MANUAL_REVIEW_FOLDER, NON_TARGET_COMPANY_FOLDER


URL_STRATEGY_VERSION = "2026-03-31-release"
BUILD_LABEL = "desktop-release"
BUILD_IDENTITY_FILE = "build-identity.generated.json"


def build_runtime_identity(build_time=None, source_revision=None, build_label=None):
    return {
        "build_time": str(build_time or os.getenv("INVOICEFLOW_BUILD_TIME") or "").strip() or "development",
        "baseline": "InvoiceFlowAI",
        "source_revision": str(source_revision or os.getenv("INVOICEFLOW_BUILD_SOURCE_REVISION") or "").strip() or "snapshot",
        "build_label": str(build_label or os.getenv("INVOICEFLOW_BUILD_LABEL") or "").strip() or BUILD_LABEL,
        "manual_review_folder": MANUAL_REVIEW_FOLDER,
        "non_target_company_folder": NON_TARGET_COMPANY_FOLDER,
        "url_strategy_version": URL_STRATEGY_VERSION,
    }


def _candidate_identity_paths():
    module_dir = Path(__file__).resolve().parent
    candidates = [module_dir / "build" / "windows" / BUILD_IDENTITY_FILE]

    if getattr(sys, "frozen", False) or hasattr(sys, "_MEIPASS"):
        meipass = Path(getattr(sys, "_MEIPASS", module_dir)).resolve()
        candidates.insert(0, meipass / "build_meta" / BUILD_IDENTITY_FILE)
        candidates.insert(1, Path(sys.executable).resolve().parent / "_internal" / "build_meta" / BUILD_IDENTITY_FILE)

    return candidates


def load_build_identity():
    identity = build_runtime_identity()
    for candidate in _candidate_identity_paths():
        try:
            if not candidate.is_file():
                continue
            loaded = json.loads(candidate.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                identity.update({key: value for key, value in loaded.items() if value not in (None, "")})
                identity["identity_path"] = str(candidate)
                break
        except Exception:
            continue
    return identity


if __name__ == "__main__":
    print(json.dumps(build_runtime_identity(), ensure_ascii=False, indent=2))
