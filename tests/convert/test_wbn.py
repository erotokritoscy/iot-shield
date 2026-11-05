"""WBN workbooks -> meter text. Grids are built in-process: real exports are operator data."""

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from iotshield.convert import wbn
from iotshield.convert._rates import SOURCE_FORMAT
from iotshield.normalize.meter import MeterNormalizer

N = 48


def _times() -> tuple[list[str], list[str]]:
    stamps = pd.date_range("2025-03-01 07:00", periods=N, freq="30min")
    return [s.strftime("%d/%m/%Y") for s in stamps], [s.strftime("%H:%M") for s in stamps]


def fs_sheet(values: dict[str, list[float]]) -> pd.DataFrame:
    """A flow-station grid: the measure sits on the Time row, the group repeated above it."""
    dates, times = _times()
    groups = list(values)
    rows = [
        [None, None, *["" for _ in groups]],
        ["", "", *groups],
        ["", "", *groups],
        ["Date", "Time", *["Flow Rate" for _ in groups]],
    ]
    for index in range(N):
        rows.append([dates[index], times[index], *[values[g][index] for g in groups]])
    rows.append(["Minimum", "", *[0.0 for _ in groups]])
    return pd.DataFrame(rows, dtype=object)


def rsv_sheet() -> pd.DataFrame:
    """A reservoir grid: the measure sits ABOVE the Time row, which holds a sub-tag."""
    dates, times = _times()
    rows = [
        [None, None, "", "", ""],
        ["", "", "R-1 Athalassa", "R-1 Athalassa", "R-1 Athalassa"],
        ["", "", "Inflow", "Outflow", "Level"],
        ["Date", "Time", "FM1", "FM2", "m"],
    ]
    for index in range(N):
        rows.append([dates[index], times[index], 100.0, 50.0, 3.2])
    return pd.DataFrame(rows, dtype=object)


def convert_sheets(sheets: list[pd.DataFrame]) -> tuple[dict[str, pd.Series], wbn.Report]:
    stats = wbn.Report()
    days = [wbn.parse_sheet(sheet, stats) for sheet in sheets]
    return wbn.meters_from(pd.concat([day for day in days if day is not None]), stats), stats


def test_a_flow_station_becomes_one_hourly_meter() -> None:
    meters, _ = convert_sheets([fs_sheet({"Station 14": [10.0, 20.0] * 24})])
    series = meters["WBN_FS_S14"]
    # The hourly mean of two half-hourly m3/h values is that hour's m3.
    assert series.iloc[1] == pytest.approx(15.0)
    assert str(pd.DatetimeIndex(series.index).tz) == "UTC"


def test_local_time_is_placed_on_utc() -> None:
    meters, _ = convert_sheets([fs_sheet({"Station 14": [10.0] * N})])
    # 07:00 in Nicosia in March, before daylight saving, is 05:00 UTC.
    assert meters["WBN_FS_S14"].index[0] == pd.Timestamp("2025-03-01 05:00", tz="UTC")


def test_a_reservoir_yields_a_meter_per_direction_and_no_context_channels() -> None:
    meters, _ = convert_sheets([rsv_sheet()])
    assert set(meters) == {"WBN_RSV_R-1_in", "WBN_RSV_R-1_out"}


def test_summary_rows_are_not_readings() -> None:
    meters, _ = convert_sheets([fs_sheet({"Station 14": [10.0] * N})])
    assert meters["WBN_FS_S14"].notna().sum() == N / 2


def test_a_dead_placeholder_channel_is_dropped_and_counted() -> None:
    meters, stats = convert_sheets([fs_sheet({"Station 14": [10.0] * N, "Station 15": [0.02] * N})])
    assert set(meters) == {"WBN_FS_S14"}
    assert stats.dead_channels == 1


def test_two_live_channels_of_one_group_are_numbered() -> None:
    sheet = fs_sheet({"Station 22": [10.0] * N, "Other": [11.0] * N})
    sheet.iat[1, 3] = sheet.iat[2, 3] = "Station 22"
    meters, _ = convert_sheets([sheet])
    assert set(meters) == {"WBN_FS_S22_c0", "WBN_FS_S22_c1"}


