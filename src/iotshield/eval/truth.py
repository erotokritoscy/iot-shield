"""Ground truth: where the anomalies really were, and *whose* they were.

The one ingredient the product never has. A detection pipeline exists because nobody knows
where production anomalies are; a *labelled corpus* is a lab artifact, recorded by whoever
broke the equipment on purpose. Everything else in this package is machinery around
comparing our output to it (ADR-0021 D1).

## The coordinate system (ADR-0028 D1)

**Truth is indexed by (asset, lane, instant), because that is the finest thing a detector
can claim.** Everything else in this module and in ``score`` is a consequence of taking
that seriously, so it is worth stating before any of it.

A detector's entire output is ``DetectionResult(asset_id, lane, window_start, window_end)``.
Those are the only coordinates it speaks in. Truth can be compared against it in those
coordinates and in no others, so the answer key's unit is *forced* by the other side rather
than chosen here.

In particular the unit is **not a row**, which this module used to claim. A row count is a
fact about an *encoding*, not about the world: SKAB's native format is wide, one row carrying
every channel, while our contract is long, one row per reading (ADR-0003 D1). The same
recording is 1,148 rows in SKAB's format and 9,184 in ours, with one fault either way. A
label per row would change size when someone reformatted the file. A label per *instant*
does not.

The unit is not finer than an instant either, and that is a statement about detectors rather
than about ambition. ``DetectionResult`` carries no metric, and a multivariate detector
(``team3_zscore``) genuinely claims an instant rather than a channel — scoring per channel
would mark a correct detection wrong for declining to localize (ADR-0028, rejected options).

## The two levels

- the per-lane instant timeline scores **point** metrics ("of the anomalous instants, how
  many did we cover?"), which is what published benchmarks report;
- collapsed into **incidents**, it scores **event** metrics ("of the real faults, how many
  did we catch, and how many alerts did each cost?"), which is what an analyst experiences.

The collapse uses ``detect.merge_runs`` — the same function a row-scoring detector uses to
form its own judgments — so truth and detections are built by an identical rule. That is
what makes the event-level comparison apples-to-apples rather than an artifact of two
different definitions of "one incident".

## Why an incident names one lane (ADR-0028 D2)

An ``Incident`` used to carry a *set* of lanes, and that set had to be read two contradictory
ways by two callers: ``affects()`` read an empty set as "this fault reached every lane", while
``score._lane_recall`` read the same empty set as "this corpus makes no per-lane claim, report
nothing". One field, two opposite meanings, and a docstring warning the next reader to choose
deliberately. That is the definition of a design smell (golden rule 2).

It is also the wrong shape for the thing being described. A compromised camera is **one cause
with several observable projections**: it scans the network in bursts, fails an integrity check
whenever one runs, and warms up and stays warm. Those have different extents, and there is no
truer fourth interval behind them — the cause itself is unobservable. A telemetry detector
cannot be blamed for missing the scan, because it has no vocabulary for it.

So a lane is an *axis*, not a set-valued attribute: one incident per (fault, lane), all sharing
a ``fault_id``. ``affects()`` is gone, the empty-set fallback is gone, and per-lane recall is a
group-by rather than a special case. Both questions an analyst might ask stay derivable — "of
the fault projections, how many did we catch" from the incidents, and "of the distinct faults"
from ``fault_id`` — which is strictly more than the set-valued version could answer.

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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TypeVar

from iotshield.contracts import Lane
from iotshield.detect import merge_runs


class Finding(Protocol):
    """The shape everything downstream judges: something about one asset over one window.

    ``DetectionResult`` and ``PrioritizedAlert`` share it through ``_WindowedContract``, so the
    matching rules are written once instead of once per level — which is what stops the two
    altitudes drifting apart on the one question they must answer identically.

    It lives here, beside ``Incident``, because it is the *vocabulary* both scoring and the fit
    protocols speak: an asset-attributed window, compared against an asset-attributed window.
    Defining it in ``score`` made ``fit`` import from ``score`` to describe what it filters — a
    sideways arrow from training to judging, justified only by where the type happened to sit.
    """

    @property
    def asset_id(self) -> str: ...
    @property
    def window_start(self) -> datetime: ...
    @property
    def window_end(self) -> datetime: ...


FindingT = TypeVar("FindingT", bound=Finding)


def overlaps(a_start: datetime, a_end: datetime, b_start: datetime, b_end: datetime) -> bool:
    """Whether two closed intervals touch — the one place the boundary rule is decided.

    **Closed on both ends**, because a point anomaly is a zero-length window (ADR-0003): a
    half-open test would silently never match one. Stated once because both scoring levels
    need it (event matching compares two windows; point coverage asks whether an instant
    falls inside one, which is the zero-length case), and a half-open edit in one of two
    copies would corrupt one metric while leaving the other right.
    """
    return a_start <= b_end and a_end >= b_start


@dataclass(frozen=True, kw_only=True)
class Incident:
    """One fault as **one lane saw it**: whose, which modality, when, and what shape.

    ``lane`` is required and there is no set-valued alternative — see the module docstring for
    why a lane is an axis rather than an attribute. A fault observable on three modalities is
    three incidents sharing one ``fault_id``.

    Constructed by keyword only. The previous shape relied on ``asset_id`` being positionally
    first so that a call written against a older signature would bind a datetime into an asset
    field and fail loudly; that trick protects exactly one refactor and this is the second one.
    A plain dataclass performs no validation, so a mis-ordered positional call would have bound
    a ``datetime`` into ``lane`` in silence. Keyword-only makes every future field addition safe
    by construction instead.

    ``kind`` and ``fault_id`` stay descriptive, but ``fault_id`` is now always populated: a
    converted corpus synthesizes one per run (``AssetTruth.from_timeline``), because "how many
    *distinct faults* did we catch" is the alert level's denominator and an empty id would
    collapse every fault in a recording into one.
    """

    asset_id: str
    lane: Lane
    start: datetime
    end: datetime
    kind: str = ""
    fault_id: str = ""

    def overlaps(self, window_start: datetime, window_end: datetime) -> bool:
        """Whether a detection/alert window touches this incident *in time*.

        Deliberately says nothing about the asset or the lane: the caller compares those,
        because only the caller knows whether it is asking "did anything fire here?" (scoring)
        or "did the *right* device fire here?" (attribution). Keeping both questions answerable
        from one predicate is what lets ``score`` report a misattribution rather than merely
        counting it wrong.
        """
        return overlaps(window_start, window_end, self.start, self.end)


def _flagged(
    moments: Sequence[datetime], incidents: Sequence[Incident], lane: Lane
) -> tuple[tuple[datetime, bool], ...]:
    """One lane's instants, each marked with whether a fault *on that lane* covers it.

    The lane test is now identity on ``Incident.lane`` rather than a set membership with a
    fallback (ADR-0028 D2). An incident naming a lane this asset has no clock for simply
    flags nothing, which is the honest outcome: a bring-your-own bundle may label a modality
    it shipped no readings for, and inventing a clock for it would be worse than scoring it
    against nothing.
    """
    ordered = sorted(moments)
    # The clock is sorted, so each incident marks one contiguous slice — found by bisection
    # rather than by testing every instant against every incident, which was measured at
    # 7-16x slower and is the larger cost in loading a corpus.
    flags = [False] * len(ordered)
    for incident in incidents:
        if incident.lane is not lane:
            continue
        low = bisect_left(ordered, incident.start)
        high = bisect_right(ordered, incident.end)
        flags[low:high] = [True] * (high - low)
    return tuple(zip(ordered, flags, strict=True))


@dataclass(frozen=True)
class AssetTruth:
    """One asset's answer key: its instant clock **per lane**, and its own faults.

    The clock is per asset because the devices in one recording genuinely do not share one.
    A rig samples every second; a network host emits a flow when it happens to talk. Scoring
    point metrics against a single pooled clock would credit or blame a device for instants
    at which it was never observed.

    ## Why per *lane* as well

    The same argument, one dimension further, and it is not a refinement — a pooled clock
    makes multi-lane point metrics meaningless. On ``device_trimodal`` each device is sampled
    at ~900 telemetry instants, ~275 flow records and ~25 log lines. Union them into one
    clock and the fastest lane owns it: a single flow detection covering sixty seconds is
    charged sixty *telemetry* false positives, for instants it was never asked about and could
    not have covered. Point precision on the compromise case read 0.041, and adding lanes to
    the corroboration A/B — the entire reason that corpus exists — showed as a collapse from
    0.83 to 0.11.

    So a lane's detections are scored against that lane's instants and no others, and an
    instant is anomalous when a fault **on that lane** covers it. Which lanes a fault reached
    is measured by the generator, never declared (``generate.port.Observed``).
    """

    asset_id: str
    timelines: Mapping[Lane, tuple[tuple[datetime, bool], ...]]
    incidents: tuple[Incident, ...]

    def incidents_on(self, lane: Lane) -> tuple[Incident, ...]:
        """This asset's faults as one lane saw them — the event level's per-lane denominator.

        A plain filter, which is the whole point of ADR-0028 D2: the set-valued predecessor
        needed a fallback rule here and a *contradictory* fallback rule in ``score``.
        """
        return tuple(incident for incident in self.incidents if incident.lane is lane)

    @property
    def is_clean(self) -> bool:
        """Asserted clean: observed throughout, and nothing was ever wrong."""
        return not self.incidents

    @property
    def clock(self) -> tuple[datetime, ...]:
        """Every instant this asset was observed at, on any lane, sorted and deduplicated.

        For the questions that are genuinely lane-agnostic — "when does this device's history
        start", which is what a fit protocol reserves against, and what a bundle stores. Never
        for scoring: that is the pooled clock this class exists to stop using.
        """
        return tuple(sorted({moment for rows in self.timelines.values() for moment, _ in rows}))

    @classmethod
    def from_timeline(
        cls,
        asset_id: str,
        rows: Sequence[tuple[datetime, bool]],
        *,
        lane: Lane = Lane.TELEMETRY,
    ) -> "AssetTruth":
        """Build one asset's truth from per-instant labels, collapsing runs into incidents.

        Rows are sorted by timestamp first: a corpus file is normally already in order, but
        the incident boundaries would be silently wrong if it were not, and a wrong answer
        key is worse than a loud failure.

        The single-lane entry point, for a converter whose dataset is one modality of one rig
        (SKAB). ``lane`` defaults to TELEMETRY because a per-row anomaly column is a sensor
        dataset's shape; a converter for another modality names its own — and that name now
        reaches the incidents, so per-lane recall on a converted corpus reports the one lane
        it genuinely covers instead of the empty dict the set-valued shape forced (ADR-0028 D2).

        Each run gets a synthesized ``fault_id``. A converted corpus knows no fault identity,
        but the alert level counts *distinct faults* rather than projections, so leaving the id
        empty would have collapsed every run in a recording into a single fault and divided the
        alert metrics by one.
        """
        ordered = tuple(sorted(rows, key=lambda row: row[0]))
        flags = [flagged for _, flagged in ordered]
        incidents = tuple(
            Incident(
                asset_id=asset_id,
                lane=lane,
                start=ordered[start][0],
                end=ordered[end][0],
                fault_id=f"{asset_id}:run:{index}",
            )
            for index, (start, end) in enumerate(merge_runs(flags))
        )
        return cls(asset_id=asset_id, timelines={lane: ordered}, incidents=incidents)

    @classmethod
    def from_incidents(
        cls,
        asset_id: str,
        clocks: Mapping[Lane, Sequence[datetime]],
        incidents: Sequence[Incident],
    ) -> "AssetTruth":
        """Build one asset's truth from *intervals* plus its per-lane clocks — the storage form.

        The inverse of ``from_timeline``, and the form a corpus bundle records, for two
        reasons. It is far smaller: per-instant booleans spend ~50 bytes to say "normal" about
        an instant the readings already list. And it is strictly *more* expressive — two
        incidents that touch with no quiet instant between them are one run of flags but two
        intervals, a distinction per-instant labels cannot represent and a synthetic corpus can
        legitimately produce.
        """
        return cls(
            asset_id=asset_id,
            timelines={
                lane: _flagged(moments, incidents, lane) for lane, moments in clocks.items()
            },
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
        cls,
        rows: Sequence[tuple[datetime, bool]],
        *,
        asset_id: str,
        lane: Lane = Lane.TELEMETRY,
    ) -> "GroundTruth":
        """The single-asset shortcut, for a converter whose corpus has exactly one device.

        Keyword-only ``asset_id`` because it is not optional information: a timeline nobody
        can attribute is a timeline nothing can be scored against.
        """
        return cls.from_assets([AssetTruth.from_timeline(asset_id, rows, lane=lane)])

    @property
    def incidents(self) -> tuple[Incident, ...]:
        """Every incident, in roster order — one entry per (fault, lane) projection."""
        return tuple(incident for asset in self.assets for incident in asset.incidents)

    @property
    def faults(self) -> frozenset[tuple[str, str]]:
        """The distinct **causes**, as ``(asset_id, fault_id)`` — the alert level's denominator.

        The other half of ADR-0028 D2. One compromise observable on three lanes is three
        incidents and *one* fault, and the two levels need different denominators: a telemetry
        detector should be judged on the telemetry projection it could have seen, while the
        alert layer is cross-lane by construction and an analyst investigates the cause once.
        Dividing alerts by projections would read as if a correctly corroborated alert had
        *split* an incident three ways.

        Keyed by the pair rather than by ``fault_id`` alone: the generated ids embed their asset
        (``asset:kind:step``) but nothing enforces that, and a bring-your-own bundle numbering
        its faults from 1 per device would otherwise merge them across the fleet.
        """
        return frozenset((incident.asset_id, incident.fault_id) for incident in self.incidents)

    @property
    def labelled_assets(self) -> frozenset[str]:
        """The roster: assets this answer key claims to know everything about."""
        return frozenset(asset.asset_id for asset in self.assets)

    @property
    def clean_assets(self) -> frozenset[str]:
        """Assets asserted clean — every detection on one of these is a false alarm."""
        return frozenset(asset.asset_id for asset in self.assets if asset.is_clean)

    @property
    def anomalous_instants(self) -> int:
        """Instants a fault covers, counted per lane — the point level's denominator.

        **Instants, not rows** (ADR-0028 D1). A flow instant and a telemetry instant at the
        same moment are two observations and count twice, because point scoring judges them
        separately; but three channel readings at one telemetry instant are *one* observation,
        because a detector claims the instant and cannot claim a channel.

        Named for what it counts. As ``anomalous_rows`` it invited the reading that a bundle's
        readings and its answer key have the same length, which no format guarantees: the same
        SKAB recording is 1,148 rows wide-form and 9,184 in our long-form contract.
        """
        return sum(
            1
            for asset in self.assets
            for rows in asset.timelines.values()
            for _, flagged in rows
            if flagged
        )

    @property
    def is_clean(self) -> bool:
        """A recording with no fault at all — every alert on it is a false alarm."""
        return not self.incidents

    def of(self, asset_id: str) -> AssetTruth | None:
        """One asset's truth, or ``None`` when it is not in the roster."""
        return next((asset for asset in self.assets if asset.asset_id == asset_id), None)
