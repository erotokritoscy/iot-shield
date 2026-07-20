"""Ground truth: where the anomalies really were.

The one ingredient the product never has. A detection pipeline exists because nobody knows
where production anomalies are; a *labelled corpus* is a lab artifact, recorded by whoever
broke the equipment on purpose. Everything else in this package is machinery around
comparing our output to it (ADR-0021 D1).

Truth arrives as a **row timeline** — one ``(timestamp, is_anomalous)`` per source row, the
shape SKAB's ``anomaly`` column and our labels JSON both have — and is used two ways:

- the timeline itself scores **point** metrics ("of the anomalous rows, how many did we
  cover?"), which is what published benchmarks report;
- collapsed into **incidents**, it scores **event** metrics ("of the real faults, how many
  did we catch, and how many alerts did each cost?"), which is what an analyst experiences.

The collapse uses ``detect.merge_runs`` — the same function a row-scoring detector uses to
form its own judgments — so truth and detections are built by an identical rule. That is
what makes the event-level comparison apples-to-apples rather than an artifact of two
different definitions of "one incident".
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from iotshield.detect import merge_runs


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Whether two closed intervals touch — the one place the boundary rule is decided.

    **Closed on both ends**, because a point anomaly is a zero-length window (ADR-0003): a
    half-open test would silently never match one. Stated once because both scoring levels
    need it (event matching compares two windows; point coverage asks whether an instant
    falls inside one, which is the zero-length case), and a half-open edit in one of two
    copies would corrupt one metric while leaving the other right.
    """
    return a_start <= b_end and a_end >= b_start


@dataclass(frozen=True)
class Incident:
    """One real fault, as an inclusive time interval on an asset's timeline."""

    start: datetime
    end: datetime

    def overlaps(self, window_start: datetime, window_end: datetime) -> bool:
        """Whether a detection/alert window touches this incident."""
        return overlaps(window_start, window_end, self.start, self.end)


@dataclass(frozen=True)
class GroundTruth:
    """A case's answer key: the labelled row timeline plus the incidents it collapses to."""

    timeline: tuple[tuple[datetime, bool], ...]
    incidents: tuple[Incident, ...]

    @classmethod
    def from_timeline(cls, rows: Sequence[tuple[datetime, bool]]) -> "GroundTruth":
        """Build truth from per-row labels, collapsing runs of anomalous rows into incidents.

        Rows are sorted by timestamp first: a corpus file is normally already in order, but
        the incident boundaries would be silently wrong if it were not, and a wrong answer
        key is worse than a loud failure.
        """
        ordered = tuple(sorted(rows, key=lambda row: row[0]))
        flags = [flagged for _, flagged in ordered]
        incidents = tuple(
            Incident(start=ordered[start][0], end=ordered[end][0])
            for start, end in merge_runs(flags)
        )
        return cls(timeline=ordered, incidents=incidents)

    @classmethod
    def from_incidents(
        cls, timestamps: Sequence[datetime], incidents: Sequence[Incident]
    ) -> "GroundTruth":
        """Build truth from *intervals* plus the row clock — the storage form (ADR-0021 D5).

        The inverse of ``from_timeline``, and the form a corpus bundle records, for two
        reasons. It is far smaller: per-row booleans spend ~50 bytes to say "normal" about
        a row the readings already list. And it is strictly *more* expressive — two
        incidents that touch with no quiet row between them are one run of flags but two
        intervals, a distinction per-row labels cannot represent and a synthetic corpus can
        legitimately produce.
        """
        ordered = sorted(timestamps)
        rows = [
            (moment, any(incident.start <= moment <= incident.end for incident in incidents))
            for moment in ordered
        ]
        return cls(timeline=tuple(rows), incidents=tuple(incidents))

    @property
    def anomalous_rows(self) -> int:
        return sum(1 for _, flagged in self.timeline if flagged)

    @property
    def is_clean(self) -> bool:
        """A recording with no fault at all — every alert on it is a false alarm."""
        return not self.incidents
