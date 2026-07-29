"""The rule detector adapter: episodes, scoring, evidence, configuration (ADR-0032 D3)."""

import re
from datetime import timedelta
from pathlib import Path

import pytest

from iotshield.contracts import EventKind, Lane, NormalizedEvent, Severity
from iotshield.detect.rules import RuleDetector
from iotshield.enrichment import EVIDENCE_KEY, Enrichment, EnrichmentSource
from iotshield.pipeline.registry import build_detector, detector_capability
from tests.rule_fixtures import T0, flow_event, log_event, rule_document, write_ruleset


def upload(offset_s: int, **kwargs: object) -> NormalizedEvent:
    """An event the packaged `high-upload-volume` rule matches and no other flow rule does."""
    return flow_event(offset_s, bytes_out=250_000, **kwargs)  # type: ignore[arg-type]


def only(detections: list, rule_id: str) -> list:  # type: ignore[type-arg]
    return [d for d in detections if d.evidence["rule_id"] == rule_id]


# --- Episode formation (D2) ------------------------------------------------------------


def test_matches_close_together_become_one_detection() -> None:
    """The decision that keeps the queue honest. One detection per matching record would
    restate a single fact hundreds of times, and the alert engine counts detections in a
    bucket as corroborating evidence — so the repetition would inflate the priority."""
    detector = RuleDetector(lane=Lane.FLOW)
    detections = only(
        detector.detect([upload(index * 10) for index in range(6)]), "high-upload-volume"
    )
    assert len(detections) == 1
    assert detections[0].evidence["matched_events"] == 6
    assert detections[0].window_start == T0
    assert detections[0].window_end == T0 + timedelta(seconds=50)


def test_matches_separated_by_more_than_the_gap_are_separate_judgments() -> None:
    detector = RuleDetector(lane=Lane.FLOW, merge_gap_minutes=5.0)
    detections = only(detector.detect([upload(0), upload(600)]), "high-upload-volume")
    assert len(detections) == 2


def test_interleaved_matches_still_merge() -> None:
    """Why the merge is by TIME rather than by adjacency in the event sequence: an asset's
    flows interleave, so an adjacency merge would collapse straight back to one detection
    per match — which is the behaviour it exists to avoid."""
    detector = RuleDetector(lane=Lane.FLOW)
    events = [upload(0), flow_event(10), upload(20), flow_event(30), upload(40)]
    detections = only(detector.detect(events), "high-upload-volume")
    assert len(detections) == 1
    assert detections[0].evidence["matched_events"] == 3


def test_a_zero_gap_makes_every_match_its_own_judgment() -> None:
    detector = RuleDetector(lane=Lane.FLOW, merge_gap_minutes=0.0)
    assert len(only(detector.detect([upload(0), upload(1)]), "high-upload-volume")) == 2


def test_a_negative_gap_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        RuleDetector(lane=Lane.FLOW, merge_gap_minutes=-1.0)


def test_episode_length_sets_the_event_kind() -> None:
    detector = RuleDetector(lane=Lane.FLOW)
    single = only(detector.detect([upload(0)]), "high-upload-volume")
    many = only(detector.detect([upload(index) for index in range(4)]), "high-upload-volume")
    assert single[0].event_kind is EventKind.POINT
    assert many[0].event_kind is EventKind.COLLECTIVE


def test_each_asset_is_judged_separately() -> None:
    detector = RuleDetector(lane=Lane.FLOW)
    events = [upload(0, asset="10.0.0.24"), upload(1, asset="10.0.0.25")]
    detections = only(detector.detect(events), "high-upload-volume")
    assert {d.asset_id for d in detections} == {"10.0.0.24", "10.0.0.25"}


def test_each_rule_judges_independently() -> None:
    """Two rules matching the same event are two findings, because they are two claims."""
    detector = RuleDetector(lane=Lane.FLOW)
    detections = detector.detect([upload(0, peer_port=23)])
    assert {d.evidence["rule_id"] for d in detections} == {
        "high-upload-volume",
        "telnet-outbound-propagation",
    }


# --- Scoring and identity (D3) ---------------------------------------------------------


def test_the_margin_carries_the_authored_severity_and_confidence() -> None:
    """`high-upload-volume` is medium severity at medium confidence -> 2.0 x 0.85."""
    detector = RuleDetector(lane=Lane.FLOW)
    detection = only(detector.detect([upload(0)]), "high-upload-volume")[0]
    assert detection.score == pytest.approx(1.7)
    assert detection.threshold == pytest.approx(1.0)


def test_method_names_the_detector_and_the_lane() -> None:
    """The port requires every detection to carry the DETECTOR's method, so the firing rule
    cannot be named here — two instances differ by lane, which is what this must express.
    Which rule fired rides in the enrichment's `origin` and in evidence instead."""
    assert RuleDetector(lane=Lane.FLOW).method == "rules:flow"
    assert RuleDetector(lane=Lane.LOG).method == "rules:log"


