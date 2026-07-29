"""The world a recording describes — with no wire format, no channels, and no IP addresses.

The reframing this package is built on. The old generator treated a labelled recording as *a
file of numbers with a fault in it*, which describes a single sensor on a single rig and
nothing else: a network capture is many devices talking to each other, and a multimodal
recording is three files describing the same fifteen minutes. Neither fits.

So a recording is not a file. **A recording is a small world**: some devices, over some
stretch of time, some of which go wrong. Files are a *rendering* of that world (``port``),
and the answer key is *derived* from the faults declared here — never written by hand.

Separating the world from its rendering is what unfuses the three questions the old
``_case`` function answered at once (what happened / how it is written down / what was
wrong), and therefore what makes each of them independently extensible.

Multimodal is not a special case in this model: ``Scenario.lanes`` is a tuple, and a
single-lane corpus is the one where it has one element.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

EPOCH = datetime(2026, 1, 1, 0, 0, 0, tzinfo=UTC)
"""A fixed start instant: generated corpora must not change when the clock does.

A wall-clock start would make every regeneration a diff and every stored result incomparable
with the next.
"""


class GenerationError(RuntimeError):
    """A scenario is internally inconsistent — caught while building, never shipped.

    Its own type because these are all the same class of mistake: a corpus that would *look*
    fine and score wrong. A fault no lane can express, a device that emitted nothing, a fault
    aimed at a device that is not in the recording — each would quietly produce a misleading
    number rather than an obvious failure, so each is refused at generation time.
    """


@dataclass(frozen=True)
class Clock:
    """A recording's instants: where it starts, how often it ticks, and for how long."""

    steps: int = 900
    step: timedelta = timedelta(seconds=1)
    epoch: datetime = EPOCH

    def at(self, index: int) -> datetime:
        return self.epoch + self.step * index

    # There is deliberately no `span(start, length)` here any more. It computed a fault's
    # interval from the *declared* window, which is what let a label describe a stretch of time
    # the data never showed anything at: it clamped nothing while the telemetry handler clamped
    # its damage, so the two disagreed. A fault's interval is now read off the instants the
    # behaviours reported perturbing (`build._incident`), which is the only version that cannot
    # drift from the bytes.

    def spread(self, start: int, length: int, count: int) -> list[int]:
        """``count`` steps spaced evenly across ``length`` from ``start``.

        Lives here because every behaviour that injects a burst needs it and needs it to mean
        the same thing: a fault must land *inside* one sliding window to read as a burst to a
        windowed detector, rather than piling onto a single instant. It was open-coded four
        times across three modules before, which is four chances for the rule to drift.
        """
        span = max(1, length)
        return [start + (index * span) // max(1, count) for index in range(max(1, count))]


@dataclass(frozen=True)
class Device:
    """One monitored thing.

    ``asset_id`` is the identity **every** lane must write, because it is the key the pipeline
    correlates on. For a device observed on the flow lane it has to be an IP inside the
    monitored networks: that is the only identity a flow record carries (ADR-0023 D2), and
    the normalizer decides which endpoint is "the asset" from those networks — so a device
    named otherwise would have its events attributed to the peer. The builder checks this
    rather than trusting it.

    ``kind`` selects a profile *within* a behaviour (a gateway and a camera are both network
    hosts, with different normal traffic); an unknown kind falls back to that behaviour's
    default, so no behaviour has to know every kind.

    ``role`` is read by no machinery, deliberately: it labels *why* a device is in a scenario
    ("inbound-heavy jump host") at the point where a reader is deciding what the scenario
    proves. A comment could not be attached to one element of a tuple, which is what makes a
    field the right home for it.
    """

    asset_id: str
    kind: str = "sensor"
    role: str = ""


@dataclass(frozen=True)
class FaultSpec:
    """One injected fault: which shape, whose, when, and how hard.

    ``intensity`` is deliberately a single scalar rather than a per-lane dict of knobs. The
    moment it becomes the latter, the scenario starts encoding lane-specific detail and the
    separation this module exists for collapses.

    ## It is dimensionless: multiples of the detector boundary (ADR-0028 D6)

    ``1.0`` means "exactly at the alarm line", whichever lane is asked, and every behaviour
    converts it into its own units by multiplying the ``Calibration`` it declares for that fault
    kind — 20 peers for a fan-out, 15 ports for a port sweep, 50 MB for an exfiltration, 5 lines
    for a log storm, a value of 80 for the telemetry threshold. So ``1.3`` is "thirty percent
    past the boundary" on all of them simultaneously, and a scenario expressing a *marginal*
    fault writes one number rather than three that happen to agree.

    The single scalar was always the right call; leaving it **dimensioned** was not. The same
    number used to mean "30 peers" to the flow behaviour and "multiply the thermal rise by 30"
    to the sensor rig, so the trimodal compromise — documented as deliberately arguable —
    reached 316.7 degrees against a threshold of 80 and the telemetry-only arm of the
    corroboration A/B caught it outright, measuring nothing it was built to measure.

    Note that the boundary itself is not necessarily a *trip*: detectors use a strict ``>``
    (the Detector port mandates it), so ``1.0`` sits exactly on the line and a fault meant to
    fire needs a little more. That asymmetry belongs to the detector, not here.

    A shape with no magnitude ignores it — ``stuck`` freezes a sensor at whatever it last read,
    and there is nothing for a multiplier to scale.
    """

    kind: str
    asset_id: str
    start_step: int
    length_steps: int
    intensity: float = 1.0

    def __post_init__(self) -> None:
        """Refuse the values no lane could ever render.

        Validated here rather than in ``build`` because these are wrong *about the object*, not
        about the scenario it sits in: a negative intensity is meaningless on every lane at once.
        A frozen dataclass that checks in ``__post_init__`` cannot exist in a broken state, which
        is strictly stronger than a check some path might skip.

        A negative ``intensity`` used to reach the telemetry handlers as a negative amplitude and
        drive a rig's pressure to -160 while the answer key still called it a spike; the flow and
        log lanes hid it instead, since ``Calibration.units`` floors at one record. Two different
        wrong answers to one meaningless input.

        ``start_step`` mirrors it. A negative one was silently clamped to the recording's start
        while a start *past* the end was refused by the unexpressed-fault guard — the same class
        of mistake handled two ways, with the quiet one producing a label the author never asked
        for. The magnitude ceiling is deliberately absent: how *large* an intensity may be is a
        per-channel question (``telemetry._rise``), so it cannot be answered here.
        """
        if self.intensity < 0:
            raise GenerationError(
                f"fault {self.fault_id!r} declares intensity {self.intensity}: intensity is "
                "multiples of a detector boundary (ADR-0028 D6), so a negative one describes no "
                "fault any lane can express"
            )
        if self.start_step < 0 or self.length_steps < 0:
            raise GenerationError(
                f"fault {self.fault_id!r} declares start_step={self.start_step}, "
                f"length_steps={self.length_steps}: a fault happens inside the recording, and a "
                "window before it starts would be labelled at an instant nothing was observed at"
            )

    @property
    def fault_id(self) -> str:
        """A stable, readable identity, so the labels file can be read by eye."""
        return f"{self.asset_id}:{self.kind}:{self.start_step}"


@dataclass(frozen=True)
class Recording:
    """One labelled example — what a bundle stores as a *case*.

    ``devices`` is written as a tuple because that reads well in a scenario, but it *means* a
    set keyed by ``asset_id``: every consumer looks a device up by that id, never by position.
    ``__post_init__`` enforces the identity the type cannot express.
    """

    name: str
    clock: Clock = field(default_factory=Clock)
    devices: tuple[Device, ...] = ()
    faults: tuple[FaultSpec, ...] = ()

    def __post_init__(self) -> None:
        """One asset id names one device, and a recording describes something.

        ## Why uniqueness is load-bearing rather than tidy

        ``asset_id`` is the key the whole pipeline correlates on, so everything downstream keys
        on it too: ``faults_on`` looks faults up by it, the builder's clock is a dict of it, the
        roster is a set of it. Two devices sharing one id therefore have no answer to "what is
        this asset's sampling profile?" — ``Device.kind`` selects one, and there would be two.

        The concrete damage was silent. ``faults_on`` handed both devices the **same**
        ``FaultSpec``, both reported perturbed instants under it, and the builder's
        ``expressed |= observed.expressed`` kept only the last writer. Measured on a rig and a
        camera sharing an id: the rig's 118 perturbed instants were discarded and the camera's 22
        survived, which — being sparse against the rig's 900-instant clock — split into **22
        single-instant incidents** where the corpus should have had one. No error, no warning, a
        plausible-looking answer key off by a factor of twenty-two.

        A recording with no devices is refused for the neighbouring reason: ``_validate`` already
        refuses a device that emitted nothing, and a recording that describes nothing is the same
        statement one level up. It built an empty case that contributed a name and no data.
        """
        if not self.devices:
            raise GenerationError(
                f"recording {self.name!r} declares no devices; a recording is a small world, and "
                "an empty one contributes a case name with nothing in it"
            )
        counted = Counter(device.asset_id for device in self.devices)
        clashing = sorted(asset_id for asset_id, count in counted.items() if count > 1)
        if clashing:
            raise GenerationError(
                f"recording {self.name!r} declares {clashing} more than once; an asset id names "
                "exactly one device, because it selects that device's profile and is the key its "
                "faults, its clock and its roster entry are all looked up by"
            )
        # A fault id names one fault, for the structurally identical reason (ADR-0033).
        # `fault_id` is asset:kind:start_step and ignores length and intensity, so two faults
        # that differ only in those collide — and the answer key keys on it: `GroundTruth.faults`
        # is a set of (asset_id, fault_id) and the alert-level denominator counts distinct
        # faults. `Observed` already identified this hazard and solved it locally by keying
        # `expressed` on the spec object; downstream still reads the string, so the refusal
        # belongs here, where every other "this id must be unique" check already lives.
        fault_ids = Counter(fault.fault_id for fault in self.faults)
        colliding = sorted(fault_id for fault_id, count in fault_ids.items() if count > 1)
        if colliding:
            raise GenerationError(
                f"recording {self.name!r} declares faults sharing the id(s) {colliding}; a "
                "fault id is (asset, kind, start_step), so two faults differing only in length "
                "or intensity collide and the answer key would merge them into one incident"
            )
        # A fault must start inside the recording. Only the lower bound was checked, and the
        # lanes disagree about what an out-of-range start means: telemetry CLAMPS it to the
        # last sample while the additive lanes keep counting past the end of the clock, so one
        # declared fault produced projections over an hour apart on different lanes, sharing a
        # fault_id — and cross-lane correlation, the whole point of a trimodal corpus, could
        # never fire. Refused rather than clamped, because clamping is what made them disagree.
        overrun = sorted(
            fault.fault_id for fault in self.faults if fault.start_step >= self.clock.steps
        )
        if overrun:
            raise GenerationError(
                f"recording {self.name!r} starts fault(s) {overrun} at or after the end of its "
                f"{self.clock.steps}-step clock; a fault happens inside the recording, and the "
                "lanes do not agree about what a fault outside it means"
            )

    def faults_on(self, asset_id: str) -> tuple[FaultSpec, ...]:
        return tuple(fault for fault in self.faults if fault.asset_id == asset_id)


@dataclass(frozen=True)
class Scenario:
    """A corpus recipe — what one bundle contains. Adding a corpus is adding one of these.

    ``formats`` names the **source formats** this corpus records, in the order a spec's
    ``sources`` must declare them: a case's inputs align 1:1 and positionally with a spec's
    sources (``runner._check_alignment``), so this tuple is also the shape any bench spec for
    the corpus has to match. Named for formats rather than lanes because that is what it
    holds — one lane can have several dialects, and choosing between them is a rendering
    decision, not a statement about the world.
    """

    name: str
    description: str
    formats: tuple[str, ...]
    recordings: tuple[Recording, ...]
    seed: int
    provenance: str = "iotshield.eval.generate — synthetic, reproducible from the seed"
    licence: str = "CC0-1.0 (generated by iotshield; no third-party data)"

    @property
    def counts(self) -> str:
        """The corpus's size, as a phrase — so no description hand-maintains these numbers.

        The pre-rewrite generator computed this and the rewrite briefly hard-coded it, which
        immediately drifted: one scenario said "15-device fleet" while a recording in it had
        16 devices, and only one of the three scenarios had its numbers pinned by a test.
        """
        devices = {device.asset_id for r in self.recordings for device in r.devices}
        faults = sum(len(recording.faults) for recording in self.recordings)
        return (
            f"{len(self.recordings)} recordings, {len(devices)} device(s), "
            f"{faults} injected fault(s)"
        )
