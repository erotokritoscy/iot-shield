"""Typed-client tests: real API in-process, contract objects out."""

from uuid import uuid4

import httpx
import pytest

from iotshield.api import Summary
from iotshield.contracts import Lane, MalformedRecord, NormalizedEvent, PrioritizedAlert
from iotshield.dashboard.client import ApiClient
from tests.dashboard.conftest import SeededDashboard


def test_alerts_parse_into_contracts(seeded: SeededDashboard) -> None:
    (alert,) = seeded.client.alerts()
    assert isinstance(alert, PrioritizedAlert)
    assert alert.alert_id == seeded.alert.alert_id


def test_events_filters_are_forwarded(seeded: SeededDashboard) -> None:
    events = seeded.client.events(asset_id="pump-02", lane=Lane.TELEMETRY)
    assert len(events) == 3
    assert all(isinstance(e, NormalizedEvent) for e in events)
    assert seeded.client.events(asset_id="no-such-asset") == []


def test_alert_detections_resolve_lineage(seeded: SeededDashboard) -> None:
    evidence = seeded.client.alert_detections(seeded.alert.alert_id)
    assert [d.detection_id for d in evidence] == [seeded.detection.detection_id]


def test_detections_lane_filter_is_forwarded(seeded: SeededDashboard) -> None:
    # The seeded detection is telemetry-lane; the lane filter must reach the API.
    assert len(seeded.client.detections(asset_id="pump-02", lane=Lane.TELEMETRY)) == 1
    assert seeded.client.detections(asset_id="pump-02", lane=Lane.LOG) == []


def test_summary_parses(seeded: SeededDashboard) -> None:
    summary = seeded.client.summary()
    assert isinstance(summary, Summary)
    assert summary.counts["alerts"] == 1
    assert summary.top_alert is not None


def test_malformed_parses(seeded: SeededDashboard) -> None:
    (record,) = seeded.client.malformed()
    assert isinstance(record, MalformedRecord)
    assert record.raw == "garbage-line"


def test_http_errors_surface_loudly(seeded: SeededDashboard) -> None:
    with pytest.raises(httpx.HTTPStatusError):
        seeded.client.alert_detections(uuid4())  # unknown alert -> 404 -> raise


def test_empty_database_yields_empty_lists(empty_client: ApiClient) -> None:
    assert empty_client.alerts() == []
    assert empty_client.assets() == []
