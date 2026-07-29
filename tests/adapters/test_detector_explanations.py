"""What a detection SAYS: the sentence must match the number, and a preset must not overreach.

`evidence["explanation"]` is the one field the port suite requires of every adapter and the
one both dashboards name a finding by, so a sentence that misstates its own detection is a
product defect rather than a cosmetic one (ADR-0032 D7 + amendment, ADR-0033).
"""

from datetime import UTC, datetime, timedelta

import pytest

from iotshield.contracts import Lane, NormalizedEvent, Severity
from iotshield.detect.log_burst import LogBurstDetector
from iotshield.detect.stub import StubThresholdDetector

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def _log(message: str, offset_s: int) -> NormalizedEvent:
    return NormalizedEvent(
        timestamp=T0 + timedelta(seconds=offset_s),
        asset_id="gw-01",
        source_type=Lane.LOG,
        source_format="syslog",
        message=message,
        severity=Severity.INFO,
        raw=message,
    )


class TestTheSentenceMatchesTheNumber:
    @pytest.mark.parametrize(
        ("value", "threshold"),
        [(80.00001, 80.0), (1234567.0, 80.0), (80.5, 80.0), (1e9, 5e8)],
    )
    def test_a_threshold_explanation_never_rounds_the_two_numbers_together(
        self, value: float, threshold: float
    ) -> None:
        """:g caps at six significant digits, so 80.00001 vs 80.0 read "80 is above the
        configured threshold of 80" — a sentence that contradicts itself."""
        event = NormalizedEvent(
            timestamp=T0,
            asset_id="pump-02",
            source_type=Lane.TELEMETRY,
            source_format="stub-csv",
            metric="Pressure",
            value=value,
            raw="row",
        )

        detection = StubThresholdDetector(threshold=threshold).detect([event])[0]
        explanation = detection.evidence["explanation"]

        assert f"{value:.12g}" in explanation
        assert f"{threshold:.12g}" in explanation
        assert "e+" not in explanation

    def test_a_byte_count_reads_as_a_number_not_an_exponent(self) -> None:
        """exfiltration's boundary is 50,000,000, so EVERY detection that preset can emit
        exceeded :g's six digits and rendered as "5e+07 bytes"."""
        assert f"{123456789.0:,.0f}" == "123,456,789"
        assert "e+" not in f"{50000001.0:,.0f}"


class TestAuthFailureMatchesAuthenticationOnly:
    def test_ordinary_application_text_does_not_raise_a_brute_force_alert(self) -> None:
        """`\\b4625\\b` matched any bare 4625 and `invalid user` matched "invalid user id",
        so five benign lines produced a confident Brute Force (T1110) claim."""
        benign = [
            "[api] rejected request: invalid user id 'a-b-c'",
            "[api] rejected request: invalid user id 'x-y-z'",
            "[api] rejected request: invalid user name (must be alphanumeric)",
            "[modbus] poll cycle took 4625 us",
            "[api] rejected request: invalid user id 'q'",
            "[jobs] job 4625 finished cleanly",
        ]
        events = [_log(message, 10 * i) for i, message in enumerate(benign)]

        detector = LogBurstDetector(preset="auth_failure", window_minutes=5.0, min_count=5)

        assert detector.detect(events) == []

    def test_a_real_ssh_brute_force_still_fires(self) -> None:
        """The preset must stay useful: sshd's actual shape is "Invalid user x from <ip>"."""
        attack = [f"sshd[123]: Invalid user admin{i} from 203.0.113.9 port 4{i}" for i in range(6)]
        events = [_log(message, 5 * i) for i, message in enumerate(attack)]

        detector = LogBurstDetector(preset="auth_failure", window_minutes=5.0, min_count=5)
        detections = detector.detect(events)

        assert len(detections) == 1
        assert detections[0].evidence["enrichment"]["threat_class"].startswith("Brute Force")

    @pytest.mark.parametrize(
        "message",
        [
            "sshd[1]: Failed password for root from 203.0.113.9 port 22",
            "pam_unix(sshd:auth): authentication failure; logname= uid=0",
            "Security: An account failed to log on.",
            "web: login failed for user bob",
        ],
    )
    def test_each_curated_phrasing_still_matches(self, message: str) -> None:
        events = [_log(message, 5 * i) for i in range(5)]

        detector = LogBurstDetector(preset="auth_failure", window_minutes=5.0, min_count=5)

        assert detector.detect(events)
