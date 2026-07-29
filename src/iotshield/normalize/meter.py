"""Meter normalizer: cumulative utility-counter exports -> TELEMETRY consumption events.

One adapter, one dialect per source (ADR-0030 D2)
------------------------------------------------
A utility meter reports a **cumulative counter**; the meaningful observation is
**consumption over an hour**. Every meter source therefore needs the same three steps —
difference the counter, be safe across gaps, handle a counter reset — and differs only in
which columns carry the reading, which timezone the wall-clock is in, and which utility
it measures. So the shared work lives here once and a source is declarative data, exactly
the shape ``flow.py``'s dialects established.

Two dialects ship:

- ``eoa-meter-csv`` — the EOA (Nicosia water) platform's per-meter history export: nine
  columns, ``dataTime``/``flow``/``deviceId`` the only ones that matter, rows written
  **newest-first**, timestamps naive Cyprus local.
- ``sensipro-hourly-csv`` — an export of SensiPro's ``ascendinghourlyreading`` rollup:
  one row per device-hour, ``last_value`` the hour-end cumulative reading. Carries water
  **and** electricity, which is what exercises one detector serving both utilities.

SensiPro is a PostgreSQL database, not a file. Reading it directly is not a normalizer's
job — the port takes text, and our ingestion is files and streams (ADR-0019) — so it
arrives as an export of that one table. That was always the plan for it
(``docs/integration/ml-detector-and-eoa-metering-plan.md`` §6); adding a database *reader*
would be a new port, not a new adapter.

Differencing, and what "nothing is silently dropped" means here
--------------------------------------------------------------
The port's first law is that no input record vanishes. A differencing normalizer maps N
readings to N-1 observations, so the accounting is per *interval*, not per line, and every
line lands in exactly one of four states:

1. **opens an interval** — the first reading of a meter, or the first after a gap or a
   reset. It establishes the baseline the next difference is taken against. Consumed, not
   discarded: without it the following event could not exist.
2. **closes an interval** -> one ``NormalizedEvent``, whose ``raw`` is this line (the
   record whose counter terminates the interval) and whose metadata carries that counter.
3. **quarantined** -> one ``MalformedRecord``: unparseable, or the counter went
   *backwards*.
4. **schema** — the header row, which describes records rather than being one, the same
   category as the blank lines the port already skips.

A **gap** produces no event by construction: if two readings are two hours apart, the
hourly consumption between them is genuinely unknown, and inventing it (or attributing
two hours of usage to one) is exactly the "gap-boundary false spike" the upstream project
documents. The gap is therefore visible as an *absence*, which is what the detector
re-grids and what a data-quality detector will later report on.

A **counter rollback** (a decrease) is quarantined rather than emitted. It is real data,
not corrupt input, but consumption across a reset is undefined and the honest options are
to invent a number or to refuse: we refuse, visibly, and the reason names it. The counter
itself is preserved on every neighbouring event, so a rollback stays recoverable
downstream without a second ingest path.

Provenance (vendored ideas, rewritten plumbing — golden rule 3)
--------------------------------------------------------------
The cleaning semantics are Panayiotis Cyprus's (``ML-Based Anomaly and Attack Detection
for Smart Meter Telemetry``, upstream commit ``d3c61a6``): ``meterdetect/cleaning.py``
(gap-safe differencing on an hourly grid, rollback handling),
``meterdetect/adapters/eoa_water.py`` and ``meterdetect/adapters/sensipro.py`` (the column
mappings and the per-source timezone declaration), ``meterdetect/data_loading.py`` (the
newest-first ordering hazard and the European-CSV quirks).

His pipeline expresses gap-safety by reindexing onto a continuous hourly grid and taking
``.diff()``, so a gap becomes NaN and is dropped later. We difference *consecutive*
readings and emit only when they are exactly one interval apart, which yields the same
event set without materialising the empty rows — and matters because the contract rejects
a non-finite ``value``, so a NaN row could not become an event anyway.

What deliberately stays in the detector, not here: the commissioning trim, the
service-start trim and the usability verdict. Those are modelling judgments, not parsing,
and a normalizer that silently discarded a meter's first months could not be inverted by
a renderer (ADR-0030 D4).
"""

