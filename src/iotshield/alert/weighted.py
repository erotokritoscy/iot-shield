"""AlertEngine adapter: team2's explainable multiplicative prioritization.

Re-implemented (not wrapped) from team2-alert-prioritization ("AlertIQ",
``reference/team2-alert-prioritization/backend/scoring.py``). team2 scores SIEM
alert records (severity / category / risk_score / confidence / entropy / ...) as a
multiplicative product of interpretable factors, with a decomposed human-readable
rationale, and ranks by batch-relative min-max normalization.

We cannot wrap it (ADR-0013): it consumes a different *kind* of input than our
detections carry, it has no correlation step (our port's core job), and its
batch-relative ranking plus its repetition *penalty* are incompatible with the
port's corroboration-monotonicity law. So we borrow team2's *philosophy* — a
product of interpretable factors, each explained, mapped to a severity label by
absolute thresholds — and re-express it on the three signals our detections
actually carry:
  - severity      <- the strongest detection's score/threshold margin (team2 severity),
  - corroboration <- cross-lane agreement in the bucket (team2's anomaly_factor hook),
  - volume        <- repeated detections in the bucket,
  - asset_weight  <- Asset.criticality (team2's device_weight).

Correlation, ranking, and the severity-label mapping are shared with the stub via
``_correlate``. The score maps to [0, 100] by an absolute, saturating transform
(NOT team2's batch-relative min-max, ADR-0013 D1). Every factor is >= 1 and
non-decreasing as detections join a bucket, so the port's monotonicity law — and
the score staying in range — hold by construction.
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
from iotshield.contracts import Asset, Criticality, DetectionResult, PrioritizedAlert

# Consumes detections, produces alerts — the universal alert-engine shape.
CAPABILITY = Capability.of(consumes={DETECTIONS}, produces={ALERTS})

# team2's device_weight, re-expressed over our asset criticality (>= 1.0).
_CRITICALITY_WEIGHT = {
    Criticality.LOW: 1.0,
    Criticality.MEDIUM: 1.1,
    Criticality.HIGH: 1.2,
    Criticality.CRITICAL: 1.3,
}

# Factor tuning (every factor >= 1, so more of anything only raises priority).
_SEVERITY_CAP = 4.0  # margin past this doesn't raise the severity factor further
_CORROBORATION_STEP = 0.5  # per extra lane agreeing in the bucket
_VOLUME_STEP = 0.15  # per extra distinct judgment, up to the cap (echoes team2's 1.6 cap)
_VOLUME_CAP = 4
# priority = 100 * (raw - 1) / (raw - 1 + K): absolute, bounded [0, 100), monotone.
_SATURATION = 2.0


class WeightedAlertEngine:
    def __init__(self, bucket: timedelta = timedelta(minutes=5)) -> None:
        self._bucket = bucket

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

    def _build_alert(self, group: list[DetectionResult], asset: Asset | None) -> PrioritizedAlert:
        asset_id = group[0].asset_id
        strongest = max(group, key=detection_margin)
        lanes = {detection.lane for detection in group}

        severity = max(1.0, min(detection_margin(strongest), _SEVERITY_CAP))
        corroboration = 1.0 + _CORROBORATION_STEP * (len(lanes) - 1)
        # Distinct judgments, not rows — see `distinct_judgments` (ADR-0033).
        judgments = distinct_judgments(group)
        volume = 1.0 + _VOLUME_STEP * min(judgments - 1, _VOLUME_CAP)
        criticality = asset.criticality if asset else Criticality.MEDIUM
        asset_weight = _CRITICALITY_WEIGHT[criticality]

        raw = severity * corroboration * volume * asset_weight
        priority = 100.0 * (raw - 1.0) / (raw - 1.0 + _SATURATION)

        asset_label = asset.name if asset else "unregistered asset"
        rationale = (
            f"priority {priority:.1f} = f(severity {severity:.2f} x corroboration"
            f" {corroboration:.2f} x volume {volume:.2f} x asset_weight {asset_weight:.2f}"
            f" -> raw {raw:.2f}); {counted(len(group), judgments)} across"
            f" {len(lanes)} lane(s) on"
            f" {asset_label} ({asset_id}, criticality {criticality}); strongest:"
            f" {strongest.method} score {strongest.score:.4g} vs threshold"
            f" {strongest.threshold:.4g}."
        )
        window_start = min(detection.window_start for detection in group)
        window_end = max(detection.window_end for detection in group)
        # What the alert SAYS is shared with the stub engine (ADR-0032 D5); only the number
        # above is this adapter's. Absent any authored claim the threat class stays
        # unclassified — an honest "nothing here knows what this is".
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
            score_components={
                "severity": severity,
                "corroboration": corroboration,
                "volume": volume,
                "asset_weight": asset_weight,
                "raw_score": raw,
            },
            detection_ids=[detection.detection_id for detection in group],
            recommended_action=explanation.recommended_action,
        )
