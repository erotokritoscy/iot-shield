"""The enrichment vocabulary: the typed hop between detect and alert (ADR-0032 D4)."""

from datetime import UTC, datetime

import pytest

from iotshield.contracts import DetectionResult, Lane
from iotshield.enrichment import (
    EVIDENCE_KEY,
    Enrichment,
    EnrichmentFieldError,
    EnrichmentSource,
    MitreTechnique,
    action_steps,
)

T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)

EXFILTRATION = MitreTechnique(
    tactic="Exfiltration", technique="Exfiltration Over C2 Channel", technique_id="T1041"
)


def make_enrichment(**overrides: object) -> Enrichment:
    defaults: dict[str, object] = {
        "threat_class": EXFILTRATION.label(),
        "source": EnrichmentSource.RULE,
        "origin": "rule:high-upload-volume",
        "mitre": (EXFILTRATION,),
        "suggested_actions": ("Review the destination.",),
    }
    return Enrichment(**{**defaults, **overrides})  # type: ignore[arg-type]


def detection(evidence: dict[str, object] | None = None, *, score: float = 2.0) -> DetectionResult:
    return DetectionResult(
        asset_id="cam-07",
        window_start=T0,
        window_end=T0,
        lane=Lane.FLOW,
        method="rules:flow",
        score=score,
        threshold=1.0,
        evidence=evidence or {},
    )


def test_round_trips_through_evidence() -> None:
    """The whole point of the typed hop: what one side writes, the other reads back."""
    original = make_enrichment()
    assert Enrichment.from_evidence(original.to_evidence()) == original


def test_technique_label_is_what_reaches_the_alert() -> None:
    assert EXFILTRATION.label() == "Exfiltration Over C2 Channel (T1041)"


@pytest.mark.parametrize(
    ("evidence", "expected"),
    [
        ("not a mapping", "must be a mapping"),
        ({"source": "rule", "origin": "x"}, "missing 'threat_class'"),
        ({"threat_class": "x", "origin": "y"}, "missing 'source'"),
        ({"threat_class": "x", "source": "wat", "origin": "y"}, "unreadable source"),
        ({"threat_class": " ", "source": "rule", "origin": "y"}, "empty 'threat_class'"),
        ({"threat_class": "x", "source": "rule", "origin": " "}, "empty 'origin'"),
        ({"threat_class": "x", "source": "rule", "origin": "y", "mitre": {}}, "must be a list"),
    ],
)
def test_malformed_evidence_names_the_field(evidence: object, expected: str) -> None:
    """A drift between writer and reader must surface with a field name.

    The alternative — reading it as absent — is an alert that says "unknown" forever, which
    is indistinguishable from a detector that had nothing to say. That is why the strict
    path exists separately from the lenient ``from_detection``.
    """
    with pytest.raises(EnrichmentFieldError, match=expected):
        Enrichment.from_evidence(evidence)


def test_a_mitre_entry_must_be_structured() -> None:
    with pytest.raises(EnrichmentFieldError, match="missing 'technique'"):
        MitreTechnique.from_dict({"tactic": "Exfiltration", "technique_id": "T1041"})
    with pytest.raises(EnrichmentFieldError, match="empty technique"):
        MitreTechnique.from_dict({"tactic": "a", "technique": "", "technique_id": "T1"})
    with pytest.raises(EnrichmentFieldError, match="must be a mapping"):
        MitreTechnique.from_dict("T1041")


def test_reading_a_detection_that_carries_none_is_not_an_error() -> None:
    """Four of five shipped detectors carry no enrichment; that is the normal case, so the
    alert layer's read path falls back rather than failing the run."""
    assert Enrichment.from_detection(detection()) is None


def test_reading_a_detection_with_malformed_enrichment_falls_back() -> None:
    assert Enrichment.from_detection(detection({EVIDENCE_KEY: {"threat_class": "x"}})) is None


def test_precedence_orders_authored_above_inferred() -> None:
    """Tier order is the product decision: a rule's own claim beats a preset's, which beats
    a guess. Exposed as a number so the alert layer sorts rather than branching on an enum."""
    rule = make_enrichment(source=EnrichmentSource.RULE)
    preset = make_enrichment(source=EnrichmentSource.DETECTOR)
    guess = make_enrichment(source=EnrichmentSource.HEURISTIC)
    assert rule.precedence < preset.precedence < guess.precedence


