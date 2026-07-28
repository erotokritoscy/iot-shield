"""Meter normalizer specifics: differencing, gaps, rollbacks, timezones, dialects.

The port suite (tests/ports/test_normalizer_port.py) covers the laws every normalizer
obeys. This covers what is true only of a *differencing* normalizer — which is where the
interesting failure modes are, because it maps N readings to N-1 observations.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from iotshield.meter import METADATA_KEY, MeterReading, MeterType
from iotshield.normalize.meter import DIALECTS, MeterNormalizer

ORIGIN = "test://meter"
EOA = "eoa-meter-csv"
SENSIPRO = "sensipro-hourly-csv"

HEADER = "params,id,updateAt,deviceId,flow,dataTime,signalCsq,isWaring,paValue"


def eoa(*rows: tuple[str, str]) -> str:
    """Build an EOA export from (timestamp, counter) pairs, written newest-first."""
    lines = [HEADER]
    for index, (stamp, counter) in enumerate(reversed(rows)):
        lines.append(f"{{}},{index},2026-07-07 09:00:00,M-1,{counter},{stamp},0,,")
    return "\n".join(lines) + "\n"


def normalize(text: str, source_format: str = EOA, **params: object):
    return MeterNormalizer(**params).normalize(  # type: ignore[arg-type]
        text, source_format=source_format, origin=ORIGIN
    )


def test_consumption_is_the_difference_between_consecutive_counters() -> None:
    result = normalize(eoa(("2026-07-07 00:00:00", "10.000"), ("2026-07-07 01:00:00", "10.250")))
    assert len(result.events) == 1
    event = result.events[0]
    assert event.value == pytest.approx(0.250)
    # Stamped at the interval's END: the reading whose counter closes it.
    assert event.timestamp == datetime(2026, 7, 6, 22, 0, tzinfo=UTC)


def test_the_first_reading_opens_an_interval_and_is_not_an_event() -> None:
    """N readings yield N-1 observations; the opener is consumed, not dropped."""
    result = normalize(eoa(("2026-07-07 00:00:00", "10.000")))
    assert result.events == []
    assert result.malformed == []


def test_a_gap_yields_no_event_rather_than_a_summed_one() -> None:
    """The 'gap-boundary false spike' the upstream project documents, refused by design."""
    result = normalize(
        eoa(
            ("2026-07-07 00:00:00", "10.000"),
            # 01:00 is missing entirely
            ("2026-07-07 02:00:00", "10.900"),
            ("2026-07-07 03:00:00", "11.000"),
        )
    )
    # Only 02:00 -> 03:00 closes an hourly interval. The 0.9 accumulated across the gap is
    # never attributed to a single hour.
    assert [event.value for event in result.events] == [pytest.approx(0.100)]
    assert result.malformed == []


def test_a_counter_rollback_is_quarantined_not_emitted() -> None:
    result = normalize(
        eoa(
            ("2026-07-07 00:00:00", "10.000"),
            ("2026-07-07 01:00:00", "10.500"),
            ("2026-07-07 02:00:00", "3.000"),  # meter change / reset
            ("2026-07-07 03:00:00", "3.400"),
        )
    )
    assert [event.value for event in result.events] == [
        pytest.approx(0.500),
        pytest.approx(0.400),
    ]
    assert len(result.malformed) == 1
    assert "decreased" in result.malformed[0].error
    # Real data, captured with its reason — never silently dropped (ADR-0004).
    assert "3.000" in result.malformed[0].raw


def test_newest_first_rows_are_sorted_before_differencing() -> None:
    """Unsorted, every difference would be negated and read as a rollback."""
    result = normalize(
        eoa(
            ("2026-07-07 00:00:00", "10.000"),
            ("2026-07-07 01:00:00", "10.100"),
            ("2026-07-07 02:00:00", "10.300"),
        )
    )
    assert [event.value for event in result.events] == [
        pytest.approx(0.100),
        pytest.approx(0.200),
    ]
    assert result.malformed == []


def test_naive_local_time_is_converted_using_the_dialect_timezone() -> None:
    # Cyprus is UTC+3 in July, so 12:00 local is 09:00 UTC.
    result = normalize(eoa(("2026-07-07 11:00:00", "1.000"), ("2026-07-07 12:00:00", "1.100")))
    assert result.events[0].timestamp == datetime(2026, 7, 7, 9, 0, tzinfo=UTC)


def test_source_timezone_can_be_overridden_per_source() -> None:
    result = normalize(
        eoa(("2026-07-07 11:00:00", "1.000"), ("2026-07-07 12:00:00", "1.100")),
        source_timezone="UTC",
    )
    assert result.events[0].timestamp == datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def test_an_ambiguous_local_hour_is_refused_rather_than_guessed() -> None:
    """A DST fall-back hour happens twice; placing it wrongly moves a reading a full hour,
    which is this data's entire resolution."""
    # 2026-10-25 03:00-04:00 local is the Europe/Nicosia autumn transition.
    result = normalize(
        eoa(
            ("2026-10-25 02:30:00", "5.000"),
            ("2026-10-25 03:30:00", "5.100"),
        )
    )
    assert any("ambiguous" in record.error for record in result.malformed)


def test_a_bad_source_timezone_fails_at_construction() -> None:
    with pytest.raises(ValueError, match="unknown source timezone"):
        MeterNormalizer(source_timezone="Mars/Olympus_Mons")


