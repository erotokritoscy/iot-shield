"""A rate as a counter: it must round-trip through the real normalizer, gaps included."""

from datetime import UTC, datetime, timedelta

import pytest

from iotshield.convert._rates import HEADER, SOURCE_FORMAT, counter_rows, representable
from iotshield.normalize.meter import MeterNormalizer

T0 = datetime(2025, 3, 1, tzinfo=UTC)


def at(hour: int) -> datetime:
    return T0 + timedelta(hours=hour)


def normalize(lines: list[str]):
    text = "\n".join([HEADER, *lines]) + "\n"
    return MeterNormalizer().normalize(text, source_format=SOURCE_FORMAT, origin="test")


def test_every_reading_becomes_exactly_one_event() -> None:
    rates = [1.5, 0.25, 0.0, 12.125]
    result = normalize(
        counter_rows([(at(i), r) for i, r in enumerate(rates)], meter_id="m", unit="m3")
    )
    assert not result.malformed
    assert [event.timestamp for event in result.events] == [at(i) for i in range(4)]
    assert [event.value for event in result.events] == pytest.approx(rates)


def test_a_one_hour_gap_never_becomes_an_invented_zero() -> None:
    """The opener would abut the previous reading, and the pair would read as a zero hour."""
    readings = [(at(0), 1.0), (at(2), 5.0), (at(3), 6.0)]
    kept = representable(readings)
    assert kept == [(at(0), 1.0), (at(3), 6.0)]
    result = normalize(counter_rows(kept, meter_id="m", unit="m3"))
    assert not result.malformed
    assert [(e.timestamp, e.value) for e in result.events] == [
        (at(0), pytest.approx(1.0)),
        (at(3), pytest.approx(6.0)),
    ]


def test_counter_rows_refuses_a_one_hour_gap_outright() -> None:
    with pytest.raises(ValueError, match="representable"):
        counter_rows([(at(0), 1.0), (at(2), 5.0)], meter_id="m", unit="m3")


def test_dropping_a_reading_is_measured_against_the_last_one_kept() -> None:
    readings = [(at(0), 1.0), (at(2), 2.0), (at(4), 3.0), (at(6), 4.0)]
    assert [stamp for stamp, _ in representable(readings)] == [at(0), at(4)]


def test_the_reading_after_a_longer_gap_survives() -> None:
    """The defect in upstream's cumsum: the hour after a hole has nothing to difference against."""
    readings = [(at(0), 1.0), (at(1), 2.0), (at(5), 7.0), (at(6), 3.0)]
    result = normalize(counter_rows(readings, meter_id="m", unit="m3"))
    assert not result.malformed
    assert [(e.timestamp, e.value) for e in result.events] == [
        (at(0), pytest.approx(1.0)),
        (at(1), pytest.approx(2.0)),
        (at(5), pytest.approx(7.0)),
        (at(6), pytest.approx(3.0)),
    ]


def test_the_gap_stays_an_absence() -> None:
    """The opener is stamped inside the gap and must not become a claim about that hour."""
    result = normalize(counter_rows([(at(0), 1.0), (at(5), 7.0)], meter_id="m", unit="m3"))
    assert at(4) not in {event.timestamp for event in result.events}


def test_a_zero_hour_is_an_exact_zero() -> None:
    """A stuck check compares with 0, so float residue would blind it."""
    readings = [(at(i), 0.1 if i % 3 else 0.0) for i in range(300)]
    result = normalize(counter_rows(readings, meter_id="m", unit="m3"))
    zeros = [event for event, (_, rate) in zip(result.events, readings, strict=True) if rate == 0]
    assert zeros
    assert all(event.value == 0.0 for event in zeros)


def test_the_unit_decides_the_utility() -> None:
    result = normalize(counter_rows([(at(0), 1.0)], meter_id="m", unit="kwh"))
    assert (result.events[0].metric or "").endswith("kwh")


@pytest.mark.parametrize(
    ("readings", "message"),
    [
        ([(datetime(2025, 1, 1), 1.0)], "naive"),
        ([(at(0), -1.0)], "negative"),
        ([(at(1), 1.0), (at(0), 1.0)], "time-ordered"),
        ([(at(0), 1.0), (at(0), 1.0)], "time-ordered"),
    ],
)
def test_input_a_counter_cannot_express_is_refused(readings, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        counter_rows(readings, meter_id="m", unit="m3")
