"""Syslog normalizer: RFC 3164 (BSD) and RFC 5424 lines -> LOG-lane events.

Our first integration of an external student module (team4-log-normalizer). It
is a deliberate *re-implementation*, not a wrapper. team4's core drops
unrecognized lines, fabricates ``datetime.now()`` for unreadable dates, emits
timezone-naive timestamps, and mangles the original text — behaviours our port
forbids and that happen *inside* its solution, upstream of any seam an adapter
could sit on (ADR-0011). When a module's output has already destroyed the
information our contract needs, no adapter can recover it, so we take its ideas
and rewrite them against our contract.

Borrowed from team4 (``reference/team4-log-normalizer/log-normalizer/core``):
  - the two syslog line shapes (``parsers/syslog.py``: standard + RFC 5424),
  - the severity taxonomy (``schema.py`` SEVERITY_MAP numeric levels and
    ``parsers/syslog.py`` message-keyword heuristics).
Rewritten to our port's laws (enforced once in ``normalize_linewise``):
  - an unrecognized line or an unreadable date becomes a MalformedRecord — never
    a silent drop, never a fabricated "now" (failure is data, ADR-0004),
  - RFC 3164 carries no time zone, so we attribute UTC (D2/ADR-0011); an RFC 5424
    timestamp keeps its offset and the contract normalizes it to UTC,
  - the original line is preserved verbatim as ``raw``.

A syslog record has no numeric reading, so events land in the LOG lane with
value/metric unset; the telemetry threshold detector correctly ignores them.
Successfully parsed events carry the fine-grained dialect as ``source_format``
(rejects carry the incoming ``syslog``, since a line that did not parse has no
known dialect). Known limitation (matching team4): RFC 5424 STRUCTURED-DATA
containing spaces folds into the message; the common nil form ("-") parses cleanly.
"""

import re
from datetime import UTC, datetime
from typing import Any

from iotshield.capabilities import Capability, FormatToken, LaneToken
from iotshield.contracts import Lane, Severity
from iotshield.normalize._linewise import LineParseError, normalize_linewise
from iotshield.normalize.port import NormalizeResult

SOURCE_FORMAT = "syslog"

# Consumes syslog text; every event it emits is log-lane. Declared for the guard.
CAPABILITY = Capability.of(consumes={FormatToken(SOURCE_FORMAT)}, produces={LaneToken(Lane.LOG)})

# Zone attributed to timestamps that arrive without one (D2, ADR-0011).
_ASSUMED_TZ = UTC

# Syslog PRI severity code (PRI & 0x07) -> our Severity. Borrowed from team4
# schema.py SEVERITY_MAP (numeric levels).
_PRI_SEVERITY = {
    0: Severity.CRITICAL,
    1: Severity.CRITICAL,
    2: Severity.CRITICAL,
    3: Severity.ERROR,
    4: Severity.WARNING,
    5: Severity.INFO,
    6: Severity.INFO,
    7: Severity.DEBUG,
}

# RFC 3164 lines frequently omit the PRI; fall back to message keywords.
# Borrowed from team4 parsers/syslog.py SEVERITY_KEYWORDS (order matters: the
# first match wins, so the most severe keywords come first).
_SEVERITY_KEYWORDS = {
    "out of memory": Severity.CRITICAL,
    "oom": Severity.CRITICAL,
    "emerg": Severity.CRITICAL,
    "alert": Severity.CRITICAL,
    "crit": Severity.CRITICAL,
    "error": Severity.ERROR,
    "err": Severity.ERROR,
    "failed": Severity.ERROR,
    "fail": Severity.ERROR,
    "warning": Severity.WARNING,
    "warn": Severity.WARNING,
    "denied": Severity.WARNING,
    "refused": Severity.WARNING,
    "notice": Severity.INFO,
    "info": Severity.INFO,
    "debug": Severity.DEBUG,
}

