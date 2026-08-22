#!/usr/bin/env python3
"""Check the package-owned headless runtime without touching mailbox data."""

from __future__ import annotations

import argparse
import importlib
import json
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--engine-root', required=True)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    engine_root = Path(args.engine_root).resolve()
    source_root = engine_root / 'src'
    engine_source = source_root / 'invoice_engine'
    adapter_root = Path(__file__).resolve().parent.parent / 'engine-adapter'
    sys.path.insert(0, str(source_root))
    sys.path.insert(0, str(engine_source))
    sys.path.insert(0, str(adapter_root))

    checks: list[dict[str, object]] = []
    for name, module in [
        ('engine', 'invoice_engine'),
        ('host adapter', 'dsh_runner'),
        ('scan adapter', 'dsh_scan'),
        ('engine API', 'app_api'),
        ('RapidOCR', 'rapidocr_onnxruntime'),
        ('PyMuPDF', 'fitz'),
        ('Playwright', 'playwright'),
    ]:
        try:
            importlib.import_module(module)
            checks.append({'name': name, 'ok': True})
        except Exception as exc:
            checks.append({'name': name, 'ok': False, 'error': type(exc).__name__})

    payload = {'ok': all(check['ok'] for check in checks), 'checks': checks}
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if payload['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
