"""The two connectors and the pull rules they share (ADR-0042).

The test that matters most is the round trip: what a connector writes must be what its
normalizer reads, row for row, or a pull would arrive on the Compose page as a pile of
malformed records. So every pull here is fed through the real normalizer.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy.engine import URL

from iotshield.connect import CONNECTORS, ConnectionUnavailable, PullRefused, list_devices, pull
from iotshield.connect import port as port_module
from iotshield.connect.connections import Connection, load_connections
from iotshield.contracts import Lane
from iotshield.flow import METADATA_KEY as FLOW_KEY
from iotshield.flow import FlowRecord
from iotshield.pipeline.registry import build_detector, build_normalizer
from tests.connect_world import World

SENSIPRO = CONNECTORS["sensipro"]
BIOLOGY = CONNECTORS["biology"]
JULY_1 = date(2023, 7, 1)


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

    def test_a_window_longer_than_a_quarter_is_refused_before_querying(self, world: World) -> None:
        with pytest.raises(PullRefused, match="at most 92"):
            pull(
                SENSIPRO, world.sensipro, date(2023, 1, 1), date(2023, 12, 31), {}, max_chars=10**6
            )

    def test_too_many_rows_is_refused_never_truncated(
        self, world: World, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(port_module, "MAX_PULL_ROWS", 5)
        with pytest.raises(PullRefused, match="more than 5 rows"):
            pull(SENSIPRO, world.sensipro, JULY_1, JULY_1, {}, max_chars=10**6)

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
