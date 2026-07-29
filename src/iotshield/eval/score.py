"""Scoring: comparing a run's output to ground truth, at two levels.

The two levels answer different questions and must never be conflated (ADR-0021 D2):

- **Detector level** — did it turn its observations into one finding per real fault? Its own
  job, since forming judgments is part of detecting.
- **Alert level** — did the alert layer group findings *across detectors* into a sane queue,
  and rank the real ones on top? A different job, at a different altitude.

Neither implies the other, because the alert engine is a *compressor*: it buckets detections
by (asset, window), so a detector that fragments badly may still produce few alerts. Only
reading both reveals what the correlation stage actually did — the compression ratio — and
only the pair distinguishes "the detector fragments" from "the alert layer is fragmenting a
clean detector".

## What a finding is matched on (ADR-0026, ADR-0028 D2)

A **detection** is compared to an incident on **(asset, lane, time)** — the full set of
coordinates it declares, and nothing less. An **alert** is compared on **(asset, time)**,
because it carries no lane: correlating across lanes is the thing it exists to do.

Both contract objects always knew their asset and a detection always knew its lane; every gap
below was only ever in the measuring instrument, which threw those coordinates away.

Five consequences worth naming, because each is a number that used to be wrong or absent:

- A detection at the right time on the *wrong device* is a false positive, and
  ``misattributed_detections`` says how many. Under time-only matching every one scored as a
  correct catch.
- A detection at the right time on the right device but the *wrong modality* is also a false
  positive, and ``off_lane_detections`` says how many. Same argument, one axis over: the corpus
  states that this lane showed nothing, so the detector agreed with a fault it could not see.
- Point and event rates are counted **per lane** and only then pooled. Summing first weights
  each modality by how fast it samples, which is a corpus-authoring choice masquerading as a
  measurement (``LaneCounts``).
- An asset in the roster with no incidents is *asserted clean*, so detections on it count
  against precision. ``clean_assets`` / ``clean_assets_detected`` report that at fleet scale
  — the false-alarm rate a fleet-wide threshold has never had to answer for.
- An asset **outside** the roster, or a lane the corpus keeps no clock for, is not scored at
  all in either direction. ``unlabelled_detections`` and ``unlabelled_lane_detections``
  disclose how much was set aside, so a partially labelled corpus cannot quietly inflate or
  deflate a rate. See ``truth`` for why those are different states from "clean".

Two things are deliberately NOT scored here, because no answer key exists for them:

- **Calibration** — whether ``priority_score`` is the *right* number, or whether alert A is
  more urgent than alert B. A corpus records that a fault happened, never how much it
  mattered; that needs analyst feedback (deferred, week-2 design §8). We measure only the
  prioritizer's *discrimination*: can it tell true from false and push true upward?
  ``mean_true_priority`` / ``mean_false_priority`` stay inside that line — comparing the two
  populations, or comparing one spec's true-alert scores against another's over *identical*
  evidence, is discrimination. Claiming 76.4 is correct would be calibration; we never do.
- **Whether a corpus is complete.** That is the corpus's own declaration (the roster), not
  something scoring can infer.
"""

import bisect
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, fields
from datetime import datetime
from typing import TypeVar
from uuid import UUID

from iotshield.contracts import DetectionResult, Lane, PrioritizedAlert
from iotshield.eval.truth import Finding, FindingT, GroundTruth, Incident

PRECISION_AT_K = (1, 5, 10)


def ratio(numerator: float, denominator: float) -> float | None:
    """The rate, or ``None`` when the denominator makes it *undefined* rather than zero.

    Load-bearing, not defensive. On a clean recording a detector that correctly emits
    nothing has no recall to report — and returning 0.0 would make it indistinguishable
    from one that missed everything, in the JSON report a CI gate reads and in the
    dashboard, not merely in a console table. ``None`` is already the harness's word for
    "not applicable" (precision@k when the queue is short, latency when nothing was
    caught), and the renderer already prints it as ``-``.
    """
    return numerator / denominator if denominator else None


def f1(precision: float | None, recall: float | None) -> float | None:
    """Harmonic mean, propagating undefined-ness from recall.

    Undefined only when there was nothing to find: with no incidents there is no F1 to
    speak of. A detector that found nothing *while there was something* has recall 0 and
    undefined precision (it made no predictions) — that is a score of 0, not a
    non-answer, so precision falls back to 0 there.
    """
    if recall is None:
        return None
    precision = precision or 0.0
    total = precision + recall
    return 2 * precision * recall / total if total else 0.0