def test_only_a_heuristic_is_marked_as_a_guess() -> None:
    """A product requirement, not bookkeeping: presenting an inference in the same voice as
    a fact is how an analyst learns to distrust the whole column."""
    assert make_enrichment(source=EnrichmentSource.HEURISTIC).is_heuristic is True
    assert make_enrichment(source=EnrichmentSource.RULE).is_heuristic is False
    assert "(inferred)" in make_enrichment(source=EnrichmentSource.HEURISTIC).rationale_clause()
    assert "(inferred)" not in make_enrichment().rationale_clause()


def test_the_rationale_clause_attributes_the_claim() -> None:
    clause = make_enrichment().rationale_clause()
    assert "Exfiltration Over C2 Channel (T1041)" in clause
    assert "rule:high-upload-volume" in clause


def test_actions_render_as_one_paragraph_or_nothing() -> None:
    assert make_enrichment(suggested_actions=("A.", "B.")).action_text() == "A. B."
    assert make_enrichment(suggested_actions=()).action_text() is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("A. B. C.", ["A.", "B.", "C."]),
        # The generated fallback: one sentence holding a period-free ISO instant and a
        # semicolon. Splitting it would invent two half-instructions out of one.
        (
            "Inspect pump-02 around 2026-07-07T12:01:00+00:00; review flow_burst evidence.",
            ["Inspect pump-02 around 2026-07-07T12:01:00+00:00; review flow_burst evidence."],
        ),
        ("Only one step.", ["Only one step."]),
        # A trailing step that never had a period does not acquire one.
        ("First step. Second with no stop", ["First step.", "Second with no stop"]),
        (None, []),
        ("", []),
        ("   ", []),
    ],
)
def test_action_steps_recovers_the_checklist(text: str | None, expected: list[str]) -> None:
    assert action_steps(text) == expected


def test_action_steps_inverts_action_text_for_every_shipped_claim() -> None:
    """The property that makes the split a fact about our data rather than a hope.

    ``recommended_action`` is one string and the thing inside it is a list, so a UI has to
    recover the steps by splitting. That is only safe while every action we ship is a single
    sentence ending in a period — an action containing a mid-sentence ``". "`` would reach an
    analyst as two half-steps. Rather than trusting the rule library and the presets to stay
    that way, this asserts it over every claim the product ships, so the day someone writes
    "Check the device's I.P. address." the test says so instead of the dashboard.
    """
    from iotshield.detect.flow_burst import PRESETS as FLOW_PRESETS
    from iotshield.detect.log_burst import PRESETS as LOG_PRESETS
    from iotshield.detect.meter_quality import ENRICHMENT as METER_QUALITY
    from iotshield.detect.rules.loader import load_ruleset

    shipped = [rule.enrichment for rule in load_ruleset()]
    shipped += [preset.enrichment for preset in (*LOG_PRESETS.values(), *FLOW_PRESETS.values())]
    shipped += list(METER_QUALITY.values())
    assert len(shipped) >= 13, "expected the six packaged rules, five presets, two meter checks"

    for enrichment in shipped:
        assert enrichment.suggested_actions, f"{enrichment.origin} suggests nothing"
        assert action_steps(enrichment.action_text()) == list(enrichment.suggested_actions), (
            f"{enrichment.origin}'s actions do not survive the join/split round trip"
        )


def test_union_deduplicates_and_preserves_order() -> None:
    """An analyst seeing failed logins AND a bulk transfer wants both sets of next steps."""
    first = make_enrichment(suggested_actions=("Block the source.", "Review the device."))
    second = make_enrichment(suggested_actions=("Review the device.", "Check the destination."))
    assert first.with_union_of([first, second]).suggested_actions == (
        "Block the source.",
        "Review the device.",
        "Check the destination.",
    )


def test_the_union_keeps_the_winners_own_claim() -> None:
    """Only the ACTIONS are pooled: the threat class is the winning claim, not a merge."""
    winner = make_enrichment(threat_class="Exfiltration Over C2 Channel (T1041)")
    other = make_enrichment(threat_class="Brute Force (T1110)", suggested_actions=("Block.",))
    merged = winner.with_union_of([winner, other])
    assert merged.threat_class == "Exfiltration Over C2 Channel (T1041)"
    assert merged.mitre == winner.mitre


def test_union_is_capped_so_the_field_stays_a_next_step() -> None:
    many = [make_enrichment(suggested_actions=(f"Action {index}.",)) for index in range(20)]
    assert len(make_enrichment().with_union_of(many).suggested_actions) == 6
