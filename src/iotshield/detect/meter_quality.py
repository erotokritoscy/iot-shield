"""Meter data quality: SensiPro's ``heartbeat`` and ``high`` anomalies, as detections (ADR-0043).

SensiPro's own anomaly script (``sensiproscript/script_finderrrorvalues.py``) compared each
pair of consecutive raw readings of a meter's cumulative counter and recorded four types;
its ``anomalyclass`` table names two more. In iotshield four of the six are refused at
ingest by the meter normalizer, as tagged rejects on Data health (``meter.QualityIssue``) —
a counter that is unreadable, negative, fell to zero or went backwards leaves an interval
whose consumption is undefined, so no event exists for a detector to judge. This detector
covers the two that survive into events:

- **heartbeat** — the meter went silent. Consecutive consumption events of one meter more
  than ``max_silence_hours`` apart. Normal hourly reporting is 1 h apart, and the meter
  normalizer emits no event across a gap (ADR-0030), so a single missed reading already
  leaves the neighbouring events 3 h apart: the default of 2 flags it.
- **high** — an hour's consumption above a fixed limit per utility. The defaults are the
  script's own numbers (35 kWh, 0.075 m3), kept so the two systems start from one
  agreement — but the script applied them to each step of the raw ``dst_*_atomic`` series,
  measured at about 3.5 minutes apart (~17 to the hour), and these are whole hours, so the
  same number is roughly 17 times *stricter* here (its hourly equivalent is ~600 kWh and
  ~1.3 m3). Measured on a week of the real fleet it flags 42 of 373 water meters and
  5 of 440 electricity meters, each continuously: large consumers, not anomalies. They are
  params for that reason. Consecutive high hours form one detection, so a sustained surge is
  one finding.

Nothing is learned (no ``fit``, no artifact): the rules are written down, like ``rules``.
"""

from collections.abc import Sequence
from datetime import timedelta
from itertools import pairwise
from math import isfinite

from iotshield.capabilities import DETECTIONS, Capability, LaneToken, PayloadToken
from iotshield.contracts import DetectionResult, EventKind, Lane, NormalizedEvent
from iotshield.detect._group import group_by_asset
from iotshield.detect._runs import classify_run, merge_runs
from iotshield.enrichment import EVIDENCE_KEY, Enrichment, EnrichmentSource
from iotshield.meter import METADATA_KEY, MeterReading, MeterType, QualityIssue, reading_of

METHOD = "meter_quality"

CAPABILITY = Capability.of(
    consumes={LaneToken(Lane.TELEMETRY), PayloadToken(METADATA_KEY)},
    produces={DETECTIONS},
)

ENRICHMENT: dict[QualityIssue, Enrichment] = {
    # No MITRE mapping, as for log_burst's error storm: a silent or surging meter is an
    # operational fact first, and naming an attack technique for it would be invention.
    QualityIssue.HEARTBEAT: Enrichment(
        threat_class="Meter data quality: missed readings",
        source=EnrichmentSource.DETECTOR,
        origin="meter_quality:heartbeat",
        suggested_actions=(
            "Check the meter's communication link and its gateway for the silent period.",
            "Confirm whether the readings are missing at the source or were lost in transit.",
        ),
    ),
    QualityIssue.HIGH: Enrichment(
        threat_class="Meter data quality: consumption above limit",
        source=EnrichmentSource.DETECTOR,
        origin="meter_quality:high",
        suggested_actions=(
            "Check whether the premises really consumed this much (a leak, a fault, a new load).",
            "Inspect the meter for a fault or a misread counter around this time.",
        ),
    ),
}


