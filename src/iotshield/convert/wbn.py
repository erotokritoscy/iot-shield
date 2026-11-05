"""Water Board of Nicosia SCADA workbooks -> ``sensipro-hourly-csv`` text (ADR-0048 D2).

WBN exports one Fast-Reports workbook per day: ``WBN_FS_*`` (flow stations) and ``WBN_RSV_*``
(reservoirs), 48 half-hourly readings over a "water day" running 07:00 -> 06:30. A normalizer
takes text and these are binary, so they are converted first, as ``connect/`` does for a
database. The output is an ordinary upload for the Compose page or a fit for ``meter.train``:

    uv sync --extra wbn
    uv run python -m iotshield.convert.wbn --source <dir of workbooks> --out wbn.csv

The output holds operator data, so it does not belong in this repository.

Grid layout (0-based rows): row 1 is a merged group header; a "Time" cell anchors the block. In
FS files the measure sits on the Time row, in RSV files one row above it. After the 48 readings
come Minimum/Maximum/AVG rows, which fail to parse as a time and fall out.

**One meter per live flow channel**: ``WBN_FS_S14``, ``WBN_RSV_R-1_in``, with ``_c<n>`` where a
group has several. A channel that never exceeds ``MIN_LIVE_FLOW`` m3/h is a dead placeholder and
is dropped. Each half-hourly value is an instantaneous m3/h, so the hourly mean is the m3 for
that hour, and ``_rates`` carries it as a counter.

Two differences from upstream, both visible in the report ``convert`` returns:

- A **negative** flow is not written, since a counter cannot fall without reading as a rollback.
  The hour becomes a gap and is counted rather than vanishing.
- The reading **after a one-hour gap** is dropped and counted, because a counter cannot express it
  without inventing a zero for the missing hour (``_rates``).
- The **pressure, level and chlorine** channels upstream carries as context are not converted.
  ``MeterReading`` has no place for them and no detector would read them yet.

The two wall-clock hours daylight saving breaks each year are dropped at the boundary, also
counted, because a naive Cyprus time in them cannot be placed on the UTC timeline.

Provenance: the workbook parsing is Panayiotis Cyprus's ``meterdetect/adapters/wbn_water.py``
(*ML-Based Anomaly and Attack Detection for Smart Meter Telemetry*, an August 2026 working copy
with no git history, so no commit can be cited; see ADR-0048).
"""

from __future__ import annotations

import argparse
import re
import warnings
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import numpy as np
import pandas as pd

from iotshield.convert._rates import HEADER, counter_rows, representable

SOURCE_ZONE = "Europe/Nicosia"
UNIT = "m3"
MIN_LIVE_FLOW = 1.0
"""m3/h. Dead channels sit flat at about 0.01-0.03, so a small floor separates them cleanly."""

_DAILY = re.compile(r"^WBN_(FS|RSV)_(\d{4})_(\d{2})_(\d{2})\b.*\.xlsx?$", re.IGNORECASE)
"""Genuine daily grids only: excludes the Detailed, Period and DailyFlow rollups."""

_FLOW_MEASURES = {"flow rate", "inflow", "outflow"}
_FIRST_YEAR = pd.Timestamp("2015-01-01")
_LAST_YEAR = pd.Timestamp("2035-01-01")
"""A few files carry a stray row whose date degrades to the 1899 Excel epoch."""

Column = tuple[str, str, str, int]
"""(short tag, group, measure, occurrence of that measure within the group)."""


@dataclass
class Report:
    """What a conversion did, including everything it declined to write."""

    files: int = 0
    skipped_files: dict[str, str] = field(default_factory=dict)
    meters: int = 0
    readings: int = 0
    negative_hours: int = 0
    after_short_gap_hours: int = 0
    dead_channels: int = 0
    unplaceable_stamps: int = 0


def discover(directory: Path, report: str = "both") -> list[Path]:
    """Every daily workbook under ``directory``, oldest first, for one family or both."""
    wanted = {"fs": {"FS"}, "rsv": {"RSV"}, "both": {"FS", "RSV"}}[report]
    found = []
    for path in directory.rglob("WBN_*.xls*"):
        match = _DAILY.match(path.name)
        if match and match.group(1).upper() in wanted:
            found.append(path)
    return sorted(found, key=lambda path: path.name)


