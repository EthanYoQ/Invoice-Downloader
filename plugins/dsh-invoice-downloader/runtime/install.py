#!/usr/bin/env python3
"""Install the explicit, package-local Invoice Downloader runtime."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument('--venv', required=True)
    parser.add_argument('--requirements', required=True)
    parser.add_argument('--engine-root', required=True)
    parser.add_argument('--health-check', required=True)
    return parser.parse_args()


def venv_executable(venv_dir: Path, name: str) -> Path:
    if os.name == 'nt':
        return venv_dir / 'Scripts' / f'{name}.exe'
    return venv_dir / 'bin' / name


def run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    subprocess.check_call(command, env=env)


def main() -> int:
    args = parse_args()
    venv_dir = Path(args.venv).resolve()
    requirements = Path(args.requirements).resolve()
    engine_root = Path(args.engine_root).resolve()
    health_check = Path(args.health_check).resolve()
    source_dir = engine_root / 'src' / 'invoice_engine'

    if not requirements.is_file() or not source_dir.is_dir() or not health_check.is_file():
        raise RuntimeError('runtime package files are incomplete; reinstall the npm package')

    if not venv_dir.exists():
        print('Creating local Python environment...')
        run([sys.executable, '-m', 'venv', str(venv_dir)])

    pip = venv_executable(venv_dir, 'pip')
    python = venv_executable(venv_dir, 'python')
    if not pip.is_file():
        print('Bootstrapping pip in the local Python environment...')
        run([str(python), '-m', 'ensurepip', '--upgrade'])
    print('Installing local invoice runtime dependencies...')
    run([str(pip), 'install', '--disable-pip-version-check', '-r', str(requirements)])
    print('Installing Chromium for invoice-link recovery...')
    run([str(python), '-m', 'playwright', 'install', 'chromium'])

    environment = {**os.environ, 'PYTHONPATH': str(source_dir)}
    print('Checking the local invoice runtime...')
    run(
        [
            str(python),
            str(health_check),
            '--engine-root',
            str(engine_root),
        ],
        env=environment,
    )
    print('Invoice runtime is ready.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