def mean(values: Sequence[float]) -> float | None:
    """The mean, or ``None`` when there is nothing to average — the same rule as ``ratio``.

    Public because ``report`` needs the identical rule when it averages a metric across
    cases; a second copy there would be a third spelling of "undefined is not zero".
    """
    return sum(values) / len(values) if values else None


@dataclass(frozen=True)
class LaneCounts:
    """One lane's tallies — the unit both the point and event levels are really counted in.

    Per lane because a lane's instants are its own (``truth.AssetTruth``) and, since ADR-0028
    D2, so are its incidents. Pooling these into one set of point counts weights each modality
    by **how fast it samples**, which is a corpus-authoring decision masquerading as a
    measurement: on ``device_trimodal`` telemetry carries 2,700 of 3,597 clock entries, so a
    pooled point precision is a telemetry number wearing a corpus-wide name.

    Kept as counts rather than rates so pooling stays addition, the same reason ``Counts``
    does — and so ``report._attribution`` can pool per-lane recall across cases instead of
    averaging rates, which is the one column ADR-0027 D4 could not reach.
    """

    point_true_positive: int = 0
    point_false_positive: int = 0
    point_false_negative: int = 0
    incidents: int = 0
    incidents_caught: int = 0
    scoreable_detections: int = 0
    true_detections: int = 0

    def __add__(self, other: "LaneCounts") -> "LaneCounts":
        return LaneCounts(*(getattr(self, f.name) + getattr(other, f.name) for f in fields(self)))

    def rates(self) -> dict[str, float | None]:
        """This lane's rates, formed by exactly the rules ``Counts.rates`` uses corpus-wide."""
        covered = self.point_true_positive
        point_precision = ratio(covered, covered + self.point_false_positive)
        point_recall = ratio(covered, covered + self.point_false_negative)
        event_precision = ratio(self.true_detections, self.scoreable_detections)
        event_recall = ratio(self.incidents_caught, self.incidents)
        return {
            "point_precision": point_precision,
            "point_recall": point_recall,
            "point_f1": f1(point_precision, point_recall),
            "event_precision": event_precision,
            "event_recall": event_recall,
            "event_f1": f1(event_precision, event_recall),
        }


