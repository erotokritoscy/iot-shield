"""Threat knowledge reaching the alert: precedence, union, fallback (ADR-0032 D5).

Before this, ``threat_class`` was the literal default on every alert this system had ever
produced and ``recommended_action`` was a generated restatement of the alert. These tests
pin the behaviour that changes that, and — just as importantly — the behaviour when nothing
claims to know what happened, which stays honest rather than guessing.
"""

from datetime import UTC, datetime, timedelta

import pytest

from iotshield.alert import StubAlertEngine, WeightedAlertEngine
from iotshield.alert._correlate import promote_enrichment
from iotshield.contracts import Asset, Criticality, DetectionResult, Lane
from iotshield.enrichment import (
    EVIDENCE_KEY,
    Enrichment,
    EnrichmentSource,
    MitreTechnique,
)

T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)

BRUTE_FORCE = Enrichment(
    threat_class="Brute Force (T1110)",
    source=EnrichmentSource.DETECTOR,
    origin="log_burst:auth_failure",
    mitre=(
        MitreTechnique(tactic="Credential Access", technique="Brute Force", technique_id="T1110"),
    ),
    suggested_actions=("Review the source addresses.", "Check for a successful login."),
)
EXFILTRATION = Enrichment(
    threat_class="Exfiltration Over C2 Channel (T1041)",
    source=EnrichmentSource.RULE,
    origin="rule:high-upload-volume",
    mitre=(
        MitreTechnique(
            tactic="Exfiltration", technique="Exfiltration Over C2 Channel", technique_id="T1041"
        ),
    ),
    suggested_actions=("Check whether the destination is expected.",),
)
GUESS = Enrichment(
    threat_class="Sustained deviation",
    source=EnrichmentSource.HEURISTIC,
    origin="shape:collective",
    suggested_actions=("Look at the channel.",),
)

CAMERA = Asset(
    asset_id="cam-07", name="Line-1 camera", asset_type="camera", criticality=Criticality.HIGH
)


def detection(
    enrichment: Enrichment | None = None,
    *,
    score: float = 2.0,
    lane: Lane = Lane.FLOW,
    method: str = "rules:flow",
    offset_s: int = 0,
) -> DetectionResult:
    evidence = {EVIDENCE_KEY: enrichment.to_evidence()} if enrichment else {}
    return DetectionResult(
        asset_id="cam-07",
        window_start=T0 + timedelta(seconds=offset_s),
        window_end=T0 + timedelta(seconds=offset_s),
        lane=lane,
        method=method,
        score=score,
        threshold=1.0,
        evidence=evidence,
    )


# --- Selection ---------------------------------------------------------------------------


def test_nothing_carried_means_nothing_promoted() -> None:
    assert promote_enrichment([detection(), detection()]) is None


def test_an_authored_rule_outranks_a_detector_preset() -> None:
    """The tier order, and the reason it exists: a rule's claim is about this specific
    signature, a preset's about a whole category. The more specific claim wins even when the
    other detection is the stronger one."""
    promoted = promote_enrichment(
        [detection(BRUTE_FORCE, score=9.0, lane=Lane.LOG), detection(EXFILTRATION, score=1.7)]
    )
    assert promoted is not None
    assert promoted.threat_class == "Exfiltration Over C2 Channel (T1041)"


def test_a_preset_outranks_an_inference() -> None:
    promoted = promote_enrichment([detection(GUESS, score=9.0), detection(BRUTE_FORCE)])
    assert promoted is not None
    assert promoted.source is EnrichmentSource.DETECTOR


def test_within_a_tier_the_strongest_detection_wins() -> None:
    weaker = EXFILTRATION
    stronger = Enrichment(
        threat_class="Remote Services (T1021)",
        source=EnrichmentSource.RULE,
        origin="rule:telnet-outbound-propagation",
    )
    promoted = promote_enrichment([detection(weaker, score=1.7), detection(stronger, score=3.0)])
    assert promoted is not None
    assert promoted.origin == "rule:telnet-outbound-propagation"


def test_selection_is_deterministic_when_margins_tie() -> None:
    first = promote_enrichment([detection(EXFILTRATION), detection(GUESS)])
    second = promote_enrichment([detection(EXFILTRATION), detection(GUESS)])
    assert first == second


