"""Flow normalizer: network flow records -> FLOW-lane events (ADR-0023).

A *flow* is not a packet — it is the summary of one conversation: every packet sharing a
5-tuple (src ip, src port, dst ip, dst port, protocol) within a window, collapsed into a
single row of counters. It is what routers and network sensors actually export, because
full packet capture is too large, too privacy-hostile, and usually unavailable.

One adapter, several **dialects** (ADR-0023 D4)
-----------------------------------------------
Flow text formats are many (Zeek, nfdump/NetFlow CSV exports, CICFlowMeter) and differ
almost entirely in *column names and units* — every one of them carries the same 5-tuple
plus counters. A normalizer per format would triplicate the orientation rule, the reject
handling and the event construction in order to vary a lookup table. So a dialect here is
declarative data: which source column feeds which canonical field, how its timestamps are
written, what it uses for "unset". Adding NetFlow or CICFlowMeter support later is a
``DIALECTS`` entry, not a new module — the same shape ``log_burst``'s presets established.

The dialect is chosen by ``source_format``, which needed no new machinery: the guard
already compares a source's format against a set of accepted ``FormatToken``s. This is the
first normalizer to accept more than one, which also lights up the orchestration UI's
never-before-exercised multi-format branch.

Two dialects ship:

- ``zeek-conn`` — Zeek's ``conn.log``: the de-facto standard in network security
  monitoring and the format a real deployment most plausibly already has. TSV with
  ``#``-prefixed directives, of which ``#fields`` names the columns.
- ``flow-csv`` — a small canonical header-driven CSV, ISO-8601 timestamps. The FLOW
  lane's equivalent of ``stub-csv``: the dumb, permanent reference dialect the port suite
  runs against, and something any exporter can be coaxed into producing.

Provenance (rewrite, not wrap — golden rule 3). team1-lognorm has the only flow parsing in
the reference set: ``logNorm/iotlog/parsers/zeek_parser.py`` (the ``#separator``/``#fields``
directive handling, borrowed here) and ``unsw_nb15_parser.py`` (the field-aliasing idea).
Both flatten a flow into ``{device_id, type, value}`` and discard the peer endpoint — which
is precisely the information a flow detector needs — so their ideas are rewritten against
our contract rather than wrapped.

Header lines are schema, not data
---------------------------------
The Normalizer port requires that every non-blank line become events or a
``MalformedRecord``. A ``#fields`` directive or a CSV header row is neither: it describes
the records rather than being one, the same category as the blank lines the port already
skips ("no content is not data"). Data arriving *before* any header is a genuine reject —
we refuse to guess column order.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError

from iotshield.capabilities import Capability, FormatToken, LaneToken
from iotshield.contracts import Lane, MalformedRecord, NormalizedEvent
from iotshield.flow import (
    DEFAULT_NETWORKS,
    METADATA_KEY,
    METRIC,
    FlowOrientationError,
    Network,
    orient,
    parse_networks,
)
from iotshield.normalize.port import NormalizeResult

# Canonical field names a dialect maps its columns onto. Required: without an origin, a
# destination, a protocol and a time there is no flow to speak of.
REQUIRED_FIELDS = ("ts", "src_ip", "dst_ip", "protocol")


def _parse_epoch(text: str) -> datetime:
    """Zeek writes ``ts`` as epoch seconds with microsecond precision."""
    return datetime.fromtimestamp(float(text), tz=UTC)


def _parse_iso(text: str) -> datetime:
    # 'Z' is not accepted by fromisoformat before 3.11's relaxation; normalize it so the
    # dialect behaves the same on either. A naive result is rejected by the contract.
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Dialect:
    """One flow text format, described rather than coded.

    ``columns`` maps a canonical field name to the column heading the format uses, so the
    whole difference between two flow formats is usually this mapping plus a timestamp
    convention.
    """

    name: str
    columns: Mapping[str, str]
    parse_timestamp: Callable[[str], datetime]
    delimiter: str = ","
    comment_prefix: str | None = None
    fields_directive: str | None = None
    missing: tuple[str, ...] = ("", "-")
    header_is_first_row: bool = True

    def is_missing(self, value: str) -> bool:
        return value.strip() in self.missing


ZEEK_CONN = Dialect(
    name="zeek-conn",
    # The standard conn.log column set. Zeek names the two endpoints "orig"/"resp"
    # (who opened the connection), which is exactly the origin/destination distinction
    # orientation needs.
    columns={
        "ts": "ts",
        "src_ip": "id.orig_h",
        "src_port": "id.orig_p",
        "dst_ip": "id.resp_h",
        "dst_port": "id.resp_p",
        "protocol": "proto",
        "service": "service",
        "duration": "duration",
        "src_bytes": "orig_bytes",
        "dst_bytes": "resp_bytes",
        "src_packets": "orig_pkts",
        "dst_packets": "resp_pkts",
        "state": "conn_state",
    },
    parse_timestamp=_parse_epoch,
    delimiter="\t",
    comment_prefix="#",
    fields_directive="#fields",
    header_is_first_row=False,
)

FLOW_CSV = Dialect(
    name="flow-csv",
    columns={
        "ts": "start",
        "src_ip": "src_ip",
        "src_port": "src_port",
        "dst_ip": "dst_ip",
        "dst_port": "dst_port",
        "protocol": "proto",
        "service": "service",
        "duration": "duration",
        "src_bytes": "src_bytes",
        "dst_bytes": "dst_bytes",
        "src_packets": "src_packets",
        "dst_packets": "dst_packets",
        "state": "state",
    },
    parse_timestamp=_parse_iso,
)

DIALECTS: dict[str, Dialect] = {ZEEK_CONN.name: ZEEK_CONN, FLOW_CSV.name: FLOW_CSV}

SOURCE_FORMATS = tuple(DIALECTS)

# Consumes every dialect's format; every event it emits is flow-lane. The first normalizer
# to declare more than one format token — the guard and the UI already supported it.
CAPABILITY = Capability.of(
    consumes={FormatToken(name) for name in SOURCE_FORMATS},
    produces={LaneToken(Lane.FLOW)},
)


class FlowParseError(Exception):
    """Reject one flow row; the message becomes the MalformedRecord's reason."""


