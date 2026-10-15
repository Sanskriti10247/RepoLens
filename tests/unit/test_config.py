from pathlib import Path

import pytest

from repolens.config import Environment, Settings


def test_settings_read_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOLENS_ENVIRONMENT", "test")
    monkeypatch.setenv("REPOLENS_API_PORT", "9123")
    monkeypatch.setenv("REPOLENS_DATA_DIR", "/srv/repolens")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.environment is Environment.TEST
    assert settings.api_port == 9123
    assert settings.mirrors_dir == Path("/srv/repolens/mirrors")


def test_api_token_is_not_exposed_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOLENS_API_TOKEN", "super-secret-value")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]
    assert settings.api_token is not None
    assert "super-secret-value" not in repr(settings)
    assert settings.api_token.get_secret_value() == "super-secret-value"


def test_invalid_port_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("REPOLENS_API_PORT", "70000")
    with pytest.raises(ValueError, match="api_port"):
        Settings(_env_file=None)  # type: ignore[call-arg]
