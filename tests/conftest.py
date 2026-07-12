from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def isolate_windows_user_data(monkeypatch, tmp_path: Path):
    """Keep tests from reading or overwriting the operator's real app data."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))