@dataclass(frozen=True)
class Counts:
    """The tallies every rate is formed from, and **the one place a rate is defined**.

    One case's counts or a whole corpus's — they add, and that is the point. ``SpecReport``
    pools a run by summing these and asking the same object for its rates, so "the corpus-level
    number is the same rule one level up" is true by construction rather than by two matching
    transcriptions. Writing the compositions twice is how a per-case column and a headline come
    to disagree silently.

    Spans both scoring levels because the report row does too (``CaseReport`` is flat for
    exactly this reason). Each caller reads the keys it owns and ignores the rest.

    ## What is stored, and what is derived (ADR-0028 D4)

    Everything with a lane lives in ``lanes`` and is **derived** from it here: the point counts,
    ``incidents``, ``incidents_caught``. Storing a lane-summed copy beside the mapping would be
    two representations of one fact, which is precisely the sin ADR-0027 D4 spent a decision
    removing one level up. Only the genuinely lane-free tallies are fields — a detection's
    scoreability and truth are per lane, but an *alert* is cross-lane by construction, so
    ``scoreable_alerts`` cannot be.

    ``faults`` is separate from ``incidents`` and is not a duplicate of it. An incident is one
    (fault, lane) projection; a fault is a cause. The event level divides by projections,
    because a telemetry detector can only be asked about the telemetry projection. The alert
    level divides by faults, because an analyst investigates the cause once and a correctly
    corroborated alert would otherwise read as though it had split one incident into three.
    """

    scoreable_alerts: int = 0
    faults: int = 0
    faults_covered: int = 0
    overlap_total: float = 0.0
    """Summed window overlap of the true detections — the numerator of the mean.

    Stored as the sum rather than the mean so pooling is addition like every other field. The
    per-case *mean* is what the report publishes, because that is the readable form."""

    lanes: Mapping[Lane, LaneCounts] = field(default_factory=dict)

    def __add__(self, other: "Counts") -> "Counts":
        # Written out rather than the generic ``fields(self)`` fold this used to be: a Mapping
        # does not support ``+``, and a fold that silently did the wrong thing for one field
        # would be worse than the explicit version. The scalars still add positionally, so a
        # new scalar field is added in one place.
        merged = dict(self.lanes)
        for lane, counts in other.lanes.items():
            merged[lane] = merged.get(lane, LaneCounts()) + counts
        return Counts(
            scoreable_alerts=self.scoreable_alerts + other.scoreable_alerts,
            faults=self.faults + other.faults,
            faults_covered=self.faults_covered + other.faults_covered,
            overlap_total=self.overlap_total + other.overlap_total,
            lanes=merged,
        )

    @property
    def totals(self) -> LaneCounts:
        """Every lane's tallies added together — the corpus-wide numerator of every rate.

        ``LaneCounts`` already adds, so pooling the lanes is the *same* operation as pooling the
        cases (``SpecReport.counts``). That is what lets ``rates()`` below be one line rather
        than a second transcription of the six formulas ``LaneCounts.rates`` states.
        """
        return sum(self.lanes.values(), LaneCounts())

    def rates(self) -> dict[str, float | None]:
        """Every rate, from these counts. ``None`` means undefined, never zero.

        The six point/event rates come from ``LaneCounts.rates`` applied to the pooled totals,
        so a lane's column and the headline above it cannot disagree about a formula — only
        about which counts went in. That is the same guarantee ``SpecReport.summary`` gets by
        asking this object rather than re-deriving, one level further down.

        The pooled point numbers are *comparable* even where they are not interpretable: two
        specs benchmarked on one corpus share every denominator exactly, so the pooled figure is
        a fair head-to-head even though its composition is set by sampling rates. What ADR-0028
        D4 forbids is letting it be the *only* point number on screen — hence ``rates_by_lane``
        and the per-lane line in the rendered report.
        """
        totals = self.totals
        return {
            **totals.rates(),
            # Over the SCOREABLE findings, like every rate above. Counting all of them made
            # these the numbers a partially labelled corpus could not justify: 24 detections on
            # an unlabelled host turned one correct catch into "25 detections per incident", on
            # the same page as a line promising those findings were excluded from every rate.
            "detections_per_incident": ratio(totals.scoreable_detections, totals.incidents),
            # Per *fault*, not per projection — see the class docstring.
            "alerts_per_incident": ratio(self.scoreable_alerts, self.faults),
            "incident_coverage": ratio(self.faults_covered, self.faults),
            "mean_true_detection_overlap": ratio(self.overlap_total, totals.true_detections),
        }

    def rates_by_lane(self) -> dict[Lane, dict[str, float | None]]:
        """Each lane's rates, in lane order — the numbers the pooled ones cannot stand in for."""
        return {lane: self.lanes[lane].rates() for lane in sorted(self.lanes)}


@dataclass(frozen=True)
class DetectorMetrics:
    """How well the detectors found the faults — level 1.

    Ratios are ``float | None``; ``None`` means undefined (no incidents to recall, or no
    detections to be precise about), never zero.

    ``detections`` counts *everything the detectors emitted*, including findings on assets
    outside the roster. The ratios that require a judgment use only the scoreable ones, and
    ``unlabelled_detections`` reconciles the two — so the raw output count stays the honest
    "how many things did this produce", while no rate is computed over a finding the corpus
    cannot adjudicate.
    """

    incidents: int
    detections: int
    point_precision: float | None
    point_recall: float | None
    point_f1: float | None
    event_precision: float | None
    event_recall: float | None
    event_f1: float | None
    detections_per_incident: float | None
    mean_latency_seconds: float | None
    # No defaults, matching every field above: there is exactly one construction site, so a
    # default would only let a metric be added here and forgotten there — reading as a
    # plausible 0 rather than failing.
    misattributed_detections: int
    off_lane_detections: int
    """Right device, right moment, **wrong modality** — the lane twin of misattribution.

    A flow detection coinciding with a telemetry-only fault agreed with something it could not
    have seen. Under lane-blind event matching every one of these scored as a correct catch, for
    exactly the reason a wrong-device coincidence used to (ADR-0026), one axis over.
    """

    unlabelled_detections: int
    unlabelled_lane_detections: int
    clean_assets: int
    clean_assets_detected: int
    lane_recall: dict[Lane, float | None]
    # The counts every rate above was divided from. Reported, not just used, because a
    # *corpus-level* number cannot be recovered from per-case rates: on a clean recording
    # every rate is undefined and averaging skips it, so four false alarms there reached no
    # headline at all. Pooling these instead makes the clean cases count (``SpecReport``).
    mean_true_detection_overlap: float | None
    lanes: dict[Lane, LaneCounts]
    """Every per-lane tally — and, since they add, every lane-summed count too.

    The six pooled counts (`scoreable_detections`, `true_detections`, `incidents_caught` and the
    three point tallies) used to be fields here as well. They were a second copy of what this
    mapping already holds, and `CaseReport.build` filters by `model_fields`, so the copies were
    computed, passed, and then silently dropped in favour of the report's own derivation. Ask
    `Counts(lanes=...)` — or `LaneCounts.rates()` for one lane — instead.

    `lane_recall` above is likewise derived from this; it stays a declared field only because it
    is what the report has always published.
    """