def test_actions_are_unioned_across_every_contributing_detection() -> None:
    """The cross-lane case this exists for: a device showing both failed logins and a bulk
    transfer needs both sets of next steps, even though only one claim names the alert."""
    promoted = promote_enrichment([detection(BRUTE_FORCE, lane=Lane.LOG), detection(EXFILTRATION)])
    assert promoted is not None
    assert promoted.threat_class == "Exfiltration Over C2 Channel (T1041)"
    # The WINNER's actions come first, and that ordering is load-bearing: the union is
    # capped, so precedence decides whose next steps survive when a bucket is busy.
    assert promoted.suggested_actions == (
        "Check whether the destination is expected.",
        "Review the source addresses.",
        "Check for a successful login.",
    )


def test_the_cap_keeps_the_winners_actions_when_a_bucket_is_busy() -> None:
    """A crowded bucket must not push the naming claim's own next steps off the end."""
    noisy = [
        Enrichment(
            threat_class="Noise",
            source=EnrichmentSource.DETECTOR,
            origin=f"detector:{index}",
            suggested_actions=(f"Filler {index} a.", f"Filler {index} b."),
        )
        for index in range(6)
    ]
    group = [detection(e, lane=Lane.LOG, score=9.0) for e in noisy] + [detection(EXFILTRATION)]
    promoted = promote_enrichment(group)
    assert promoted is not None
    assert promoted.suggested_actions[0] == "Check whether the destination is expected."


# --- What the analyst ends up reading -----------------------------------------------------


@pytest.fixture(params=[WeightedAlertEngine, StubAlertEngine], ids=["weighted", "stub"])
def engine(request: pytest.FixtureRequest):  # type: ignore[no-untyped-def]
    """Both engines, because which threat class an alert carries is a product convention,
    not a property of whichever scoring formula produced the number."""
    return request.param()


def test_an_enriched_alert_names_the_threat_and_the_next_step(engine) -> None:  # type: ignore[no-untyped-def]
    alerts = engine.build_alerts([detection(EXFILTRATION)], {"cam-07": CAMERA})
    assert alerts[0].threat_class == "Exfiltration Over C2 Channel (T1041)"
    assert alerts[0].recommended_action == "Check whether the destination is expected."
    assert "rule:high-upload-volume" in alerts[0].rationale


def test_an_unenriched_alert_stays_honestly_unclassified(engine) -> None:  # type: ignore[no-untyped-def]
    """ "unknown" now means "nothing here claims to know what this is" — which is a real
    statement, and the reason the inference tier was deferred rather than filled with a
    shape label that would be indistinguishable from a diagnosis."""
    alerts = engine.build_alerts([detection()], {"cam-07": CAMERA})
    assert alerts[0].threat_class == "unknown"
    assert "Inspect cam-07" in (alerts[0].recommended_action or "")
    assert "Threat class" not in alerts[0].rationale


def test_an_inferred_class_is_labelled_as_a_guess(engine) -> None:  # type: ignore[no-untyped-def]
    alerts = engine.build_alerts([detection(GUESS)], {"cam-07": CAMERA})
    assert "(inferred)" in alerts[0].rationale


def test_enrichment_does_not_move_the_priority(engine) -> None:  # type: ignore[no-untyped-def]
    """Explanation is not evidence. Two identical detections must rank identically whether
    or not one of them happens to know what it found — otherwise writing a MITRE mapping
    would quietly promote an alert, and the ranking would stop meaning what it says."""
    plain = engine.build_alerts([detection()], {"cam-07": CAMERA})
    enriched = engine.build_alerts([detection(EXFILTRATION)], {"cam-07": CAMERA})
    assert plain[0].priority_score == pytest.approx(enriched[0].priority_score)
    assert plain[0].severity == enriched[0].severity


def test_an_alert_with_a_malformed_enrichment_falls_back_rather_than_failing(engine) -> None:  # type: ignore[no-untyped-def]
    broken = DetectionResult(
        asset_id="cam-07",
        window_start=T0,
        window_end=T0,
        lane=Lane.FLOW,
        method="rules:flow",
        score=2.0,
        threshold=1.0,
        evidence={EVIDENCE_KEY: {"threat_class": "x"}},
    )
    alerts = engine.build_alerts([broken], {"cam-07": CAMERA})
    assert alerts[0].threat_class == "unknown"
