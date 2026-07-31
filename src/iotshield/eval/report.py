"""The evaluation report: a diffable artifact, and how it renders.

JSON rather than console-only on purpose. A benchmark's value compounds when you can compare
today's run to last week's — so the report is a serializable document CI could gate on
("this change dropped event-F1 by 0.1"), and the same document the dashboard's Evaluate mode
renders, whether it came back from ``POST /eval/run`` or was uploaded from disk.

These models live here and **not** in ``contracts/``. ``contracts/`` is the *product's* core
vocabulary — what a normalizer, detector, and alert engine agree on. A benchmark report is
the *harness's* vocabulary, and keeping it out of the contracts package is itself the
statement that the harness sits outside the product (ADR-0021 D1).
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field, computed_field

from iotshield.contracts import Lane
from iotshield.eval.score import AlertMetrics, Counts, DetectorMetrics, LaneCounts, mean, ratio


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _compression(counts: Counts) -> float | None:
    """Scoreable detections / scoreable alerts — what the alert layer absorbed.

    NOT the quotient of the two ``/inc`` columns, though it was until ADR-0028 D2 gave
    the levels different denominators: ``det/inc`` divides by incidents (projections) and
    ``alt/inc`` by faults, so on a multi-lane corpus those differ by ``incidents/faults``.
    Dividing counts by counts is the denominator-free reading, which is what >1 / ~1 / <1
    is meant to be read against.

    The transfer function between the two levels, and the only number that shows the stages
    interacting: >1 means correlation compressed the detector's output, ~1 means it passed it
    through, and a clean detector with a ratio below 1 means the alert layer is *splitting*
    incidents rather than merging them (ADR-0021 D2).

    Takes the counts rather than reading them, so ``summary()`` can aggregate the corpus once
    and pass the same object in instead of re-pooling every case a second time.
    """
    return (
        ratio(counts.totals.scoreable_detections, counts.scoreable_alerts)
        if counts.totals.incidents
        else None
    )


TIMELINE_SPAN_CAP = 2000
"""Most spans recorded per lane per case.

