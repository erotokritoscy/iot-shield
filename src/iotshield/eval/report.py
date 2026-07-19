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

from datetime import UTC, datetime

from pydantic import BaseModel, Field

from iotshield.eval.score import AlertMetrics, DetectorMetrics


def _utc_now() -> datetime:
    return datetime.now(UTC)


class CaseReport(BaseModel):
    """One spec's result on one labelled case."""

    case: str
    incidents: int
    detections: int
    alerts: int
    fitted: bool
    notes: list[str] = Field(default_factory=list)
    point_f1: float
    event_precision: float
    event_recall: float
    event_f1: float
    detections_per_incident: float
    alerts_per_incident: float
    incident_coverage: float
    mean_latency_seconds: float | None
    mean_rank_of_true: float | None
    precision_at_k: dict[int, float | None] = Field(default_factory=dict)

    @classmethod
    def build(
        cls,
        *,
        case: str,
        detector_metrics: DetectorMetrics,
        alert_metrics: AlertMetrics,
        fitted: bool,
        notes: list[str],
    ) -> "CaseReport":
        return cls(
            case=case,
            incidents=detector_metrics.incidents,
            detections=detector_metrics.detections,
            alerts=alert_metrics.alerts,
            fitted=fitted,
            notes=notes,
            point_f1=detector_metrics.point_f1,
            event_precision=detector_metrics.event_precision,
            event_recall=detector_metrics.event_recall,
            event_f1=detector_metrics.event_f1,
            detections_per_incident=detector_metrics.detections_per_incident,
            alerts_per_incident=alert_metrics.alerts_per_incident,
            incident_coverage=alert_metrics.incident_coverage,
            mean_latency_seconds=detector_metrics.mean_latency_seconds,
            mean_rank_of_true=alert_metrics.mean_rank_of_true,
            precision_at_k=alert_metrics.precision_at_k,
        )


class SpecReport(BaseModel):
    """One benchmarked pipeline spec, across every case in the corpus."""

    spec: str
    detectors: list[str]
    alert_engine: str
    cases: list[CaseReport]

    def _mean(self, attribute: str) -> float:
        values = [getattr(case, attribute) for case in self.cases]
        return sum(values) / len(values) if values else 0.0

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
        alerts_per_incident = self._mean("alerts_per_incident")
        return (
            self._mean("detections_per_incident") / alerts_per_incident
            if alerts_per_incident
            else None
        )

    @property
    def is_clean_corpus(self) -> bool:
        """No labelled incidents anywhere — so every detection and alert is a false alarm.

        The per-incident ratios are all division-by-zero here and would render as 0.00,
        which reads like a perfect score rather than "not applicable". The caller switches
        to absolute counts instead.
        """
        return all(case.incidents == 0 for case in self.cases)

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


def render(report: EvaluationReport) -> str:
    """Render the head-to-head comparison, then per-case detail for each spec."""
    # ASCII-only separators: Windows consoles mangle UTF-8 punctuation (as RunReport found).
    clean = report.incidents == 0
    lines = [
        f"corpus: {report.corpus} - {report.corpus_description}",
        f"  {report.cases} case(s), {report.incidents} labelled incident(s)"
        f"   fit protocol: {report.fit_protocol}",
        "",
    ]
    if clean:
        # No incidents means the per-incident ratios are undefined, and rendering them as
        # 0.00 would read as a perfect score. Absolute false-alarm counts are the answer here.
        lines.append("  no labelled incidents: every detection and alert below is a FALSE ALARM")
        lines.append(f"{'spec':<22}{'detectors':<28}{'false det':>11}{'false alerts':>14}")
        for spec in report.specs:
            summary = spec.summary()
            lines.append(
                f"{spec.spec[:21]:<22}{','.join(spec.detectors)[:27]:<28}"
                f"{_cell(summary['detections'], 11, 1)}{_cell(summary['alerts'], 14, 1)}"
            )
        return "\n".join(lines)

    lines.append(
        f"{'spec':<22}{'detectors':<28}{'evF1':>8}{'evP':>8}{'evR':>8}"
        f"{'ptF1':>8}{'det/inc':>9}{'alt/inc':>9}{'cover':>8}{'fitted':>8}"
    )
    for spec in report.specs:
        summary = spec.summary()
        lines.append(
            f"{spec.spec[:21]:<22}{','.join(spec.detectors)[:27]:<28}"
            f"{_cell(summary['event_f1'])}{_cell(summary['event_precision'])}"
            f"{_cell(summary['event_recall'])}{_cell(summary['point_f1'])}"
            f"{_cell(summary['detections_per_incident'], 9)}"
            f"{_cell(summary['alerts_per_incident'], 9)}"
            f"{_cell(summary['incident_coverage'])}"
            f"{f'{spec.cases_fitted}/{len(spec.cases)}':>8}"
        )

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
