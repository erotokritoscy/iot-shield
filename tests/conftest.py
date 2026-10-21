"""Suite-wide fixtures."""

import pytest

# The connector tests' two stand-in databases (ADR-0042), shared by tests/connect and tests/api.
pytest_plugins = ["tests.connect_world"]

_IOTSHIELD_ENV_VARS = (
    "IOTSHIELD_DATABASE_URL",
    "IOTSHIELD_API_HOST",
    "IOTSHIELD_API_PORT",
    "IOTSHIELD_API_URL",
    "IOTSHIELD_AUTH",
    "IOTSHIELD_OPEN_REGISTRATION",
    "IOTSHIELD_SEED_SUPERADMIN_USERNAME",
    "IOTSHIELD_SEED_SUPERADMIN_PASSWORD",
    "IOTSHIELD_SEED_ORGANIZATION",
    "IOTSHIELD_SEED_DEMO_CONFIG",
)


@pytest.fixture(autouse=True)
def _hermetic_iotshield_env(monkeypatch: pytest.MonkeyPatch) -> None:
    # load_config now lets IOTSHIELD_DATABASE_URL override the config file, and
    # the services read their wiring from IOTSHIELD_* too. Clear all of them so a
    # developer's shell env can never redirect a test's database or API target.
    # Tests that exercise the override set the var explicitly afterwards.
    for var in _IOTSHIELD_ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # No test reads the deployment's real connections file (config/connections.yaml); one
    # that wants connections declares its own.
    monkeypatch.setenv("IOTSHIELD_CONNECTIONS_PATH", "/nonexistent/connections.yaml")
