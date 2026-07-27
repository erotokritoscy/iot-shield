"""Ground truth: where the anomalies really were, and *whose* they were.

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

## Why truth is per *asset* (ADR-0026)

This model originally held one timeline and one list of intervals, with no asset dimension,
and scoring compared detections to incidents on **time alone**. For a single-rig recording
that is complete, because with one device *time is identity*: there is only one thing that
could have been wrong.

A fleet destroys that. "Something was wrong at 00:05" cannot distinguish a scanning camera
from the innocent gateway beside it, so an innocent device tripping a detector inside the
incident window scored as a **correct catch**. Every metric downstream inherited the error.

So an incident names its asset, and a case's answer key is a list of ``AssetTruth`` — one
per device the corpus has an opinion about.

## The roster, and the difference between "innocent" and "unknown"

Which assets appear here is a **declaration**, never inferred from the readings. Three
states, and the middle one is the point:

- **listed, with incidents** — faulty in those windows, clean outside them.
- **listed, with no incidents** — *asserted clean*. Every detection on it is a false alarm.
  ``incidents: []`` in the labels file is that statement, written down.
- **not listed at all** — out of scope; the corpus makes no claim. Detections there are
  counted and reported, never scored.

Collapsing the last two would be the harness's cardinal sin. A bring-your-own bundle may
cover 500 hosts and label 3; treating the other 497 as clean publishes a precision number
the corpus never justified — the same failure ``runner._check_alignment`` exists to prevent.
A generated corpus is the opposite case: we built every device, so every device is listed,
which is exactly what makes "that gateway should have stayed quiet" a *measurable* claim.
"""

from bisect import bisect_left, bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime

from iotshield.contracts import Lane
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
    """One real fault: whose it was, when, and — where known — what it looked like.

    ``asset_id`` is required and *first* so that a positional call written against the old
    two-argument shape fails loudly instead of binding a datetime into an asset field.

    ``kind``, ``fault_id`` and ``lanes`` are descriptive and default to empty, because a
    converted third-party corpus knows none of them: SKAB's ``anomaly`` column says a fault
    happened, not what shape it was. A *generated* corpus knows all three, and ``lanes`` in
    particular is **derived** from which behaviours actually perturbed their output rather
    than declared by hand — a compromised camera's telemetry genuinely reads 22 degrees
    (ADR-0024 D2), so the lanes that carry a signal are a measured fact, not an intention.
    """

    asset_id: str
    start: datetime
    end: datetime
    kind: str = ""
    fault_id: str = ""
    lanes: frozenset[Lane] = frozenset()

    def overlaps(self, window_start: datetime, window_end: datetime) -> bool:
        """Whether a detection/alert window touches this incident *in time*.

        Deliberately says nothing about the asset: the caller compares identities, because
        only the caller knows whether it is asking "did anything fire here?" (scoring) or
        "did the *right* device fire here?" (attribution). Keeping both questions answerable
        from one predicate is what lets ``score`` report a misattribution rather than merely
        counting it wrong.
        """
        return overlaps(window_start, window_end, self.start, self.end)


