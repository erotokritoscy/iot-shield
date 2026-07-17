"""LOG-burst behaviour: what each preset matches, and how bursts become episodes.

Port-level invariants (lane/method stamping, windows inside the input range, strict
score > threshold, determinism, foreign-lane isolation) live in
tests/ports/test_detector_port.py — these cover what is particular to this adapter: the
preset predicates, the sliding window, and the episode merge that keeps one incident one
detection (ADR-0020).
"""

from datetime import UTC, datetime, timedelta

import pytest

from iotshield.contracts import EventKind, Lane, NormalizedEvent, Severity
from iotshield.detect import LogBurstDetector

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)
_AUTH_FAIL = "Failed password for root from 10.0.0.9 port 52344 ssh2"


def log_event(
    *,
    offset_s: int,
    message: str = _AUTH_FAIL,
    severity: Severity = Severity.INFO,
    asset_id: str = "gateway-01",
) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id=asset_id,
        source_type=Lane.LOG,
        source_format="syslog-rfc5424",
        message=message,
        severity=severity,
        raw=f"<38>1 ... {message}",
    )


def auth_burst() -> LogBurstDetector:
    return LogBurstDetector(preset="auth_failure", window_minutes=1.0, min_count=5)


def severity_rule() -> LogBurstDetector:
    return LogBurstDetector(preset="severity_spike", window_minutes=5.0, min_count=3)


class TestAuthFailurePreset:
    def test_burst_becomes_one_collective_detection(self) -> None:
        (detection,) = auth_burst().detect([log_event(offset_s=i * 5) for i in range(6)])
        assert detection.lane is Lane.LOG
        assert detection.method == "log_burst:auth_failure"
        assert detection.asset_id == "gateway-01"
        # A burst is several events by construction — never a point anomaly.
        assert detection.event_kind is EventKind.COLLECTIVE
        assert detection.window_start == T0
        assert detection.window_end == T0 + timedelta(seconds=25)
        assert detection.score == 6.0
        # The exceedance boundary: the largest count still normal (min_count - 1).
        assert detection.threshold == 4.0

    def test_below_min_count_is_silent(self) -> None:
        # Four mistyped passwords are not a brute force.
        assert auth_burst().detect([log_event(offset_s=i * 5) for i in range(4)]) == []

    def test_matches_spread_wider_than_the_window_are_silent(self) -> None:
        # Six failures, but 30s apart -> never five inside a 1-minute window. Rate is the
        # signal; a raw count without the window would alert on a slow trickle.
        assert auth_burst().detect([log_event(offset_s=i * 30) for i in range(6)]) == []

    def test_non_auth_messages_do_not_count(self) -> None:
        events = [log_event(offset_s=i * 5) for i in range(5)]
        events.append(log_event(offset_s=30, message="Accepted password for admin from 10.0.0.9"))
        (detection,) = auth_burst().detect(events)
        assert detection.score == 5.0
        # The episode ends at the last *failure*, not at the successful login.
        assert detection.window_end == T0 + timedelta(seconds=20)

    def test_fires_at_info_severity_where_a_severity_rule_is_blind(self) -> None:
        # The reason a content preset exists: sshd logs failures at INFO, so the
        # format-agnostic severity rule cannot see the burst that matters most.
        events = [log_event(offset_s=i * 5, severity=Severity.INFO) for i in range(5)]
        assert auth_burst().detect(events)
        severity_rule = LogBurstDetector(preset="severity_spike", window_minutes=1.0, min_count=5)
        assert severity_rule.detect(events) == []


class TestSeveritySpikePreset:
    def test_counts_error_and_worse_only(self) -> None:
        events = [
            log_event(offset_s=0, message="disk pressure", severity=Severity.WARNING),
            log_event(offset_s=5, message="disk failure", severity=Severity.ERROR),
            log_event(offset_s=10, message="disk failure", severity=Severity.ERROR),
            log_event(offset_s=15, message="halted", severity=Severity.CRITICAL),
            log_event(offset_s=20, message="heartbeat", severity=Severity.INFO),
        ]
        (detection,) = severity_rule().detect(events)
        assert detection.method == "log_burst:severity_spike"
        assert detection.score == 3.0  # two errors + one critical; warning/info excluded
        assert detection.window_start == T0 + timedelta(seconds=5)
        assert detection.window_end == T0 + timedelta(seconds=15)
        assert detection.evidence["severities"] == ["critical", "error"]

    def test_severity_floor_is_ranked_not_lexical(self) -> None:
        # Severity is a StrEnum, where a text comparison puts "critical" < "debug" — a
        # lexical floor would silently drop the single most severe class of event.
        events = [
            log_event(offset_s=i * 5, message="halted", severity=Severity.CRITICAL)
            for i in range(3)
        ]
        assert severity_rule().detect(events)


class TestEpisodes:
    def test_one_sustained_burst_stays_one_detection(self) -> None:
        # 90 attempts, one per second -> ONE alert, not 90. This anti-fragmentation is
        # exactly what team3's IsolationForest failed at (91 events for one fault).
        (detection,) = auth_burst().detect([log_event(offset_s=i) for i in range(90)])
        assert detection.score == 61.0  # the peak count in any 60s window
        assert detection.window_start == T0
        assert detection.window_end == T0 + timedelta(seconds=89)

    def test_bursts_separated_by_a_quiet_gap_are_separate_detections(self) -> None:
        first = [log_event(offset_s=i * 5) for i in range(5)]
        second = [log_event(offset_s=600 + i * 5) for i in range(5)]
        detections = auth_burst().detect(first + second)
        assert len(detections) == 2
        assert detections[0].window_start == T0
        assert detections[1].window_start == T0 + timedelta(seconds=600)

    def test_assets_are_counted_independently(self) -> None:
        # Three failures each on two hosts is not a six-failure burst on either.
        events = [log_event(offset_s=i * 5, asset_id="gateway-01") for i in range(3)]
        events += [log_event(offset_s=i * 5, asset_id="gateway-02") for i in range(3)]
        assert auth_burst().detect(events) == []

    def test_unsorted_input_still_detects(self) -> None:
        # The runner hands detectors (asset, time)-ordered events (ADR-0019), but the port
        # promises a standalone caller nothing, so the detector sorts defensively.
        events = [log_event(offset_s=i * 5) for i in range(6)]
        (detection,) = auth_burst().detect(list(reversed(events)))
        assert detection.window_start == T0
        assert detection.window_end == T0 + timedelta(seconds=25)


class TestEvidence:
    def test_evidence_carries_category_and_capped_samples(self) -> None:
        (detection,) = auth_burst().detect([log_event(offset_s=i * 5) for i in range(6)])
        evidence = detection.evidence
        assert evidence["category"] == "auth_failure"  # D6: the label rides in evidence
        assert evidence["matched_events"] == 6
        assert evidence["peak_in_window"] == 6
        assert evidence["window_minutes"] == 1.0
        # Samples are for the analyst, not a copy of the burst.
        assert len(evidence["sample_messages"]) == 3
        assert all("Failed password" in message for message in evidence["sample_messages"])


class TestParams:
    def test_unknown_preset_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="unknown log_burst preset"):
            LogBurstDetector(preset="nope")

    def test_min_count_below_two_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="min_count must be >= 2"):
            LogBurstDetector(min_count=1)

    def test_non_positive_window_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="window_minutes must be positive"):
            LogBurstDetector(window_minutes=0)