@dataclass(frozen=True)
class AlertMetrics:
    """What the analyst's queue looked like — level 2.

    ``alerts_per_incident`` and ``incident_coverage`` keep their published names and are divided
    by distinct **faults**, not by (fault, lane) projections (ADR-0028 D2). The names are the
    report's JSON contract, and the honest denominator here is the cause: an analyst investigates
    a compromised camera once, so dividing by three projections would report a correctly
    corroborated alert as having covered a third of an incident.
    """

    alerts: int
    alerts_per_incident: float | None
    incident_coverage: float | None
    precision_at_k: dict[int, float | None]
    mean_rank_of_true: float | None
    unlabelled_alerts: int
    clean_assets_alerted: int
    mean_true_priority: float | None
    mean_false_priority: float | None
    mean_lanes_per_true_alert: float | None
    # The counts, for the same reason ``DetectorMetrics`` carries them — including ``faults``,
    # which is *this* level's denominator. It rode in on ``DetectorMetrics`` while no
    # detector-level rate divided by it, so `score_alerts` computed it once for its own rates
    # and the copy that reached the report came from the other function entirely.
    scoreable_alerts: int
    faults: int
    faults_covered: int


class _Spans:
    """Disjoint sorted spans, answering "does anything cover this?" by binary search.

    The one index both levels need. Point scoring asks it once per labelled row, and a
    detector that does not merge its own judgments can emit a detection per row — so the
    naive scan is quadratic in recording length (measured: 10.5M comparisons over one
    corpus). Merging once turns every later question into a bisection.

    Built from ``(start, end)`` pairs rather than from findings, so the *same* index serves
    the two directions that used to be separate loops: detections merged to test row
    coverage, and incidents merged to test whether a finding landed on a real fault
    anywhere. The second was a full linear scan per finding and measured 54% of
    ``score_detections`` on a 30-asset fleet.
    """

    __slots__ = ("_ends", "_starts")

    def __init__(self, spans: Iterable[tuple[datetime, datetime]]) -> None:
        self._starts: list[datetime] = []
        self._ends: list[datetime] = []
        for start, end in sorted(spans):
            if self._ends and start <= self._ends[-1]:
                self._ends[-1] = max(self._ends[-1], end)
            else:
                self._starts.append(start)
                self._ends.append(end)

    def covers(self, moment: datetime) -> bool:
        """Whether any span contains this instant — the zero-length window case."""
        return self.overlaps(moment, moment)

    def overlaps(self, start: datetime, end: datetime) -> bool:
        """Whether any span touches ``[start, end]``, closed on both ends.

        The only candidate is the last span starting at or before ``end``: spans are
        disjoint and sorted, so an earlier one ends even earlier. Both comparisons are
        inclusive, which is ``truth.overlaps``'s rule.
        """
        index = bisect.bisect_right(self._starts, end) - 1
        return index >= 0 and start <= self._ends[index]

    def covered_seconds(self, start: datetime, end: datetime) -> float:
        """How much of ``[start, end]`` these spans actually cover, in seconds.

        The quantitative form of ``overlaps``, and the one thing event matching could not ask.
        The spans are disjoint and sorted, so both ``_starts`` and ``_ends`` are sorted and the
        intersecting spans are a contiguous slice: the first that ends at or after ``start``,
        through the last that begins at or before ``end``. Every span in that slice overlaps by
        construction, so the intersection needs no guard.
        """
        first = bisect.bisect_left(self._ends, start)
        last = bisect.bisect_right(self._starts, end)
        return sum(
            (min(end, self._ends[index]) - max(start, self._starts[index])).total_seconds()
            for index in range(first, last)
        )


