"""Dashboard test harness: ApiClient wired to the real FastAPI app in-process
(TestClient IS an httpx.Client), over a seeded or empty SQLite database."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from iotshield.api import create_app
from iotshield.api.control import create_control_app
from iotshield.contracts import (
    AlertSeverity,
    Asset,
    Criticality,
    DetectionResult,
    Lane,
    MalformedRecord,
    NormalizedEvent,
    PrioritizedAlert,
)
from iotshield.dashboard.client import ApiClient
from iotshield.dashboard.control_client import ControlClient
from iotshield.storage import create_schema, make_engine, make_session_factory, repository

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


@dataclass
class SeededDashboard:
    client: ApiClient
    alert: PrioritizedAlert
    detection: DetectionResult


def _client_for(tmp_path: Path) -> tuple[ApiClient, Engine]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'dash.db').as_posix()}")
    create_schema(engine)  # the API no longer self-creates it (ADR-0010)
    return ApiClient(TestClient(create_app(engine))), engine


@pytest.fixture()
def empty_client(tmp_path: Path) -> ApiClient:
    client, _ = _client_for(tmp_path)
    return client


# NOTE: third suite seeding the same pump-02/T0 scenario (tests/api, tests/storage).
# Known consolidation debt: a shared tests/support builder module, migrating all
# three suites at once — tracked as a follow-up, deliberately not done piecemeal.
@pytest.fixture()
def seeded(tmp_path: Path) -> SeededDashboard:
    client, engine = _client_for(tmp_path)
    events = [
        NormalizedEvent(
            timestamp=T0 + timedelta(seconds=30 * i),
            asset_id="pump-02",
            source_type=Lane.TELEMETRY,
            source_format="stub-csv",
            metric="Pressure",
            value=[45.0, 96.0, 47.0][i],
            raw=f"line-{i}",
        )
        for i in range(3)
    ]
    detection = DetectionResult(
        asset_id="pump-02",
        window_start=T0 + timedelta(seconds=30),
        window_end=T0 + timedelta(seconds=30),
        lane=Lane.TELEMETRY,
        method="stub_threshold",
        score=96.0,
        threshold=80.0,
    )
    alert = PrioritizedAlert(
        asset_id="pump-02",
        window_start=detection.window_start,
        window_end=detection.window_end,
        severity=AlertSeverity.HIGH,
        priority_score=62.0,
        rationale="seeded incident on Feed pump 2",
        score_components={"base": 52.0, "asset_criticality": 10.0},
        detection_ids=[detection.detection_id],
        recommended_action="Inspect pump-02.",
    )
    with make_session_factory(engine)() as session, session.begin():
        repository.upsert_assets(
            session,
            [
                Asset(
                    asset_id="pump-02",
                    name="Feed pump 2",
                    asset_type="pump",
                    criticality=Criticality.HIGH,
                )
            ],
        )
        repository.insert_events(session, events)
        repository.insert_malformed(
            session,
            [
                MalformedRecord(
                    raw="garbage-line",
                    error="unparseable",
                    source_format="stub-csv",
                    origin="test://seed",
                )
            ],
        )
        repository.insert_detections(session, [detection])
        repository.insert_alerts(session, [alert])
    return SeededDashboard(client=client, alert=alert, detection=detection)


@pytest.fixture()
def control_client(tmp_path: Path) -> ControlClient:
    """A ControlClient wired to the real Pipeline API in-process over a fresh SQLite db."""
    engine = make_engine(f"sqlite:///{(tmp_path / 'compose.db').as_posix()}")
    create_schema(engine)  # init owns the schema; the write API assumes it exists
    return ControlClient(TestClient(create_control_app(engine)))


@pytest.fixture()
def eval_control_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ControlClient:
    """A ControlClient whose Pipeline API has the evaluation plane mounted.

    Separate from ``control_client`` because the flag must be set *before* the app is
    built — the eval router is registered at construction, not consulted per request, which
    is what makes it absent rather than merely guarded (ADR-0021 D1).
    """
    monkeypatch.setenv("IOTSHIELD_EVAL_API", "1")
    engine = make_engine(f"sqlite:///{(tmp_path / 'eval.db').as_posix()}")
    create_schema(engine)
    return ControlClient(TestClient(create_control_app(engine)))
