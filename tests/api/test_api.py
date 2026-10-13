"""API endpoint tests: seeded through the repository, read through HTTP.

The round-trip test is the load-bearing one: an API response parses back into
the contract model unchanged — the contracts ARE the wire format (ADR-0007).
"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

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
from iotshield.storage import DEFAULT_ORG_ID, repository
from tests.api.conftest import ApiHarness

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def event(
    *, asset_id: str = "pump-02", lane: Lane = Lane.TELEMETRY, offset_s: int = 0
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id=asset_id,
        source_type=lane,
        source_format="stub-csv",
        metric="Pressure",
        value=45.0 + offset_s,
        raw=f"r{offset_s}",
    )


def detection(*, asset_id: str = "pump-02", offset_s: int = 0) -> DetectionResult:
    start = T0 + timedelta(seconds=offset_s)
    return DetectionResult(
        asset_id=asset_id,
        window_start=start,
        window_end=start,
        lane=Lane.TELEMETRY,
        method="stub_threshold",
        score=96.0,
        threshold=80.0,
        evidence={"metric": "Pressure"},
    )


def alert(detections: list[DetectionResult], *, priority: float = 70.0) -> PrioritizedAlert:
    return PrioritizedAlert(
        asset_id=detections[0].asset_id,
        window_start=min(d.window_start for d in detections),
        window_end=max(d.window_end for d in detections),
        severity=AlertSeverity.HIGH,
        priority_score=priority,
        rationale="seeded test alert",
        score_components={"base": priority},
        detection_ids=[d.detection_id for d in detections],
    )


def seed_incident(api: ApiHarness) -> tuple[list[DetectionResult], PrioritizedAlert]:
    """One registered asset, three events, two detections, one alert, one reject."""
    detections = [detection(offset_s=0), detection(offset_s=30)]
    stored_alert = alert(detections)
    with api.session_factory() as session, session.begin():
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
            org_id=DEFAULT_ORG_ID,
        )
        repository.insert_events(
            session,
            [event(offset_s=0), event(offset_s=30), event(asset_id="gw-01", lane=Lane.LOG)],
            org_id=DEFAULT_ORG_ID,
        )
        repository.insert_malformed(
            session,
            [
                MalformedRecord(
                    raw="garbage",
                    error="unparseable",
                    source_format="stub-csv",
                    origin="test://seed",
                )
            ],
            org_id=DEFAULT_ORG_ID,
        )
        repository.insert_detections(session, detections, org_id=DEFAULT_ORG_ID)
        repository.insert_alerts(session, [stored_alert], org_id=DEFAULT_ORG_ID)
    return detections, stored_alert


class TestHealthAndEmpty:
    def test_health_reports_ok(self, api: ApiHarness) -> None:
        body = api.client.get("/health").json()
        assert body["status"] == "ok"

    def test_empty_database_yields_empty_lists_not_errors(self, api: ApiHarness) -> None:
        for path in ("/assets", "/events", "/detections", "/alerts", "/malformed"):
            response = api.client.get(path)
            assert response.status_code == 200
            assert response.json() == []

    def test_summary_on_empty_database(self, api: ApiHarness) -> None:
        body = api.client.get("/summary").json()
        assert body["counts"]["alerts"] == 0
        assert body["top_alert"] is None


class TestReads:
    def test_assets_include_registered_and_discovered(self, api: ApiHarness) -> None:
        seed_incident(api)
        by_id = {a["asset_id"]: a for a in api.client.get("/assets").json()}
        assert by_id["pump-02"]["name"] == "Feed pump 2"
        assert by_id["gw-01"]["metadata"] == {"auto_discovered": True}

    def test_events_filter_by_asset_and_lane(self, api: ApiHarness) -> None:
        seed_incident(api)
        rows = api.client.get("/events", params={"asset_id": "pump-02", "lane": "telemetry"}).json()
        assert len(rows) == 2
        assert all(r["asset_id"] == "pump-02" for r in rows)

    def test_events_time_range_is_closed(self, api: ApiHarness) -> None:
        seed_incident(api)
        rows = api.client.get(
            "/events",
            params={
                "asset_id": "pump-02",
                "start": T0.isoformat(),
                "end": (T0 + timedelta(seconds=30)).isoformat(),
            },
        ).json()
        # Closed upper bound: r30 sits exactly on `end` and is included, so a detection's
        # evidence recovers events on its window_end (models.py DetectionResult).
        assert [r["raw"] for r in rows] == ["r0", "r30"]

    def test_alerts_are_the_ranked_queue(self, api: ApiHarness) -> None:
        seed_incident(api)
        extra = detection(asset_id="gw-01", offset_s=7200)
        with api.session_factory() as session, session.begin():
            repository.insert_detections(session, [extra], org_id=DEFAULT_ORG_ID)
            repository.insert_alerts(
                session, [alert([extra], priority=95.0)], org_id=DEFAULT_ORG_ID
            )
        scores = [a["priority_score"] for a in api.client.get("/alerts").json()]
        assert scores == sorted(scores, reverse=True)

    def test_alert_drill_down_returns_its_evidence(self, api: ApiHarness) -> None:
        detections, stored_alert = seed_incident(api)
        rows = api.client.get(f"/alerts/{stored_alert.alert_id}/detections").json()
        assert {r["detection_id"] for r in rows} == {str(d.detection_id) for d in detections}

    def test_malformed_audit_view(self, api: ApiHarness) -> None:
        seed_incident(api)
        (row,) = api.client.get("/malformed").json()
        assert row["raw"] == "garbage"
        assert row["error"] == "unparseable"

    def test_summary_counts_and_top_alert(self, api: ApiHarness) -> None:
        _, stored_alert = seed_incident(api)
        body = api.client.get("/summary").json()
        assert body["counts"] == {
            "assets": 2,
            "events": 3,
            "malformed_records": 1,
            "detections": 2,
            "alerts": 1,
        }
        assert body["top_alert"]["alert_id"] == str(stored_alert.alert_id)


class TestWireFormat:
    def test_alert_response_round_trips_into_contract(self, api: ApiHarness) -> None:
        _, stored_alert = seed_incident(api)
        payload = api.client.get(f"/alerts/{stored_alert.alert_id}").json()
        restored = PrioritizedAlert.model_validate(payload)
        assert restored.model_dump(exclude={"detection_ids"}) == stored_alert.model_dump(
            exclude={"detection_ids"}
        )
        assert set(restored.detection_ids) == set(stored_alert.detection_ids)


class TestValidation:
    def test_unknown_alert_id_is_404(self, api: ApiHarness) -> None:
        assert api.client.get(f"/alerts/{uuid4()}").status_code == 404
        assert api.client.get(f"/alerts/{uuid4()}/detections").status_code == 404

    def test_malformed_uuid_is_422(self, api: ApiHarness) -> None:
        assert api.client.get("/alerts/not-a-uuid").status_code == 422

    def test_naive_datetime_param_is_422(self, api: ApiHarness) -> None:
        # Golden rule 4 at the API boundary: naive timestamps are a client error.
        response = api.client.get("/events", params={"start": "2026-07-07T12:00:00"})
        assert response.status_code == 422

    def test_aware_non_utc_datetime_param_is_normalized(self, api: ApiHarness) -> None:
        # Same instant expressed in +03:00 must behave identically to UTC —
        # TimeParam applies the contracts' normalize-to-UTC rule, not just
        # naive-rejection.
        seed_incident(api)
        athens = T0.astimezone(timezone(timedelta(hours=3)))
        rows = api.client.get(
            "/events",
            params={
                "asset_id": "pump-02",
                "start": athens.isoformat(),
                "end": (T0 + timedelta(seconds=30)).isoformat(),
            },
        ).json()
        # athens == T0, so r0 is included; the range is closed, so r30 (on `end`) is too.
        assert [r["raw"] for r in rows] == ["r0", "r30"]

    def test_invalid_lane_is_422(self, api: ApiHarness) -> None:
        assert api.client.get("/events", params={"lane": "carrier-pigeon"}).status_code == 422

    def test_limit_bounds_enforced(self, api: ApiHarness) -> None:
        assert api.client.get("/events", params={"limit": 0}).status_code == 422
        assert api.client.get("/events", params={"limit": 999_999}).status_code == 422
