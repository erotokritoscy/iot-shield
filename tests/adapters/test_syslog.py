"""Syslog-specific behavior (dialect dispatch, timezone policy, severity mapping).

Port-level invariants (conservation, aware-UTC, verbatim raw, unsupported format)
live in tests/ports/test_normalizer_port.py — these cover only what is particular
to the syslog adapter itself, especially the two behaviors that distinguish our
rewrite from team4's original: bad dates become rejects (not a fabricated "now"),
and zone-less RFC 3164 timestamps are attributed UTC (ADR-0011).
"""

from datetime import UTC, datetime

from iotshield.contracts import Lane, Severity
from iotshield.normalize import SYSLOG_SOURCE_FORMAT, NormalizeResult, SyslogNormalizer

ORIGIN = "test://syslog"


def _normalize(text: str) -> NormalizeResult:
    return SyslogNormalizer().normalize(text, source_format=SYSLOG_SOURCE_FORMAT, origin=ORIGIN)


class TestSyslogNormalizer:
    def test_rfc5424_offset_normalized_to_utc(self) -> None:
        line = "<34>1 2026-07-07T12:00:00+02:00 host-a su - - - failed password"
        (event,) = _normalize(line).events
        # +02:00 wall time becomes its UTC instant.
        assert event.timestamp == datetime(2026, 7, 7, 10, 0, 0, tzinfo=UTC)
        assert event.asset_id == "host-a"
        assert event.source_type == Lane.LOG
        assert event.source_format == "syslog-rfc5424"
        assert event.value is None
        assert event.metric is None
        assert event.raw == line

    def test_rfc5424_severity_from_priority(self) -> None:
        # PRI 34 -> severity 34 & 0x07 = 2 -> CRITICAL.
        (event,) = _normalize("<34>1 2026-07-07T12:00:00Z host-a su - - - x").events
        assert event.severity == Severity.CRITICAL

    def test_rfc3164_naive_timestamp_attributed_utc_and_current_year(self) -> None:
        line = "Jul  7 12:00:02 host-b sshd[1234]: Accepted publickey for admin"
        (event,) = _normalize(line).events
        # No year in RFC 3164, no zone -> current year, UTC (the D2 policy).
        assert event.timestamp == datetime(datetime.now(UTC).year, 7, 7, 12, 0, 2, tzinfo=UTC)
        assert event.source_format == "syslog-rfc3164"

    def test_rfc3164_severity_guessed_from_message(self) -> None:
        # No PRI present -> keyword heuristic; "out of memory" -> CRITICAL.
        line = "Jul  7 12:00:02 host-b kernel: out of memory: killed process"
        (event,) = _normalize(line).events
        assert event.severity == Severity.CRITICAL

    def test_unrecognized_line_becomes_malformed(self) -> None:
        result = _normalize("this is not syslog at all")
        assert result.events == []
        (record,) = result.malformed
        assert record.origin == ORIGIN
        # Rejects carry the incoming format, not a per-dialect guess.
        assert record.source_format == SYSLOG_SOURCE_FORMAT

    def test_bad_timestamp_becomes_malformed_not_fabricated(self) -> None:
        # The key improvement over team4: an unreadable date is a reject, not now().
        result = _normalize("<34>1 not-a-timestamp host-a su - - - bad date")
        assert result.events == []
        (record,) = result.malformed
        assert "timestamp" in record.error
