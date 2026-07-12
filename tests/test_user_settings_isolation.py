from pathlib import Path


def test_default_settings_store_is_confined_to_test_appdata(tmp_path: Path):
    from user_settings import UserSettingsStore

    store = UserSettingsStore()
    store.save(
        {
            "email": "test@example.invalid",
            "auth_code": "test-auth",
            "api_key": "test-key",
        }
    )

    settings_path = Path(store.settings_path).resolve()
    assert settings_path.is_relative_to((tmp_path / "AppData" / "Roaming").resolve())
    assert store.load()["email"] == "test@example.invalid"
