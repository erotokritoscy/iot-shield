"""Suite-wide fixtures."""

import pytest

_IOTSHIELD_ENV_VARS = (
    "IOTSHIELD_DATABASE_URL",
    "IOTSHIELD_API_HOST",
    "IOTSHIELD_API_PORT",
    "IOTSHIELD_API_URL",
)


@pytest.fixture(autouse=True)
def _hermetic_iotshield_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_config now lets IOTSHIELD_DATABASE_URL override the config file, and
    # the services read their wiring from IOTSHIELD_* too. Clear all of them so a
    # developer's shell env can never redirect a test's database or API target.
    # Tests that exercise the override set the var explicitly afterwards.
    for var in _IOTSHIELD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
