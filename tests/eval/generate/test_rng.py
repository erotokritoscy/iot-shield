"""Determinism, and the insertion-stability that makes a corpus growable.

Two properties, and the second is the one that could not even be *stated* before. A committed
corpus must be byte-fixed, or every benchmark measures a different dataset. And adding a
device, a lane, or a recording must not disturb the rest — otherwise growing the corpus
invalidates every number already published against it.
"""

import os
import subprocess
import sys

from iotshield.contracts import Lane
from iotshield.eval.generate import Device, FaultSpec, Recording, Scenario, build, stream
from tests.eval.generate.conftest import CLOCK, scenario_of

CAMERA = Device(asset_id="10.0.0.24", kind="sensor")
GATEWAY = Device(asset_id="10.0.0.11", kind="sensor")
SENSOR = Device(asset_id="10.0.0.31", kind="sensor")

SPIKE = FaultSpec(kind="spike", asset_id=CAMERA.asset_id, start_step=5, length_steps=4)


def lines_of(scenario: Scenario, asset_id: str) -> list[str]:
    """Just the rendered lines attributable to one device."""
    (case,) = build(scenario).cases
    text = case.inputs[0].text
    return [line for line in text.splitlines() if f",{asset_id}," in line]


class TestStream:
    def test_the_same_names_give_the_same_values(self) -> None:
        assert (
            stream(1, "a", "b").normal(size=5).tolist()
            == stream(1, "a", "b").normal(size=5).tolist()
        )

    def test_different_names_give_different_values(self) -> None:
        assert stream(1, "a").normal(size=5).tolist() != stream(1, "b").normal(size=5).tolist()

    def test_different_seeds_give_different_values(self) -> None:
        assert stream(1, "a").normal(size=5).tolist() != stream(2, "a").normal(size=5).tolist()

    def test_the_name_parts_cannot_be_confused(self) -> None:
        # ("a", "bc") and ("ab", "c") must not collide: they would if the parts were simply
        # concatenated, and a colliding stream is two devices generating identical data.
        assert (
            stream(1, "a", "bc").normal(size=3).tolist()
            != stream(1, "ab", "c").normal(size=3).tolist()
        )

    def test_generation_does_not_depend_on_pythonhashseed(self) -> None:
        """The reason this uses blake2b and never ``hash()``.

        Python randomizes string hashing per process, so a seed derived from ``hash()`` would
        produce different data on every run — silently destroying the one property a committed
        corpus must have. Two subprocesses with different hash seeds must agree exactly.
        """
        code = (
            "from iotshield.eval.generate import stream;"
            "print(stream(3, 'scenario', 'case', 'dev').integers(0, 10**9, size=4).tolist())"
        )
        # The parent environment is inherited with only PYTHONHASHSEED overridden: a bare env
        # cannot launch Python on Windows (it needs SYSTEMROOT), and the point here is the hash
        # seed, not a pristine environment.
        outputs = set()
        for seed in ("0", "1"):
            environment = {**os.environ, "PYTHONHASHSEED": seed}
            finished = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True,
                text=True,
                check=True,
                env=environment,
            )
            outputs.add(finished.stdout.strip())
        assert len(outputs) == 1, outputs


class TestReproducibility:
    def test_two_builds_are_identical(self) -> None:
        first, second = build(scenario_of(CAMERA)), build(scenario_of(CAMERA))
        assert [case.name for case in first.cases] == [case.name for case in second.cases]
        for before, after in zip(first.cases, second.cases, strict=True):
            assert after.inputs[0].text == before.inputs[0].text
            assert after.truth.assets == before.truth.assets

    def test_the_clock_does_not_move_between_builds(self) -> None:
        # A wall-clock start would make every regeneration a diff and every stored result
        # incomparable with the next.
        first, second = build(scenario_of(CAMERA)), build(scenario_of(CAMERA))
        assert first.cases[0].truth.assets[0].clock[0] == second.cases[0].truth.assets[0].clock[0]


