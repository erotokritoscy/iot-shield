"""A SYNTHETIC attack on the Biology BACnet network, for presenting the IT charts.

Prints SQL for the Biology project's MySQL ``packets`` table (cicflowmeter flows), to be piped
into its database. Nothing here is real: it is an invented attack, written in Biology's own
format, so that a normal Biology pull (ADR-0042) carries it and the IT overview has something
to show beside Biology's 40 real, quiet BACnet flows.

    # insert (back up first: see MANUAL_TEST.md)
    python3 scripts/biology_fake_attack.py insert \
      | (cd ~/projects/biology && docker compose exec -T db sh -c \
           'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" laravel')
    # remove exactly what insert added
    python3 scripts/biology_fake_attack.py remove | (same pipe)

Every fake row is **marked twice**, so ``remove`` can never touch a real one: it comes from the
attacker address ``192.168.0.66`` (no real Biology flow does) and has ``created_at`` =
``MARKER`` (2000-01-01, before the capture existed). ``insert`` removes any earlier copy first,
so running it twice does not double the attack.

The story, on 2023-07-22 (UTC), the afternoon after Biology's real morning traffic:

    13:00  192.168.0.66 appears, port-scans the controller 192.168.0.140
           -> flow_burst:port_scan
    14:00  it sweeps the BACnet subnet, 30 devices in a minute -> flow_burst:fan_out
    14:30  it tries telnet to six outside hosts (TEST-NET-2) -> rules: telnet-outbound
    15:30  it uploads 64 MB to an outside host (TEST-NET-3) over 10 minutes
           -> flow_burst:exfiltration + rules: high-upload-volume

Outside addresses are the documentation ranges (RFC 5737), which route nowhere.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from typing import Tuple  # noqa: UP035 - runs on the host, whose python3 may be 3.8

ATTACKER = "192.168.0.66"
CONTROLLER = "192.168.0.140"
MARKER = "2000-01-01 00:00:00"
DAY = datetime(2023, 7, 22)
TCP, UDP = 6, 17

COLUMNS = (
    "src_ip",
    "dst_ip",
    "src_port",
    "dst_port",
    "protocol",
    "timestamp",
    "flow_duration",
    "tot_fwd_pkts",
    "tot_bwd_pkts",
    "totlen_fwd_pkts",
    "totlen_bwd_pkts",
    "created_at",
    "updated_at",
)

Flow = Tuple[str, int, int, int, datetime, float, int, int, int, int]  # noqa: UP006
"""dst_ip, src_port, dst_port, protocol, time, duration (µs), fwd/bwd packets, fwd/bwd bytes."""


def flows() -> list[Flow]:
    out: list[Flow] = []
    # 13:00 — a SYN scan of 20 ports on the controller, one every 2 s.
    scan_ports = [
        21,
        22,
        23,
        25,
        53,
        80,
        102,
        110,
        139,
        143,
        443,
        445,
        502,
        1911,
        3389,
        4911,
        5900,
        8080,
        20000,
        44818,
    ]
    for index, port in enumerate(scan_ports):
        out.append(
            (
                CONTROLLER,
                40000 + index,
                port,
                TCP,
                DAY.replace(hour=13) + timedelta(seconds=2 * index),
                900.0,
                1,
                1,
                60,
                54,
            )
        )
    # 14:00 — a BACnet who-is sweep of 30 devices in under a minute.
    for index in range(30):
        peer = f"192.168.0.{101 + index}"
        out.append(
            (
                peer,
                47808,
                47808,
                UDP,
                DAY.replace(hour=14) + timedelta(seconds=2 * index),
                1500.0,
                1,
                1,
                24,
                24,
            )
        )
    # 14:30 — telnet attempts to six outside hosts, ten seconds apart.
    for index in range(6):
        peer = f"198.51.100.{10 + index}"
        out.append(
            (
                peer,
                41000 + index,
                23,
                TCP,
                DAY.replace(hour=14, minute=30) + timedelta(seconds=10 * index),
                3_000_000.0,
                3,
                0,
                180,
                0,
            )
        )
    # 15:30 — 4 x 16 MB to one outside host over 10 minutes (64 MB, past the 50 MB line).
    for index in range(4):
        out.append(
            (
                "203.0.113.50",
                42000 + index,
                443,
                TCP,
                DAY.replace(hour=15, minute=30) + timedelta(minutes=3 * index),
                120_000_000.0,
                11000,
                4000,
                16_000_000,
                240_000,
            )
        )
    return out


def _remove_sql() -> str:
    return f"DELETE FROM packets WHERE src_ip = '{ATTACKER}' AND created_at = '{MARKER}';"


def insert_sql() -> str:
    rows = []
    for dst, sport, dport, proto, when, duration, fwd, bwd, fwd_bytes, bwd_bytes in flows():
        values = (
            f"'{ATTACKER}', '{dst}', {sport}, {dport}, {proto}, '{when:%Y-%m-%d %H:%M:%S}', "
            f"{duration}, {fwd}, {bwd}, {fwd_bytes}, {bwd_bytes}, '{MARKER}', '{MARKER}'"
        )
        rows.append(f"({values})")
    return (
        f"{_remove_sql()}\n"
        f"INSERT INTO packets ({', '.join(COLUMNS)}) VALUES\n  " + ",\n  ".join(rows) + ";\n"
        "SELECT COUNT(*) AS fake_flows FROM packets "
        f"WHERE src_ip = '{ATTACKER}' AND created_at = '{MARKER}';\n"
    )


def remove_sql() -> str:
    return (
        f"{_remove_sql()}\n"
        f"SELECT COUNT(*) AS fake_flows_left FROM packets WHERE src_ip = '{ATTACKER}';\n"
    )


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"insert", "remove"}:
        print("usage: biology_fake_attack.py insert|remove", file=sys.stderr)
        return 2
    print(insert_sql() if argv[1] == "insert" else remove_sql())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
