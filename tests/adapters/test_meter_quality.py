"""meter_quality: SensiPro's heartbeat and high anomalies as detections (ADR-0043).

The port suite holds the shared laws. These pin the two rules, the arithmetic between a
missed reading and the silence it leaves (checked end to end through the real meter
normalizer, since the normalizer's gap handling is what produces that silence), and the
per-utility limits.
"""

from datetime import UTC, datetime, timedelta

import pytest

from iotshield.contracts import EventKind, Lane, NormalizedEvent
from iotshield.detect import MeterQualityDetector
from iotshield.enrichment import EVIDENCE_KEY, Enrichment
from iotshield.meter import METADATA_KEY, MeterReading, MeterType, metric_for
from iotshield.pipeline.registry import build_detector, build_normalizer

T0 = datetime(2023, 7, 1, tzinfo=UTC)


def reading(
    hour: int, used: float, *, kind: MeterType = MeterType.ELECTRICITY, asset: str = "e1"
) -> NormalizedEvent:
    unit = "kwh" if kind is MeterType.ELECTRICITY else "m3"
    meter = MeterReading(meter_type=kind, unit=unit, counter=100.0 + hour, interval_hours=1.0)
    return NormalizedEvent(
        timestamp=T0 + timedelta(hours=hour),
        asset_id=asset,
        source_type=Lane.TELEMETRY,
        source_format="sensipro-hourly-csv",
        metric=metric_for(unit),
        value=used,
        raw=f"{asset},{hour}",
        metadata={METADATA_KEY: meter.to_metadata()},
    )


def hourly(hours: range, used: float = 1.0, **kwargs: object) -> list[NormalizedEvent]:
    return [reading(hour, used, **kwargs) for hour in hours]  # type: ignore[arg-type]


def detect(events: list[NormalizedEvent], **params: float) -> list:  # type: ignore[type-arg]
    return MeterQualityDetector(**params).detect(events)


class TestHeartbeat:
    def test_steady_hourly_readings_raise_nothing(self) -> None:
        assert detect(hourly(range(48))) == []

    def test_a_silence_becomes_one_detection_spanning_it(self) -> None:
        events = hourly(range(10)) + hourly(range(15, 20))
        (found,) = detect(events)
        assert found.evidence["quality"] == "heartbeat"
        assert (found.window_start, found.window_end) == (
            T0 + timedelta(hours=9),
            T0 + timedelta(hours=15),
        )
        assert found.score == pytest.approx(6.0) and found.threshold == pytest.approx(2.0)
        assert found.evidence["sensipro_code"] == [1, 0]
        assert "reported nothing for 6 h" in found.evidence["explanation"]

    def test_one_missed_reading_through_the_real_normalizer_is_caught_and_counted(self) -> None:
        # Hour 05:00 is missing. The normalizer emits no event for 06:00 either (it only
        # re-opens the interval), so the neighbouring events are 04:00 and 07:00.
        rows = [
            f"e1,2023-07-01 {hour:02d}:00:00,{100 + hour}.0,kWh" for hour in range(10) if hour != 5
        ]
        text = "device_id,from_timestamp,last_value,unit\n" + "\n".join(rows) + "\n"
        events = (
            build_normalizer("meter")
            .normalize(text, source_format="sensipro-hourly-csv", origin="t")
            .events
        )
        (found,) = build_detector("meter_quality", {}).detect(events)
        assert found.evidence["quality"] == "heartbeat"
        assert found.evidence["missed_readings"] == 1
        assert found.score == pytest.approx(3.0)

    def test_the_allowed_silence_is_a_param(self) -> None:
        events = hourly(range(5)) + hourly(range(9, 12))
        assert detect(events, max_silence_hours=5.0) == []
        assert len(detect(events, max_silence_hours=3.0)) == 1


class TestHigh:
    def test_an_hour_over_the_electricity_limit_is_flagged(self) -> None:
        events = [*hourly(range(5)), reading(5, 40.0), *hourly(range(6, 10))]
        (found,) = detect(events)
        assert found.evidence["quality"] == "high"
        assert found.event_kind is EventKind.POINT
        assert (found.score, found.threshold) == (40.0, 35.0)
        assert found.evidence["sensipro_code"] == [2, 3]
        assert "above the limit of 35 kwh" in found.evidence["explanation"]

    def test_consecutive_high_hours_are_one_finding_with_the_peak(self) -> None:
        surge = [reading(3, 36.0), reading(4, 50.0), reading(5, 37.0)]
        events = [*hourly(range(3)), *surge, *hourly(range(6, 9))]
        (found,) = detect(events)
        assert found.event_kind is EventKind.COLLECTIVE
        assert found.score == 50.0 and found.evidence["hours"] == 3
        assert (found.window_start, found.window_end) == (
            T0 + timedelta(hours=3),
            T0 + timedelta(hours=5),
        )

    def test_each_utility_is_judged_against_its_own_limit(self) -> None:
        # 0.5 m3 is far over the water limit; 30 kWh is under the electricity one.
        water = [
            *hourly(range(3), 0.01, kind=MeterType.WATER, asset="w1"),
            reading(3, 0.5, kind=MeterType.WATER, asset="w1"),
        ]
        power = hourly(range(4), 30.0, asset="e1")
        found = detect(water + power)
        assert [(item.asset_id, item.evidence["quality"]) for item in found] == [("w1", "high")]
        assert found[0].threshold == pytest.approx(0.075)

    def test_the_limits_are_params(self) -> None:
        events = hourly(range(4), 30.0)
        assert len(detect(events, high_kwh=25.0)) == 1


class TestConstruction:
    @pytest.mark.parametrize(
        "params", [{"high_kwh": 0.0}, {"high_m3": -1.0}, {"max_silence_hours": float("nan")}]
    )
    def test_a_non_positive_or_non_finite_param_is_refused(self, params: dict[str, float]) -> None:
        with pytest.raises(ValueError, match="positive, finite"):
            MeterQualityDetector(**params)

    def test_the_registry_builds_it_with_quoted_yaml_numbers(self) -> None:
        detector = build_detector("meter_quality", {"high_kwh": "10"})
        assert len(detector.detect(hourly(range(3), 12.0))) == 1

    def test_findings_carry_a_threat_class_and_actions_but_no_attack_technique(self) -> None:
        events = hourly(range(3)) + hourly(range(8, 9)) + [reading(9, 99.0)]
        for found in detect(events):
            enrichment = Enrichment.from_evidence(found.evidence[EVIDENCE_KEY])
            assert enrichment is not None
            assert enrichment.threat_class.startswith("Meter data quality")
            assert enrichment.suggested_actions and enrichment.mitre == ()
