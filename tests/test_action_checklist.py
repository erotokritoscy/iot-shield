"""The recommended-action checklist: what survives the round trip, and what it discloses.

`recommended_action` is one `str` holding a *list*, so `action_text` and `action_steps` are
each other's inverse. That is a claim about our own prose — and prose is what a contributor
writes, in any directory named by `ruleset:`. These pin both halves: the shapes that are
refused at authoring time, and the fact that a trimmed checklist says so (ADR-0033).
"""

import pytest

from iotshield.enrichment import (
    Enrichment,
    EnrichmentSource,
    action_steps,
    validate_action_step,
)


def _enrichment(*actions: str) -> Enrichment:
    return Enrichment(
        threat_class="Test Threat (T0000)",
        source=EnrichmentSource.RULE,
        origin="rule:test",
        suggested_actions=tuple(actions),
    )


class TestAuthoringRefusals:
    @pytest.mark.parametrize(
        ("step", "because"),
        [
            ("", "empty"),
            ("   ", "empty"),
            ("Block the source", "no terminal punctuation, so it merges with the next step"),
            ("Check the I.P. address.", "an interior '. ' is the separator itself"),
            ("Escalate to the OT team (e.g. the plant engineer).", "abbreviation splits"),
            ("Do A.\nThen keep going.", "a YAML block scalar would split it"),
        ],
    )
    def test_a_step_that_could_not_round_trip_is_refused(self, step: str, because: str) -> None:
        with pytest.raises(ValueError):
            validate_action_step(step)

    @pytest.mark.parametrize(
        "step",
        [
            "Isolate the device.",
            "Did the destination answer?",
            "Stop the transfer now!",
            "Inspect firmware v2.1 for tampering.",
        ],
    )
    def test_an_ordinary_step_is_accepted_and_stripped(self, step: str) -> None:
        assert validate_action_step(f"  {step}  ") == step

    def test_every_accepted_step_actually_survives_the_round_trip(self) -> None:
        """The refusals exist to make this property true, so assert the property itself
        rather than trusting that the list of refusals is complete."""
        steps = (
            "Isolate the device.",
            "Check whether the destination answered.",
            "Inspect firmware v2.1 for tampering.",
        )
        for step in steps:
            validate_action_step(step)

        assert tuple(action_steps(_enrichment(*steps).action_text())) == steps


class TestTruncationIsDisclosed:
    def test_a_trimmed_checklist_says_how_many_steps_were_dropped(self) -> None:
        """The cap is a real decision; applying it silently is not. On the shipped
        demo_rules camera alert six of twelve steps vanished with no marker."""
        contributing = [
            _enrichment(*[f"Step {group}{index} happened." for index in range(4)])
            for group in "abc"
        ]

        promoted = contributing[0].with_union_of(contributing)

        assert len(promoted.suggested_actions) == 6
        assert "were omitted" in promoted.suggested_actions[-1]
        assert "7 further step(s)" in promoted.suggested_actions[-1]

    def test_the_marker_is_recoverable_as_a_real_step(self) -> None:
        """Carried as a step, not as prose, so both dashboards render it for free."""
        contributing = [
            _enrichment(*[f"Step {group}{index} happened." for index in range(4)])
            for group in "abc"
        ]

        promoted = contributing[0].with_union_of(contributing)

        assert action_steps(promoted.action_text()) == list(promoted.suggested_actions)

    def test_a_checklist_within_the_cap_is_untouched(self) -> None:
        contributing = [_enrichment("Isolate the device.", "Check the firewall.")]

        promoted = contributing[0].with_union_of(contributing)

        assert promoted.suggested_actions == ("Isolate the device.", "Check the firewall.")

    def test_the_winning_claims_own_steps_are_never_the_ones_evicted(self) -> None:
        """The naming claim is first in the bucket, so its steps must survive the cap."""
        winner = _enrichment("Winner step one.", "Winner step two.")
        others = [_enrichment(*[f"Other {group}{i} happened." for i in range(4)]) for group in "ab"]

        promoted = winner.with_union_of([winner, *others])

        assert promoted.suggested_actions[0] == "Winner step one."
        assert promoted.suggested_actions[1] == "Winner step two."
