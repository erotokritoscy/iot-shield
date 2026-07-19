"""Ground truth: collapsing a labelled row timeline into incidents.

The answer key everything else is measured against, so its edges are pinned hard — a wrong
incident boundary would silently corrupt every event-level metric downstream.
"""

from datetime import UTC, datetime, timedelta

from iotshield.eval import GroundTruth, Incident

T0 = datetime(2026, 7, 7, 12, 0, 0, tzinfo=UTC)


def at(second: int) -> datetime:
    return T0 + timedelta(seconds=second)


def timeline(flags: list[bool]) -> GroundTruth:
    return GroundTruth.from_timeline([(at(i), flag) for i, flag in enumerate(flags)])


class TestCollapse:
    def test_consecutive_anomalous_rows_are_one_incident(self) -> None:
        truth = timeline([False, True, True, True, False])
        assert truth.incidents == (Incident(start=at(1), end=at(3)),)

    def test_a_quiet_gap_separates_incidents(self) -> None:
        truth = timeline([True, True, False, False, True])
        assert truth.incidents == (Incident(at(0), at(1)), Incident(at(4), at(4)))

    def test_a_single_row_is_a_zero_length_incident(self) -> None:
        # Mirrors the contract: a point anomaly is a zero-length window (ADR-0003).
        (incident,) = timeline([False, True, False]).incidents
        assert incident.start == incident.end == at(1)

    def test_a_fault_running_to_the_end_is_closed(self) -> None:
        (incident,) = timeline([False, True, True]).incidents
        assert incident.end == at(2)

    def test_clean_recording_has_no_incidents(self) -> None:
        truth = timeline([False, False, False])
        assert truth.incidents == ()
        assert truth.is_clean
        assert truth.anomalous_rows == 0

    def test_rows_are_sorted_before_collapsing(self) -> None:
        # A shuffled corpus file would otherwise produce silently wrong boundaries, and a
        # wrong answer key is worse than a loud failure.
        rows = [(at(2), True), (at(0), False), (at(1), True)]
        assert GroundTruth.from_timeline(rows).incidents == (Incident(at(1), at(2)),)


class TestOverlap:
    def test_touching_at_a_boundary_counts(self) -> None:
        # Closed on both ends: a zero-length detection exactly at the incident's edge is a
        # hit. A half-open test would silently never match a point anomaly.
        incident = Incident(at(10), at(20))
        assert incident.overlaps(at(20), at(20))
        assert incident.overlaps(at(10), at(10))

    def test_disjoint_windows_do_not_overlap(self) -> None:
        incident = Incident(at(10), at(20))
        assert not incident.overlaps(at(21), at(30))
        assert not incident.overlaps(at(0), at(9))

    def test_a_window_enclosing_the_incident_overlaps(self) -> None:
        assert Incident(at(10), at(20)).overlaps(at(0), at(99))