def test_metadata_carries_the_counter_so_the_boundary_is_invertible() -> None:
    result = normalize(eoa(("2026-07-07 00:00:00", "10.000"), ("2026-07-07 01:00:00", "10.250")))
    reading = MeterReading.from_metadata(result.events[0].metadata[METADATA_KEY])
    assert reading is not None
    assert reading.counter == pytest.approx(10.250)
    assert reading.meter_type is MeterType.WATER
    assert reading.unit == "m3"
    assert reading.interval_hours == 1.0


def test_sensipro_dialect_reads_the_unit_per_row() -> None:
    text = (
        "device_id,from_timestamp,to_timestamp,first_value,last_value,diff_last_first,unit\n"
        "SP-1,2026-07-07 00:00:00,2026-07-07 00:59:59,100.0,100.5,0.5,m3\n"
        "SP-1,2026-07-07 01:00:00,2026-07-07 01:59:59,100.5,101.0,0.5,m3\n"
        "SP-2,2026-07-07 00:00:00,2026-07-07 00:59:59,8000.0,8055.0,55.0,kWh\n"
        "SP-2,2026-07-07 01:00:00,2026-07-07 01:59:59,8055.0,8110.0,55.0,kWh\n"
    )
    result = normalize(text, SENSIPRO)
    by_asset = {event.asset_id: event for event in result.events}
    assert by_asset["SP-1"].metric == "hourly_consumption_m3"
    assert by_asset["SP-2"].metric == "hourly_consumption_kwh"
    assert by_asset["SP-2"].value == pytest.approx(55.0)


def test_a_non_consumption_unit_is_quarantined() -> None:
    text = (
        "device_id,from_timestamp,to_timestamp,first_value,last_value,diff_last_first,unit\n"
        "SP-3,2026-07-07 00:00:00,2026-07-07 00:59:59,5.0,6.0,1.0,V\n"
    )
    result = normalize(text, SENSIPRO)
    assert result.events == []
    assert "not a consumption unit" in result.malformed[0].error


def test_meters_are_differenced_independently() -> None:
    """Two meters in one file must not be differenced against each other."""
    text = (
        "device_id,from_timestamp,to_timestamp,first_value,last_value,diff_last_first,unit\n"
        "SP-1,2026-07-07 00:00:00,2026-07-07 00:59:59,100.0,100.5,0.5,m3\n"
        "SP-2,2026-07-07 00:00:00,2026-07-07 00:59:59,9000.0,9000.5,0.5,m3\n"
        "SP-1,2026-07-07 01:00:00,2026-07-07 01:59:59,100.5,101.0,0.5,m3\n"
        "SP-2,2026-07-07 01:00:00,2026-07-07 01:59:59,9000.5,9001.0,0.5,m3\n"
    )
    result = normalize(text, SENSIPRO)
    assert sorted(event.asset_id for event in result.events) == ["SP-1", "SP-2"]
    assert all(event.value == pytest.approx(0.5) for event in result.events)
    assert result.malformed == []


def test_a_duplicated_row_is_quarantined_not_counted_twice() -> None:
    result = normalize(
        eoa(
            ("2026-07-07 00:00:00", "10.000"),
            ("2026-07-07 01:00:00", "10.100"),
            ("2026-07-07 01:00:00", "10.100"),
        )
    )
    assert len(result.events) == 1
    assert any("duplicate timestamp" in record.error for record in result.malformed)


def test_a_negative_counter_is_refused() -> None:
    result = normalize(eoa(("2026-07-07 00:00:00", "-1.000")))
    assert "negative" in result.malformed[0].error


def test_header_missing_required_columns_rejects_the_data() -> None:
    text = "params,id,deviceId\n{},1,M-1\n"
    result = normalize(text)
    assert result.events == []
    # The header is reported once, then each orphaned data row.
    assert len(result.malformed) == 2
    assert "missing required column" in result.malformed[0].error


def test_a_semicolon_export_with_comma_decimals_is_read() -> None:
    """European exports pair ';' delimiters with ',' decimals — the two travel together, and
    splitting on ',' first makes the comma-decimal handling unreachable (found by testing:
    every such row failed the field-count check instead)."""
    text = (
        "params;id;updateAt;deviceId;flow;dataTime;signalCsq;isWaring;paValue\n"
        "{};1;2026-07-07 09:00:00;M-1;10,000;2026-07-07 00:00:00;0;;\n"
        "{};2;2026-07-07 09:00:00;M-1;10,250;2026-07-07 01:00:00;0;;\n"
    )
    result = normalize(text)
    assert [event.value for event in result.events] == [pytest.approx(0.250)]
    assert result.malformed == []


def test_a_cadence_mismatch_is_reported_rather_than_silently_empty() -> None:
    """A 15-minute export yields no hourly interval. Saying nothing would make "I cannot read
    this" indistinguishable from "nothing was anomalous"."""
    result = normalize(
        eoa(
            ("2026-05-01 00:00:00", "1.000"),
            ("2026-05-01 00:15:00", "1.100"),
            ("2026-05-01 00:30:00", "1.200"),
        )
    )
    assert result.events == []
    assert len(result.malformed) == 1
    assert "cadence" in result.malformed[0].error
    assert "15min" in result.malformed[0].error


def test_a_gappy_but_valid_meter_earns_no_cadence_complaint() -> None:
    """The cadence report must fire only when a meter yielded nothing at all."""
    result = normalize(
        eoa(
            ("2026-05-01 00:00:00", "1.000"),
            ("2026-05-01 01:00:00", "1.100"),
            ("2026-05-09 00:00:00", "9.000"),
        )
    )
    assert len(result.events) == 1
    assert result.malformed == []


def test_every_dialect_declares_a_distinct_format() -> None:
    assert set(DIALECTS) == {EOA, SENSIPRO}
    assert len({dialect.name for dialect in DIALECTS.values()}) == len(DIALECTS)