Not arbitrary: Vega-Lite refuses to render past 5,000 rows by default, and a row-scoring
detector produces 1,135 detections *per recording* — so an uncapped timeline would be both
unrenderable and a multi-megabyte report. Truncation is flagged rather than silent.
"""


class CaseTimeline(BaseModel):
    """Where the faults, detections and alerts actually fell, for one case.

    The data behind the timeline chart, and the only part of a report that is about *shape*
    rather than *score*. It answers the question a table cannot: a detector with
    ``det/inc 300`` is fragmenting, but you only *see* what that costs an analyst when the
    three lanes are drawn against one clock.

    Off by default (``include_timeline``): the report's other job is to be a small, diffable
    artifact a CI gate reads, and spans would dominate it.
    """

    truth: list[tuple[datetime, datetime]] = Field(default_factory=list)
    detections: list[tuple[datetime, datetime]] = Field(default_factory=list)
    alerts: list[tuple[datetime, datetime]] = Field(default_factory=list)
    truncated: bool = False


class CaseReport(BaseModel):
    """One spec's result on one labelled case.

    Flat rather than two nested metric objects, deliberately: this is the JSON a CI gate
    keys into (``cases[].event_f1``), so the shape is part of the artifact's contract.
    ``None`` on any ratio means *undefined*, never zero — see ``score.ratio``.
    """

    case: str
    incidents: int
    detections: int
    alerts: int
    fitted: bool
    notes: list[str] = Field(default_factory=list)
    point_precision: float | None
    point_recall: float | None
    point_f1: float | None
    event_precision: float | None
    event_recall: float | None
    event_f1: float | None
    detections_per_incident: float | None
    alerts_per_incident: float | None
    incident_coverage: float | None
    mean_latency_seconds: float | None
    mean_rank_of_true: float | None
    precision_at_k: dict[int, float | None] = Field(default_factory=dict)
    # Asset-aware fields (ADR-0026). Defaulted so an older report still parses, and so a
    # single-asset corpus — where every one of them is 0 or empty — reads unchanged.
    misattributed_detections: int = 0
    off_lane_detections: int = 0
    unlabelled_detections: int = 0
    unlabelled_lane_detections: int = 0
    unlabelled_alerts: int = 0
    clean_assets: int = 0
    clean_assets_detected: int = 0
    clean_assets_alerted: int = 0
    mean_true_priority: float | None = None
    mean_false_priority: float | None = None
    mean_lanes_per_true_alert: float | None = None
    mean_true_detection_overlap: float | None = None
    # The raw counts behind every rate above, so a corpus-level number can be *pooled* rather
    # than averaged from per-case rates — the only form in which a clean recording's false
    # alarms reach the headline (see ``SpecReport.summary``). Defaulted like the fields above
    # so an older report still parses.
    #
    # ``lanes`` carries the per-lane tallies, which is what makes every per-lane column
    # poolable too — including the per-lane recall that ADR-0027 D4 had to leave as a
    # macro-average because the counts behind it were not in this row (ADR-0028 D4).
    lanes: dict[Lane, LaneCounts] = Field(default_factory=dict)
    faults: int = 0
    scoreable_alerts: int = 0
    faults_covered: int = 0
    timeline: CaseTimeline | None = None

    @property
    def counts(self) -> Counts:
        """This row's tallies, in the shape ``score`` forms rates from.

        Only the lane-free fields are read back: everything with a lane lives in ``lanes`` and
        ``Counts`` derives its own totals from there, so a per-lane and a pooled copy of one
        count cannot disagree.

        The mean overlap is turned back into its own numerator, so pooling stays plain addition:
        ``true_detections`` is exactly the count it was averaged over.
        """
        return Counts(
            scoreable_alerts=self.scoreable_alerts,
            faults=self.faults,
            faults_covered=self.faults_covered,
            overlap_total=(self.mean_true_detection_overlap or 0.0) * self.true_detections,
            lanes=self.lanes,
        )

    # The lane-summed counts, **derived** from `lanes` and still serialized, because a CI gate
    # keys into `cases[].true_detections` and the JSON is part of this artifact's contract.
    # `computed_field` is what makes those two facts compatible: one representation in the
    # model, full shape in the dump. As stored fields they would be a second copy free to
    # disagree with the breakdown beside them.
    @property
    def _pooled(self) -> LaneCounts:
        """This row's lanes added together — the one place the pooling happens.

        Not ``self.counts``: that reads ``true_detections`` below to rebuild ``overlap_total``,
        so routing these through it would recurse.
        """
        return sum(self.lanes.values(), LaneCounts())

    @computed_field
    @property
    def scoreable_detections(self) -> int:
        return self._pooled.scoreable_detections

    @computed_field
    @property
    def true_detections(self) -> int:
        return self._pooled.true_detections

    @computed_field
    @property
    def incidents_caught(self) -> int:
        return self._pooled.incidents_caught

    @computed_field
    @property
    def point_true_positive(self) -> int:
        return self._pooled.point_true_positive

    @computed_field
    @property
    def point_false_positive(self) -> int:
        return self._pooled.point_false_positive

    @computed_field
    @property
    def point_false_negative(self) -> int:
        return self._pooled.point_false_negative

    @computed_field
    @property
    def lane_recall(self) -> dict[Lane, float | None]:
        """Per-lane event recall, derived — it was the seventh stored copy of ``lanes``."""
        return {lane: counts.rates()["event_recall"] for lane, counts in sorted(self.lanes.items())}

    @classmethod
    def build(
        cls,
        *,
        case: str,
        detector_metrics: DetectorMetrics,
        alert_metrics: AlertMetrics,
        fitted: bool,
        notes: list[str],
        timeline: CaseTimeline | None = None,
    ) -> "CaseReport":
        """Flatten both metric objects into the report row.

        Every metric field is named identically on its source dataclass, so the copy is
        mechanical and filtering by ``model_fields`` keeps it that way — adding a metric
        that this report should carry means declaring it above and nothing else.
        """
        metrics = asdict(detector_metrics) | asdict(alert_metrics)
        return cls(
            case=case,
            fitted=fitted,
            notes=notes,
            timeline=timeline,
            **{name: value for name, value in metrics.items() if name in cls.model_fields},
        )


class SpecReport(BaseModel):
    """One benchmarked pipeline spec, across every case in the corpus."""

    spec: str
    detectors: list[str]
    alert_engine: str
    cases: list[CaseReport]

    def _mean(self, attribute: str) -> float | None:
        """Mean over the cases where the metric is *defined*, or None if none are.

        For the **count** columns only. Every *rate* is pooled instead — see ``summary``.
        """
        return mean(
            [value for case in self.cases if (value := getattr(case, attribute)) is not None]
        )

    @property
    def counts(self) -> Counts:
        """Every case's tallies, added up — the corpus-level numerator of every rate."""
        return sum((case.counts for case in self.cases), Counts())

    @property
    def cases_fitted(self) -> int:
        return sum(1 for case in self.cases if case.fitted)

    @property
    def compression_ratio(self) -> float | None:
        """Scoreable detections ÷ scoreable alerts — what the alert layer absorbed.

        The transfer function between the two levels, and the only number that shows the
        stages interacting: >1 means correlation compressed the detector's output, ~1 means
        it passed it through, and a clean detector with a ratio below 1 means the alert
        layer is *splitting* incidents rather than merging them (ADR-0021 D2).
        """
        return _compression(self.counts)

    def rates_by_lane(self) -> dict[Lane, dict[str, float | None]]:
        """Corpus-level rates per lane, **pooled** from the per-case counts.

        The column ADR-0027 D4 had to leave behind. Its own decision established that a
        corpus-level rate must be pooled from counts rather than averaged from per-case rates —
        a rate is undefined on a clean recording and averaging skips undefined values — and then
        per-lane recall stayed a macro-average anyway, printed immediately below, because the
        counts behind it were not in ``CaseReport``. Now they are (``CaseReport.lanes``), so
        this is the same rule reaching the last column that escaped it.
        """
        return self.counts.rates_by_lane()

    def summary(self) -> dict[str, float | None]:
        """The corpus-level numbers, **pooled from counts** rather than averaged from rates.

        This is the difference between a headline that can see a clean recording and one that
        cannot. On a case with no incidents every rate is legitimately undefined, and averaging
        skips undefined values — so a spec that stayed silent on the clean recordings and one
        that fired four false alarms on them reported *identical* event and point F1. Every
        shipped corpus includes clean recordings specifically to measure that, and the table was
        structurally incapable of showing it.

        Pooling fixes it because a false alarm on a clean case still lands in the *denominator*
        of precision: there is no incident to recall, but there is a detection to be wrong
        about. It also removes an unstated macro-average — a 3-case recording used to weigh as
        much as a 300-case one.

        The rates themselves come from ``score.Counts``, the same object ``score_detections``
        asks within a case, so "the same rule one level up" is literal rather than transcribed:
        there is no second place where event precision's denominator is decided.

        The per-case table keeps its own rates and its ``-`` where a rate does not exist; only
        the aggregate changes.

        ``detections`` and ``alerts`` are **corpus totals**, and used to be per-case means — the
        last two macro-averages in a row this docstring criticises macro-averaging for. Being
        means they also needed decimals to be readable, and did not have them: a spec that fired
        on two of four recordings has a mean of 0.5, which formats at zero decimal places as
        ``0`` (Python rounds halves to even, so not even ``1``). The headline therefore read
        ``det 0  alerts 0`` for a detector the per-case table two lines below showed firing —
        exactly the "row of zeros indistinguishable from a bad detector" this harness exists to
        prevent. A total is an integer, so it is honest at zero decimal places, and it answers
        the question a reader has of an absolute column: how much did this spec produce?
        """
        counts = self.counts
        return {
            **counts.rates(),
            "detections": float(sum(case.detections for case in self.cases)),
            "alerts": float(sum(case.alerts for case in self.cases)),
            "compression_ratio": _compression(counts),
        }

    @computed_field
    @property
    def pooled(self) -> dict[str, float | None]:
        """``summary()``, serialized — the headline a CI gate can read without re-pooling.

        ADR-0027 left this as a gap: the report carried the per-case counts but no corpus-level
        number, so a gate wanting the pooled figure had to sum ``cases[].*`` and divide, which
        is a third place that would know how a rate is formed. Exposing the *result* of the one
        definition is not a second definition.
        """
        return self.summary()

    @computed_field
    @property
    def pooled_by_lane(self) -> dict[Lane, dict[str, float | None]]:
        """``rates_by_lane()``, serialized, for the same reason as ``pooled``."""
        return self.rates_by_lane()