class MeterQualityDetector:
    def __init__(
        self, high_kwh: float = 35.0, high_m3: float = 0.075, max_silence_hours: float = 2.0
    ) -> None:
        for name, value in (
            ("high_kwh", high_kwh),
            ("high_m3", high_m3),
            ("max_silence_hours", max_silence_hours),
        ):
            # Positive and finite for the same reason stub_threshold refuses otherwise: the
            # threshold is the alert engine's margin denominator (ADR-0033).
            if not isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive, finite number, got {value!r}")
        self._limits = {MeterType.ELECTRICITY: high_kwh, MeterType.WATER: high_m3}
        self._max_silence = timedelta(hours=max_silence_hours)

    @property
    def lane(self) -> Lane:
        return Lane.TELEMETRY

    @property
    def method(self) -> str:
        return METHOD

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        detections: list[DetectionResult] = []
        for asset_id, asset_events in group_by_asset(
            events, lane=Lane.TELEMETRY, keep=lambda event: reading_of(event.metadata) is not None
        ):
            readings = [
                (event, MeterReading.from_metadata(event.metadata[METADATA_KEY]))
                for event in asset_events
            ]
            detections += self._heartbeats(asset_id, readings)
            detections += self._highs(asset_id, readings)
        return detections

    def _heartbeats(
        self, asset_id: str, readings: list[tuple[NormalizedEvent, MeterReading]]
    ) -> list[DetectionResult]:
        found: list[DetectionResult] = []
        for (before, before_reading), (after, _) in pairwise(readings):
            silence = after.timestamp - before.timestamp
            if silence <= self._max_silence:
                continue
            hours = silence.total_seconds() / 3600
            interval = before_reading.interval_hours
            # Readings are `interval` apart and the reading after a gap only opens the next
            # interval, so the events either side are `missing + 2` intervals apart.
            missing = max(1, round(hours / interval) - 2) if interval > 0 else 0
            found.append(
                self._detection(
                    QualityIssue.HEARTBEAT,
                    asset_id,
                    before,
                    after,
                    score=hours,
                    threshold=self._max_silence.total_seconds() / 3600,
                    kind=EventKind.CONTEXTUAL,
                    explanation=(
                        f"meter {asset_id} reported nothing for {hours:.12g} h between "
                        f"{before.timestamp:%Y-%m-%d %H:%M} and "
                        f"{after.timestamp:%Y-%m-%d %H:%M} UTC (about {missing} missed reading(s))"
                    ),
                    extra={"silence_hours": hours, "missed_readings": missing},
                )
            )
        return found

    def _highs(
        self, asset_id: str, readings: list[tuple[NormalizedEvent, MeterReading]]
    ) -> list[DetectionResult]:
        def limit(reading: MeterReading) -> float:
            return self._limits[reading.meter_type]

        mask = [
            event.value is not None and event.value > limit(reading) for event, reading in readings
        ]
        found: list[DetectionResult] = []
        for start, end in merge_runs(mask):
            run = readings[start : end + 1]
            first, first_reading = run[0]
            last, _ = run[-1]
            peak = max(event.value or 0.0 for event, _ in run)
            unit = first_reading.unit
            hours = end - start + 1
            found.append(
                self._detection(
                    QualityIssue.HIGH,
                    asset_id,
                    first,
                    last,
                    score=peak,
                    threshold=limit(first_reading),
                    kind=classify_run(hours),
                    explanation=(
                        f"meter {asset_id} consumed up to {peak:.12g} {unit} in an hour, above the "
                        f"limit of {limit(first_reading):.12g} {unit}"
                        + (f", for {hours} consecutive hours" if hours > 1 else "")
                    ),
                    extra={"peak": peak, "unit": unit, "hours": hours},
                )
            )
        return found

    def _detection(
        self,
        issue: QualityIssue,
        asset_id: str,
        first: NormalizedEvent,
        last: NormalizedEvent,
        *,
        score: float,
        threshold: float,
        kind: EventKind,
        explanation: str,
        extra: dict[str, object],
    ) -> DetectionResult:
        category, subcategory = issue.sensipro_code
        return DetectionResult(
            asset_id=asset_id,
            window_start=first.timestamp,
            window_end=last.timestamp,
            lane=Lane.TELEMETRY,
            method=METHOD,
            score=score,
            threshold=threshold,
            event_kind=kind,
            evidence={
                "explanation": explanation,
                "quality": issue.value,
                "sensipro_code": [category, subcategory],
                **extra,
                EVIDENCE_KEY: ENRICHMENT[issue].to_evidence(),
            },
        )
