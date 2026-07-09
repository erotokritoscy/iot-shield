"""The compose schema-init entrypoint (iotshield.storage.init)."""

from pathlib import Path

import pytest
from sqlalchemy import inspect

from iotshield.storage import make_engine
from iotshield.storage.init import main


def test_init_creates_schema(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    url = f"sqlite:///{(tmp_path / 'init.db').as_posix()}"
    monkeypatch.setenv("IOTSHIELD_DATABASE_URL", url)

    assert main() == 0

    engine = make_engine(url)
    tables = set(inspect(engine).get_table_names())
    engine.dispose()
    # The core tables the API and runner rely on exist after a bare init.
    assert {"assets", "events", "detections", "alerts"} <= tables


def test_init_without_env_exits_two(capsys: pytest.CaptureFixture[str]) -> None:
    # The hermetic autouse fixture guarantees IOTSHIELD_DATABASE_URL is unset.
    assert main() == 2
    assert "IOTSHIELD_DATABASE_URL" in capsys.readouterr().err
