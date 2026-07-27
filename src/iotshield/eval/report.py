"""The evaluation report: a diffable artifact, and how it renders.

JSON rather than console-only on purpose. A benchmark's value compounds when you can compare
today's run to last week's — so the report is a serializable document CI could gate on
("this change dropped event-F1 by 0.1"), and the dashboard's Evaluate mode reads exactly
this file rather than launching runs of its own.

These models live here and **not** in ``contracts/``. ``contracts/`` is the *product's* core
vocabulary — what a normalizer, detector, and alert engine agree on. A benchmark report is
the *harness's* vocabulary, and keeping it out of the contracts package is itself the
statement that the harness sits outside the product (ADR-0021 D1).
"""

from dataclasses import asdict, dataclass
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from iotshield.contracts import Lane
from iotshield.eval.score import AlertMetrics, DetectorMetrics, f1, mean, ratio


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    unlabelled_detections: int = 0
    unlabelled_alerts: int = 0
    clean_assets: int = 0
    clean_assets_detected: int = 0
    clean_assets_alerted: int = 0
    lane_recall: dict[Lane, float | None] = Field(default_factory=dict)
    mean_true_priority: float | None = None
    mean_false_priority: float | None = None
    mean_lanes_per_true_alert: float | None = None
    mean_true_detection_overlap: float | None = None
    # The raw counts behind every rate above, so a corpus-level number can be *pooled* rather
    # than averaged from per-case rates — the only form in which a clean recording's false
    # alarms reach the headline (see ``SpecReport.summary``). Defaulted like the fields above
    # so an older report still parses.
    scoreable_detections: int = 0
    true_detections: int = 0
    incidents_caught: int = 0
    point_true_positive: int = 0
    point_false_positive: int = 0
    point_false_negative: int = 0
    scoreable_alerts: int = 0
    true_alerts: int = 0
    incidents_covered: int = 0
    timeline: CaseTimeline | None = None

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

    def _total(self, attribute: str) -> int:
        return sum(getattr(case, attribute) for case in self.cases)

    @property
    def cases_fitted(self) -> int:
        return sum(1 for case in self.cases if case.fitted)

    @property
    def compression_ratio(self) -> float | None:
        """Detections per incident ÷ alerts per incident — what the alert layer absorbed.

        The transfer function between the two levels, and the only number that shows the
        stages interacting: >1 means correlation compressed the detector's output, ~1 means
        it passed it through, and a clean detector with a ratio below 1 means the alert
        layer is *splitting* incidents rather than merging them (ADR-0021 D2).
        """
        incidents = self._total("incidents")
        alerts = self._total("scoreable_alerts")
        return self._total("scoreable_detections") / alerts if incidents and alerts else None

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
        much as a 300-case one — and it is the same rule ``score`` already applies within a
        case, now applied one level up rather than approximated.

        The per-case table keeps its own rates and its ``-`` where a rate does not exist; only
        the aggregate changes. ``detections`` and ``alerts`` stay means, because those columns
        report "per case" and always did.
        """
        incidents = self._total("incidents")
        point_positive = self._total("point_true_positive")
        event_precision = ratio(self._total("true_detections"), self._total("scoreable_detections"))
        event_recall = ratio(self._total("incidents_caught"), incidents)
        point_precision = ratio(
            point_positive, point_positive + self._total("point_false_positive")
        )
        point_recall = ratio(point_positive, point_positive + self._total("point_false_negative"))
        return {
            "event_f1": f1(event_precision, event_recall),
            "event_precision": event_precision,
            "event_recall": event_recall,
            "point_f1": f1(point_precision, point_recall),
            "detections": self._mean("detections"),
            "alerts": self._mean("alerts"),
            "detections_per_incident": ratio(self._total("scoreable_detections"), incidents),
            "alerts_per_incident": ratio(self._total("scoreable_alerts"), incidents),
            "incident_coverage": ratio(self._total("incidents_covered"), incidents),
            # Weighted by how many true detections each case contributed, which is what makes
            # it a pooled mean rather than a mean of means: `true_detections` is exactly the
            # count `mean_true_detection_overlap` was averaged over inside the case.
            "mean_true_detection_overlap": ratio(
                sum(
                    case.mean_true_detection_overlap * case.true_detections
                    for case in self.cases
                    if case.mean_true_detection_overlap is not None
                ),
                self._total("true_detections"),
            ),
            "compression_ratio": self.compression_ratio,
        }


class EvaluationReport(BaseModel):
    corpus: str
    corpus_description: str
    fit_protocol: str
    cases: int
    incidents: int
    generated_at: datetime = Field(default_factory=_utc_now)
    specs: list[SpecReport] = Field(default_factory=list)


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
            f"{spec.spec[:cell]:<{cell + 1}}{','.join(spec.detectors)[:27]:<28}{cells}{fitted:>8}"
        )
    return [f"{heading}{'fitted':>8}", *rows]


def render(report: EvaluationReport) -> str:
    """Render the head-to-head comparison, then per-case detail for each spec."""
    # ASCII-only separators: Windows consoles mangle UTF-8 punctuation (as RunReport found).
    lines = [
        f"corpus: {report.corpus} - {report.corpus_description}",
        f"  {report.cases} case(s), {report.incidents} labelled incident(s)"
        f"   fit protocol: {report.fit_protocol}",
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
    """Multi-asset facts the fixed-width table has no column for (ADR-0026).

    Printed only when non-zero, so a single-asset corpus renders exactly as it always did.
    They earn the space because each is a number that used to be invisible: a detection at
    the right moment on the wrong device scored as a correct catch, an asserted-clean device
    raising an alert was indistinguishable from a real catch elsewhere, and findings on an
    unlabelled device were silently folded into rates the corpus could not justify.
    """
    facts: list[str] = []
    misattributed = sum(case.misattributed_detections for case in spec.cases)
    if misattributed:
        facts.append(
            f"{misattributed} detection(s) fired at a faulty moment on a DIFFERENT asset "
            "(counted as false positives, not catches)"
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
    # Averaged over the cases where each lane is defined, the same rule as every other mean
    # in this report: a case that labelled no flow incident must not drag flow recall down.
    lanes: dict[Lane, list[float]] = {}
    for case in spec.cases:
        for lane, value in case.lane_recall.items():
            if value is not None:
                lanes.setdefault(lane, []).append(value)
    if lanes:
        # Averaged inline rather than through `score.mean`: that helper's job is to return
        # None for "nothing to average", and these lists are non-empty by construction (a
        # lane only appears here once a defined value was appended).
        rendered = ", ".join(
            f"{lane}={sum(values) / len(values):.2f}" for lane, values in sorted(lanes.items())
        )
        facts.append(f"per-lane event recall: {rendered}")
    return facts