def _read_sheet(path: Path) -> pd.DataFrame:
    """The raw first sheet: ``.xls`` through xlrd, and ``.xlsx`` through calamine because
    WBN's Fast-Reports stylesheet is one openpyxl rejects."""
    engine = "xlrd" if path.suffix.lower() == ".xls" else "calamine"
    try:
        sheet = pd.read_excel(path, sheet_name=0, header=None, engine=engine, dtype=object)
    except ImportError as exc:
        raise RuntimeError(
            "reading WBN workbooks needs the optional dependencies: uv sync --extra wbn"
        ) from exc
    return sheet.replace(r"^\s*$", np.nan, regex=True)


def _blank(cell: object) -> bool:
    return cast("bool", pd.isna(cell))  # type: ignore[arg-type]


def _deduplicated(frame: pd.DataFrame) -> pd.DataFrame:
    """Time-ordered, one row per instant, first wins: two files can overlap at a day's end."""
    return cast("pd.DataFrame", frame[~frame.index.duplicated(keep="first")].sort_index())


def _time_cell(sheet: pd.DataFrame) -> tuple[int, int]:
    for row in range(min(8, len(sheet))):
        for column in range(min(8, sheet.shape[1])):
            if str(sheet.iat[row, column]).strip().lower() == "time":
                return row, column
    raise ValueError("no 'Time' header cell found, so this is not a WBN daily grid")


def _short_tag(group: str) -> str:
    station = re.search(r"Station\s+(\d+)", group)
    if station:
        return f"S{int(station.group(1)):02d}"
    reservoir = re.search(r"(R-\d+)", group)
    if reservoir:
        return reservoir.group(1)
    return re.sub(r"[^0-9A-Za-z]+", "_", group).strip("_")[:24] or "grp"


def parse_sheet(sheet: pd.DataFrame, stats: Report) -> pd.DataFrame | None:
    """One day's grid as a wide float frame indexed by UTC, or None when it holds no readings.

    Columns are ``Column`` tuples, flow channels only.
    """
    if sheet.empty:
        return None
    time_row, time_column = _time_cell(sheet)
    groups = sheet.iloc[1].ffill()

    channels: dict[int, tuple[str, str]] = {}
    for column in range(time_column + 1, sheet.shape[1]):
        group = groups.iat[column] if column < len(groups) else None
        if _blank(group):
            continue
        above = sheet.iat[time_row - 1, column] if time_row > 0 else None
        # FS: the measure sits on the Time row, with the group repeated above it.
        # RSV: the measure sits one row above, and the Time row holds a flowmeter sub-tag.
        measure = sheet.iat[time_row, column] if str(above).strip() == str(group).strip() else above
        if _blank(measure):
            continue
        channels[column] = (str(group), str(measure).strip())

    body = sheet.iloc[time_row + 1 :]
    stamps = pd.to_datetime(
        body.iloc[:, 0].astype(str) + " " + body.iloc[:, time_column].astype(str),
        dayfirst=True,
        errors="coerce",
        format="mixed",
    )
    valid = (stamps.notna() & (stamps >= _FIRST_YEAR) & (stamps < _LAST_YEAR)).to_numpy()
    if not valid.any():
        return None

    local = pd.DatetimeIndex(stamps[valid]).tz_localize(
        SOURCE_ZONE, ambiguous="NaT", nonexistent="NaT"
    )
    placed = ~local.isna()
    stats.unplaceable_stamps += int((~placed).sum())

    data: dict[Column, np.ndarray] = {}
    seen: dict[tuple[str, str], int] = {}
    for column, (group, measure) in channels.items():
        occurrence = seen.get((group, measure), 0)
        seen[(group, measure)] = occurrence + 1
        if measure.lower() not in _FLOW_MEASURES:
            continue
        values = np.asarray(pd.to_numeric(body.iloc[:, column], errors="coerce"), dtype=float)
        data[(_short_tag(group), group, measure, occurrence)] = values[valid][placed]
    if not data:
        return None

    frame = pd.DataFrame(data, index=local[placed].tz_convert("UTC"))
    return _deduplicated(frame)


def _direction(measure: str) -> str:
    lowered = measure.lower()
    return {"inflow": "in", "outflow": "out"}.get(lowered, "flow")


