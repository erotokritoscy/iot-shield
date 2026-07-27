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

    def span(self, start: int, length: int) -> tuple[datetime, datetime]:
        """The closed interval a fault occupies: ``length`` steps from ``start``.

        Closed on both ends, matching ``truth.overlaps``: the last *affected* instant, not the
        first unaffected one. An off-by-one here would shift every label in the corpus.
        """
        return self.at(start), self.at(start + max(length, 1) - 1)

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
    separation this module exists for collapses. One scalar means "how hard", and each
    behaviour reads it in its own units — peers for a flow scan, degrees for a thermal rise,
    lines per window for a log storm.
    """

    kind: str
    asset_id: str
    start_step: int
    length_steps: int
    intensity: float = 1.0

    @property
    def fault_id(self) -> str:
        """A stable, readable identity, so the labels file can be read by eye."""
        return f"{self.asset_id}:{self.kind}:{self.start_step}"


@dataclass(frozen=True)
class Recording:
    """One labelled example — what a bundle stores as a *case*."""

    name: str
    clock: Clock = field(default_factory=Clock)
    devices: tuple[Device, ...] = ()
    faults: tuple[FaultSpec, ...] = ()

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