from __future__ import annotations

import csv
import math
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import ValidationError

from iotshield.capabilities import Capability, FormatToken, LaneToken
from iotshield.contracts import Lane, MalformedRecord, NormalizedEvent
from iotshield.meter import (
    METADATA_KEY,
    TYPE_BY_UNIT,
    UNIT_BY_TYPE,
    MeterReading,
    MeterType,
    metric_for,
)
from iotshield.normalize._linewise import split_lines
from iotshield.normalize.port import NormalizeResult

INTERVAL = timedelta(hours=1)
"""The reporting cadence every shipped meter dialect uses.

A module constant rather than a ``Dialect`` field: neither shipped format disagrees, and
``flow.py:MISSING`` sets the house rule for exactly this case — "a knob nothing turns is a
knob that goes untested". Promote it onto ``Dialect`` the first time a 15-minute or daily
export actually ships; until then a cadence that does not match is reported, not configured.
"""
INTERVAL_HOURS = INTERVAL.total_seconds() / 3600.0


class MeterParseError(Exception):
    """Reject one meter row; the message becomes the MalformedRecord's reason."""


def _parse_naive_or_aware(text: str) -> datetime:
    """Parse an ISO-8601-ish timestamp, leaving tz-awareness as written.

    Both shipped sources export **naive wall-clock**, so the dialect's declared timezone
    is what makes it absolute. An export that already carries an offset is honoured as
    written rather than re-localized — otherwise a source that fixed its own timezone
    handling would start reading an hour off.
    """
    # A space between date and time is what both platforms emit; `fromisoformat` accepts
    # any single separator since 3.11, which is this project's floor.
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


@dataclass(frozen=True)
class Dialect:
    """One meter export format, described rather than coded."""

    name: str
    timestamp_column: str
    counter_column: str
    meter_id_column: str
    default_meter_type: MeterType
    unit_column: str | None = None
    """Column naming the utility's unit, when the export carries it per row.

    SensiPro serves water and electricity from one table, so its export must say which;
    EOA is a water-only platform and says nothing. When absent, the normalizer's
    ``meter_type`` parameter decides.
    """
    source_timezone: str = "Europe/Nicosia"
    """The wall-clock zone this source's naive timestamps are recorded in.

    Declared per dialect, not globally: it is a property of the platform that wrote the
    file. Both shipped sources are Cyprus; a source exporting UTC declares ``"UTC"`` and
    nothing else changes.
    """


EOA_METER_CSV = Dialect(
    name="eoa-meter-csv",
    timestamp_column="dataTime",
    counter_column="flow",
    meter_id_column="deviceId",
    default_meter_type=MeterType.WATER,
)

SENSIPRO_HOURLY_CSV = Dialect(
    name="sensipro-hourly-csv",
    timestamp_column="from_timestamp",
    # The hour-END cumulative reading. SensiPro also ships `diff_last_first`, its own
    # intra-hour delta, which we deliberately do NOT use: differencing `last_value`
    # ACROSS hours is what stays correct at a gap or reset boundary, whereas an
    # intra-hour delta silently reports a partial hour as a whole one.
    counter_column="last_value",
    meter_id_column="device_id",
    default_meter_type=MeterType.WATER,
    unit_column="unit",
)

DIALECTS: dict[str, Dialect] = {
    EOA_METER_CSV.name: EOA_METER_CSV,
    SENSIPRO_HOURLY_CSV.name: SENSIPRO_HOURLY_CSV,
}

MISSING = ("", "-", "nan", "none", "null")
"""Tokens an export uses for "no value". Compared case-insensitively."""

CAPABILITY = Capability.of(
    consumes={FormatToken(name) for name in DIALECTS},
    produces={LaneToken(Lane.TELEMETRY)},
)


