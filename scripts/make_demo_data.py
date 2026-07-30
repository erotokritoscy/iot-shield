"""Generate the demo inputs the dashboard walkthrough uploads.

    uv run python scripts/make_demo_data.py

Every file under ``data/demo/`` used to be hand-authored with no way to rebuild it, which
made "what exactly does this demo prove?" a question only a careful reading of the bytes
could answer. This script is the answer instead: the scenario is declared here, the files
are derived, and changing what the demo shows is an edit in one place.

It is deliberately NOT the evaluation generator (``iotshield.eval.generate``). That one
builds *labelled corpora* for measurement — a world model, per-lane behaviours, renderers,
and an answer key. This builds three small, curated, deliberately obvious files for a
five-minute walkthrough. Curation is the point: a demo input should be short enough to read
on a projector and should trip exactly the detections the presenter is about to explain.

## The scenario

One compromised IP camera (``10.0.0.24``) observed on all three lanes at once, plus two
devices that behave, so the queue has contrast rather than a single row.

    10.0.0.24  camera    telemetry: runs hot (>80)      -> stub_threshold
                         flow:      dials telnet out    -> rules: telnet-outbound-propagation
                         flow:      4.2 MB upload       -> rules: high-upload-volume
                         log:       sshd break-in warn  -> rules: ssh-possible-break-in
                         log:       firmware integrity  -> rules: critical-log-event
    10.0.0.11  gateway   flow:      inbound SSH         -> rules: inbound-remote-admin
    10.0.0.31  sensor    nothing interesting            -> stays silent, correctly

The camera is the point: it is the same device on three lanes, so judging one lane finds a
fragment and judging three finds the whole thing with independent corroboration. The
gateway earns a real but lower-priority alert, which is what makes the ranking visible. The
sensor earns nothing, which is what makes the ranking *credible*.

Identity: flows carry only an IP, so every lane names this camera by its IP. Reconciling
hostname-keyed and IP-keyed identity in general needs the asset-aliasing layer we have
deliberately not built (ADR-0023 Consequences).

Two syslog lines are malformed on purpose. Nothing is silently dropped, so they land in
quarantine with a reason and show up on the Data Health page.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEMO_DIR = REPO_ROOT / "data" / "demo"

T0 = datetime(2026, 7, 30, 12, 0, 0, tzinfo=UTC)

CAMERA = "10.0.0.24"
GATEWAY = "10.0.0.11"
SENSOR = "10.0.0.31"

# TEST-NET-2/3, reserved for documentation, so orientation sees exactly one local endpoint
# and attributes each flow to our device. The normalizer's default home networks are the
# private/link-local ranges, which is what makes 10.0.0.0/8 above "ours" without configuration.
SCAN_TARGETS = [f"198.51.100.{n}" for n in (11, 12, 13, 14, 15, 16)]
UPLINK = "203.0.113.9"
NTP_SERVER = "10.0.0.5"


def at(seconds: int) -> datetime:
    return T0 + timedelta(seconds=seconds)


# --- telemetry: stub-csv ``timestamp,asset_id,metric,value`` -------------------------------


def telemetry() -> str:
    """A camera that runs hot while it is busy scanning, and two devices that do not.

    ``stub_threshold`` compares an absolute value against 80.0, so the fault has to cross
    that line to be found at all.

    The excursion is deliberately SHORT — three samples, ninety seconds, timed to sit inside
    the telnet burst on the flow lane. ``stub_threshold`` is the reference adapter and emits
    one detection per row above the line rather than merging a run (``detect/_runs.py`` is
    what a real detector uses for that), so a long excursion would hand the camera a dozen
    telemetry detections and the alert would read "11 detections" for what a human would
    call four findings. Three keeps the arithmetic on screen checkable by eye, which is the
    only reason a demo input exists.
    """
    # Steps 14-16 (420s, 450s, 480s) — inside the 390-465s telnet burst.
    hot = {14: 84.2, 15: 91.6, 16: 86.0}
    rows: list[str] = []
    for step in range(30):  # 30 samples, 30s apart = 15 minutes
        stamp = at(step * 30).isoformat()
        camera_temp = hot.get(step, 52.0 + (step % 3) * 1.4)
        rows.append(f"{stamp},{CAMERA},Temperature,{camera_temp:.1f}")
        rows.append(f"{stamp},{GATEWAY},Temperature,{41.0 + (step % 4) * 0.5:.1f}")
        rows.append(f"{stamp},{SENSOR},Temperature,{38.0 + (step % 5) * 0.3:.1f}")
    return "\n".join(rows) + "\n"


# --- flow: Zeek conn.log ------------------------------------------------------------------

_ZEEK_HEADER = [
    "#separator \\x09",
    "#set_separator\t,",
    "#empty_field\t(empty)",
    "#unset_field\t-",
    "#path\tconn",
    "#fields\tts\tid.orig_h\tid.orig_p\tid.resp_h\tid.resp_p\tproto\tservice\t"
    "duration\torig_bytes\tresp_bytes\torig_pkts\tresp_pkts\tconn_state",
    "#types\ttime\taddr\tport\taddr\tport\tenum\tstring\t"
    "interval\tcount\tcount\tcount\tcount\tstring",
]


def _conn(
    *,
    seconds: int,
    src: str,
    src_port: int,
    dst: str,
    dst_port: int,
    service: str = "-",
    duration: float = 0.2,
    orig_bytes: int = 340,
    resp_bytes: int = 0,
    orig_pkts: int = 4,
    resp_pkts: int = 0,
    state: str = "S0",
) -> str:
    """One conn.log row. Zeek stamps flows with an epoch float and never quotes a field."""
    ts = at(seconds).timestamp()
    return "\t".join(
        (
            f"{ts:.6f}",
            src,
            str(src_port),
            dst,
            str(dst_port),
            "tcp",
            service,
            f"{duration:.6f}",
            str(orig_bytes),
            str(resp_bytes),
            str(orig_pkts),
            str(resp_pkts),
            state,
        )
    )


def flows() -> str:
    """The camera's compromise, one benign neighbour, and one silent one.

    Byte counts are chosen so each flow trips exactly one rule. The telnet probes carry a
    few hundred bytes so they cannot also trip ``high-upload-volume`` (>10 KB), and the
    gateway's inbound SSH session sends back under that line for the same reason — one
    device, one finding, so a live audience can count the detections against the rules.
    """
    rows = list(_ZEEK_HEADER)

    # Routine chatter first, so the capture does not consist only of the interesting part.
    for index, seconds in enumerate((15, 75, 135)):
        rows.append(
            _conn(
                seconds=seconds,
                src=SENSOR,
                src_port=41000 + index,
                dst=NTP_SERVER,
                dst_port=123,
                service="ntp",
                orig_bytes=76,
                resp_bytes=76,
                resp_pkts=1,
                state="SF",
            )
        )
    rows.append(
        _conn(
            seconds=90,
            src=CAMERA,
            src_port=44120,
            dst=NTP_SERVER,
            dst_port=123,
            service="ntp",
            orig_bytes=76,
            resp_bytes=76,
            resp_pkts=1,
            state="SF",
        )
    )

    # The camera starts dialling telnet: six probes over ninety seconds. Six is well under
    # flow_burst:fan_out's twenty-distinct-peers boundary, which is the whole argument for
    # having signatures beside rate detection -- a camera has no reason to dial port 23 once.
    for index, target in enumerate(SCAN_TARGETS):
        rows.append(
            _conn(
                seconds=390 + index * 15,
                src=CAMERA,
                src_port=45010 + index,
                dst=target,
                dst_port=23,
                orig_bytes=352,
                orig_pkts=5,
            )
        )

    # ...and then ships 4.2 MB out. Also far under flow_burst:exfiltration's 50 MB.
    rows.append(
        _conn(
            seconds=520,
            src=CAMERA,
            src_port=45120,
            dst=UPLINK,
            dst_port=443,
            service="ssl",
            duration=18.4,
            orig_bytes=4_200_000,
            resp_bytes=6_100,
            orig_pkts=3_010,
            resp_pkts=88,
            state="SF",
        )
    )

    # The gateway is administered from outside. Real, worth surfacing, and much less serious
    # than the camera -- which is what gives the queue something to rank.
    rows.append(
        _conn(
            seconds=300,
            src=UPLINK,
            src_port=52344,
            dst=GATEWAY,
            dst_port=22,
            service="ssh",
            duration=96.2,
            orig_bytes=2_400,
            resp_bytes=3_800,
            orig_pkts=42,
            resp_pkts=51,
            state="SF",
        )
    )

    return "\n".join(rows) + "\n"


# --- logs: RFC 5424 syslog ----------------------------------------------------------------

# PRI = facility * 8 + severity, and the normalizer reads severity as PRI & 0x07:
# <38> -> 6 -> INFO, <34> -> 2 -> CRITICAL.
_INFO = 38
_CRITICAL = 34


def _syslog(pri: int, seconds: int, host: str, app: str, pid: int, message: str) -> str:
    stamp = at(seconds).strftime("%Y-%m-%dT%H:%M:%SZ")
    return f"<{pri}>1 {stamp} {host} {app} {pid} - - {message}"


def logs() -> str:
    """Two findings on the camera, ordinary noise around them, two malformed lines.

    The break-in warning is the rule library's own worked example of why signatures earn
    their place: the line is severity INFO and there is exactly one of it, so neither
    "unusual severity" nor "unusual rate" can see it. Only a rule written by somebody who
    knew it mattered will ever catch it.
    """
    rows = [
        _syslog(
            _INFO,
            30,
            GATEWAY,
            "sshd",
            4402,
            "Accepted publickey for admin from 10.0.0.4 port 51990 ssh2",
        ),
        _syslog(_INFO, 120, CAMERA, "streamd", 811, "stream session started for client 10.0.0.4"),
        _syslog(_INFO, 240, SENSOR, "sensord", 640, "calibration check passed"),
        _syslog(
            _INFO,
            432,
            CAMERA,
            "sshd",
            1420,
            "reverse mapping checking getaddrinfo for host-198-51-100-23.example.net "
            "[198.51.100.23] failed - POSSIBLE BREAK-IN ATTEMPT!",
        ),
        # The OLDER RFC 3164 dialect, in the same file as the RFC 5424 lines around it.
        # Real estates mix dialects and the normalizer reads both; the host is the camera's
        # own IP so this does not invent a fourth device nobody can account for.
        f"<38>Jul 30 12:08:02 {CAMERA} watchdog: restarting capture pipeline",
        _syslog(
            _CRITICAL,
            520,
            CAMERA,
            "firmware",
            902,
            "firmware integrity check failed for partition /dev/mtd3",
        ),
        _syslog(_INFO, 600, GATEWAY, "cron", 5511, "session opened for user root"),
        # Genuinely unparseable, in the two shapes real feeds produce them: a truncated
        # write (log rotation caught mid-line) and a line with no PRI at all.
        "<38>1 2026-07-30T12:11:1",
        "restarting capture pipeline after watchdog timeout",
        _syslog(_INFO, 720, SENSOR, "sensord", 640, "heartbeat ok"),
    ]
    return "\n".join(rows) + "\n"


def main() -> int:
    DEMO_DIR.mkdir(parents=True, exist_ok=True)
    written = {
        "camera_telemetry.csv": telemetry(),
        "camera_flow.log": flows(),
        "camera_syslog.log": logs(),
    }
    for name, text in written.items():
        path = DEMO_DIR / name
        # newline="\n": these files are parsed byte-for-byte and a CRLF checkout has bitten
        # this repository before (ADR-0028).
        path.write_text(text, encoding="utf-8", newline="\n")
        print(f"wrote {path.relative_to(REPO_ROOT)}  ({len(text.splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