@dataclass
class _Reader:
    """Per-run parse state: the column layout, once a header has been seen."""

    dialect: Dialect
    index_of: dict[str, int] = field(default_factory=dict)
    header_seen: bool = False
    """Whether a header line has been consumed — tracked separately from ``ready``.

    An *unusable* header (one missing required columns) is still a header: without this
    flag the next line would be read as a replacement header, so a file with a bad header
    row would silently parse its first data row as the schema.
    """

    @property
    def ready(self) -> bool:
        return bool(self.index_of)

    def take_header(self, headings: Sequence[str]) -> None:
        """Bind canonical fields to column positions, ignoring columns we do not use."""
        positions = {name.strip(): index for index, name in enumerate(headings)}
        self.index_of = {
            canonical: positions[column]
            for canonical, column in self.dialect.columns.items()
            if column in positions
        }

    def missing_required(self) -> list[str]:
        return [name for name in REQUIRED_FIELDS if name not in self.index_of]

    def value(self, parts: Sequence[str], name: str) -> str | None:
        """The raw text of one canonical field, or None when absent/unset."""
        index = self.index_of.get(name)
        if index is None or index >= len(parts):
            return None
        text = parts[index]
        return None if self.dialect.is_missing(text) else text.strip()


def _number(reader: _Reader, parts: Sequence[str], name: str, *, default: int = 0) -> int:
    """A counter, defaulting when unset — a flow with no bytes recorded is normal.

    A rejected connection legitimately has ``orig_bytes`` unset; treating that as a parse
    failure would quarantine exactly the scan traffic we most want to see.
    """
    text = reader.value(parts, name)
    if text is None:
        return default
    try:
        # Some exporters write counters as floats ("1024.0"); int(float(x)) accepts both.
        return int(float(text))
    except ValueError as exc:
        raise FlowParseError(f"{name} is not a number: {text!r}") from exc


def _port(reader: _Reader, parts: Sequence[str], name: str) -> int | None:
    text = reader.value(parts, name)
    if text is None:
        return None  # ICMP and other portless protocols
    try:
        return int(text)
    except ValueError as exc:
        raise FlowParseError(f"{name} is not a port number: {text!r}") from exc


def _required(reader: _Reader, parts: Sequence[str], name: str) -> str:
    text = reader.value(parts, name)
    if text is None:
        raise FlowParseError(f"{name} is missing")
    return text