@dataclass(frozen=True, slots=True)
class _Reading:
    """One parsed counter reading, before differencing."""

    timestamp: datetime
    counter: float
    meter_id: str
    meter_type: MeterType
    line: str

    @property
    def unit(self) -> str:
        """Derived, never stored: a unit that disagreed with its type would be unreachable
        nonsense, and two fields that must agree are two fields that can stop agreeing."""
        return UNIT_BY_TYPE[self.meter_type]


@dataclass(frozen=True, slots=True)
class _Layout:
    """Everything about a file that is fixed once its header has been read.

    Resolving the column positions and the per-file constants here means the row loop does
    no dictionary lookups and no re-derivation — on a fleet export that loop runs tens of
    millions of times.
    """

    timestamp_at: int
    counter_at: int
    meter_id_at: int
    unit_at: int | None
    column_count: int
    fallback_type: MeterType


def _resolve_zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        # A config error, not a data error: every row would be wrong, so fail loudly.
        raise ValueError(f"unknown source timezone {name!r}") from exc


def _localize(stamp: datetime, zone: ZoneInfo) -> datetime:
    """Attach ``zone`` to a naive wall clock, refusing the two clocks that lie.

    A daylight-saving transition makes one local hour **ambiguous** (it happens twice) and
    one **nonexistent** (it never happens). ``zoneinfo`` resolves both silently, which
    would place a reading up to an hour from where it belongs — and an hour is the entire
    resolution of this data. Comparing the two ``fold`` interpretations detects both cases,
    so they are quarantined with a reason instead.

    Returns **UTC**, not a zone-attached local time. That is what puts the reading on the
    timeline this docstring promises, and it is load-bearing rather than tidy: CPython's
    ``datetime.__sub__`` short-circuits when both operands share a ``tzinfo`` *object*, and
    ``_parse_row`` hands every row the same ``ZoneInfo``. Subtraction therefore compared
    naive **wall clocks**, so the interval test in ``_closed_intervals`` was a wall-clock
    test — and at the spring-forward the readings at local 02:00 and 04:00 are one real hour
    apart but two wall-clock hours, so no event was emitted and no MalformedRecord either.
    One hour of consumption vanished with no trace, once a year, per meter (ADR-0033).
    """
    if stamp.tzinfo is not None:
        return stamp.astimezone(UTC)
    first = stamp.replace(tzinfo=zone, fold=0)
    second = stamp.replace(tzinfo=zone, fold=1)
    if first.utcoffset() != second.utcoffset():
        raise MeterParseError(
            f"local time {stamp.isoformat()} is ambiguous or nonexistent in "
            f"{zone.key} (daylight-saving transition); cannot be placed on the UTC timeline"
        )
    return first.astimezone(UTC)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    stripped = text.strip()
    return None if stripped.lower() in MISSING else stripped