def _coverage(
    timeline: Sequence[tuple[datetime, bool]], detections: Sequence[DetectionResult]
) -> Iterator[tuple[bool, bool]]:
    """Yield ``(is_anomalous, is_covered)`` per labelled row of ONE asset's ONE lane."""
    spans = _Spans((d.window_start, d.window_end) for d in detections)
    for moment, flagged in timeline:
        yield flagged, spans.covers(moment)


_KeyT = TypeVar("_KeyT")


def _grouped_by(
    findings: Sequence[FindingT], key: Callable[[FindingT], _KeyT]
) -> dict[_KeyT, list[FindingT]]:
    """Findings bucketed by whatever identifies them here — asset, then lane.

    One loop rather than one per key: the module had this idiom three times (by asset, by lane,
    and inline in ``_lane_recall`` keyed on the pair), and they had already drifted apart.
    Insertion order is preserved, which the point loop relies on.
    """
    grouped: dict[_KeyT, list[FindingT]] = {}
    for finding in findings:
        grouped.setdefault(key(finding), []).append(finding)
    return grouped


def _by_asset(findings: Sequence[FindingT]) -> dict[str, list[FindingT]]:
    return _grouped_by(findings, lambda finding: finding.asset_id)


def _incidents_of(truth: GroundTruth) -> dict[str, tuple[Incident, ...]]:
    """The asset -> incidents index ``GroundTruth`` already stores, as a lookup.

    Read once per scoring call and consulted by asset thereafter. The alternative — flatten
    with ``truth.incidents`` and compare ``asset_id`` inside the matching loop — rebuilds
    this index one comparison at a time, once per (finding, incident) pair.
    """
    return {asset.asset_id: asset.incidents for asset in truth.assets}


def _verdicts(
    findings: Sequence[FindingT], incidents_for: Callable[[FindingT], Sequence[Incident]]
) -> list[bool]:
    """Per finding, whether it overlaps an incident it is *entitled* to match.

    The entitlement is the caller's, because the two levels differ on exactly one axis
    (ADR-0028 D2). A **detection** must match on (asset, lane, time): it declares a lane, and a
    flow detection coinciding with a telemetry-only fault on the right device is a false alarm
    that happened to land at a busy moment — the same argument that made time-only matching
    wrong, one axis over. An **alert** must match on (asset, time) only, because an alert is
    cross-lane by construction and carries no lane at all.

    A parallel list of booleans rather than a set of ids: it needs no hashing, and both
    levels read verdicts positionally anyway (precision@k walks the ranked queue), so this
    is the shape the callers already want.
    """
    return [
        any(
            incident.overlaps(finding.window_start, finding.window_end)
            for incident in incidents_for(finding)
        )
        for finding in findings
    ]


def _first_hit(incident: Incident, findings: Sequence[Finding]) -> datetime | None:
    """The earliest start among findings touching this incident, or None if none do."""
    first: datetime | None = None
    for finding in findings:
        if incident.overlaps(finding.window_start, finding.window_end) and (
            first is None or finding.window_start < first
        ):
            first = finding.window_start
    return first


def _faults_covered(truth: GroundTruth, grouped: Mapping[str, Sequence[Finding]]) -> int:
    """How many distinct **causes** some finding on their asset touched (ADR-0028 D2).

    The alert level's numerator. A fault observable on three lanes is three incidents and one
    cause; an alert that covers any of its projections has told the analyst about the cause, so
    counting projections would report a correctly corroborated alert as one-third coverage.

    Deliberately lane-blind, matching ``_verdicts``' alert entitlement: ``PrioritizedAlert``
    carries no lane, so there is nothing to compare even if we wanted to.
    """
    covered: set[tuple[str, str]] = set()
    for asset in truth.assets:
        own = grouped.get(asset.asset_id, ())
        if not own:
            continue
        for incident in asset.incidents:
            key = (incident.asset_id, incident.fault_id)
            if key not in covered and _first_hit(incident, own) is not None:
                covered.add(key)
    return len(covered)