# RFC 5424: <PRI>VER SP TIMESTAMP SP HOST SP APP SP PROCID SP MSGID SP SD SP MSG.
_RFC5424 = re.compile(
    r"<(?P<pri>\d+)>(?P<ver>\d+)\s+(?P<ts>\S+)\s+(?P<host>\S+)\s+(?P<app>\S+)\s+"
    r"(?P<pid>\S+)\s+(?P<msgid>\S+)\s+(?P<sd>\S+)\s+(?P<msg>.+)"
)
# RFC 3164 (BSD): optional <PRI>, then "Mmm dd hh:mm:ss host tag[pid]: msg".
_RFC3164 = re.compile(
    r"(?:<(?P<pri>\d+)>)?(?P<month>[A-Z][a-z]{2})\s+(?P<day>\d{1,2})\s+"
    r"(?P<time>\d{2}:\d{2}:\d{2})\s+(?P<host>\S+)\s+"
    r"(?P<tag>[^\[:\s]+)(?:\[(?P<pid>\d+)\])?:\s*(?P<msg>.+)"
)


def _assume_utc(dt: datetime) -> datetime:
    # A zone-less timestamp is attributed UTC (D2); one that already carries an
    # offset is left for the contract to normalize.
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=_ASSUMED_TZ)


def _guess_severity(message: str) -> Severity:
    lowered = message.lower()
    for keyword, severity in _SEVERITY_KEYWORDS.items():
        if keyword in lowered:
            return severity
    return Severity.INFO


def _parse_rfc5424(m: re.Match[str]) -> dict[str, Any]:
    ts_text = m["ts"]
    if ts_text == "-":
        raise LineParseError("RFC5424 timestamp is nil ('-')")
    try:
        # fromisoformat accepts the offset; normalize the 'Z' shorthand for < 3.11 parity.
        timestamp = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LineParseError(f"unparseable RFC5424 timestamp: {ts_text!r}") from exc
    return {
        "timestamp": _assume_utc(timestamp),
        "asset_id": m["host"],
        "source_type": Lane.LOG,
        "source_format": "syslog-rfc5424",
        "severity": _PRI_SEVERITY[int(m["pri"]) & 0x07],
        "message": m["msg"],
    }


def _parse_rfc3164(m: re.Match[str], *, assumed_year: int) -> dict[str, Any]:
    # RFC 3164 omits the year entirely; the run interprets it as the current year.
    stamp = f"{assumed_year} {m['month']} {m['day']} {m['time']}"
    try:
        timestamp = datetime.strptime(stamp, "%Y %b %d %H:%M:%S")
    except ValueError as exc:
        raise LineParseError(f"unparseable RFC3164 timestamp: {stamp!r}") from exc
    pri = m["pri"]
    severity = _PRI_SEVERITY[int(pri) & 0x07] if pri is not None else _guess_severity(m["msg"])
    return {
        "timestamp": _assume_utc(timestamp),
        "asset_id": m["host"],
        "source_type": Lane.LOG,
        "source_format": "syslog-rfc3164",
        "severity": severity,
        "message": f"[{m['tag']}] {m['msg']}",
    }


def _parse_line(line: str, *, assumed_year: int) -> dict[str, Any]:
    m = _RFC5424.match(line)
    if m is not None:
        return _parse_rfc5424(m)
    m = _RFC3164.match(line)
    if m is not None:
        return _parse_rfc3164(m, assumed_year=assumed_year)
    raise LineParseError("unrecognized syslog line (neither RFC3164 nor RFC5424)")


class SyslogNormalizer:
    def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult:
        # RFC 3164 omits the year; resolve every bare date to the current year, once.
        assumed_year = datetime.now(UTC).year
        return normalize_linewise(
            text,
            source_format=source_format,
            expected_format=SOURCE_FORMAT,
            origin=origin,
            parse_line=lambda line: _parse_line(line, assumed_year=assumed_year),
        )