class EvaluationReport(BaseModel):
    corpus: str
    corpus_description: str
    fit_protocol: str
    cases: int
    incidents: int
    faults: int = 0
    """Distinct causes behind ``incidents`` (ADR-0028 D2).

    Defaulted so an older report parses. Equal to ``incidents`` on a single-lane corpus; lower
    wherever one fault was observable on more than one modality, and the gap is the corpus's
    multimodality made visible.
    """

    generated_at: datetime = Field(default_factory=_utc_now)
    specs: list[SpecReport] = Field(default_factory=list)


def _clip(text: str, width: int) -> str:
    """Truncate with an ellipsis, so a cut-off value cannot be mistaken for a complete one.

    A three-detector spec rendered as ``stub_threshold,log_burst,fl`` — which reads as a
    detector named ``fl`` rather than as a list that did not fit.
    """
    return text if len(text) <= width else text[: width - 1] + "…"


def _cell(value: float | None, width: int = 8, places: int = 2) -> str:
    return f"{'-':>{width}}" if value is None else f"{value:>{width}.{places}f}"


@dataclass(frozen=True)
class _Column:
    """One numeric column of the head-to-head table: its heading, source key, and format."""

    heading: str
    key: str
    width: int = 8
    places: int = 2


# The rate columns; on a clean corpus every one of them is legitimately undefined and
# renders as '-', so the same table serves both cases with only the caption differing.
_SUMMARY_COLUMNS = (
    _Column("evF1", "event_f1"),
    _Column("evP", "event_precision"),
    _Column("evR", "event_recall"),
    # Beside precision because it qualifies it: precision says a detection touched a fault,
    # `ovl` says how much of the window was fault rather than padding. A recording-wide
    # detection reads evP 1.00 / ovl 0.04.
    _Column("ovl", "mean_true_detection_overlap", 6),
    _Column("ptF1", "point_f1"),
    _Column("det/inc", "detections_per_incident", 9),
    _Column("alt/inc", "alerts_per_incident", 9),
    _Column("cover", "incident_coverage"),
    # Corpus totals, not per-case means, so zero decimal places is honest (see `summary`).
    _Column("det", "detections", 8, 0),
    _Column("alerts", "alerts", 8, 0),
)