def _window_overlap(finding: Finding, incidents: _Spans) -> float:
    """How much of a detection's window sits inside a real fault, as a fraction of its width.

    The dimension pure interval matching is blind to. Event precision asks only *whether* a
    detection touches an incident, so one window spanning the whole recording scores a perfect
    row — precision 1.00, recall 1.00, latency 0s, one detection per incident — while carrying
    no information at all. This is what separates that from a detector whose windows land on
    the fault: 40 seconds of real fault inside a 900-second window is 0.04.

    Measured, not refused. A wide window is sometimes the honest output of a detector that
    genuinely cannot localize better, so the harness reports the width rather than rejecting
    it — the same stance ``runner._compatibility_notes`` takes on a silent detector.

    A zero-width window is 1.0 when it lands inside one, because a point detection that hits is
    entirely inside by definition; dividing by its width would be 0/0.
    """
    width = (finding.window_end - finding.window_start).total_seconds()
    covered = incidents.covered_seconds(finding.window_start, finding.window_end)
    return covered / width if width > 0 else 1.0


@dataclass(frozen=True)
class _Tallies:
    """What one pass over (asset, lane) yields — counts, latency, and the index it built.

    ``spans`` is the merged incident index per (asset, lane), handed back rather than discarded.
    ``score_detections`` needs exactly that index twice more (to judge each detection, and to
    measure how tightly the true ones sat on their faults), and rebuilding it there meant the
    same filter-and-merge ran three times per (asset, lane) — once here, once per detection
    inside a closure, and once more in the overlap loop.
    """

    lanes: dict[Lane, LaneCounts]
    latency: float | None
    off_clock: int
    spans: dict[tuple[str, Lane], _Spans]


def _lane_tallies(truth: GroundTruth, scoreable: Sequence[DetectionResult]) -> _Tallies:
    """Every per-lane count for one case, plus mean latency and the (asset, lane) span index.

    One pass over (asset, lane), which is the coordinate system's own shape — so the point
    counts, the event counts and per-lane recall all come out of the same loop instead of the
    three separate scans (one of them a nested rescan) they used to need. ``_lane_recall`` is
    gone: it is now ``LaneCounts.rates()['event_recall']``, computed by the same rule as every
    other rate and *poolable*, which is what lets the rendered report stop macro-averaging it
    across cases (ADR-0027's own deferred item).

    A lane a corpus declares but no detector covers still appears, with recall 0.0 — that is the
    honest answer to "did this run judge everything the corpus labelled?", and it is why the
    loop is driven by the answer key's lanes rather than by the detectors' lanes.

    A detection whose lane the asset has *no clock for* is counted separately and scored in
    neither direction. It cannot touch point metrics (there are no instants to be wrong about),
    so leaving it in event precision alone would have made it a silent one-sided penalty.
    """
    by_asset_lane = _grouped_by(scoreable, lambda d: (d.asset_id, d.lane))
    tallies: dict[Lane, LaneCounts] = {}
    spans_of: dict[tuple[str, Lane], _Spans] = {}
    latencies: list[float] = []
    measured = 0

    for asset in truth.assets:
        for lane, timeline in asset.timelines.items():
            own = by_asset_lane.get((asset.asset_id, lane), [])
            measured += len(own)

            true_positive = false_positive = false_negative = 0
            for flagged, covered in _coverage(timeline, own):
                if flagged and covered:
                    true_positive += 1
                elif flagged:
                    false_negative += 1
                elif covered:
                    false_positive += 1

            lane_incidents = asset.incidents_on(lane)
            caught = 0
            for incident in lane_incidents:
                first = _first_hit(incident, own)
                if first is not None:
                    caught += 1
                    # Latency averages only over incidents actually caught — averaging a miss
                    # as "infinitely late" would conflate two different failures (missed it vs.
                    # found it slowly), and recall already reports the misses.
                    latencies.append(max(0.0, (first - incident.start).total_seconds()))

            # Flattened over spans, not incidents: an incident is a projection whose extent
            # may have gaps (ADR-0029), and merging `start`..`end` would re-fill them — turning
            # every quiet instant a fault straddled back into a labelled one.
            spans = spans_of[(asset.asset_id, lane)] = _Spans(
                span for i in lane_incidents for span in i.spans
            )
            tallies[lane] = tallies.get(lane, LaneCounts()) + LaneCounts(
                point_true_positive=true_positive,
                point_false_positive=false_positive,
                point_false_negative=false_negative,
                incidents=len(lane_incidents),
                incidents_caught=caught,
                scoreable_detections=len(own),
                true_detections=sum(1 for d in own if spans.overlaps(d.window_start, d.window_end)),
            )
    return _Tallies(
        lanes=tallies,
        latency=mean(latencies),
        off_clock=len(scoreable) - measured,
        spans=spans_of,
    )


