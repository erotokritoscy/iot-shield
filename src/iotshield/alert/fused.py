"""AlertEngine adapter: upstream's competence-weighted noisy-OR (ADR-0048 D6). Off by default.

``joint = 1 - prod(1 - W[detector][signature])``, so agreement raises confidence and a lone flag
from a detector that is poor at what it claims stays low. It is here so it can be measured
head-to-head with ``weighted`` on the same detections, not because the evidence favours it. On
LEAD's real labels nothing upstream tried beat Isolation Forest alone, and the weights below are:

- **synthetic-harness recall**, from upstream's own injected anomalies on 4-12 meters per source.
  LOF's rating on that harness collapsed against real labels.
- **recall, not precision**: ``W`` is P(flag | anomaly), used as if it were P(anomaly | flag). A
  detector that flags everything would score 1.0. Upstream's fusion ignores its false-alarm
  column, and so does this.

Three deliberate differences from ``meterdetect/fusion.py``, which is also where ``WEIGHTS`` comes
from (``outputs/reliability_weights.json``, pooled matrix; see ADR-0048 on provenance):

1. **A low score is a low-priority alert, not a dropped one.** ``Fuser.run`` removes incidents
   under 0.30, which the port forbids: every detection is covered by an alert and ranking filters
   rather than hiding (ADR-0004 D3).
2. **Each detector is weighted by what it claimed, not by one type chosen for the incident.**
   Upstream types the incident by its most competent member and looks every detector up under
   that type, so a new detection could change the type and *lower* the joint score, which breaks
   the port's monotonicity law. With baseline(spike) + lof(spike) the joint is 0.89; add
   baseline(stuck) and upstream's answer falls to 0.88. Here a detector contributes the best
   weight among its own claims, which only ever rises.
3. **Anything without a measured weight gets ``other_weight``**, since the matrix only covers
   three meter detectors and an engine must take every lane. It is a typed-in number, and so any
   ranking it causes outside metering is not evidence of anything (ADR-0021 D7).

Asset criticality is not a factor: the matrix has nowhere to put it, and multiplying it in would
make the score stop meaning a probability. A heartbeat from ``meter_quality`` is structural, as
upstream's transmission gap is: joint 1.0.
"""

from collections.abc import Mapping, Sequence
from datetime import timedelta

from iotshield.alert._correlate import (
    correlate_and_rank,
    counted,
    detection_margin,
    distinct_judgments,
    narrate,
    severity_from_priority,
)
from iotshield.capabilities import ALERTS, DETECTIONS, Capability
from iotshield.contracts import Asset, DetectionResult, PrioritizedAlert
from iotshield.meter import SIGNATURE_KEY, QualityIssue, Signature

CAPABILITY = Capability.of(consumes={DETECTIONS}, produces={ALERTS})

WEIGHTS: dict[str, dict[str, float]] = {
    "meter_iforest": {
        Signature.SPIKE: 0.45,
        Signature.SUSTAINED: 0.98,
        Signature.PROFILE: 0.01,
        Signature.STUCK: 0.02,
    },
    "meter_lof": {
        Signature.SPIKE: 0.68,
        Signature.SUSTAINED: 0.95,
        Signature.PROFILE: 0.34,
        Signature.STUCK: 0.03,
    },
    "meter_baseline": {
        Signature.SPIKE: 0.67,
        Signature.SUSTAINED: 0.67,
        Signature.PROFILE: 0.12,
        Signature.STUCK: 0.88,
    },
}
"""Upstream's pooled matrix at ~1 alarm per meter-month. See the module docstring's caveats."""

_STRUCTURAL = 1.0
_QUALITY_METHOD = "meter_quality"
_QUALITY_KEY = "quality"


class FusedAlertEngine:
    def __init__(self, bucket: timedelta = timedelta(minutes=5), other_weight: float = 0.5) -> None:
        if not 0.0 <= other_weight < 1.0:
            raise ValueError(f"other_weight must be in [0, 1), got {other_weight!r}")
        self._bucket = bucket
        self._other_weight = other_weight

    def build_alerts(
        self,
        detections: Sequence[DetectionResult],
        assets: Mapping[str, Asset],
    ) -> list[PrioritizedAlert]:
        return correlate_and_rank(
            detections,
            bucket=self._bucket,
            build_alert=lambda group: self._build_alert(group, assets.get(group[0].asset_id)),
        )

    def weight_of(self, detection: DetectionResult) -> float:
        """How much one detection is worth, from who claimed it and what they claimed."""
        evidence = detection.evidence
        if (
            detection.method == _QUALITY_METHOD
            and evidence.get(_QUALITY_KEY) == QualityIssue.HEARTBEAT.value
        ):
            return _STRUCTURAL
        table = WEIGHTS.get(detection.method)
        if table is None:
            return self._other_weight
        return table.get(str(evidence.get(SIGNATURE_KEY)), self._other_weight)

    def _build_alert(self, group: list[DetectionResult], asset: Asset | None) -> PrioritizedAlert:
        asset_id = group[0].asset_id
        strongest = max(group, key=detection_margin)

        best: dict[str, float] = {}
        for detection in group:
            best[detection.method] = max(best.get(detection.method, 0.0), self.weight_of(detection))
        miss = 1.0
        for weight in best.values():
            miss *= 1.0 - weight
        joint = 1.0 - miss
        priority = 100.0 * joint

        named = ", ".join(f"{method} {weight:.2f}" for method, weight in sorted(best.items()))
        asset_label = asset.name if asset else "unregistered asset"
        judgments = distinct_judgments(group)
        rationale = (
            f"priority {priority:.1f} = 100 x noisy-OR of detector competence ({named}); "
            f"{counted(len(group), judgments)} on {asset_label} ({asset_id}); strongest: "
            f"{strongest.method} score {strongest.score:.4g} vs threshold "
            f"{strongest.threshold:.4g}."
        )
        window_start = min(detection.window_start for detection in group)
        window_end = max(detection.window_end for detection in group)
        explanation = narrate(
            group,
            rationale=rationale,
            asset_id=asset_id,
            window_start=window_start,
            strongest=strongest,
        )
        return PrioritizedAlert(
            asset_id=asset_id,
            window_start=window_start,
            window_end=window_end,
            severity=severity_from_priority(priority),
            priority_score=priority,
            threat_class=explanation.threat_class,
            rationale=explanation.rationale,
            score_components={"joint": joint, **{f"w_{m}": w for m, w in sorted(best.items())}},
            detection_ids=[detection.detection_id for detection in group],
            recommended_action=explanation.recommended_action,
        )
