"""Two SQLite files standing in for SensiPro's Postgres and Biology's MySQL (ADR-0042).

A fixture plugin, registered in tests/conftest.py, so tests/connect and tests/api share it.

Only the tables and columns the connectors read, filled with a small known world:

- SensiPro: two electricity meters and one water meter, six hours each on 2023-07-01
  (Cyprus local), plus one electricity hour on 2023-07-03 that a 07-01..07-02 window must
  exclude. Counters rise by 1.5 kWh / 0.25 m3 an hour. A third electricity meter, e3, has ten
  hours on 2023-07-05 in which 02:00 received nothing (the rollup repeats the counter) and
  04:00's readings were all rejected by SensiPro — the two ways its real meters fail — and
  then recovers, so there are readings on both sides of the trouble.
- Biology: four cicflowmeter rows — three BACnet broadcasts from 192.168.0.140 and one DHCP
  request from 0.0.0.0 that the flow normalizer cannot attribute to any asset.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL

from iotshield.connect.connections import Connection, register_engine

SENSIPRO_SCHEMA = """
CREATE TABLE devices (id INTEGER PRIMARY KEY, destination_address VARCHAR, description VARCHAR,
                      unit VARCHAR);
CREATE TABLE ascendinghourlyreading (id INTEGER PRIMARY KEY, device_id INTEGER,
                                     from_timestamp TIMESTAMP, last_value NUMERIC,
                                     count_accepted INTEGER DEFAULT 12,
                                     count_rejected INTEGER DEFAULT 0);
"""
BIOLOGY_SCHEMA = """
CREATE TABLE packets (id INTEGER PRIMARY KEY, src_ip VARCHAR, dst_ip VARCHAR, src_port INTEGER,
                      dst_port INTEGER, protocol INTEGER, "timestamp" TIMESTAMP,
                      flow_duration DOUBLE, tot_fwd_pkts DOUBLE, tot_bwd_pkts DOUBLE,
                      totlen_fwd_pkts DOUBLE, totlen_bwd_pkts DOUBLE, flow_byts_s DOUBLE);
"""


CONNECTIONS_YAML = """
connections:
  sensipro:
    connector: sensipro
    driver: postgresql
    host: db.invalid
    port: 5432
    database: sensipro
    user: u
    password_env: TEST_SENSIPRO_PASSWORD
    organizations: "*"
  biology:
    connector: biology
    driver: postgresql
    host: db.invalid
    port: 5432
    database: biology
    user: u
    password_env: TEST_BIOLOGY_PASSWORD
    organizations: [default]
"""
"""The world's two connections as a deployment would declare them: the same URLs, so the
engines registered for them are the ones a route resolves. Biology is the default org's only."""


@dataclass
class World:
    sensipro: Connection
    biology: Connection
    sensipro_engine: Engine
    biology_engine: Engine


def _connection(name: str, connector: str, organizations: frozenset[str] | None) -> Connection:
    return Connection(
        name=name,
        connector=connector,
        url=URL.create(
            "postgresql+psycopg", host="db.invalid", port=5432, database=name, username="u"
        ),
        password_env=None,
        organizations=organizations,
    )


def _engine(path: Path, schema: str) -> Engine:
    engine = create_engine(f"sqlite:///{path.as_posix()}")
    with engine.begin() as connection:
        for statement in schema.split(";"):
            if statement.strip():
                connection.execute(text(statement))
    return engine


def _fill_sensipro(engine: Engine) -> None:
    devices = [
        (1, "e1", "Office A", "kWh"),
        (2, "e2", None, "kWh"),
        (3, "w1", "Kitchen", "m3"),
        (4, None, "unaddressed", "W"),
        (5, "e3", "Failing meter", "kWh"),
    ]
    rows: list[tuple[int, str, float]] = []
    for device_id, step in ((1, 1.5), (2, 1.5), (3, 0.25)):
        for hour in range(6):
            rows.append((device_id, f"2023-07-01 {hour:02d}:00:00", 100.0 + step * hour))
    rows.append((1, "2023-07-03 00:00:00", 200.0))
    with engine.begin() as connection:
        connection.execute(
            text("INSERT INTO devices VALUES (:i, :a, :d, :u)"),
            [{"i": i, "a": a, "d": d, "u": u} for i, a, d, u in devices],
        )
        connection.execute(
            text(
                "INSERT INTO ascendinghourlyreading (device_id, from_timestamp, last_value) "
                "VALUES (:d, :t, :v)"
            ),
            [{"d": d, "t": t, "v": v} for d, t, v in rows],
        )
        # e3: (hour, counter, accepted, rejected). 02:00 repeats 01:00's counter with nothing
        # behind it; 04:00 repeats 03:00's with every reading rejected.
        failing = [
            (0, 10.0, 12, 0),
            (1, 11.0, 12, 0),
            (2, 11.0, 0, 0),
            (3, 13.0, 12, 0),
            (4, 13.0, 0, 3),
            (5, 15.0, 12, 1),
            *((hour, 10.0 + hour, 12, 0) for hour in range(6, 10)),
        ]
        connection.execute(
            text(
                "INSERT INTO ascendinghourlyreading (device_id, from_timestamp, last_value, "
                "count_accepted, count_rejected) VALUES (5, :t, :v, :a, :r)"
            ),
            [
                {"t": f"2023-07-05 {hour:02d}:00:00", "v": value, "a": accepted, "r": rejected}
                for hour, value, accepted, rejected in failing
            ],
        )


def _fill_biology(engine: Engine) -> None:
    rows = [
        (
            "192.168.0.140",
            "192.168.0.255",
            47808,
            47808,
            17,
            "2023-07-22 11:45:00",
            1500.0,
            1,
            0,
            24,
            0,
        ),
        (
            "192.168.0.140",
            "192.168.0.255",
            47808,
            47808,
            17,
            "2023-07-22 11:46:00",
            1200.0,
            1,
            0,
            24,
            0,
        ),
        (
            "192.168.0.140",
            "192.168.0.255",
            47808,
            47808,
            17,
            "2023-07-22 11:47:00",
            900.0,
            1,
            0,
            24,
            0,
        ),
        ("0.0.0.0", "255.255.255.255", 68, 67, 17, "2023-06-20 07:35:00", 0.0, 1, 0, 300, 0),
    ]
    with engine.begin() as connection:
        connection.execute(
            text(
                'INSERT INTO packets (src_ip, dst_ip, src_port, dst_port, protocol, "timestamp", '
                "flow_duration, tot_fwd_pkts, tot_bwd_pkts, totlen_fwd_pkts, totlen_bwd_pkts) "
                "VALUES (:a, :b, :c, :d, :e, :f, :g, :h, :i, :j, :k)"
            ),
            [dict(zip("abcdefghijk", row, strict=True)) for row in rows],
        )


@pytest.fixture()
def world(tmp_path: Path) -> Iterator[World]:
    sensipro_engine = _engine(tmp_path / "sensipro.db", SENSIPRO_SCHEMA)
    biology_engine = _engine(tmp_path / "biology.db", BIOLOGY_SCHEMA)
    _fill_sensipro(sensipro_engine)
    _fill_biology(biology_engine)
    sensipro = _connection("sensipro", "sensipro", None)
    biology = _connection("biology", "biology", frozenset({"default"}))
    register_engine(sensipro, sensipro_engine)
    register_engine(biology, biology_engine)
    yield World(sensipro, biology, sensipro_engine, biology_engine)
    sensipro_engine.dispose()
    biology_engine.dispose()