class MeterNormalizer:
    """Cumulative meter exports -> hourly consumption events, one dialect per source."""

    def __init__(
        self,
        meter_type: str | None = None,
        source_timezone: str | None = None,
    ) -> None:
        """``meter_type`` and ``source_timezone`` override the dialect's own defaults.

        Both exist because they are deployment facts a dialect cannot know: an export of
        SensiPro's electricity meters is the same *format* as its water meters, and a
        client in another country writes the same columns in another zone. Overriding
        beats forking a dialect per combination.
        """
        self._meter_type = MeterType(meter_type) if meter_type is not None else None
        # Resolved once, at construction: a bad zone is a config error every row would
        # share, and re-resolving it per call was pure repetition.
        self._zone = _resolve_zone(source_timezone) if source_timezone is not None else None

    def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult:
        dialect = DIALECTS.get(source_format)
        if dialect is None:
            raise ValueError(
                f"unsupported source_format: {source_format!r}; "
                f"known: {', '.join(sorted(DIALECTS))}"
            )

        result = NormalizeResult()
        zone = self._zone or _resolve_zone(dialect.source_timezone)

        def reject(line: str, error: str) -> None:
            result.malformed.append(
                MalformedRecord(raw=line, error=error, source_format=source_format, origin=origin)
            )

        readings = self._read_rows(text, dialect, zone, reject)
        for reading, previous in _closed_intervals(readings, reject):
            try:
                result.events.append(
                    NormalizedEvent.model_validate(
                        _consumption_event(reading, previous, source_format)
                    )
                )
            except ValidationError as exc:
                # Contract rules are never re-implemented here; a violation is captured
                # with the contract's own reason.
                reject(reading.line, "; ".join(error["msg"] for error in exc.errors()))
        return result

    def _read_rows(
        self,
        text: str,
        dialect: Dialect,
        zone: ZoneInfo,
        reject: Callable[[str, str], None],
    ) -> list[_Reading]:
        """Parse every data row into a reading, quarantining the ones that fail."""
        rows = [line for line in split_lines(text) if line.strip()]
        if not rows:
            return []
        # The header is peeled off before the loop, so the loop needs no "is this the first
        # line" flag and never re-tests it.
        header, *data = rows
        delimiter = sniff_delimiter(header)
        headings = _split(header, delimiter)
        layout, missing = self._bind(headings, dialect)
        if layout is None:
            reject(header, f"header is missing required column(s): {', '.join(missing)}")
            for line in data:
                reject(line, "meter data with no usable header describing its columns")
            return []

        readings: list[_Reading] = []
        # A fleet export repeats one hourly clock across every meter, so the same few
        # thousand timestamp strings recur. Resolving each once — including the DST probe,
        # the most expensive step per row — turns that work into a dict hit.
        resolved: dict[str, datetime | str] = {}
        for line in data:
            parts = _split(line, delimiter)
            if len(parts) != layout.column_count:
                reject(
                    line,
                    f"row has {len(parts)} field(s) but the header declared {layout.column_count}",
                )
                continue
            try:
                readings.append(self._parse_row(parts, layout, dialect, zone, resolved, line))
            except MeterParseError as exc:
                reject(line, str(exc))
        return readings

    def _bind(self, headings: Sequence[str], dialect: Dialect) -> tuple[_Layout | None, list[str]]:
        """Resolve column positions and per-file constants, or report what is missing."""
        positions = {name.strip(): index for index, name in enumerate(headings)}
        required = (dialect.timestamp_column, dialect.counter_column, dialect.meter_id_column)
        missing = [name for name in required if name not in positions]
        if missing:
            return None, missing
        unit_at = positions.get(dialect.unit_column) if dialect.unit_column is not None else None
        return (
            _Layout(
                timestamp_at=positions[dialect.timestamp_column],
                counter_at=positions[dialect.counter_column],
                meter_id_at=positions[dialect.meter_id_column],
                unit_at=unit_at,
                column_count=len(headings),
                fallback_type=self._meter_type or dialect.default_meter_type,
            ),
            [],
        )

    def _parse_row(
        self,
        parts: Sequence[str],
        layout: _Layout,
        dialect: Dialect,
        zone: ZoneInfo,
        resolved: dict[str, datetime | str],
        line: str,
    ) -> _Reading:
        stamp_text = _clean(parts[layout.timestamp_at])
        if stamp_text is None:
            raise MeterParseError(f"{dialect.timestamp_column} is missing")
        cached = resolved.get(stamp_text)
        if cached is None:
            try:
                cached = _localize(_parse_naive_or_aware(stamp_text), zone)
            except ValueError:
                cached = f"unparseable timestamp: {stamp_text!r}"
            except MeterParseError as exc:
                cached = str(exc)
            resolved[stamp_text] = cached
        if isinstance(cached, str):
            raise MeterParseError(cached)

        counter_text = _clean(parts[layout.counter_at])
        if counter_text is None:
            raise MeterParseError(f"{dialect.counter_column} is missing")
        try:
            # Some exports use a comma decimal separator (they are the ';'-delimited ones);
            # accept it rather than reading "89,91" as the integer 8991.
            counter = float(counter_text.replace(",", "."))
        except (ValueError, OverflowError) as exc:
            raise MeterParseError(
                f"{dialect.counter_column} is not a usable number: {counter_text!r}"
            ) from exc
        if not math.isfinite(counter):
            raise MeterParseError(f"{dialect.counter_column} is not finite: {counter_text!r}")
        if counter < 0:
            # A cumulative utility counter cannot be negative; a negative one is corrupt.
            raise MeterParseError(f"{dialect.counter_column} is negative: {counter_text!r}")

        meter_id = _clean(parts[layout.meter_id_at])
        if meter_id is None:
            raise MeterParseError(f"{dialect.meter_id_column} is missing")

        return _Reading(
            timestamp=cached,
            counter=counter,
            meter_id=meter_id,
            meter_type=self._meter_type_for(parts, layout, dialect),
            line=line,
        )

    def _meter_type_for(self, parts: Sequence[str], layout: _Layout, dialect: Dialect) -> MeterType:
        """Which utility this row measures: the row's own unit, else config, else dialect.

        The row wins because the unit is the physical truth about the device — the rule
        the upstream project arrived at after finding SensiPro's ``category_id``
        unreliable and ``devices.unit`` authoritative (``docs/sensipro_data.md``).
        """
        if layout.unit_at is None:
            return layout.fallback_type
        unit_text = _clean(parts[layout.unit_at])
        if unit_text is None:
            return layout.fallback_type
        kind = TYPE_BY_UNIT.get(unit_text.lower())
        if kind is None:
            raise MeterParseError(
                f"{dialect.unit_column} {unit_text!r} is not a consumption unit; "
                f"expected one of {', '.join(sorted(TYPE_BY_UNIT))}"
            )
        return kind


