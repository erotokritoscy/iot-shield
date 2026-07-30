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
from iotshield.enrichment import EVIDENCE_KEY, Enrichment, EnrichmentSource, MitreTechnique
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


@pytest.fixture()
def rule_seeded(tmp_path: Path) -> ApiClient:
    """One alert whose evidence is two rules on ONE lane — the shape `method` cannot name.

    The Detector port gives an adapter one lane and the port suite pins `detection.method`
    to it, so every rule on the flow lane reports `rules:flow` and the firing rule rides in
    `Enrichment.origin` (ADR-0032 D3). Both detections below are therefore `rules:flow`,
    exactly as the walkthrough's rule detector produces.
    """
    client, engine = _client_for(tmp_path)
    telnet = Enrichment(
        threat_class="Remote Services (T1021)",
        source=EnrichmentSource.RULE,
        origin="rule:telnet-outbound-propagation",
        mitre=(
            MitreTechnique(
                tactic="Lateral Movement", technique="Remote Services", technique_id="T1021"
            ),
        ),
        suggested_actions=("Isolate the device.", "Block outbound telnet."),
    )
    upload = Enrichment(
        threat_class="Exfiltration Over C2 Channel (T1041)",
        source=EnrichmentSource.RULE,
        origin="rule:high-upload-volume",
        mitre=(
            MitreTechnique(
                tactic="Exfiltration",
                technique="Exfiltration Over C2 Channel",
                technique_id="T1041",
            ),
        ),
        suggested_actions=("Review the destination.",),
    )
    # Distinct instants, because `get_detections_by_ids` orders by (window_start,
    # detection_id) — sharing one would leave a random UUID deciding the row order.
    detections = [
        DetectionResult(
            asset_id="cam-07",
            window_start=T0 + timedelta(minutes=offset),
            window_end=T0 + timedelta(minutes=offset),
            lane=Lane.FLOW,
            method="rules:flow",
            score=score,
            threshold=1.0,
            evidence={"explanation": explanation, EVIDENCE_KEY: enrichment.to_evidence()},
        )
        for offset, explanation, score, enrichment in [
            (0, "The device originated a connection to port 23 (telnet).", 2.55, telnet),
            (1, "Outbound bytes on one flow exceed 10000.", 1.70, upload),
        ]
    ]
    promoted = telnet.with_union_of([telnet, upload])
    alert = PrioritizedAlert(
        asset_id="cam-07",
        window_start=T0,
        window_end=T0,
        severity=AlertSeverity.CRITICAL,
        priority_score=73.9,
        threat_class=promoted.threat_class,
        rationale="two rules fired on one device",
        score_components={"severity": 2.55},
        detection_ids=[d.detection_id for d in detections],
        recommended_action=promoted.action_text(),
    )
    with make_session_factory(engine)() as session, session.begin():
        repository.insert_detections(session, detections)
        repository.insert_alerts(session, [alert])
    return client
