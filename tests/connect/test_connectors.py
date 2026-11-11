"""The two connectors and the pull rules they share (ADR-0042).

The test that matters most is the round trip: what a connector writes must be what its
normalizer reads, row for row, or a pull would arrive on the Compose page as a pile of
malformed records. So every pull here is fed through the real normalizer.
"""

from datetime import date, datetime, timedelta
from itertools import pairwise
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from iotshield.connect import (
    CONNECTORS,
    ConnectionUnavailable,
    PullRefused,
    list_devices,
    pull,
    pull_chunks,
    pull_summary,
)
from iotshield.connect import port as port_module
from iotshield.connect import sensipro as sensipro_module
from iotshield.connect.connections import Connection, load_connections
from iotshield.connect.port import SLICE_DAYS, day_slices, time_slices
from iotshield.contracts import Lane
from iotshield.flow import METADATA_KEY as FLOW_KEY
from iotshield.flow import FlowRecord
from iotshield.pipeline.registry import build_detector, build_normalizer
from tests.connect_world import World

SENSIPRO = CONNECTORS["sensipro"]
BIOLOGY = CONNECTORS["biology"]
JULY_1 = date(2023, 7, 1)
JUNE_20, JULY_22 = date(2023, 6, 20), date(2023, 7, 22)  # the Biology fixture's span


def normalized(content: str, normalizer: str, source_format: str):  # type: ignore[no-untyped-def]
    return build_normalizer(normalizer).normalize(
        content, source_format=source_format, origin="pull"
    )


class TestSensipro:
    def test_pulls_one_utility_as_hourly_csv_the_meter_normalizer_reads(self, world: World) -> None:
        pulled = pull(
            SENSIPRO, world.sensipro, JULY_1, JULY_1, {"utility": "electricity"}, max_chars=10**6
        )
        assert (pulled.normalizer, pulled.source_format, pulled.rows) == (
            "meter",
            "sensipro-hourly-csv",
            12,
        )
        lines = pulled.content.splitlines()
        assert lines[0] == "device_id,from_timestamp,last_value,unit"
        meter, stamp, counter, unit = lines[1].split(",")
        # The counter's text is the database's own (100, 100.0, 100.000 by backend).
        assert (meter, stamp, float(counter), unit) == ("e1", "2023-07-01 00:00:00", 100.0, "kWh")

        result = normalized(pulled.content, pulled.normalizer, pulled.source_format)
        assert result.malformed == []
        # Six readings per meter difference into five hourly consumptions.
        assert len(result.events) == 10
        assert {event.asset_id for event in result.events} == {"e1", "e2"}
        assert {event.value for event in result.events} == {1.5}
        assert {event.metric for event in result.events} == {"hourly_consumption_kwh"}

    def test_water_is_its_own_utility(self, world: World) -> None:
        pulled = pull(
            SENSIPRO, world.sensipro, JULY_1, JULY_1, {"utility": "water"}, max_chars=10**6
        )
        result = normalized(pulled.content, pulled.normalizer, pulled.source_format)
        assert {event.asset_id for event in result.events} == {"w1"}
        assert {event.value for event in result.events} == {0.25}

    def test_the_end_day_is_included_and_the_day_after_is_not(self, world: World) -> None:
        two_days = pull(SENSIPRO, world.sensipro, JULY_1, date(2023, 7, 2), {}, max_chars=10**6)
        three_days = pull(SENSIPRO, world.sensipro, JULY_1, date(2023, 7, 3), {}, max_chars=10**6)
        assert (two_days.rows, three_days.rows) == (12, 13)

    def test_narrows_to_named_meters(self, world: World) -> None:
        pulled = pull(
            SENSIPRO, world.sensipro, JULY_1, JULY_1, {"devices": ["e2"]}, max_chars=10**6
        )
        assert pulled.rows == 6
        assert all(line.startswith("e2,") for line in pulled.content.splitlines()[1:])

    def test_lists_the_meters_of_a_utility_with_their_description(self, world: World) -> None:
        devices = list_devices(SENSIPRO, world.sensipro, {"utility": "electricity"})
        assert [(device.id, device.label) for device in devices] == [
            ("e1", "e1 — Office A"),
            ("e2", "e2"),
            ("e3", "e3 — Failing meter"),
        ]


