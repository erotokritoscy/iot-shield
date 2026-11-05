"""The baseline's own behaviour; the port suite holds its contract."""

from datetime import timedelta

import pytest

from iotshield.detect.meter_baseline import MeterBaselineDetector
from iotshield.enrichment import EVIDENCE_KEY, Enrichment
from iotshield.meter import SIGNATURE_KEY, Signature
from tests import meter_fixtures as fx


def rebuilt(events, change):
    """The same events with some hours' consumption replaced, counters kept consistent."""
    out = []
    counter = 100.0
    for hour, event in enumerate(events):
        used = change(hour, float(event.value or 0.0))
        counter = round(counter + used, 4)
        out.append(fx.meter_event(event.timestamp, used, counter))
    return out


def checks(detections) -> list[str]:
    return [detection.evidence["check"] for detection in detections]


def test_a_frozen_day_is_one_stuck_detection() -> None:
    found = [
        d
        for d in MeterBaselineDetector().detect(fx.stuck_events())
        if d.evidence["check"] == "stuck"
    ]
    assert len(found) == 1
    assert found[0].evidence[SIGNATURE_KEY] == Signature.STUCK
    assert found[0].score >= 12


def test_zeros_during_normally_idle_hours_are_not_stuck() -> None:
    """The fixture is idle overnight, every night, for 21 nights."""
    assert "stuck" not in checks(MeterBaselineDetector().detect(fx.clean_events()))


def test_a_short_freeze_is_below_the_line() -> None:
    events = rebuilt(fx.clean_events(), lambda h, v: 0.0 if 400 <= h < 405 else v)
    assert "stuck" not in checks(MeterBaselineDetector().detect(events))


def test_a_gap_breaks_a_stuck_run() -> None:
    """Twenty frozen hours with the middle missing are two runs of unknown, not one of zero."""
    events = rebuilt(fx.clean_events(), lambda h, v: 0.0 if 400 <= h < 420 else v)
    holed = [e for i, e in enumerate(events) if not 408 <= i < 412]
    detector = MeterBaselineDetector(stuck_min_hours=10)
    assert "stuck" not in checks(detector.detect(holed))


def test_a_surge_is_a_zscore_detection_and_one_run_is_one_finding() -> None:
    events = rebuilt(fx.clean_events(), lambda h, v: 1.0 if 400 <= h < 403 else v)
    found = [d for d in MeterBaselineDetector().detect(events) if d.evidence["check"] == "zscore"]
    assert len(found) == 1
    assert found[0].window_end - found[0].window_start == timedelta(hours=2)
    assert found[0].evidence[SIGNATURE_KEY] == Signature.SUSTAINED
    assert "above" in found[0].evidence["explanation"]


def test_one_hour_is_a_spike() -> None:
    events = rebuilt(fx.clean_events(), lambda h, v: 1.0 if h == 400 else v)
    found = [d for d in MeterBaselineDetector().detect(events) if d.evidence["check"] == "zscore"]
    assert [d.evidence[SIGNATURE_KEY] for d in found] == [Signature.SPIKE]


def test_clean_history_stays_mostly_quiet() -> None:
    assert len(MeterBaselineDetector().detect(fx.clean_events())) <= 2


def test_use_at_an_abnormal_time_is_a_profile_detection() -> None:
    # The z-score is raised out of the way so the check under test is the one that speaks.
    events = rebuilt(fx.clean_events(), lambda h, v: 0.2 if h == 24 * 15 + 2 else v)
    found = MeterBaselineDetector(z_threshold=1000.0).detect(events)
    assert checks(found) == ["profile"]
    assert found[0].evidence[SIGNATURE_KEY] == Signature.PROFILE
    assert "at night" in found[0].evidence["explanation"]


def test_an_hour_is_claimed_by_one_check_only() -> None:
    events = rebuilt(fx.clean_events(), lambda h, v: 5.0 if h == 400 else v)
    found = MeterBaselineDetector().detect(events)
    covering = [d for d in found if d.window_start <= events[400].timestamp <= d.window_end]
    assert len(covering) == 1


class TestLeakRule:
    # The fixture starts at midnight UTC. Nicosia is UTC+3 in April, so 02:00/03:00 local are
    # 23:00/00:00 UTC, and hour 23 is the last UTC hour of the first day.
    NIGHT = (24 * 15 + 23, 24 * 16)

    def night_flow(self):
        return rebuilt(fx.clean_events(), lambda h, v: 0.02 if h in self.NIGHT else v)

    def test_it_is_off_by_default(self) -> None:
        assert "leak" not in checks(MeterBaselineDetector().detect(self.night_flow()))

    def test_it_reads_the_night_on_the_sources_clock(self) -> None:
        found = MeterBaselineDetector(leak_m3=0.01).detect(self.night_flow())
        assert checks(found).count("leak") == 1

    def test_utc_would_have_read_a_different_night(self) -> None:
        found = MeterBaselineDetector(leak_m3=0.01, leak_timezone="UTC").detect(self.night_flow())
        assert "leak" not in checks(found)

    def test_one_of_the_two_hours_is_not_enough(self) -> None:
        events = rebuilt(fx.clean_events(), lambda h, v: 0.02 if h == self.NIGHT[0] else v)
        assert "leak" not in checks(MeterBaselineDetector(leak_m3=0.01).detect(events))


def test_a_constant_load_defeats_the_floor_and_removing_it_restores_the_check() -> None:
    """A building never idles, so any percentile sits near its load and hides a 2x surge."""
    import numpy as np

    rng = np.random.default_rng(3)
    base = 50.0 + rng.normal(0.0, 1.0, 24 * 21)
    base[400] = 100.0
    events = []
    counter = 0.0
    for hour, used in enumerate(base.tolist()):
        counter += used
        events.append(fx.meter_event(fx.T0 + timedelta(hours=hour), used, counter))
    assert MeterBaselineDetector().detect(events) == []
    assert MeterBaselineDetector(floor_percentile=0.05).detect(events) == []
    found = MeterBaselineDetector(floor_percentile=0.0).detect(events)
    assert any(d.window_start == events[400].timestamp for d in found)
    # Without a floor the rolling MAD is the scale, not a near-zero divisor flagging every hour.
    assert len(found) < len(events) / 20


def test_a_meter_with_too_little_history_is_skipped_and_named() -> None:
    events = fx.meter_events(100)
    detector = MeterBaselineDetector()
    assert detector.detect(events) == []
    assert fx.METER_ID in (detector.why_silent(events) or "")
    assert detector.why_silent(fx.clean_events()) is None


def test_every_check_carries_its_own_threat_knowledge() -> None:
    events = rebuilt(fx.stuck_events(), lambda h, v: 1.0 if h == 400 else v)
    for detection in MeterBaselineDetector().detect(events):
        enrichment = Enrichment.from_evidence(detection.evidence[EVIDENCE_KEY])
        assert enrichment.origin == f"meter_baseline:{detection.evidence['check']}"


@pytest.mark.parametrize(
    "bad",
    [
        {"z_threshold": 0.0},
        {"profile_k": float("nan")},
        {"stuck_min_hours": 0},
        {"window_hours": 0},
        {"min_history_hours": 0},
        {"floor_percentile": -0.1},
        {"floor_percentile": 1.5},
        {"leak_m3": -1.0},
        {"leak_timezone": "Mars/Olympus"},
    ],
)
def test_a_nonsense_setting_is_refused_at_construction(bad: dict) -> None:
    with pytest.raises(ValueError):
        MeterBaselineDetector(**bad)