def _parse_row(reader: _Reader, parts: Sequence[str], home: Sequence[Network]) -> dict[str, Any]:
    """One data row -> the NormalizedEvent field mapping (all but ``raw``)."""
    dialect = reader.dialect
    stamp = _required(reader, parts, "ts")
    try:
        timestamp = dialect.parse_timestamp(stamp)
    except (ValueError, OSError, OverflowError) as exc:
        raise FlowParseError(f"unparseable timestamp: {stamp!r}") from exc

    duration_text = reader.value(parts, "duration")
    try:
        duration = float(duration_text) if duration_text is not None else 0.0
    except ValueError as exc:
        raise FlowParseError(f"duration is not a number: {duration_text!r}") from exc

    # Orientation is what turns two endpoints into (asset, peer) — see flow.orient. Its
    # failure is a data-quality fact about this row, so it becomes a reject with the reason.
    try:
        record = orient(
            src_ip=_required(reader, parts, "src_ip"),
            dst_ip=_required(reader, parts, "dst_ip"),
            protocol=_required(reader, parts, "protocol"),
            src_port=_port(reader, parts, "src_port"),
            dst_port=_port(reader, parts, "dst_port"),
            duration_s=duration,
            src_bytes=_number(reader, parts, "src_bytes"),
            dst_bytes=_number(reader, parts, "dst_bytes"),
            src_packets=_number(reader, parts, "src_packets"),
            dst_packets=_number(reader, parts, "dst_packets"),
            state=reader.value(parts, "state"),
            service=reader.value(parts, "service"),
            home_networks=home,
        )
    except FlowOrientationError as exc:
        raise FlowParseError(str(exc)) from exc

    return {
        "timestamp": timestamp,
        "asset_id": record.asset_ip,
        "source_type": Lane.FLOW,
        "source_format": dialect.name,
        # The one scalar worth surfacing, so the lane-parameterized stub detector and the
        # numeric dashboard views work on flows with no special-casing (flow.METRIC).
        "metric": METRIC,
        "value": float(record.bytes_out),
        "message": record.summary(),
        "metadata": {METADATA_KEY: record.to_metadata()},
    }


class FlowNormalizer:
    def __init__(self, home_networks: Sequence[str] | str | None = None) -> None:
        """``home_networks`` decides which endpoint of a flow is the monitored asset.

        Defaults to private + link-local space, which is right for almost every IoT
        deployment without configuration. Accepts a list (YAML) or a comma-separated
        string (a UI text box), because both are how it actually arrives.
        """
        if isinstance(home_networks, str):
            home_networks = [part for part in home_networks.split(",") if part.strip()]
        self._home = parse_networks(home_networks) if home_networks else DEFAULT_NETWORKS

    def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult:
        # An unsupported format is a configuration error, not a data error (port rule 3).
        dialect = DIALECTS.get(source_format)
        if dialect is None:
            raise ValueError(
                f"unsupported source_format: {source_format!r}; "
                f"known: {', '.join(sorted(DIALECTS))}"
            )

        result = NormalizeResult()
        reader = _Reader(dialect=dialect)
        delimiter = dialect.delimiter

        def reject(line: str, error: str) -> None:
            result.malformed.append(
                MalformedRecord(raw=line, error=error, source_format=source_format, origin=origin)
            )

        for line in text.splitlines():
            if not line.strip():
                continue

            if dialect.comment_prefix and line.startswith(dialect.comment_prefix):
                # A Zeek directive. `#separator` announces the delimiter every *other* line
                # uses, so it is itself whitespace-separated and written escaped (`\x09`);
                # `#fields` names the columns; the rest is metadata we must not read as data.
                if line.startswith("#separator"):
                    _, _, value = line.partition(" ")
                    if value.strip():
                        delimiter = _unescape(value)
                elif dialect.fields_directive and line.startswith(dialect.fields_directive):
                    reader.header_seen = True
                    reader.take_header(line.split(delimiter)[1:])
                    _reject_if_incomplete(reader, line, reject)
                continue

            if not reader.header_seen and dialect.header_is_first_row:
                reader.header_seen = True
                reader.take_header(line.split(delimiter))
                _reject_if_incomplete(reader, line, reject)
                continue

            if not reader.ready:
                reject(line, "flow data with no usable header describing its columns")
                continue

            try:
                fields = _parse_row(reader, line.split(delimiter), self._home)
                result.events.append(NormalizedEvent.model_validate({**fields, "raw": line}))
            except FlowParseError as exc:
                reject(line, str(exc))
            except ValidationError as exc:
                # Contract rules (aware timestamp, finite value, non-empty asset) are never
                # re-implemented here; a violation is captured with the contract's own reason.
                reject(line, "; ".join(error["msg"] for error in exc.errors()))

        return result


def _reject_if_incomplete(reader: _Reader, line: str, reject: Callable[[str, str], None]) -> None:
    """A header that names no usable columns is itself the malformed record.

    Otherwise a wrong-format file would reject every *data* line with a confusing
    per-row message, hiding the single real problem: the header did not fit the dialect.
    """
    missing = reader.missing_required()
    if missing:
        reader.index_of.clear()  # unusable: subsequent rows must not be parsed against it
        reject(line, f"header is missing required column(s): {', '.join(missing)}")


def _unescape(value: str) -> str:
    return value.strip().encode().decode("unicode_escape")
