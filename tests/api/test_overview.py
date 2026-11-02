"""The IT and OT overview routes (ADR-0044), over results a real run stored.

One meter world is run through the Pipeline API with the meter normalizer and
``meter_quality``, so every one of SensiPro's six types arrives the way it would in
production — two as detections, four as tagged rejects — and the read API's overview must
count all six, place them on their meter and day, and keep one organization's out of another's.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from iotshield.api import create_app
from iotshield.api.control import create_control_app
from iotshield.detect.meter_quality import METHOD
from iotshield.storage import create_schema, make_engine
from iotshield.storage.overview import METER_QUALITY
from tests.api.conftest import Stack

# Cyprus local time (UTC+3 in July): 2023-07-01 03:00 local is 00:00 UTC.
METERS = """device_id,from_timestamp,last_value,unit
e1,2023-07-01 03:00:00,100.0,kWh
e1,2023-07-01 04:00:00,101.0,kWh
e1,2023-07-01 05:00:00,102.0,kWh
e1,2023-07-01 07:00:00,103.0,kWh
e1,2023-07-01 08:00:00,104.0,kWh
e1,2023-07-01 09:00:00,150.0,kWh
e1,2023-07-01 10:00:00,151.0,kWh
e1,2023-07-01 11:00:00,0,kWh
e2,2023-07-01 03:00:00,50.0,kWh
e2,2023-07-01 04:00:00,51.0,kWh
e2,2023-07-01 05:00:00,40.0,kWh
e2,2023-07-01 06:00:00,-5.0,kWh
e2,2023-07-01 07:00:00,abc,kWh
"""
# e1: 06:00 missing (heartbeat), 09:00 uses 46 kWh (high), 11:00 falls to 0 (zero).
# e2: 05:00 falls (small), 06:00 is negative, 07:00 is unreadable (valueError).

FLOWS = """\
start,src_ip,src_port,dst_ip,dst_port,proto,duration,src_bytes,dst_bytes,src_packets,dst_packets,\
state
2026-07-07T12:00:00+00:00,10.0.0.24,51000,203.0.113.5,23,tcp,0.1,60,0,1,0,S0
2026-07-07T12:00:05+00:00,10.0.0.24,51001,203.0.113.6,443,tcp,0.1,60,40,1,1,SF
2026-07-08T09:00:00+00:00,10.0.0.31,40000,8.8.8.8,53,udp,0.1,60,90,1,1,SF
"""


@dataclass
class Apps:
    read: TestClient
    pipeline: TestClient


@pytest.fixture()
def apps(tmp_path: Path) -> Iterator[Apps]:
    engine = make_engine(f"sqlite:///{(tmp_path / 'overview.db').as_posix()}")
    create_schema(engine)
    with (
        TestClient(create_app(engine, auth=False)) as read,
        TestClient(create_control_app(engine, auth=False)) as pipeline,
    ):
        yield Apps(read, pipeline)
    engine.dispose()


def run(
    client: TestClient, content: str, normalizer: str, source_format: str, detector: dict[str, Any]
) -> Any:
    response = client.post(
        "/runs",
        json={
            "spec": {
                "sources": [{"normalizer": normalizer, "source_format": source_format}],
                "detectors": [detector],
                "alert_engine": {"name": "stub"},
            },
            "inputs": [{"content": content}],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()


def run_meters(client: TestClient) -> None:
    run(client, METERS, "meter", "sensipro-hourly-csv", {"name": "meter_quality"})


def test_the_storage_constant_names_the_detector() -> None:
    assert METER_QUALITY == METHOD


class TestOt:
    def test_counts_all_six_types_from_detections_and_rejects(self, apps: Apps) -> None:
        run_meters(apps.pipeline)
        body = apps.read.get("/overview/ot?start=2023-07-01&end=2023-07-01").json()
        by_type = {row["issue"]: row["count"] for row in body["per_type"]}
        assert by_type == {
            "heartbeat": 1,
            "valueError": 1,
            "zero": 1,
            "negative": 1,
            "high": 1,
            "small": 1,
        }
        # SensiPro's code order, codes included.
        assert [(row["category"], row["subcategory"]) for row in body["per_type"]] == [
            (1, 0),
            (2, 0),
            (2, 1),
            (2, 2),
            (2, 3),
            (2, 4),
        ]
        assert (body["total"], body["unplaced"], body["meters"]) == (6, 0, 2)
        assert {row["asset_id"]: row["count"] for row in body["per_meter"]} == {"e1": 3, "e2": 3}
        assert body["latest_day"] == "2023-07-01"
        # The replay's per-meter series adds up to the per-meter totals.
        assert sum(point["count"] for point in body["meter_series"]) == 6

    def test_places_each_anomaly_on_its_utc_day_or_hour(self, apps: Apps) -> None:
        run_meters(apps.pipeline)
        daily = apps.read.get("/overview/ot?start=2023-07-01&end=2023-07-01").json()["series"]
        assert {row["bucket"] for row in daily} == {"2023-07-01"}
        hourly = apps.read.get("/overview/ot?start=2023-07-01&end=2023-07-01&bucket=hour").json()
        buckets = {(row["issue"], row["bucket"]) for row in hourly["series"]}
        # 11:00 local is 08:00 UTC; the fall to zero is placed there, from the reject's tag.
        assert ("zero", "2023-07-01T08:00:00+00:00") in buckets
        assert ("high", "2023-07-01T06:00:00+00:00") in buckets

    def test_a_range_without_the_data_is_all_zeros(self, apps: Apps) -> None:
        run_meters(apps.pipeline)
        body = apps.read.get("/overview/ot?start=2023-08-01&end=2023-08-31").json()
        assert body["total"] == 0 and all(row["count"] == 0 for row in body["per_type"])
        # Where the data is, whatever was asked for.
        assert body["latest_day"] == "2023-07-01"

    def test_narrows_to_one_meter(self, apps: Apps) -> None:
        run_meters(apps.pipeline)
        body = apps.read.get("/overview/ot?start=2023-07-01&end=2023-07-01&asset_id=e2").json()
        assert {row["issue"] for row in body["per_type"] if row["count"]} == {
            "small",
            "negative",
            "valueError",
        }

    def test_offers_every_meter_with_a_reading_whatever_the_narrowing(self, apps: Apps) -> None:
        run_meters(apps.pipeline)
        for query in ("", "&asset_id=e2"):
            body = apps.read.get(f"/overview/ot?start=2023-07-01&end=2023-07-01{query}").json()
            assert body["meter_ids"] == ["e1", "e2"]
        # A range holding no readings offers none.
        empty = apps.read.get("/overview/ot?start=2023-08-01&end=2023-08-01").json()
        assert empty["meter_ids"] == []

    def test_lists_the_records_with_a_total(self, apps: Apps) -> None:
        run_meters(apps.pipeline)
        response = apps.read.get("/overview/ot/records?start=2023-07-01&end=2023-07-01&limit=2")
        assert response.headers["X-Total-Count"] == "6" and len(response.json()) == 2
        high = apps.read.get(
            "/overview/ot/records?start=2023-07-01&end=2023-07-01&issue=high"
        ).json()
        assert [(r["asset_id"], r["source"]) for r in high] == [("e1", "detection")]
        assert "above the limit of 35" in high[0]["detail"]
        zero = apps.read.get(
            "/overview/ot/records?start=2023-07-01&end=2023-07-01&issue=zero"
        ).json()
        assert zero[0]["source"] == "reject" and zero[0]["detail"].startswith("[zero] meter e1 at ")

    @pytest.mark.parametrize(
        "query",
        [
            "start=2023-07-02&end=2023-07-01",
            "start=2022-01-01&end=2023-12-31",
            "start=x&end=2023-07-01",
        ],
    )
    def test_a_bad_range_is_refused(self, apps: Apps, query: str) -> None:
        assert apps.read.get(f"/overview/ot?{query}").status_code == 422


class TestIt:
    def test_counts_flows_the_detections_covered_and_detections_per_day(self, apps: Apps) -> None:
        run(apps.pipeline, FLOWS, "flow", "flow-csv", {"name": "rules", "params": {"lane": "flow"}})
        body = apps.read.get("/overview/it?start=2026-07-07&end=2026-07-08").json()
        assert body["flows"] == 3
        # Only the telnet flow is inside a detection's window (a point detection on it).
        assert body["flagged"] == 1
        assert body["per_day"] == [{"day": "2026-07-07", "count": 1}]
        assert body["per_method"] == [{"method": "rules:flow", "count": 1}]
        assert body["series"] == [
            {"bucket": "2026-07-07", "flows": 2, "flagged": 1, "detections": 1},
            {"bucket": "2026-07-08", "flows": 1, "flagged": 0, "detections": 0},
        ]
        hourly = apps.read.get("/overview/it?start=2026-07-07&end=2026-07-07&bucket=hour").json()
        assert [point["bucket"] for point in hourly["series"]] == ["2026-07-07T12:00:00+00:00"]

    def test_a_day_with_no_flows(self, apps: Apps) -> None:
        run(apps.pipeline, FLOWS, "flow", "flow-csv", {"name": "rules", "params": {"lane": "flow"}})
        body = apps.read.get("/overview/it?start=2026-07-09&end=2026-07-09").json()
        assert body == {
            "flows": 0,
            "flagged": 0,
            "per_day": [],
            "per_method": [],
            "latest_day": "2026-07-08",
            "series": [],
        }


def test_one_organization_never_counts_anothers(stack: Stack) -> None:
    acme = stack.browser()
    acme.register("Acme", "alice")
    run(acme.pipeline, METERS, "meter", "sensipro-hourly-csv", {"name": "meter_quality"})
    globex = stack.browser()
    globex.register("Globex", "gina")
    assert acme.read.get("/overview/ot?start=2023-07-01&end=2023-07-01").json()["total"] == 6
    assert globex.read.get("/overview/ot?start=2023-07-01&end=2023-07-01").json()["total"] == 0