class TestInsertionStability:
    """The property that makes the corpus growable — untestable under a shared RNG stream."""

    def test_adding_a_device_does_not_change_the_others_bytes(self) -> None:
        small = scenario_of(CAMERA, GATEWAY)
        large = scenario_of(CAMERA, GATEWAY, SENSOR)
        for device in (CAMERA, GATEWAY):
            assert lines_of(small, device.asset_id) == lines_of(large, device.asset_id), (
                device.asset_id
            )

    def test_reordering_devices_changes_nothing_at_all(self) -> None:
        # The builder sorts before rendering, so the tuple's order is presentation, not data.
        assert (
            build(scenario_of(CAMERA, GATEWAY)).cases[0].inputs[0].text
            == build(scenario_of(GATEWAY, CAMERA)).cases[0].inputs[0].text
        )

    def test_adding_a_recording_does_not_change_the_others(self) -> None:
        one = scenario_of(CAMERA)
        two = scenario_of(CAMERA, extra=(Recording(name="extra", clock=CLOCK, devices=(CAMERA,)),))
        assert build(two).cases[0].inputs[0].text == build(one).cases[0].inputs[0].text

    def test_renaming_a_device_does_change_its_bytes(self) -> None:
        # Not a defect: a rename *is* a different device, and pretending otherwise would mean
        # the stream was not really scoped to identity.
        renamed = Device(asset_id="10.0.0.99", kind=CAMERA.kind)
        original = [
            line.split(",", 2)[2] for line in lines_of(scenario_of(CAMERA), CAMERA.asset_id)
        ]
        moved = [line.split(",", 2)[2] for line in lines_of(scenario_of(renamed), renamed.asset_id)]
        assert original != moved


class TestDerivedLabels:
    """Labels come from what was injected and expressed — never from annotation."""

    def test_the_labelled_interval_is_the_span_that_actually_changed(self) -> None:
        """Read off the perturbed instants, not off the declared window.

        `SPIKE` is declared at step 5 for 4 steps, and the label is **6..7**. A spike is a
        half-sine over its span, `sin(linspace(0, pi, 4))` = `[0, .87, .87, 0]`, so the first
        and last samples add exactly nothing: steps 5 and 8 render byte-identical to the
        unfaulted world. Labelling them anyway claims an anomaly no detector can see and no
        reader can diagnose — an unwinnable recall miss baked into a committed corpus.

        This is the whole reason the interval comes from `Observed.expressed` rather than from
        `FaultSpec`: intent and effect are different things, and only one of them is in the
        file the detector reads.
        """
        scenario = scenario_of(CAMERA, faults=(SPIKE,))
        (case,) = build(scenario).cases
        (found,) = case.truth.incidents
        assert found.start == CLOCK.at(6)
        assert found.end == CLOCK.at(7)  # closed interval: the last affected instant
        assert found.kind == "spike"
        assert found.fault_id == f"{CAMERA.asset_id}:spike:5"

    def test_the_lanes_are_the_ones_that_actually_expressed_it(self) -> None:
        # Derived, not declared: only the telemetry lane is rendered here, so that is the only
        # lane the answer key may claim carried the signal.
        scenario = scenario_of(CAMERA, faults=(SPIKE,))
        (found,) = build(scenario).cases[0].truth.incidents
        assert found.lanes == frozenset({Lane.TELEMETRY})

    def test_every_observed_device_joins_the_roster(self) -> None:
        # Which is what makes "that device should have stayed quiet" measurable: a device with
        # a clock and no incidents is asserted clean, not unknown (ADR-0026).
        scenario = scenario_of(CAMERA, GATEWAY, faults=(SPIKE,))
        truth = build(scenario).cases[0].truth
        assert truth.labelled_assets == {CAMERA.asset_id, GATEWAY.asset_id}
        assert truth.clean_assets == {GATEWAY.asset_id}