def score_detections(truth: GroundTruth, detections: Sequence[DetectionResult]) -> DetectorMetrics:
    """Score the detector level: point coverage, event matching, fragmentation, latency.

    The event rule is what makes fragmentation visible, and it is deliberately asymmetric:
    **recall** credits an incident once no matter how many detections hit it, while
    **precision** judges every detection on its own. So 91 fragments of one real fault score
    full recall and ~0.01 precision — the analyst's experience (91 things to look at, one
    of them worth it), not the row-wise view that would call it near-perfect.
    """
    roster = truth.labelled_assets
    scoreable = [d for d in detections if d.asset_id in roster]
    unlabelled = len(detections) - len(scoreable)
    grouped = _by_asset(scoreable)

    # Every per-lane tally in one pass over the coordinate system's own shape (asset, lane).
    # Point level: per labelled instant of each asset's own clock FOR ONE LANE, is it inside any
    # detection window on that asset in THAT LANE? Neither dimension is optional. A pooled
    # asset clock credits a device for instants it was never observed at; a pooled *lane*
    # clock hands every lane to whichever samples fastest, so one flow detection covering a
    # minute is charged sixty telemetry false positives (``truth.AssetTruth``).
    tallies = _lane_tallies(truth, scoreable)

    # Event level, and the two ways a detection can be *nearly* right — one pass, because the
    # true and the false branch are disjoint halves of the same question. A detection is true
    # when it overlaps an incident on its own asset AND its own lane, which is a bisection into
    # the index `_lane_tallies` already built rather than a rescan of that lane's incidents.
    incidents_of = _incidents_of(truth)
    elsewhere = _Spans(span for incident in truth.incidents for span in incident.spans)
    # Merged per-asset spans, built lazily and only for the assets a *false* detection actually
    # lands on: on a 30-asset fleet with two faulty hosts, eagerly indexing the roster builds 28
    # empty ones per case, and a spec with no false detections consults none of them.
    same_asset: dict[str, _Spans] = {}
    misattributed = off_lane = 0
    overlaps: list[float] = []

    for detection in scoreable:
        window = (detection.window_start, detection.window_end)
        own = tallies.spans.get((detection.asset_id, detection.lane))
        if own is not None and own.overlaps(*window):
            # How tightly a true detection sat on its fault — the dimension interval matching
            # is blind to (``_window_overlap``).
            overlaps.append(_window_overlap(detection, own))
            continue
        if detection.asset_id not in same_asset:
            same_asset[detection.asset_id] = _Spans(
                span
                for incident in incidents_of.get(detection.asset_id, ())
                for span in incident.spans
            )
        if same_asset[detection.asset_id].overlaps(*window):
            # Right device, right moment, WRONG modality: the corpus says this lane showed
            # nothing, so the detector agreed with a fault it could not have seen.
            off_lane += 1
        elif elsewhere.overlaps(*window):
            misattributed += 1

    counts = Counts(
        faults=len(truth.faults),
        overlap_total=sum(overlaps),
        lanes=tallies.lanes,
    )
    rates = counts.rates()
    clean = truth.clean_assets

    return DetectorMetrics(
        incidents=counts.totals.incidents,
        detections=len(detections),
        point_precision=rates["point_precision"],
        point_recall=rates["point_recall"],
        point_f1=rates["point_f1"],
        event_precision=rates["event_precision"],
        event_recall=rates["event_recall"],
        event_f1=rates["event_f1"],
        detections_per_incident=rates["detections_per_incident"],
        mean_latency_seconds=tallies.latency,
        misattributed_detections=misattributed,
        off_lane_detections=off_lane,
        unlabelled_detections=unlabelled,
        unlabelled_lane_detections=tallies.off_clock,
        clean_assets=len(clean),
        clean_assets_detected=sum(1 for asset_id in clean if grouped.get(asset_id)),
        lane_recall={
            lane: lane_rates["event_recall"] for lane, lane_rates in counts.rates_by_lane().items()
        },
        mean_true_detection_overlap=rates["mean_true_detection_overlap"],
        lanes=tallies.lanes,
    )