_SPEC_COLUMN_MIN = 21
_SPEC_COLUMN_MAX = 43
"""Bounds on the spec-name column.

A fixed 22 was fine while labels were file stems; a sweep generates
``team3_scratch[window_size=120]``, and a table where every swept row truncates to the
same prefix hides the one thing the sweep varied. Sized to the longest label present so
the common case stays narrow, capped so a pathological label cannot wrap the table.
"""


def _summary_table(specs: list[SpecReport]) -> list[str]:
    # `cell` is the widest label shown; the column is one wider so there is always a space
    # before `detectors`, exactly as the fixed-width version had.
    longest = max((len(spec.spec) for spec in specs), default=0)
    cell = min(max(longest, _SPEC_COLUMN_MIN), _SPEC_COLUMN_MAX)
    heading = f"{'spec':<{cell + 1}}{'detectors':<28}" + "".join(
        f"{column.heading:>{column.width}}" for column in _SUMMARY_COLUMNS
    )
    rows = []
    for spec in specs:
        summary = spec.summary()
        cells = "".join(
            _cell(summary[column.key], column.width, column.places) for column in _SUMMARY_COLUMNS
        )
        fitted = f"{spec.cases_fitted}/{len(spec.cases)}"
        rows.append(
            f"{spec.spec[:cell]:<{cell + 1}}{_clip(','.join(spec.detectors), 27):<28}"
            f"{cells}{fitted:>8}"
        )
    return [f"{heading}{'fitted':>8}", *rows]


