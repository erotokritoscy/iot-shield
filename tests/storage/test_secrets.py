"""Credentials of an API data source are sealed under the server's key (ADR-0049 D2)."""

import pytest
from cryptography.fernet import Fernet

from iotshield.storage.secrets import SECRET_KEY_ENV, SecretsUnavailable, seal, unseal


def test_round_trips_and_the_token_does_not_contain_the_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(SECRET_KEY_ENV, Fernet.generate_key().decode())
    token = seal({"api_key": "sk-live-123", "password": " spaced "})
    assert "sk-live-123" not in token
    assert unseal(token) == {"api_key": "sk-live-123", "password": " spaced "}


def test_no_key_refuses_rather_than_storing_in_the_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRET_KEY_ENV, raising=False)
    with pytest.raises(SecretsUnavailable, match=SECRET_KEY_ENV):
        seal({"api_key": "x"})


def test_a_malformed_key_is_named(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_KEY_ENV, "not-a-key")
    with pytest.raises(SecretsUnavailable, match="not a valid key"):
        seal({"api_key": "x"})


def test_a_rotated_key_asks_for_the_credentials_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRET_KEY_ENV, Fernet.generate_key().decode())
    token = seal({"api_key": "x"})
    monkeypatch.setenv(SECRET_KEY_ENV, Fernet.generate_key().decode())
    with pytest.raises(SecretsUnavailable, match="enter them again"):
        unseal(token)
