"""Record boundaries and clocks: three ways input used to be silently mis-read (ADR-0033).

Each case is one where the normalizer produced neither an event nor a MalformedRecord for
something it should have handled — the failure "quarantine, never drop" exists to prevent,
and the one no count in the run report can reveal.
"""

import json

import pytest

from iotshield.meter import MeterFieldError, MeterReading
from iotshield.normalize._linewise import split_lines
from iotshield.normalize.meter import MeterNormalizer
from iotshield.normalize.passthrough import PassThroughNormalizer

SENSIPRO_HEADER = (
    "device_id,from_timestamp,to_timestamp,first_value,last_value,diff_last_first,unit"
)

SPLITLINES_ONLY_BREAKS = [
    chr(code) for code in (0x2028, 0x2029, 0x0B, 0x0C, 0x1C, 0x1D, 0x1E, 0x85)
]
"""Every character ``str.splitlines`` treats as a record boundary and a real newline is not.

Built from code points rather than written literally: as source text they are invisible, and
U+2028/U+2029 are exactly the ambiguous characters a linter is right to reject anywhere a
human is meant to read them. LINE SEPARATOR is unescaped-legal inside a JSON string, and form
feed / NEL occur in real syslog payloads — so each one can sit inside a single valid record.
"""


def _sensipro(*rows: str) -> str:
    return "\n".join([SENSIPRO_HEADER, *rows])


class TestARecordEndsOnlyAtANewline:
    @pytest.mark.parametrize("separator", SPLITLINES_ONLY_BREAKS)
    def test_a_unicode_separator_inside_a_record_does_not_split_it(self, separator: str) -> None:
        """Splitting on these made one valid record into two bogus rejects, and the reject
        reason ("not valid JSON: Unterminated string") blamed the operator's data for our
        parsing. It also broke two port laws at once: neither one event nor *one* malformed
        record, and ``raw`` stopped being the verbatim input."""
        line = json.dumps(
            {
                "timestamp": "2026-01-01T00:00:00Z",
                "asset_id": "pump-02",
                "metric": "note",
                "value": 1.0,
                "message": f"before{separator}after",
            },
            ensure_ascii=False,
        )

        result = PassThroughNormalizer().normalize(
            line, source_format="normalized-jsonl", origin="partner"
        )

        assert len(result.events) == 1
        assert not result.malformed

    @pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
    def test_real_newlines_still_separate_records(self, newline: str) -> None:
        assert split_lines(f"a{newline}b") == ["a", "b"]


class TestTheMeterClockIsTheUtcTimeline:
    def test_the_spring_forward_hour_is_not_lost(self) -> None:
        """Both readings shared one ZoneInfo object, and CPython's datetime.__sub__ then
        subtracts naive WALL CLOCKS — so local 02:00 -> 04:00 measured two hours, failed the
        "exactly one interval apart" test, and one real hour of consumption vanished with no
        event and no reject. Once a year, per meter."""
        rows = [
            "deviceId,dataTime,flow",
            "m1,2026-03-29 01:00:00,100",
            "m1,2026-03-29 02:00:00,105",
            "m1,2026-03-29 04:00:00,110",
            "m1,2026-03-29 05:00:00,115",
        ]

        result = MeterNormalizer().normalize(
            "\n".join(rows), source_format="eoa-meter-csv", origin="eoa"
        )

        assert [event.value for event in result.events] == [5.0, 5.0, 5.0]
        assert not result.malformed

    def test_every_emitted_timestamp_is_utc(self) -> None:
        rows = [
            "deviceId,dataTime,flow",
            "m1,2026-06-01 01:00:00,100",
            "m1,2026-06-01 02:00:00,105",
        ]

        result = MeterNormalizer().normalize(
            "\n".join(rows), source_format="eoa-meter-csv", origin="eoa"
        )

        assert result.events
        for event in result.events:
            offset = event.timestamp.utcoffset()
            assert offset is not None and offset.total_seconds() == 0


class TestOneMeterIdIsOneUtility:
    def test_two_utilities_under_one_id_are_refused_not_differenced(self) -> None:
        """Grouping keyed on the id alone while the unit is resolved per row, so a water
        counter was differenced against an electricity one into a plausible phantom."""
        text = _sensipro(
            "SP-1,2026-06-01 01:00:00,,,100,,m3",
            "SP-1,2026-06-01 02:00:00,,,5000,,kWh",
            "SP-1,2026-06-01 03:00:00,,,5010,,kWh",
        )

        result = MeterNormalizer().normalize(
            text, source_format="sensipro-hourly-csv", origin="sensipro"
        )

        assert not result.events
        assert result.malformed
        assert "two utilities" in result.malformed[0].error

    def test_the_same_hour_reported_twice_is_not_called_a_duplicate_timestamp(self) -> None:
        """The dangerous mode: an entire utility disappeared under a message that
        misdiagnosed it, sending an operator after a duplicate-row bug that did not exist."""
        text = _sensipro(
            "SP-1,2026-06-01 01:00:00,,,100,,m3",
            "SP-1,2026-06-01 01:00:00,,,5000,,kWh",
            "SP-1,2026-06-01 02:00:00,,,102,,m3",
            "SP-1,2026-06-01 02:00:00,,,5010,,kWh",
        )

        result = MeterNormalizer().normalize(
            text, source_format="sensipro-hourly-csv", origin="sensipro"
        )

        reasons = {record.error for record in result.malformed}
        assert reasons
        assert all("two utilities" in reason for reason in reasons)
        assert not any("duplicate timestamp" in reason for reason in reasons)

    def test_one_utility_per_id_is_untouched(self) -> None:
        """The shipped demo shape: SP-4471 is water, SP-9302 is electricity."""
        text = _sensipro(
            "SP-4471,2026-06-01 01:00:00,,,100,,m3",
            "SP-4471,2026-06-01 02:00:00,,,102,,m3",
            "SP-9302,2026-06-01 01:00:00,,,5000,,kWh",
            "SP-9302,2026-06-01 02:00:00,,,5010,,kWh",
        )

        result = MeterNormalizer().normalize(
            text, source_format="sensipro-hourly-csv", origin="sensipro"
        )

        assert len(result.events) == 2
        assert not result.malformed


class TestAnIntervalWidthMustBeUsable:
    @pytest.mark.parametrize("width", [0, -1.0, float("inf"), float("nan")])
    def test_a_non_positive_or_non_finite_width_is_refused(self, width: float) -> None:
        """Zero reached pandas as freq=Timedelta(0) and took the whole run down with a
        ZeroDivisionError — neither NotFitted nor ValueError, so nothing read it as data."""
        with pytest.raises(MeterFieldError):
            MeterReading.from_metadata(
                {"meter_type": "water", "unit": "m3", "counter": 1.0, "interval_hours": width}
            )

    def test_an_ordinary_width_is_accepted(self) -> None:
        reading = MeterReading.from_metadata(
            {"meter_type": "water", "unit": "m3", "counter": 1.0, "interval_hours": 0.25}
        )

        assert reading.interval_hours == 0.25
