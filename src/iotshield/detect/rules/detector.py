"""Rule detector: named, authored signatures over single events (ADR-0032).

The capability gap this fills
-----------------------------
Every detector shipped before this one answers the same shape of question: *is this
unusual?* The threshold detector asks it of a number, team3's z-score and the meter forest
ask it of learned normal behaviour, the burst detectors ask it of a rate. They are all, at
bottom, statistics.

None of them can answer the other question a security product is expected to answer: *is
this a known-bad thing?* That distinction is not academic — it is the difference between
"this device's traffic is 8 sigma above its normal" (an observation an analyst must
interpret) and "this device attempted an SMB connection, which is lateral movement" (a named
accusation that carries its own meaning). The two families fail in opposite directions:
statistics catch what nobody anticipated but cannot say what it is; rules say exactly what
something is but only catch what somebody already wrote down. We had only one half.

The second thing it changes is who can extend the product. Every other detector requires
Python, the contracts, and the port suite to extend. This one is extended by writing a text
file — which is the difference between a system experts extend and a system experts *use*,
and therefore the thing most likely to keep this repo alive after the internship.

Provenance: the rule *grammar* and three of the shipped rules come from Antonia Voutouri's
``iot-ot-alert-correlation`` (see ``library/`` for per-rule credit, and ADR-0032 for the
integration agreement). The engine is re-implemented rather than wrapped, as that agreement
proposed: hers reads her canonical event
model, and converting our events into it would mean inventing fields we do not have
(ADR-0032 D1).

Shape of the adapter (ADR-0032 D3)
----------------------------------
**One adapter, one instance per lane, many rules per instance.** The Detector port gives a
detector a single ``lane`` and the pipeline routes events by it, so a rule set spanning
network and logs is two configured entries — not two adapters, and emphatically not one
entry per rule, which would put every new rule into every deployment's config and destroy
the "drop in a file" promise.

That shape forces one thing worth knowing: ``method`` is a property of the *detector*, and
the port suite requires every detection to carry it, so the firing rule cannot be named
there. It is named in ``Enrichment.origin`` instead (``rule:high-upload-volume``), which is
what reaches the alert rationale — the text an analyst actually reads.

Matches merge into episodes (ADR-0032 D2)
------------------------------------------
A ``DetectionResult`` is one judgment over a window, and the alert engine counts detections
in a bucket as *corroborating evidence*. Emitting one detection per matching record would
therefore restate a single fact hundreds of times and inflate the resulting alert's
priority: ``fleet_scan``'s exfiltration recording alone holds ~400 matching flows. So
matches of one rule on one asset within ``merge_gap_minutes`` collapse into one detection
carrying the count, and the analyst reads "fired 6 times in 5 minutes" as one queue entry.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from typing import Any

from iotshield.capabilities import DETECTIONS, Capability, LaneToken, PayloadToken, Token
from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect._group import group_by_asset
from iotshield.detect._runs import classify_run, group_by_gap, sample_messages
from iotshield.detect.rules.fields import PAYLOAD_BY_LANE, EventView
from iotshield.detect.rules.loader import load_rules
from iotshield.detect.rules.model import Rule
from iotshield.enrichment import EVIDENCE_KEY

DEFAULT_LANE = Lane.FLOW
"""Most shipped rules are network signatures, and the flow lane is where a rule most clearly
earns its place next to the statistical detectors. Mirrored by ``registry._rules``."""


def _at(event: NormalizedEvent) -> datetime:
    return event.timestamp


DEFAULT_MERGE_GAP_MINUTES = 5.0
"""Matches this close together are one episode. Defaulted to the alert engine's own bucket
width: anything that would land in the same alert should not be split into separate
findings first, since splitting only to re-merge is where the double-counting comes from."""


class RuleDetector:
    def __init__(
        self,
        *,
        lane: Lane = DEFAULT_LANE,
        ruleset: str | None = None,
        merge_gap_minutes: float = DEFAULT_MERGE_GAP_MINUTES,
    ) -> None:
        if merge_gap_minutes < 0:
            raise ValueError(f"merge_gap_minutes must not be negative, got {merge_gap_minutes}")
        self._lane = lane
        self._ruleset = ruleset
        # Loaded at CONSTRUCTION, not at first detect(): a malformed rule, an unknown field
        # or a lane with no rules is a config error, and a config error must fail before the
        # run has any side effect (the same contract every other adapter's params honour).
        self._rules = load_rules(lane, ruleset)
        self._gap = timedelta(minutes=merge_gap_minutes)

    @property
    def lane(self) -> Lane:
        return self._lane

    @property
    def method(self) -> str:
        # The lane rides in the method because two configured instances of this adapter
        # differ by lane and therefore judge with different rules — the port's requirement
        # that two instances judging differently must not share a name. Which *rule* fired
        # cannot live here (one method per detector, port law 1); it rides in the
        # enrichment's `origin` and in evidence.
        return f"rules:{self._lane.value}"

    @property
    def rules(self) -> tuple[Rule, ...]:
        """The compiled rule set, so a run can report what it loaded."""
        return self._rules

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        detections: list[DetectionResult] = []
        # keep=True: unlike the burst detectors, "is this judgeable" is per-rule rather than
        # per-detector, so every event of our lane reaches the rule loop. The routing laws
        # (foreign lanes dropped, assets sorted, events time-sorted) still come from here.
        for asset_id, asset_events in group_by_asset(
            events, lane=self._lane, keep=lambda _event: True
        ):
            # Decoded once per event, outside the rule loop: with R rules this is one
            # FlowRecord parse instead of R (measured: the cache saves more than the rest of
            # detect() costs on the flow lane).
            views = [EventView(event) for event in asset_events]
            for rule in self._rules:
                matched = [view.event for view in views if rule.matches(view)]
                episodes = group_by_gap(matched, gap=self._gap, timestamp=_at)
                detections.extend(
                    DetectionResult(
                        asset_id=asset_id,
                        window_start=episode[0].timestamp,
                        window_end=episode[-1].timestamp,
                        lane=self._lane,
                        method=self.method,
                        score=rule.score,
                        threshold=rule.threshold,
                        event_kind=classify_run(len(episode)),
                        evidence=self._evidence(rule, episode),
                    )
                    for episode in episodes
                )
        return detections

    def _evidence(self, rule: Rule, episode: Sequence[NormalizedEvent]) -> dict[str, Any]:
        return {
            # The analyst-readable headline, the convention the meter detector established
            # (ADR-0030) and the one key every detector is expected to emit (ADR-0032 D7).
            "explanation": rule.explanation,
            "rule_id": rule.id,
            "rule_title": rule.title,
            "matched_events": len(episode),
            "severity_hint": rule.severity_hint.value,
            "confidence": rule.confidence.value,
            "sample_messages": sample_messages(episode),
            # The typed record that crosses detect -> alert. Written through the vocabulary
            # module so neither side ever names a key (iotshield/enrichment.py).
            EVIDENCE_KEY: rule.enrichment.to_evidence(),
        }


def capability(params: Mapping[str, Any]) -> Capability:
    """This detector's lane is configurable, so its declared capability follows params.

    Mirrors the factory's default (``registry._rules``); the meta-test in
    tests/pipeline/test_capabilities.py pins the two together against a built instance.

    The payload follows the lane rather than the adapter, which is the clearest case that
    ADR-0035 D2's axis is per-*configuration*: ``rules(lane: flow)`` reads every flow field
    through ``FlowRecord`` and needs the payload, while ``rules(lane: log)`` reads the
    contract's own fields and needs none. One adapter, two different plug shapes.
    """
    lane = Lane(params.get("lane", DEFAULT_LANE))
    consumes: set[Token] = {LaneToken(lane)}
    consumes |= {PayloadToken(payload) for payload in PAYLOAD_BY_LANE.get(lane, frozenset())}
    return Capability.of(consumes=consumes, produces={DETECTIONS})
