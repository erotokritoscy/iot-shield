"""The generator's refusals, and the shape of the answer key it derives.

``build`` describes four invariants as existing "because breaking it would produce a corpus
that *looks* fine and scores wrong", and until now **nothing tested any of them** — there was
no reference to ``GenerationError`` anywhere in this suite. These guards are the corpus's own
type checker: everything they catch is, by construction, something that produces a plausible
answer key rather than an obvious failure, so an untested guard is one that can quietly stop
guarding and leave no trace but a wrong number.

Each test below builds a deliberately broken one-recording scenario and asserts the message
names the cause, not merely that *something* was refused. A refusal whose message sends the
author to the wrong place is barely better than none — that was the state for a spike aimed at
a slow-sampling device, which was reported as an unfindable *label*.
"""

import pytest

from iotshield.contracts import Lane
from iotshield.eval.generate import Clock, Device, FaultSpec, Recording, build
from iotshield.eval.generate.flow import SOURCE_FORMAT as FLOW_CSV
from iotshield.eval.generate.flow import ZeekConnRenderer
from iotshield.eval.generate.world import GenerationError
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV
from iotshield.normalize.syslog import SOURCE_FORMAT as SYSLOG
from tests.eval.generate.conftest import CLOCK, scenario_of

RIG = Device(asset_id="rig", kind="sensor")
CAMERA = Device(asset_id="10.0.0.24", kind="camera")
GATEWAY = Device(asset_id="10.0.0.11", kind="gateway")


class TestIdentityInvariants:
    """An asset id names one device — enforced where the object is built, not where it is used."""

    def test_two_devices_may_not_share_an_asset_id(self) -> None:
        """The failure this prevents is silent and large.

        ``faults_on`` looks faults up by asset id, so two devices sharing one are handed the
        *same* ``FaultSpec``; both report perturbed instants under it, and the builder's merge
        keeps only the last writer's. Measured before the guard: a rig's 118 perturbed instants
        were discarded in favour of a camera's 22, which — being sparse against the rig's own
        900-instant clock — split into 22 single-instant incidents where there should be one.
        """
        with pytest.raises(GenerationError, match="more than once"):
            Recording(
                name="clash",
                clock=CLOCK,
                devices=(
                    Device(asset_id="dup", kind="sensor"),
                    Device(asset_id="dup", kind="camera"),
                ),
            )

    def test_a_recording_must_describe_something(self) -> None:
        with pytest.raises(GenerationError, match="no devices"):
            Recording(name="empty", clock=CLOCK, devices=())

    def test_a_fault_may_not_be_aimed_at_a_device_that_is_not_there(self) -> None:
        scenario = scenario_of(RIG, faults=(FaultSpec("spike", "ghost", 5, 6),))
        with pytest.raises(GenerationError, match="not in the recording"):
            build(scenario)

    def test_a_negative_intensity_is_not_a_weak_fault(self) -> None:
        # It used to reach the telemetry handlers as a negative amplitude and drive a rig's
        # pressure to -160 while the answer key still called it a spike.
        with pytest.raises(GenerationError, match="negative one describes no fault"):
            FaultSpec("spike", RIG.asset_id, 5, 6, -2.0)

    def test_a_fault_may_not_start_before_the_recording(self) -> None:
        with pytest.raises(GenerationError, match="happens inside the recording"):
            FaultSpec("spike", RIG.asset_id, -100, 200)


class TestOneLanePerScenario:
    """Two formats of one lane are ingested *additively*, and nothing downstream says so."""

    def test_the_same_format_twice_is_refused(self) -> None:
        with pytest.raises(GenerationError, match="reach the detectors twice"):
            build(scenario_of(CAMERA, formats=(FLOW_CSV, FLOW_CSV)))

    def test_two_dialects_of_one_lane_are_refused(self) -> None:
        """The tempting mistake, because comparing dialects is a real thing to want.

        A run pools its sources into lanes, so both files' events reach the flow detector; the
        answer key does *not* double, because a clock is a set of instants and the duplicate
        timestamps collapse. Peer counts, byte totals and burst counts silently double against
        a truth that counted each flow once.
        """
        with pytest.raises(GenerationError, match="both flow formats"):
            build(scenario_of(CAMERA, formats=(FLOW_CSV, ZeekConnRenderer.source_format)))

    def test_distinct_lanes_are_fine(self) -> None:
        corpus = build(scenario_of(CAMERA, formats=(STUB_CSV, SYSLOG, FLOW_CSV)))
        assert [given.source_format for given in corpus.cases[0].inputs] == [
            STUB_CSV,
            SYSLOG,
            FLOW_CSV,
        ]