def render(report: EvaluationReport) -> str:
    """Render the head-to-head comparison, then per-case detail for each spec."""
    # ASCII-only separators: Windows consoles mangle UTF-8 punctuation (as RunReport found).
    # Both totals, because they are different denominators and the reader needs to know which
    # one a column used: the event level divides by projections, the alert level by faults
    # (ADR-0028 D2). They coincide on a single-lane corpus, where the phrase collapses to what
    # it always said.
    scale = f"{report.incidents} labelled incident(s)"
    if report.faults and report.faults != report.incidents:
        scale += f" across {report.faults} fault(s)"
    lines = [
        f"corpus: {report.corpus} - {report.corpus_description}",
        f"  {report.cases} case(s), {scale}   fit protocol: {report.fit_protocol}",
        "",
    ]
    if report.incidents == 0:
        # Every rate is undefined here and prints as '-'; what the reader needs instead is
        # the framing, since the absolute det/alerts columns are pure false alarms.
        lines.append("  no labelled incidents: every detection and alert below is a FALSE ALARM")
    lines.extend(_summary_table(report.specs))

    for spec in report.specs:
        lines.extend(["", f"--- {spec.spec} - per case ---"])
        lines.append(
            f"{'case':<24}{'inc':>5}{'det':>6}{'alerts':>8}{'evF1':>8}"
            f"{'det/inc':>9}{'alt/inc':>9}{'latency':>10}"
        )
        for case in spec.cases:
            latency = (
                "-" if case.mean_latency_seconds is None else f"{case.mean_latency_seconds:.0f}s"
            )
            lines.append(
                f"{case.case[:23]:<24}{case.incidents:>5}{case.detections:>6}{case.alerts:>8}"
                f"{_cell(case.event_f1)}{_cell(case.detections_per_incident, 9)}"
                f"{_cell(case.alerts_per_incident, 9)}{latency:>10}"
            )
        notes = {note for case in spec.cases for note in case.notes}
        lines.extend(f"  ! {note}" for note in sorted(notes))
        lines.extend(f"  * {fact}" for fact in _attribution(spec))

    return "\n".join(lines)


def _attribution(spec: SpecReport) -> list[str]:
    """Multi-asset and multi-lane facts the fixed-width table has no column for.

    Printed only when non-zero, so a single-asset single-lane corpus renders exactly as it
    always did. They earn the space because each is a number that used to be invisible: a
    detection at the right moment on the wrong device (or the wrong modality) scored as a
    correct catch, an asserted-clean device raising an alert was indistinguishable from a real
    catch elsewhere, and findings the corpus cannot adjudicate were folded into rates it could
    not justify.

    The per-lane lines are the reason the pooled ``ptF1`` column is no longer allowed to stand
    alone (ADR-0028 D4). Pooling weights each modality by how fast it samples, so a corpus whose
    flow lane emits a flow every two seconds can report a headline point precision of 0.39
    while the same run's flow-lane precision *without* one deliberately chatty control device is
    0.90. Both numbers are true; only the per-lane one is diagnosable. (These move whenever a
    corpus profile changes; they illustrate the shape, and the per-lane columns are the
    thing to read.)
    """
    facts: list[str] = []
    misattributed = sum(case.misattributed_detections for case in spec.cases)
    if misattributed:
        facts.append(
            f"{misattributed} detection(s) fired at a faulty moment on a DIFFERENT asset "
            "(counted as false positives, not catches)"
        )
    off_lane = sum(case.off_lane_detections for case in spec.cases)
    if off_lane:
        facts.append(
            f"{off_lane} detection(s) fired at a faulty moment on the right asset but a lane "
            "the corpus says showed nothing (false positives, not catches)"
        )
    alerted = sum(case.clean_assets_alerted for case in spec.cases)
    if alerted:
        clean = sum(case.clean_assets for case in spec.cases)
        facts.append(f"{alerted} of {clean} asserted-clean asset(s) raised alerts")
    unscored = sum(case.unlabelled_detections + case.unlabelled_alerts for case in spec.cases)
    if unscored:
        facts.append(
            f"{unscored} finding(s) on assets the corpus does not label: excluded from "
            "every rate, never counted as false"
        )
    off_clock = sum(case.unlabelled_lane_detections for case in spec.cases)
    if off_clock:
        facts.append(
            f"{off_clock} detection(s) on a lane the corpus keeps no clock for: excluded from "
            "every rate, in both directions"
        )
    # Pooled from counts, not averaged over cases — the same rule `summary()` follows, now that
    # `CaseReport.lanes` carries the counts behind these columns (ADR-0028 D4).
    by_lane = spec.rates_by_lane()
    if by_lane:
        recall = ", ".join(
            f"{lane}={_cell(rates['event_recall'], 0)}" for lane, rates in by_lane.items()
        )
        facts.append(f"per-lane event recall: {recall}")
        point = ", ".join(
            f"{lane}=P{_cell(rates['point_precision'], 0)}/R{_cell(rates['point_recall'], 0)}"
            for lane, rates in by_lane.items()
        )
        facts.append(f"per-lane point precision/recall: {point}")
    return facts
