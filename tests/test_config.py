"""Tests for the settings (app/config.py).

    docker compose exec app pytest tests/test_config.py -v

  test_secrets_never_printed
      A failed test once printed the settings, and with them part of the
      server's login secret, into a public GitHub log. Secrets must not
      appear in the settings' printed form.

  test_defaults_do_not_depend_on_the_server
      Tests run on the server, whose .env switches things on (the trial note,
      the hosted model). Defaults must be read without that environment.
"""
from __future__ import annotations

from app.config import Settings


def test_secrets_never_printed() -> None:
    s = Settings(
        _env_file=None,
        chainlit_auth_secret="login-secret-12345",
        generation_api_key="api-key-67890",
        APP_DATABASE_URL="postgresql://emr:db-password-111@db:5432/emr_helper",
    )
    shown = repr(s) + str(s)
    for secret in ("login-secret-12345", "api-key-67890", "db-password-111"):
        assert secret not in shown
    assert s.chainlit_auth_secret == "login-secret-12345"        # still usable, only hidden


def default_settings(monkeypatch) -> Settings:
    """Settings as the code defines them, ignoring this machine's .env and environment."""
    for name in ("TRIAL_NOTICE", "LOGIN_REQUIRED", "RESTRICTED_MODULES", "GENERATION_PROVIDER"):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None)


def test_defaults_do_not_depend_on_the_server(monkeypatch) -> None:
    monkeypatch.setenv("TRIAL_NOTICE", "true")                    # as on the online server
    assert default_settings(monkeypatch).trial_notice is False
    assert default_settings(monkeypatch).login_required is True
    assert default_settings(monkeypatch).generation_provider == "ollama"