@dataclass(frozen=True)
class AssetTruth:
    """One asset's answer key: its own row clock, and its own faults.

    The clock is per asset because the devices in one recording genuinely do not share one.
    A rig samples every second; a network host emits a flow when it happens to talk. Scoring
    point metrics against a single pooled clock would credit or blame a device for instants
    at which it was never observed.
    """

    asset_id: str
    timeline: tuple[tuple[datetime, bool], ...]
    incidents: tuple[Incident, ...]

    @property
    def is_clean(self) -> bool:
        """Asserted clean: observed throughout, and nothing was ever wrong."""
        return not self.incidents

    @classmethod
    def from_timeline(cls, asset_id: str, rows: Sequence[tuple[datetime, bool]]) -> "AssetTruth":
        """Build one asset's truth from per-row labels, collapsing runs into incidents.

        Rows are sorted by timestamp first: a corpus file is normally already in order, but
        the incident boundaries would be silently wrong if it were not, and a wrong answer
        key is worse than a loud failure.
        """
        ordered = tuple(sorted(rows, key=lambda row: row[0]))
        flags = [flagged for _, flagged in ordered]
        incidents = tuple(
            Incident(asset_id=asset_id, start=ordered[start][0], end=ordered[end][0])
            for start, end in merge_runs(flags)
        )
        return cls(asset_id=asset_id, timeline=ordered, incidents=incidents)

    @classmethod
    def from_incidents(
        cls,
        asset_id: str,
        timestamps: Sequence[datetime],
        incidents: Sequence[Incident],
    ) -> "AssetTruth":
        """Build one asset's truth from *intervals* plus its clock — the storage form.

        The inverse of ``from_timeline``, and the form a corpus bundle records, for two
        reasons. It is far smaller: per-row booleans spend ~50 bytes to say "normal" about
        a row the readings already list. And it is strictly *more* expressive — two
        incidents that touch with no quiet row between them are one run of flags but two
        intervals, a distinction per-row labels cannot represent and a synthetic corpus can
        legitimately produce.
        """
        ordered = sorted(timestamps)
        # The clock is sorted, so each incident marks one contiguous slice — found by
        # bisection rather than by testing every row against every incident, which was
        # measured at 7-16x slower and is the larger cost in loading a corpus.
        flags = [False] * len(ordered)
        for incident in incidents:
            low = bisect_left(ordered, incident.start)
            high = bisect_right(ordered, incident.end)
            flags[low:high] = [True] * (high - low)
        return cls(
            asset_id=asset_id,
            timeline=tuple(zip(ordered, flags, strict=True)),
            incidents=tuple(incidents),
        )


@dataclass(frozen=True)
class GroundTruth:
    """A case's answer key: one ``AssetTruth`` per asset the corpus has an opinion about.

    The tuple *is* the roster (see the module docstring). Sorted by asset id on construction
    so a bundle's labels — and every flattened list derived from them — are deterministic.
    """

    assets: tuple[AssetTruth, ...] = field(default_factory=tuple)

    @classmethod
    def from_assets(cls, assets: Sequence[AssetTruth]) -> "GroundTruth":
        return cls(assets=tuple(sorted(assets, key=lambda asset: asset.asset_id)))

    @classmethod
    def from_timeline(
        cls, rows: Sequence[tuple[datetime, bool]], *, asset_id: str
    ) -> "GroundTruth":
        """The single-asset shortcut, for a converter whose corpus has exactly one device.

        Keyword-only ``asset_id`` because it is not optional information: a timeline nobody
        can attribute is a timeline nothing can be scored against.
        """
        return cls.from_assets([AssetTruth.from_timeline(asset_id, rows)])

    @property
    def incidents(self) -> tuple[Incident, ...]:
        """Every incident, in roster order — the corpus-wide fault count."""
        return tuple(incident for asset in self.assets for incident in asset.incidents)

    @property
    def labelled_assets(self) -> frozenset[str]:
        """The roster: assets this answer key claims to know everything about."""
        return frozenset(asset.asset_id for asset in self.assets)

    @property
    def clean_assets(self) -> frozenset[str]:
        """Assets asserted clean — every detection on one of these is a false alarm."""
        return frozenset(asset.asset_id for asset in self.assets if asset.is_clean)

    @property
    def anomalous_rows(self) -> int:
        return sum(1 for asset in self.assets for _, flagged in asset.timeline if flagged)

    @property
    def is_clean(self) -> bool:
        """A recording with no fault at all — every alert on it is a false alarm."""
        return not self.incidents

    def of(self, asset_id: str) -> AssetTruth | None:
        """One asset's truth, or ``None`` when it is not in the roster."""
        return next((asset for asset in self.assets if asset.asset_id == asset_id), None)