def sniff_delimiter(header: str) -> str:
    """Which delimiter this export uses, decided once from its header row.

    European exports of exactly this kind are routinely semicolon-delimited *because* their
    decimals are commas — the two travel together, and upstream's loader sniffs for the same
    reason (``meterdetect/data_loading.py``). Sniffing the **header** rather than a data row
    is what makes it safe: the header contains no numbers, so a comma decimal cannot be
    mistaken for a separator.
    """
    counts = {candidate: header.count(candidate) for candidate in (",", ";", "\t", "|")}
    best = max(counts, key=lambda candidate: counts[candidate])
    return best if counts[best] else ","


def _split(line: str, delimiter: str = ",") -> list[str]:
    """One CSV line into fields, honouring RFC 4180 quoting when it is present.

    Only a line containing a quote can split differently, so the common case takes
    ``str.split`` — building a ``csv.reader`` per row is several times slower and needless
    on an export of hundreds of thousands of readings.
    """
    if '"' in line:
        return next(csv.reader([line], delimiter=delimiter))
    return line.split(delimiter)


def _closed_intervals(
    readings: Iterable[_Reading],
    reject: Callable[[str, str], None],
) -> Iterator[tuple[_Reading, _Reading]]:
    """Yield each reading paired with the one exactly ``INTERVAL`` before it, per meter.

    Yields ``(closing, opening)`` pairs — the only readings that can become consumption
    events — as a generator, so a fleet export never materializes a second full-size list
    beside the readings it was built from. Sorting per meter is what makes the EOA export's
    **newest-first** row order harmless: unsorted, every difference would be negated and
    read as a rollback.
    """
    by_meter: dict[str, list[_Reading]] = {}
    for reading in readings:
        by_meter.setdefault(reading.meter_id, []).append(reading)

    for meter_id in sorted(by_meter):
        series = sorted(by_meter[meter_id], key=lambda item: item.timestamp)
        # One id, two utilities: refuse the meter rather than difference across them.
        #
        # The SensiPro dialect exists BECAUSE one table serves water and electricity, and the
        # unit is resolved per row for that reason (`_meter_type_for`) — but the grouping key
        # above is the id alone, so a water counter was differenced against an electricity one
        # and emitted a physically meaningless number with no reject. Worse, when both
        # utilities report the SAME hour the de-duplication below dropped one as a "duplicate
        # timestamp": an entire utility disappeared under a message that misdiagnosed it.
        #
        # Grouping by (id, type) instead would fix the arithmetic and keep the harm: asset_id
        # is the meter id, so both series would still land on one asset and feed one
        # per-meter model. There is no honest way to pick a winner either. So this is refused
        # visibly, exactly as ADR-0030 already refuses a gap and a rollback — real data the
        # contract cannot represent. The remedy is upstream: give each utility its own id.
        # Checked BEFORE the dedupe so the operator gets the true reason (ADR-0033).
        utilities = {reading.meter_type for reading in series}
        if len(utilities) > 1:
            named = ", ".join(sorted(kind.value for kind in utilities))
            for reading in series:
                reject(
                    reading.line,
                    f"meter {meter_id!r} reports two utilities ({named}); consumption cannot "
                    "be attributed to one asset - give each utility its own device id",
                )
            continue
        # De-duplicate first, so the pairing below is a plain walk over adjacent readings
        # rather than a walk with a "did this one count?" flag threaded through it.
        kept: list[_Reading] = []
        for reading in series:
            if kept and reading.timestamp == kept[-1].timestamp:
                # A duplicated export row. Keep the first and say so, rather than
                # differencing a reading against itself and emitting a phantom zero.
                reject(reading.line, f"duplicate timestamp for meter {meter_id!r}")
                continue
            kept.append(reading)

        closed_any = False
        for previous, reading in pairwise(kept):
            if reading.timestamp - previous.timestamp != INTERVAL:
                continue
            if reading.counter < previous.counter:
                # Real data, but consumption across a counter reset is undefined.
                reject(
                    reading.line,
                    "cumulative counter decreased "
                    f"({previous.counter} -> {reading.counter}): a rollback or meter "
                    "change, so consumption for this interval is undefined",
                )
            else:
                closed_any = True
                yield reading, previous

        if len(kept) > 1 and not closed_any:
            _report_cadence(meter_id, kept, reject)