def score_alerts(
    truth: GroundTruth,
    alerts: Sequence[PrioritizedAlert],
    detections: Sequence[DetectionResult],
) -> AlertMetrics:
    """Score the alert level: queue size per incident, coverage, and ranking quality.

    ``alerts`` must already be in the engine's ranked order (the port ranks by priority),
    since precision@k and mean-rank read positions in that queue.

    ``detections`` is the same run's detections, used to count how many lanes backed each
    true alert — the direct evidence that cross-lane corroboration fired. Required rather
    than defaulted: every real caller has them, and an optional argument would buy nothing
    but a third meaning for ``None`` in the reported metric.

    Unlabelled alerts are removed before ranking rather than counted either way. Including
    one would force a wrong answer (false is a lie about a device we never adjudicated;
    true is worse), and *removing* it preserves the relative order of every alert we can
    judge. On a fully labelled corpus nothing is removed, so this is a no-op there.
    """
    roster = truth.labelled_assets
    scoreable = [a for a in alerts if a.asset_id in roster]
    grouped = _by_asset(scoreable)

    # Lane-blind on both sides, and necessarily so: a PrioritizedAlert carries no lane, because
    # correlating across lanes is the thing it exists to do (ADR-0028 D2).
    incidents_of = _incidents_of(truth)
    is_true = _verdicts(scoreable, lambda alert: incidents_of.get(alert.asset_id, ()))
    covered = _faults_covered(truth, grouped)
    true_alerts = [a for a, truthful in zip(scoreable, is_true, strict=True) if truthful]
    false_alerts = [a for a, truthful in zip(scoreable, is_true, strict=True) if not truthful]

    # precision@k is undefined when the queue is shorter than k — reporting it as 0 would
    # read as "the top 10 were all wrong" when there were only 3 alerts. None means "n/a".
    precision_at_k: dict[int, float | None] = {
        k: (sum(is_true[:k]) / k if len(scoreable) >= k else None) for k in PRECISION_AT_K
    }
    clean = truth.clean_assets

    # How many distinct lanes backed each true alert — the direct evidence that cross-lane
    # corroboration fired, rather than inferring it from the priority score it produced.
    # Only the ids on *true* alerts are ever read, so the index is narrowed to those first: a
    # run can carry tens of thousands of detections and a handful of true alerts, and on a
    # clean recording there are none at all.
    wanted = {i for alert in true_alerts for i in alert.detection_ids}
    lane_of: dict[UUID, Lane] = (
        {d.detection_id: d.lane for d in detections if d.detection_id in wanted} if wanted else {}
    )
    # An alert whose detections cannot be resolved contributes nothing rather than a zero: "we
    # could not tell how many lanes backed this" is the undefined case, and averaging it as 0
    # would understate every run where it happened.
    lanes_per_true = [
        count
        for alert in true_alerts
        if (count := float(len({lane_of[i] for i in alert.detection_ids if i in lane_of})))
    ]

    counts = Counts(
        faults=len(truth.faults), scoreable_alerts=len(scoreable), faults_covered=covered
    )
    rates = counts.rates()

    return AlertMetrics(
        alerts=len(alerts),
        alerts_per_incident=rates["alerts_per_incident"],
        incident_coverage=rates["incident_coverage"],
        precision_at_k=precision_at_k,
        mean_rank_of_true=mean(
            [rank for rank, truthful in enumerate(is_true, start=1) if truthful]
        ),
        unlabelled_alerts=len(alerts) - len(scoreable),
        clean_assets_alerted=sum(1 for asset_id in clean if grouped.get(asset_id)),
        mean_true_priority=mean([a.priority_score for a in true_alerts]),
        mean_false_priority=mean([a.priority_score for a in false_alerts]),
        mean_lanes_per_true_alert=mean(lanes_per_true),
        scoreable_alerts=counts.scoreable_alerts,
        faults=counts.faults,
        faults_covered=counts.faults_covered,
    )