class TestSensiproFillerHours:
    """SensiPro writes every hour even when nothing usable arrived (ADR-0043 amendment)."""

    def pull_e3(self, world: World):  # type: ignore[no-untyped-def]
        day = date(2023, 7, 5)
        return pull(SENSIPRO, world.sensipro, day, day, {"devices": ["e3"]}, max_chars=10**6)

    def test_an_hour_with_nothing_received_is_left_out(self, world: World) -> None:
        pulled = self.pull_e3(world)
        stamps = [line.split(",")[1] for line in pulled.content.splitlines()[1:]]
        assert "2023-07-05 02:00:00" not in stamps
        assert pulled.rows == 9

    def test_an_hour_whose_readings_were_all_rejected_says_so(self, world: World) -> None:
        lines = self.pull_e3(world).content.splitlines()
        assert "e3,2023-07-05 04:00:00,rejected-by-sensipro,kWh" in lines
        # Some readings rejected but a real counter: passed on as it is.
        assert any(line.startswith("e3,2023-07-05 05:00:00,15") for line in lines)

    def test_through_the_normalizer_and_meter_quality_they_become_heartbeat_and_value_error(
        self, world: World
    ) -> None:
        pulled = self.pull_e3(world)
        result = normalized(pulled.content, pulled.normalizer, pulled.source_format)
        (rejected,) = result.malformed
        assert rejected.error.startswith("[valueError] meter e3 at 2023-07-05T01:00:00+00:00: ")
        assert "rejected-by-sensipro" in rejected.error
        found = build_detector("meter_quality", {}).detect(result.events)
        assert [item.evidence["quality"] for item in found] == ["heartbeat"]
        # The filler is gone, so no hour of "zero consumption" is invented either.
        assert 0.0 not in {event.value for event in result.events}


class TestBiology:
    def test_pulls_flows_as_cicflowmeter_csv_the_flow_normalizer_reads(self, world: World) -> None:
        pulled = pull(
            BIOLOGY, world.biology, date(2023, 6, 1), date(2023, 7, 31), {}, max_chars=10**6
        )
        assert (pulled.normalizer, pulled.source_format, pulled.rows) == (
            "flow",
            "cicflowmeter-csv",
            4,
        )
        result = normalized(pulled.content, pulled.normalizer, pulled.source_format)
        # The DHCP request from 0.0.0.0 has no monitored endpoint: kept, with its reason.
        assert len(result.events) == 3 and len(result.malformed) == 1
        assert "neither endpoint" in result.malformed[0].error
        event = result.events[0]
        record = FlowRecord.from_metadata(event.metadata[FLOW_KEY])
        assert event.source_type is Lane.FLOW and event.asset_id == "192.168.0.140"
        assert event.timestamp.isoformat() == "2023-07-22T11:45:00+00:00"
        assert (record.protocol, record.peer_port, record.bytes_out) == ("udp", 47808, 24)
        assert record.duration_s == pytest.approx(0.0015)

    def test_rows_come_oldest_first(self, world: World) -> None:
        pulled = pull(
            BIOLOGY, world.biology, date(2023, 6, 1), date(2023, 7, 31), {}, max_chars=10**6
        )
        stamps = [line.split(",")[5] for line in pulled.content.splitlines()[1:]]
        assert stamps == sorted(stamps)

    def test_narrows_to_named_hosts_and_lists_them(self, world: World) -> None:
        pulled = pull(
            BIOLOGY,
            world.biology,
            date(2023, 6, 1),
            date(2023, 7, 31),
            {"devices": ["192.168.0.140"]},
            max_chars=10**6,
        )
        assert pulled.rows == 3
        devices = list_devices(BIOLOGY, world.biology, {})
        assert [device.label for device in devices] == [
            "0.0.0.0 — 1 flows",
            "192.168.0.140 — 3 flows",
        ]


