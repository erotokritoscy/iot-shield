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

from iotshield.eval.score import AlertMetrics, DetectorMetrics


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
    ``None`` on any ratio means *undefined*, never zero — see ``score._ratio``.
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

        Skipping ``None`` rather than treating it as zero is what keeps a corpus that mixes
        clean and faulty recordings honest: the clean ones have no recall to average in, and
        counting them as 0.0 would silently drag every rate down.
        """
        values = [value for case in self.cases if (value := getattr(case, attribute)) is not None]
        return sum(values) / len(values) if values else None

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
        detections = self._mean("detections_per_incident")
        alerts = self._mean("alerts_per_incident")
        return detections / alerts if detections is not None and alerts else None

    def summary(self) -> dict[str, float | None]:
        return {
            "event_f1": self._mean("event_f1"),
            "event_precision": self._mean("event_precision"),
            "event_recall": self._mean("event_recall"),
            "point_f1": self._mean("point_f1"),
            "detections": self._mean("detections"),
            "alerts": self._mean("alerts"),
            "detections_per_incident": self._mean("detections_per_incident"),
            "alerts_per_incident": self._mean("alerts_per_incident"),
            "incident_coverage": self._mean("incident_coverage"),
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
    _Column("ptF1", "point_f1"),
    _Column("det/inc", "detections_per_incident", 9),
    _Column("alt/inc", "alerts_per_incident", 9),
    _Column("cover", "incident_coverage"),
    _Column("det", "detections", 8, 0),
    _Column("alerts", "alerts", 8, 0),
)


def _summary_table(specs: list[SpecReport]) -> list[str]:
    heading = f"{'spec':<22}{'detectors':<28}" + "".join(
        f"{column.heading:>{column.width}}" for column in _SUMMARY_COLUMNS
    )
    rows = []
    for spec in specs:
        summary = spec.summary()
        cells = "".join(
            _cell(summary[column.key], column.width, column.places) for column in _SUMMARY_COLUMNS
        )
        fitted = f"{spec.cases_fitted}/{len(spec.cases)}"
        rows.append(f"{spec.spec[:21]:<22}{','.join(spec.detectors)[:27]:<28}{cells}{fitted:>8}")
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

    return "\n".join(lines)