def meters_from(wide: pd.DataFrame, stats: Report) -> dict[str, pd.Series]:
    """The concatenated days as ``{meter id: hourly m3 series}``, live channels only."""
    hourly = wide.resample("1h").mean()
    columns = cast("list[Column]", list(hourly.columns))

    live: list[Column] = []
    for column in columns:
        values = hourly[column].to_numpy(dtype=float)
        if np.isfinite(values).any() and float(np.nanmax(values)) > MIN_LIVE_FLOW:
            live.append(column)
        else:
            stats.dead_channels += 1

    per_group: dict[tuple[str, str], int] = {}
    for tag, _group, measure, _occurrence in live:
        key = (tag, _direction(measure))
        per_group[key] = per_group.get(key, 0) + 1

    meters: dict[str, pd.Series] = {}
    numbered: dict[tuple[str, str], int] = {}
    for column in live:
        tag, _group, measure, _occurrence = column
        direction = _direction(measure)
        key = (tag, direction)
        number = numbered.get(key, 0)
        numbered[key] = number + 1
        parts = ["WBN", "RSV" if direction in ("in", "out") else "FS", tag]
        if direction != "flow":
            parts.append(direction)
        meter_id = "_".join(parts)
        if per_group[key] > 1:
            meter_id = f"{meter_id}_c{number}"
        meters[meter_id] = hourly[column].astype(float)
    return meters


def _family(paths: Sequence[Path], stats: Report) -> dict[str, pd.Series]:
    days = []
    for path in paths:
        try:
            day = parse_sheet(_read_sheet(path), stats)
        except RuntimeError:
            raise
        except Exception as exc:  # one corrupt day must not lose a year; it is reported instead
            stats.skipped_files[path.name] = f"{type(exc).__name__}: {exc}"
            continue
        stats.files += 1
        if day is not None and not day.empty:
            days.append(day)
    if not days:
        return {}
    wide = pd.concat(days, axis=0)
    return meters_from(_deduplicated(wide), stats)


def render(meters: dict[str, pd.Series], stats: Report) -> list[str]:
    """Every meter as counter rows, sorted by meter id, without a header."""
    lines: list[str] = []
    for meter_id in sorted(meters):
        series = cast("pd.Series", meters[meter_id].dropna())
        negative = series < 0
        stats.negative_hours += int(negative.sum())
        kept = cast("pd.Series", series[~negative])
        if kept.empty:
            continue
        stamps = pd.DatetimeIndex(kept.index).to_pydatetime()  # type: ignore[attr-defined]
        every = list(zip(stamps, kept.to_numpy(dtype=float).tolist(), strict=True))
        pairs = representable(every)
        stats.after_short_gap_hours += len(every) - len(pairs)
        lines.extend(counter_rows(pairs, meter_id=meter_id, unit=UNIT))
        stats.meters += 1
        stats.readings += len(pairs)
    return lines


def convert(
    directory: Path, *, report: str = "both", limit: int | None = None
) -> tuple[str, Report]:
    """All workbooks under ``directory`` -> (``sensipro-hourly-csv`` text, what happened).

    FS and RSV are converted apart: they share timestamps and have disjoint meters, so joining
    them into one wide frame would collapse each other's rows.
    """
    stats = Report()
    meters: dict[str, pd.Series] = {}
    for family in ("rsv", "fs") if report == "both" else (report,):
        paths = discover(directory, family)
        if limit is not None:
            paths = paths[:limit]
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            meters.update(_family(paths, stats))
    return "\n".join([HEADER, *render(meters, stats)]) + "\n", stats


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    parser.add_argument("--source", type=Path, required=True, help="directory of WBN workbooks")
    parser.add_argument("--out", type=Path, required=True, help="CSV to write")
    parser.add_argument("--report", choices=("fs", "rsv", "both"), default="both")
    parser.add_argument("--limit", type=int, default=None, help="files per family (a quick look)")
    args = parser.parse_args(argv)
    if not args.source.is_dir():
        parser.error(f"not a directory: {args.source}")

    text, stats = convert(args.source, report=args.report, limit=args.limit)
    args.out.write_text(text, encoding="utf-8", newline="\n")
    print(
        f"{stats.files} workbook(s) -> {stats.meters} meter(s), {stats.readings} hourly reading(s) "
        f"-> {args.out}\n"
        f"  not written: {stats.dead_channels} dead channel(s), {stats.negative_hours} negative "
        f"hour(s), {stats.after_short_gap_hours} hour(s) after a one-hour gap, "
        f"{stats.unplaceable_stamps} daylight-saving stamp(s)"
    )
    for name, why in sorted(stats.skipped_files.items()):
        print(f"  skipped {name}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