def test_the_firing_rule_is_recoverable_from_the_detection() -> None:
    detector = RuleDetector(lane=Lane.FLOW)
    detection = only(detector.detect([upload(0)]), "high-upload-volume")[0]
    enrichment = Enrichment.from_detection(detection)
    assert enrichment is not None
    assert enrichment.origin == "rule:high-upload-volume"
    assert enrichment.source is EnrichmentSource.RULE


# --- Evidence --------------------------------------------------------------------------


def test_evidence_carries_the_headline_and_the_supporting_detail() -> None:
    detector = RuleDetector(lane=Lane.FLOW)
    evidence = only(detector.detect([upload(0), upload(5)]), "high-upload-volume")[0].evidence
    assert evidence["explanation"] == "Outbound bytes on one flow exceed 10000."
    assert evidence["rule_title"] == "High Outbound Upload Volume"
    assert evidence["severity_hint"] == "medium"
    assert evidence["confidence"] == "medium"
    assert evidence["matched_events"] == 2
    assert len(evidence["sample_messages"]) == 2
    assert EVIDENCE_KEY in evidence


def test_sample_messages_are_capped() -> None:
    detector = RuleDetector(lane=Lane.FLOW)
    evidence = only(detector.detect([upload(index) for index in range(10)]), "high-upload-volume")[
        0
    ].evidence
    assert len(evidence["sample_messages"]) == 3


# --- Lanes and configuration -----------------------------------------------------------


def test_the_log_instance_runs_the_log_rules() -> None:
    detector = RuleDetector(lane=Lane.LOG)
    events = [
        log_event(0, "[sshd] reverse mapping ... failed - POSSIBLE BREAK-IN ATTEMPT!"),
        log_event(10, "[kernel] out of memory: killed process", Severity.CRITICAL),
        log_event(20, "[cron] session opened for user backup"),
    ]
    fired = {d.evidence["rule_id"] for d in detector.detect(events)}
    assert fired == {"ssh-possible-break-in", "critical-log-event"}


def test_a_lane_with_no_rules_fails_at_construction() -> None:
    """Not at first detect(): a config error must fail before the run has any side effect,
    and a rule detector judging nothing is indistinguishable from a quiet night."""
    with pytest.raises(ValueError, match="no 'telemetry' rules"):
        RuleDetector(lane=Lane.TELEMETRY)


def test_a_custom_ruleset_replaces_the_packaged_library(tmp_path: Path) -> None:
    ruleset = write_ruleset(
        tmp_path,
        rule_document(
            id="quiet-service",
            title="Quiet Service",
            detection={
                "selection": {"flow.service": {"operator": "==", "value": "ssl"}},
                "condition": "selection",
            },
        ),
    )
    detector = RuleDetector(lane=Lane.FLOW, ruleset=ruleset)
    assert [rule.id for rule in detector.rules] == ["quiet-service"]
    assert len(detector.detect([flow_event(0)])) == 1


def test_a_broken_rule_fails_the_run_at_construction_naming_the_file(tmp_path: Path) -> None:
    ruleset = write_ruleset(
        tmp_path,
        rule_document(
            detection={
                "selection": {"flow.byte_out": {"operator": ">", "value": 1}},
                "condition": "selection",
            }
        ),
    )
    with pytest.raises(ValueError, match=re.escape("rule0.yml")):
        RuleDetector(lane=Lane.FLOW, ruleset=ruleset)


def test_a_rule_for_another_lane_is_not_evaluated(tmp_path: Path) -> None:
    """Every file still COMPILES — a malformed rule for any lane is a defect and must fail
    loudly — but only this lane's rules are judged."""
    ruleset = write_ruleset(
        tmp_path,
        rule_document(id="f-rule"),
        rule_document(
            id="l-rule",
            lane="log",
            detection={
                "selection": {"log.message": {"operator": "contains", "value": "x"}},
                "condition": "selection",
            },
        ),
    )
    detector = RuleDetector(lane=Lane.FLOW, ruleset=ruleset)
    assert [rule.id for rule in detector.rules] == ["f-rule"]


# --- Registry wiring -------------------------------------------------------------------


def test_the_registry_builds_it_and_its_capability_follows_the_lane() -> None:
    built = build_detector("rules", {"lane": "log"})
    assert built.lane is Lane.LOG
    capability = detector_capability("rules", {"lane": "log"})
    assert any(getattr(token, "lane", None) is Lane.LOG for token in capability.consumes)


def test_a_bad_param_fails_named_at_construction() -> None:
    with pytest.raises(ValueError, match="invalid params for detector 'rules'"):
        build_detector("rules", {"lane": "nonsense"})