def test_a_negative_hour_becomes_a_counted_gap_and_the_output_normalizes_cleanly() -> None:
    values = [10.0] * N
    values[4] = values[5] = -3.0
    meters, stats = convert_sheets([fs_sheet({"Station 14": values})])
    lines = wbn.render(meters, stats)
    text = "\n".join([wbn.HEADER, *lines]) + "\n"
    result = MeterNormalizer().normalize(text, source_format=SOURCE_FORMAT, origin="t")
    assert stats.negative_hours == 1
    # A one-hour gap, so the reading after it goes too, rather than a zero being invented for it.
    assert stats.after_short_gap_hours == 1
    assert not result.malformed
    assert len(result.events) == stats.readings == N / 2 - 2
    assert all(event.value == pytest.approx(10.0) for event in result.events)


def test_a_daylight_saving_hour_that_cannot_be_placed_is_counted() -> None:
    sheet = fs_sheet({"Station 14": [10.0] * N})
    # 03:00 local on 30 March 2025 does not exist in Nicosia.
    sheet.iat[4, 0], sheet.iat[4, 1] = "30/03/2025", "03:30"
    stats = wbn.Report()
    wbn.parse_sheet(sheet, stats)
    assert stats.unplaceable_stamps == 1


def test_an_empty_sheet_is_no_data_rather_than_an_error() -> None:
    assert wbn.parse_sheet(pd.DataFrame(), wbn.Report()) is None


def test_a_sheet_without_a_time_cell_is_refused() -> None:
    with pytest.raises(ValueError, match="not a WBN daily grid"):
        wbn.parse_sheet(pd.DataFrame(np.zeros((3, 3))), wbn.Report())


def test_discovery_takes_daily_grids_only(tmp_path: Path) -> None:
    for name in (
        "WBN_FS_2025_01_02.xls",
        "WBN_RSV_2025_01_01.xlsx",
        "WBN_FS_Detailed_2025_01.xls",
        "WBN_RSV_DailyFlow_2025.xls",
        "WBN_FS22_2025_03_17.xlsx",
    ):
        (tmp_path / name).touch()
    assert [p.name for p in wbn.discover(tmp_path)] == [
        "WBN_FS_2025_01_02.xls",
        "WBN_RSV_2025_01_01.xlsx",
    ]
    assert [p.name for p in wbn.discover(tmp_path, "rsv")] == ["WBN_RSV_2025_01_01.xlsx"]


def test_one_corrupt_day_is_reported_and_loses_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    good, bad = tmp_path / "WBN_FS_2025_03_01.xls", tmp_path / "WBN_FS_2025_03_02.xls"
    good.touch()
    bad.touch()

    def read(path: Path) -> pd.DataFrame:
        if path == bad:
            raise ValueError("truncated")
        return fs_sheet({"Station 14": [10.0] * N})

    monkeypatch.setattr(wbn, "_read_sheet", read)
    text, stats = wbn.convert(tmp_path)
    assert stats.files == 1
    assert "truncated" in stats.skipped_files[bad.name]
    assert "WBN_FS_S14" in text


def test_a_missing_optional_dependency_says_how_to_get_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "WBN_FS_2025_03_01.xls").touch()

    def read_excel(*_args: object, **_kwargs: object) -> pd.DataFrame:
        raise ImportError("xlrd")

    monkeypatch.setattr(pd, "read_excel", read_excel)
    with pytest.raises(RuntimeError, match="--extra wbn"):
        wbn.convert(tmp_path)


def test_the_cli_writes_a_file_and_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "WBN_FS_2025_03_01.xls").touch()
    monkeypatch.setattr(wbn, "_read_sheet", lambda _: fs_sheet({"Station 14": [10.0] * N}))
    out = tmp_path / "wbn.csv"
    assert wbn.main(["--source", str(tmp_path), "--out", str(out)]) == 0
    assert out.read_text().startswith(wbn.HEADER)
    assert "1 meter(s)" in capsys.readouterr().out