def _report_cadence(
    meter_id: str, kept: list[_Reading], reject: Callable[[str, str], None]
) -> None:
    """Say so when a meter's readings parsed but none of them form an interval.

    Left unsaid, a 15-minute or daily export would produce zero events and zero rejects, and
    the run would read as "nothing anomalous" when the truth is "I could not read your data
    at all". One record per meter, and only when the meter yielded nothing, so it cannot
    become noise.
    """
    observed = min(b.timestamp - a.timestamp for a, b in pairwise(kept))
    reject(
        kept[1].line,
        f"meter {meter_id!r}: {len(kept)} readings parsed but no two are "
        f"{_describe(INTERVAL)} apart (closest pair: {_describe(observed)}); this "
        f"export's sampling cadence does not match the dialect's",
    )


def _describe(span: timedelta) -> str:
    minutes = span.total_seconds() / 60
    return f"{minutes / 60:g}h" if minutes >= 60 else f"{minutes:g}min"


def _consumption_event(
    reading: _Reading,
    previous: _Reading,
    source_format: str,
) -> dict[str, Any]:
    """One closed interval -> the complete NormalizedEvent field mapping.

    ``raw`` is set here rather than merged in by the caller: this already builds the one
    dict the contract is validated from, and merging produced a second full copy per event.
    """
    consumption = reading.counter - previous.counter
    unit = reading.unit
    record = MeterReading(
        meter_type=reading.meter_type,
        unit=unit,
        counter=reading.counter,
        interval_hours=INTERVAL_HOURS,
    )
    return {
        # Stamped at the interval's END, matching the upstream convention and the way an
        # hourly export labels its rows (see iotshield.meter for why this differs from FLOW).
        "timestamp": reading.timestamp,
        "asset_id": reading.meter_id,
        "source_type": Lane.TELEMETRY,
        "source_format": source_format,
        "metric": metric_for(unit),
        "value": consumption,
        "raw": reading.line,
        # The interval is in metadata, so it is not repeated here: this string is formatted
        # once per event and a fleet run holds millions of them.
        "message": f"{consumption:.4f} {unit} (counter {reading.counter:g})",
        "metadata": {METADATA_KEY: record.to_metadata()},
    }
