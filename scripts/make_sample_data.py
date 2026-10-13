"""Generate the sample library the Compose wizard offers, and its scenarios.

    uv run python scripts/make_sample_data.py

Writes small, readable input files — each built to trip exactly one documented finding, or
deliberately none — plus ``manifest.json``, which names every file, the normalizer that reads
it, what it should produce, and the one-click **scenarios** that prefill the wizard. Two
copies, byte for byte:

- ``web/public/samples/``  served by the dashboard at ``/samples/`` (a static file, so the
  wizard needs no new API endpoint — the dashboard still computes nothing, ADR-0031);
- ``data/samples/``        for the CLI and compose runner, beside ``data/demo/``.

``tests/test_sample_data.py`` holds the two to identity and **runs every scenario through the
real pipeline**, asserting the counts the manifest promises — so a description in the wizard
cannot drift from what the bytes actually do.

Like ``make_demo_data.py`` (whose camera walkthrough is reused here verbatim, as the first
scenario) this is curation, not the evaluation generator: no labels, no world model, just
inputs short enough to read and obvious enough to explain.
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIRS = (REPO_ROOT / "web" / "public" / "samples", REPO_ROOT / "data" / "samples")

sys.path.insert(0, str(Path(__file__).resolve().parent))
import make_demo_data as demo  # noqa: E402  (a sibling script, not a package)

T0 = datetime(2026, 8, 3, 9, 0, 0, tzinfo=UTC)


def at(seconds: float) -> datetime:
    return T0 + timedelta(seconds=seconds)


# --- telemetry ------------------------------------------------------------------------------


def pump_telemetry() -> str:
    """A pump that overheats for four samples; a compressor and a valve that do not.

    ``stub_threshold`` flags every row above 80.0 (it does not merge runs), so four hot
    samples are exactly four detections on one device.
    """
    hot = {20: 82.4, 21: 88.9, 22: 93.1, 23: 85.7}
    rows: list[str] = []
    for step in range(40):  # 40 samples, 30 s apart = 20 minutes
        stamp = at(step * 30).isoformat()
        rows.append(f"{stamp},pump-01,Temperature,{hot.get(step, 61.0 + (step % 4) * 0.8):.1f}")
        rows.append(f"{stamp},compressor-02,Temperature,{55.0 + (step % 5) * 0.6:.1f}")
        rows.append(f"{stamp},valve-03,Pressure,{4.2 + (step % 3) * 0.1:.2f}")
    return "\n".join(rows) + "\n"


def quiet_telemetry() -> str:
    """Three devices well inside their normal range for twenty minutes: nothing to find."""
    rows: list[str] = []
    for step in range(40):
        stamp = at(step * 30).isoformat()
        rows.append(f"{stamp},pump-01,Temperature,{60.0 + (step % 4) * 0.7:.1f}")
        rows.append(f"{stamp},compressor-02,Temperature,{54.0 + (step % 5) * 0.5:.1f}")
        rows.append(f"{stamp},valve-03,Pressure,{4.1 + (step % 3) * 0.1:.2f}")
    return "\n".join(rows) + "\n"


# --- flow: Zeek conn.log --------------------------------------------------------------------

WORKSTATION = "10.0.0.42"
FILE_SERVER = "10.0.0.7"
DNS = "10.0.0.2"
NTP = "10.0.0.5"
WEB = "203.0.113.80"  # TEST-NET-3: an outside web server


def _conn(seconds: float, src: str, sport: int, dst: str, dport: int, **kwargs: Any) -> str:
    """One conn.log row; the field layout is the demo generator's, at this module's clock."""
    row = demo._conn(seconds=0, src=src, src_port=sport, dst=dst, dst_port=dport, **kwargs)
    fields = row.split("\t")
    fields[0] = f"{at(seconds).timestamp():.6f}"
    return "\t".join(fields)


def _routine_flows(rows: list[str], hosts: tuple[str, ...]) -> None:
    """DNS, NTP and small HTTPS for each host, every two minutes. Matches no rule or preset."""
    for minute in range(0, 20, 2):
        base = minute * 60
        for index, host in enumerate(hosts):
            offset = base + index * 7
            rows.append(
                _conn(offset, host, 50000 + minute * 10 + index, DNS, 53, service="dns",
                      orig_bytes=64, resp_bytes=128, resp_pkts=1, state="SF")
            )  # fmt: skip
            rows.append(
                _conn(offset + 3, host, 51000 + minute * 10 + index, NTP, 123, service="ntp",
                      orig_bytes=76, resp_bytes=76, resp_pkts=1, state="SF")
            )  # fmt: skip
            rows.append(
                _conn(offset + 5, host, 52000 + minute * 10 + index, WEB, 443, service="ssl",
                      duration=1.2, orig_bytes=2_100, resp_bytes=18_000, orig_pkts=12,
                      resp_pkts=20, state="SF")
            )  # fmt: skip


def portscan_flow() -> str:
    """A workstation probes 24 ports on the file server inside 48 seconds, amid routine traffic.

    ``flow_burst:port_scan`` counts distinct destination ports a device opens in one minute
    and fires above 15. Ports avoid 22/23/139/445 so no *signature* in the rule library fires
    as well — this sample is about the rate detector.
    """
    rows = list(demo._ZEEK_HEADER)
    _routine_flows(rows, (WORKSTATION, FILE_SERVER))
    ports = [21, 25, 53, 80, 110, 111, 135, 143, 389, 443, 465, 587,
             631, 993, 995, 1433, 1521, 2049, 3306, 3389, 5432, 5900, 6379, 8080]  # fmt: skip
    for index, port in enumerate(ports):
        rows.append(
            _conn(600 + index * 2, WORKSTATION, 40000 + index, FILE_SERVER, port,
                  duration=0.01, orig_bytes=0, orig_pkts=1, state="REJ")
        )  # fmt: skip
    return "\n".join(rows) + "\n"


def quiet_flow() -> str:
    """Twenty minutes of routine DNS, NTP and HTTPS from three devices: nothing to find."""
    rows = list(demo._ZEEK_HEADER)
    _routine_flows(rows, (WORKSTATION, FILE_SERVER, "10.0.0.31"))
    return "\n".join(rows) + "\n"


# --- logs: RFC 5424 syslog ------------------------------------------------------------------

_INFO = 38  # PRI: facility 4 (auth) * 8 + severity 6 (info)
GATEWAY = "10.0.0.11"
ATTACKER = "203.0.113.50"


def _syslog(seconds: float, host: str, app: str, pid: int, message: str, pri: int = _INFO) -> str:
    stamp = at(seconds).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"<{pri}>1 {stamp} {host} {app} {pid} - - {message}"


def _routine_logs() -> list[str]:
    return [
        _syslog(30, GATEWAY, "sshd", 4402,
                "Accepted publickey for admin from 10.0.0.4 port 51990 ssh2"),
        _syslog(90, "10.0.0.31", "sensord", 640, "calibration check passed"),
        _syslog(240, GATEWAY, "cron", 5511, "session opened for user root"),
        _syslog(400, "10.0.0.31", "sensord", 640, "heartbeat ok"),
        _syslog(700, GATEWAY, "ntpd", 301, "synchronized to 10.0.0.5, stratum 2"),
        _syslog(900, "10.0.0.31", "sensord", 640, "heartbeat ok"),
    ]  # fmt: skip


def bruteforce_syslog() -> str:
    """Eight failed SSH passwords for root from one outside address in two minutes.

    ``log_burst:auth_failure`` fires on 5+ authentication failures from one device inside
    five minutes. sshd logs these at INFO, which is why the preset has no severity floor.
    """
    rows = _routine_logs()
    for index in range(8):
        rows.append(
            _syslog(300 + index * 15, GATEWAY, "sshd", 4500 + index,
                    f"Failed password for root from {ATTACKER} port {40100 + index} ssh2")
        )  # fmt: skip
    return "\n".join(sorted(rows, key=lambda line: line.split(" ")[1])) + "\n"


def quiet_syslog() -> str:
    """Routine logins, cron and heartbeats: nothing to find."""
    return "\n".join(_routine_logs()) + "\n"


def broken_syslog() -> str:
    """Six good lines and four broken ones, in the shapes real feeds produce.

    Nothing is silently dropped: the four land in quarantine with a reason, which is what the
    Data health page is for.
    """
    good = _routine_logs()
    broken = [
        "<38>1 2026-08-03T09:11:1",  # a write truncated mid-timestamp by log rotation
        "restarting capture pipeline after watchdog timeout",  # no PRI header at all
        "<38>1 2026-13-45T09:12:00Z gw sshd 1 - - an impossible date",
        "<38>1 not-a-timestamp gw app 1 - - unreadable time",
    ]
    rows = good[:3] + broken[:2] + good[3:] + broken[2:]
    return "\n".join(rows) + "\n"


# --- the manifest ---------------------------------------------------------------------------

SAMPLES: list[dict[str, Any]] = [
    {
        "id": "camera_flow",
        "file": "camera_flow.log",
        "title": "Compromised camera — network flows",
        "description": "Zeek conn.log: the camera dials telnet to six hosts and uploads 4.2 MB.",
        "normalizer": "flow",
        "source_format": "zeek-conn",
        "render": demo.flows,
    },
    {
        "id": "camera_syslog",
        "file": "camera_syslog.log",
        "title": "Compromised camera — syslog",
        "description": "A possible SSH break-in and a failed firmware integrity check; "
        "two broken lines.",
        "normalizer": "syslog",
        "source_format": "syslog",
        "render": demo.logs,
    },
    {
        "id": "camera_telemetry",
        "file": "camera_telemetry.csv",
        "title": "Compromised camera — telemetry",
        "description": "The camera runs above 80 °C for three samples while it scans.",
        "normalizer": "stub",
        "source_format": "stub-csv",
        "render": demo.telemetry,
    },
    {
        "id": "portscan_flow",
        "file": "portscan_flow.log",
        "title": "Port scan — network flows",
        "description": "A workstation probes 24 ports on the file server in 48 seconds.",
        "normalizer": "flow",
        "source_format": "zeek-conn",
        "render": portscan_flow,
    },
    {
        "id": "bruteforce_syslog",
        "file": "bruteforce_syslog.log",
        "title": "SSH brute force — syslog",
        "description": "Eight failed root passwords from one outside address in two minutes.",
        "normalizer": "syslog",
        "source_format": "syslog",
        "render": bruteforce_syslog,
    },
    {
        "id": "pump_telemetry",
        "file": "pump_telemetry.csv",
        "title": "Overheating pump — telemetry",
        "description": "pump-01 climbs above 80 °C for four samples; "
        "two other devices stay normal.",
        "normalizer": "stub",
        "source_format": "stub-csv",
        "render": pump_telemetry,
    },
    {
        "id": "quiet_flow",
        "file": "quiet_flow.log",
        "title": "Quiet network — flows",
        "description": "Routine DNS, NTP and HTTPS only.",
        "normalizer": "flow",
        "source_format": "zeek-conn",
        "render": quiet_flow,
    },
    {
        "id": "quiet_syslog",
        "file": "quiet_syslog.log",
        "title": "Quiet network — syslog",
        "description": "Routine logins, cron and heartbeats only.",
        "normalizer": "syslog",
        "source_format": "syslog",
        "render": quiet_syslog,
    },
    {
        "id": "quiet_telemetry",
        "file": "quiet_telemetry.csv",
        "title": "Quiet network — telemetry",
        "description": "Three devices well inside their normal range.",
        "normalizer": "stub",
        "source_format": "stub-csv",
        "render": quiet_telemetry,
    },
    {
        "id": "broken_syslog",
        "file": "broken_syslog.log",
        "title": "Broken input — syslog",
        "description": "Six good lines and four malformed ones, to see quarantine at work.",
        "normalizer": "syslog",
        "source_format": "syslog",
        "render": broken_syslog,
    },
]

_WEIGHTED = {"name": "weighted", "params": {"bucket_minutes": 5}}

# `expected` is what a run of exactly this scenario produces. It is not prose: the test suite
# runs every scenario and asserts these numbers, so the wizard's promise is checked.
SCENARIOS: list[dict[str, Any]] = [
    {
        "id": "camera",
        "title": "Compromised camera (all three lanes)",
        "description": "The walkthrough: one IP camera seen on flows, logs and telemetry at "
        "once. Signatures and a threshold corroborate each other; the camera ranks CRITICAL.",
        "samples": ["camera_flow", "camera_syslog", "camera_telemetry"],
        "detectors": [
            {"name": "rules", "params": {"lane": "flow"}},
            {"name": "rules", "params": {"lane": "log"}},
            {"name": "stub_threshold", "params": {}},
        ],
        "alert_engine": _WEIGHTED,
        "expected": {"events": 110, "malformed": 2, "detections": 8, "alerts": 2},
    },
    {
        "id": "port_scan",
        "title": "Port scan",
        "description": "Rate detection on the flow lane: a workstation probing many ports on "
        "one host. flow_burst's port_scan preset fires; Network Service Discovery (T1046).",
        "samples": ["portscan_flow"],
        "detectors": [{"name": "flow_burst", "params": {"preset": "port_scan"}}],
        "alert_engine": _WEIGHTED,
        "expected": {"events": 84, "malformed": 0, "detections": 1, "alerts": 1},
    },
    {
        "id": "brute_force",
        "title": "SSH brute force",
        "description": "Rate detection on the log lane: repeated failed passwords. log_burst's "
        "auth_failure preset fires; Brute Force (T1110).",
        "samples": ["bruteforce_syslog"],
        "detectors": [{"name": "log_burst", "params": {"preset": "auth_failure"}}],
        "alert_engine": _WEIGHTED,
        "expected": {"events": 14, "malformed": 0, "detections": 1, "alerts": 1},
    },
    {
        "id": "overheating_pump",
        "title": "Overheating pump",
        "description": "A telemetry threshold: four readings above 80 °C on one pump become "
        "four detections and one alert.",
        "samples": ["pump_telemetry"],
        "detectors": [{"name": "stub_threshold", "params": {}}],
        "alert_engine": _WEIGHTED,
        "expected": {"events": 120, "malformed": 0, "detections": 4, "alerts": 1},
    },
    {
        "id": "quiet",
        "title": "Quiet network (control)",
        "description": "Ordinary traffic on all three lanes with every detector switched on. "
        "The right answer is nothing: a detector that fires here is a false alarm.",
        "samples": ["quiet_flow", "quiet_syslog", "quiet_telemetry"],
        "detectors": [
            {"name": "rules", "params": {"lane": "flow"}},
            {"name": "rules", "params": {"lane": "log"}},
            {"name": "flow_burst", "params": {"preset": "port_scan"}},
            {"name": "log_burst", "params": {"preset": "auth_failure"}},
            {"name": "stub_threshold", "params": {}},
        ],
        "alert_engine": _WEIGHTED,
        "expected": {"events": 216, "malformed": 0, "detections": 0, "alerts": 0},
    },
    {
        "id": "broken_input",
        "title": "Broken input",
        "description": "Four malformed syslog lines are quarantined with a reason rather than "
        "dropped. Open Data health after the run to see them.",
        "samples": ["broken_syslog"],
        "detectors": [{"name": "rules", "params": {"lane": "log"}}],
        "alert_engine": _WEIGHTED,
        "expected": {"events": 6, "malformed": 4, "detections": 0, "alerts": 0},
    },
]


def manifest() -> dict[str, Any]:
    samples = [{k: v for k, v in s.items() if k != "render"} for s in SAMPLES]
    return {
        "_comment": "Generated by scripts/make_sample_data.py; do not edit by hand.",
        "samples": samples,
        "scenarios": SCENARIOS,
    }


def files() -> dict[str, str]:
    written = {sample["file"]: sample["render"]() for sample in SAMPLES}
    written["manifest.json"] = json.dumps(manifest(), indent=2, ensure_ascii=False) + "\n"
    return written


def main() -> int:
    for out in OUT_DIRS:
        out.mkdir(parents=True, exist_ok=True)
        for name, text in files().items():
            # newline="\n": parsed byte for byte, and a CRLF checkout has bitten this repo.
            (out / name).write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {len(SAMPLES)} samples + manifest.json to {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