class TestExpressibility:
    """A label nothing can see is unfindable by construction, so it is refused not shipped."""

    def test_a_fault_no_lane_models_is_refused(self) -> None:
        scenario = scenario_of(RIG, faults=(FaultSpec("meltdown", RIG.asset_id, 5, 6),))
        with pytest.raises(GenerationError, match="expressed"):
            build(scenario)

    def test_a_fault_the_recorded_lane_cannot_show_is_refused(self) -> None:
        # `scan` is a flow shape; a telemetry-only corpus can never render it.
        scenario = scenario_of(RIG, faults=(FaultSpec("scan", RIG.asset_id, 5, 6),))
        with pytest.raises(GenerationError, match="expressed"):
            build(scenario)

    def test_an_intensity_that_would_invert_the_fault_is_refused(self) -> None:
        """ADR-0028 D6 promises intensity is multiples of the alarm line. Below one it inverted.

        The handlers reach an *absolute* target, so a target under the channel's own baseline
        made the reading dip while the label still said "drift". A rig's Temperature sits at 75
        against a boundary of 80, so anything below 0.938 inverted — squarely inside the range
        an author uses to write "deliberately marginal". The message has to name the floor,
        because the floor is per channel and nothing else would tell them what to write.
        """
        scenario = scenario_of(RIG, faults=(FaultSpec("drift", RIG.asset_id, 5, 6, 0.9),))
        with pytest.raises(GenerationError, match="would run downward"):
            build(scenario)

    def test_the_refusal_names_the_lowest_intensity_that_works(self) -> None:
        scenario = scenario_of(RIG, faults=(FaultSpec("drift", RIG.asset_id, 5, 6, 0.9),))
        with pytest.raises(GenerationError, match=r"lowest intensity .* is 0\.9[0-9]"):
            build(scenario)

    def test_a_stuck_sensor_needs_two_samples_to_be_stuck(self) -> None:
        """The one shape with a *semantic* minimum: freezing is a claim about two readings.

        Refused with the cause named, rather than silently expressing nothing and surfacing as
        the generic "no lane expressed this" — which points the author at their label when the
        problem is that their device does not sample often enough.
        """
        slow = Device(asset_id="slow", kind="gateway")  # samples every 30 steps
        scenario = scenario_of(
            slow, faults=(FaultSpec("stuck", "slow", 0, 10),), clock=Clock(steps=60)
        )
        with pytest.raises(GenerationError, match="needs at least 2"):
            build(scenario)

    def test_a_shaped_fault_survives_a_single_sample(self) -> None:
        """The counterpart, and the bug that motivated `_bump`.

        A 40-step fault on a device sampling every 30 steps covers one sample. That is the
        honest rendering of a slow sensor, and it used to perturb *nothing*: the spike's sine
        shape was evaluated at its own endpoints, which are both zero. The scenario was then
        refused for having an unfindable label, sending the author to the wrong place entirely.
        """
        slow = Device(asset_id="slow", kind="gateway")
        scenario = scenario_of(
            slow, faults=(FaultSpec("spike", "slow", 30, 40, 1.2),), clock=Clock(steps=120)
        )
        (found,) = build(scenario).cases[0].truth.incidents
        assert found.lane is Lane.TELEMETRY
        assert found.spans  # it expressed something at all, which is the whole point


class TestProjectionsNotFragments:
    """ADR-0029: one incident per (fault, lane), whatever the interleaving does to the runs."""

    @staticmethod
    def _storm(start: int, count_over: float) -> FaultSpec:
        return FaultSpec("error_storm", GATEWAY.asset_id, start, 60, count_over)

    def test_a_fault_interrupted_by_routine_traffic_is_still_one_incident(self) -> None:
        """The measurement that motivated the change.

        A gateway logs a routine heartbeat every 60 steps. An error storm injecting eight lines
        across 60 steps from t=400 puts injections at 400, 407, 415, 422 ... so the heartbeat at
        t=420 falls *between* two of them and is genuinely not perturbed. Under one-incident-
        per-run that produced **two** incidents from one fault, and `incidents` is what event
        recall divides by.
        """
        scenario = scenario_of(
            GATEWAY,
            formats=(SYSLOG,),
            faults=(self._storm(400, 1.6),),
            clock=Clock(steps=900),
        )
        (found,) = build(scenario).cases[0].truth.incidents
        assert len(found.spans) == 2, "the gap at t=420 must survive, for point scoring"
        assert found.lane is Lane.LOG

    def test_the_gap_is_not_labelled(self) -> None:
        # The other half: splitting exists so a detector is not charged with a miss at an
        # instant the device behaved completely normally.
        scenario = scenario_of(
            GATEWAY,
            formats=(SYSLOG,),
            faults=(self._storm(400, 1.6),),
            clock=Clock(steps=900),
        )
        (asset,) = build(scenario).cases[0].truth.assets
        flagged = {moment for moment, bad in asset.timelines[Lane.LOG] if bad}
        assert CLOCK.epoch.replace(minute=7, second=0) not in flagged

    def test_interleaving_does_not_change_the_denominator(self) -> None:
        """Two identical storms differing only in whether a heartbeat lands *on* an injection.

        At t=600 the injection coincides with the heartbeat and the run is unbroken; at t=400 it
        does not and the run splits. Both are one projection of one fault, and that is what the
        event level counts — the number that used to move between them for no reason a reader
        of the scenario could see.
        """
        counts = []
        for start, over in ((400, 1.6), (600, 2.0)):
            scenario = scenario_of(
                GATEWAY,
                formats=(SYSLOG,),
                faults=(self._storm(start, over),),
                clock=Clock(steps=900),
                name=f"storm-{start}",
            )
            counts.append(len(build(scenario).cases[0].truth.incidents))
        assert counts == [1, 1]