class TestThePullRules:
    def test_an_end_before_the_start_is_refused(self, world: World) -> None:
        with pytest.raises(PullRefused, match="before the start"):
            pull(SENSIPRO, world.sensipro, JULY_1, date(2023, 6, 30), {}, max_chars=10**6)

    def test_a_window_of_years_is_read_whole(self, world: World) -> None:
        # ADR-0052: no window limit. The fixture's rows all fall in July 2023, so a 1,220-day
        # window around it reads exactly what that month does, from either connector.
        years = (date(2021, 1, 1), date(2024, 5, 4))
        assert (years[1] - years[0]).days + 1 == 1220
        for spec, connection in ((SENSIPRO, world.sensipro), (BIOLOGY, world.biology)):
            month = pull(spec, connection, JUNE_20, JULY_22, {}, max_chars=10**6)
            long = pull(spec, connection, *years, {}, max_chars=10**6)
            assert long.content == month.content, spec.name
            # The count is of rows read, before SensiPro's filler hours are dropped.
            counted = pull_summary(spec, connection, JUNE_20, JULY_22, {}).rows
            assert pull_summary(spec, connection, *years, {}).rows == counted, spec.name

    def test_there_is_no_row_ceiling(self, world: World, monkeypatch: pytest.MonkeyPatch) -> None:
        # ADR-0047: a run streams the window and batches it, so a large window is no longer
        # refused for its size. Squeezing every limit that remains to its minimum still reads
        # every row: one meter per query, one line per chunk.
        monkeypatch.setattr(sensipro_module, "METERS_PER_QUERY", 1)
        whole = pull(SENSIPRO, world.sensipro, JULY_1, JULY_1, {})
        chunks = list(pull_chunks(SENSIPRO, world.sensipro, JULY_1, JULY_1, {}, chunk_lines=1))
        assert sum(len(chunk.splitlines()) - 1 for chunk in chunks) == whole.rows == 12
        assert not hasattr(port_module, "MAX_PULL_ROWS")

    def test_too_much_text_for_one_source_is_refused(self, world: World) -> None:
        with pytest.raises(PullRefused, match="over the 100 one source may hold"):
            pull(SENSIPRO, world.sensipro, JULY_1, JULY_1, {}, max_chars=100)

    @pytest.mark.parametrize("params", [{"utility": "gas"}, {"colour": "red"}, {"devices": "e1"}])
    def test_bad_params_are_refused_by_name(self, world: World, params: dict[str, object]) -> None:
        with pytest.raises(PullRefused, match="invalid pull parameters for sensipro"):
            pull(SENSIPRO, world.sensipro, JULY_1, JULY_1, params, max_chars=10**6)

    def test_a_database_failure_names_the_connection_and_not_where_it_is(
        self, world: World
    ) -> None:
        broken = Connection(
            name="sensipro-down",
            connector="sensipro",
            url=URL.create("sqlite", database="/nonexistent/dir/secret-host.db"),
            password_env=None,
            organizations=None,
        )
        with pytest.raises(ConnectionUnavailable) as caught:
            pull(SENSIPRO, broken, JULY_1, JULY_1, {}, max_chars=10**6)
        assert str(caught.value) == "could not read from connection 'sensipro-down'"


