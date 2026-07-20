"""The synthetic corpus: the licence-free benchmark a fresh clone actually gets.

Two things matter about generated data, and both are pinned here. It must be **fixed** —
same recipes, same bytes — or every benchmark silently measures a different dataset. And the
fault shapes must **discriminate**: a corpus where every detector scores the same teaches
nothing, so the point of shipping three shapes is that a fixed-threshold rule catches two of
them and is structurally blind to the third.
"""

from pathlib import Path

from iotshield.eval import evaluate, load_bundle, write_bundle
from iotshield.eval.generate import FaultKind, build
from iotshield.pipeline import load_spec

STUB_SPEC = Path("config/bench/stub.yaml")


class TestDeterminism:
    def test_two_builds_are_identical(self) -> None:
        first, second = build(), build()
        assert [case.name for case in first.cases] == [case.name for case in second.cases]
        for before, after in zip(first.cases, second.cases, strict=True):
            assert after.inputs[0].text == before.inputs[0].text
            assert after.truth.incidents == before.truth.incidents

    def test_the_clock_does_not_move_between_builds(self) -> None:
        # A wall-clock start would make every regeneration a diff and every stored result
        # incomparable with the next.
        first, second = build(), build()
        assert first.cases[0].truth.timeline[0][0] == second.cases[0].truth.timeline[0][0]


class TestShape:
    def test_it_carries_every_fault_kind_and_clean_recordings(self) -> None:
        names = [case.name for case in build().cases]
        for kind in FaultKind:
            assert any(name.startswith(kind.value) for name in names), kind
        assert any(name.startswith("clean") for name in names)

    def test_each_faulty_case_has_exactly_one_incident(self) -> None:
        # One fault per recording makes "alerts per incident" directly readable, the same
        # property that made SKAB a good benchmark shape.
        for case in build().cases:
            expected = 0 if case.name.startswith("clean") else 1
            assert len(case.truth.incidents) == expected, case.name

    def test_more_than_one_recording_per_shape(self) -> None:
        # A single example per shape would let a detector look good or bad by luck, and
        # per-case variance is one of the things the harness exists to show.
        names = [case.name for case in build().cases]
        for kind in FaultKind:
            assert sum(1 for name in names if name.startswith(kind.value)) > 1, kind

    def test_it_declares_its_own_licence_and_provenance(self) -> None:
        corpus = build()
        assert "iotshield" in corpus.provenance
        assert corpus.licence


class TestDiscrimination:
    """The reason for three shapes: they must not rank every detector identically."""

    def _by_case(self) -> dict[str, float | None]:
        corpus = build()
        report = evaluate(corpus, [("stub", load_spec(STUB_SPEC))])
        return {case.case: case.event_recall for case in report.specs[0].cases}

    def test_a_fixed_threshold_catches_spikes_and_drift(self) -> None:
        recalls = self._by_case()
        for name, recall in recalls.items():
            if name.startswith(("spike", "drift")):
                assert recall == 1.0, name

    def test_a_fixed_threshold_is_blind_to_a_stuck_sensor(self) -> None:
        # The discriminating shape: the reading never leaves its normal range, so no
        # absolute threshold can see it. A detector that models *normal* can.
        recalls = self._by_case()
        stuck = [recall for name, recall in recalls.items() if name.startswith("stuck")]
        assert stuck and all(recall == 0.0 for recall in stuck)

    def test_clean_recordings_produce_no_false_alarms(self) -> None:
        report = evaluate(build(), [("stub", load_spec(STUB_SPEC))])
        clean = [case for case in report.specs[0].cases if case.case.startswith("clean")]
        assert clean and all(case.detections == 0 for case in clean)


def test_the_generated_corpus_round_trips_through_a_bundle(tmp_path: Path) -> None:
    corpus = build()
    write_bundle(tmp_path / "synthetic", corpus)
    restored = load_bundle(tmp_path / "synthetic")
    assert len(restored.cases) == len(corpus.cases)
    assert restored.incidents == corpus.incidents