class TestStreaming:
    """A run reads a pull as chunks it normalizes one at a time (ADR-0047)."""

    def test_a_chunk_never_cuts_a_meter_in_two(self, world: World) -> None:
        # chunk_lines=1 asks for a cut after every line; the cut waits for the meter to change,
        # because the normalizer differences a meter's counter row to row.
        chunks = list(pull_chunks(SENSIPRO, world.sensipro, JULY_1, JULY_1, {}, chunk_lines=1))
        meters = [{line.split(",")[0] for line in chunk.splitlines()[1:]} for chunk in chunks]
        assert meters == [{"e1"}, {"e2"}]
        assert all(
            chunk.startswith("device_id,from_timestamp,last_value,unit\n") for chunk in chunks
        )

    def test_chunks_normalize_to_exactly_what_the_whole_window_does(self, world: World) -> None:
        start, end = JULY_1, date(2023, 7, 5)
        whole = pull(SENSIPRO, world.sensipro, start, end, {})
        expected = normalized(whole.content, "meter", "sensipro-hourly-csv")
        events, malformed = [], []
        for chunk in pull_chunks(SENSIPRO, world.sensipro, start, end, {}, chunk_lines=2):
            result = normalized(chunk, "meter", "sensipro-hourly-csv")
            events.extend(result.events)
            malformed.extend(result.malformed)
        # event_id is minted per event, so it is the one field two readings cannot share.
        assert [e.model_dump(exclude={"event_id"}) for e in events] == [
            e.model_dump(exclude={"event_id"}) for e in expected.events
        ]
        minted = {"record_id", "created_at"}
        assert [m.model_dump(exclude=minted) for m in malformed] == [
            m.model_dump(exclude=minted) for m in expected.malformed
        ]

    def test_flows_may_be_cut_after_any_line(self, world: World) -> None:
        chunks = list(pull_chunks(BIOLOGY, world.biology, JUNE_20, JULY_22, {}, chunk_lines=1))
        assert [len(chunk.splitlines()) for chunk in chunks] == [2, 2, 2, 2]

    def test_the_stream_is_lazy(self, world: World) -> None:
        broken = Connection(
            name="down",
            connector="sensipro",
            url=URL.create("sqlite", database="/nonexistent/dir/x.db"),
            password_env=None,
            organizations=None,
        )
        chunks = pull_chunks(SENSIPRO, broken, JULY_1, JULY_1, {})  # nothing queried yet
        with pytest.raises(ConnectionUnavailable, match="'down'"):
            next(chunks)

    def test_a_bad_window_is_refused_before_anything_is_read(self, world: World) -> None:
        with pytest.raises(PullRefused, match="before the start"):
            next(pull_chunks(SENSIPRO, world.sensipro, date(2023, 12, 31), date(2023, 1, 1), {}))


class TestSlicing:
    """How a long window is read without any one query growing (ADR-0052)."""

    def test_time_slices_cover_the_window_once_in_order(self) -> None:
        start, end = datetime(2021, 1, 1), datetime(2024, 5, 5)
        slices = time_slices(start, end)
        assert slices[0][0] == start and slices[-1][1] == end
        assert all(a[1] == b[0] for a, b in pairwise(slices))
        assert all(after - first <= timedelta(days=SLICE_DAYS) for first, after in slices)
        assert time_slices(start, start + timedelta(hours=1)) == [
            (start, start + timedelta(hours=1))
        ]

    def test_day_slices_are_inclusive_and_never_share_a_day(self) -> None:
        assert day_slices(JULY_1, JULY_1) == [(JULY_1, JULY_1)]
        slices = day_slices(date(2021, 1, 1), date(2024, 5, 4))
        assert len(slices) == 14
        assert all(b[0] - a[1] == timedelta(days=1) for a, b in pairwise(slices))
        assert all((last - first).days + 1 <= SLICE_DAYS for first, last in slices)

    @pytest.mark.parametrize(
        ("days", "meters"), [(1, 100), (92, 100), (184, 50), (365, 25), (1220, 7), (10**6, 1)]
    )
    def test_sensipro_shrinks_its_meter_group_to_keep_rows_per_query(
        self, days: int, meters: int
    ) -> None:
        assert sensipro_module.meters_per_query(days) == meters

    def test_sensipro_keeps_meter_order_over_a_long_window(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # One meter per query at this length, and still one contiguous run per meter.
        monkeypatch.setattr(sensipro_module, "METERS_PER_QUERY", 2)
        assert sensipro_module.meters_per_query(1220) == 1
        content = pull(
            SENSIPRO, world.sensipro, date(2021, 1, 1), date(2024, 5, 4), {}, max_chars=10**6
        ).content
        meters = [line.split(",")[0] for line in content.splitlines()[1:]]
        runs = [m for i, m in enumerate(meters) if i == 0 or meters[i - 1] != m]
        assert runs == sorted(set(meters))

    def test_biology_reads_a_window_split_across_slices_in_time_order(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Slices of one day put the fixture's four flows into separate queries.
        monkeypatch.setattr(port_module, "SLICE_DAYS", 1)
        sliced = [
            line
            for chunk in pull_chunks(BIOLOGY, world.biology, JUNE_20, JULY_22, {})
            for line in chunk.splitlines()[1:]
        ]
        monkeypatch.undo()
        whole = pull(BIOLOGY, world.biology, JUNE_20, JULY_22, {}).content.splitlines()[1:]
        assert sliced == whole and len(whole) == 4


class TestSummary:
    """What the compose page is shown instead of the window: a count and a sample."""

    def test_counts_rows_read_and_samples_the_first_lines(self, world: World) -> None:
        day = date(2023, 7, 5)
        summary = pull_summary(SENSIPRO, world.sensipro, day, day, {"devices": ["e3"]})
        # Ten hours read, one of them left out (nothing arrived): the count is of rows READ.
        assert summary.rows == 10
        lines = summary.sample.splitlines()
        assert lines[0] == "device_id,from_timestamp,last_value,unit"
        assert len(lines) == 1 + 9
        assert (summary.normalizer, summary.source_format) == ("meter", "sensipro-hourly-csv")

    def test_the_sample_is_capped(self, world: World) -> None:
        summary = pull_summary(SENSIPRO, world.sensipro, JULY_1, JULY_1, {}, sample_lines=3)
        assert summary.rows == 12
        assert len(summary.sample.splitlines()) == 1 + 3

    def test_flows(self, world: World) -> None:
        summary = pull_summary(BIOLOGY, world.biology, JUNE_20, JULY_22, {})
        assert summary.rows == 4
        assert summary.sample.splitlines()[0].startswith("src_ip,dst_ip")


class TestConnectionsFile:
    def write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "connections.yaml"
        path.write_text(body, encoding="utf-8")
        return path

    def test_reads_each_connection_without_ever_holding_a_password(self, tmp_path: Path) -> None:
        path = self.write(
            tmp_path,
            """
connections:
  sensipro:
    connector: sensipro
    driver: postgresql
    host: host.docker.internal
    port: 5433
    database: sensipro
    user: grafana
    password_env: SENSIPRO_DB_PASSWORD
    organizations: [ucy]
  biology:
    connector: biology
    driver: mysql
    host: host.docker.internal
    port: 3307
    database: laravel
    user: root
    password_env: BIOLOGY_DB_PASSWORD
    organizations: "*"
""",
        )
        connections = load_connections(path)
        sensipro, biology = connections["sensipro"], connections["biology"]
        assert sensipro.url.drivername == "postgresql+psycopg"
        assert sensipro.url.password is None
        assert biology.url.drivername == "mysql+pymysql"
        assert sensipro.allows("ucy") and not sensipro.allows("acme")
        assert biology.allows("acme")

    def test_no_file_means_no_connections(self, tmp_path: Path) -> None:
        assert load_connections(tmp_path / "absent.yaml") == {}

    @pytest.mark.parametrize(
        "body",
        [
            "connections: [not, a, mapping]",
            "connections:\n  x:\n    connector: sensipro\n    driver: oracle\n    host: h\n"
            "    port: 1\n    database: d\n    user: u\n    password_env: P\n"
            "    organizations: '*'",
            "connections:\n  x:\n    connector: sensipro\n    driver: mysql\n    host: h\n"
            "    port: 1\n    database: d\n    user: u\n    password: plaintext\n"
            "    password_env: P\n    organizations: '*'",
            "connections: {x: [",
        ],
        ids=["not-a-mapping", "unknown-driver", "a-password-in-the-file", "bad-yaml"],
    )
    def test_a_malformed_file_fails_loudly_naming_itself(self, tmp_path: Path, body: str) -> None:
        path = self.write(tmp_path, body)
        with pytest.raises(ValueError, match="not a valid connections file"):
            load_connections(path)

    def test_a_missing_password_variable_makes_the_connection_unavailable(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("NOT_SET_ANYWHERE", raising=False)
        connection = Connection(
            name="nopass",
            connector="sensipro",
            url=URL.create("postgresql+psycopg", host="db.invalid", database="d"),
            password_env="NOT_SET_ANYWHERE",
            organizations=None,
        )
        with pytest.raises(ConnectionUnavailable, match="no password configured"):
            connection.engine()
