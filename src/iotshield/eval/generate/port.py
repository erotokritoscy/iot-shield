"""The two extension points: how a device **behaves**, and how that is **written down**.

Something has to pass between "what happened" and "the file", and we already own the perfect
vocabulary for one observation: ``NormalizedEvent``, the contract at the centre of the
product. It genuinely carries all three lanes — ``severity`` for a log line, ``metadata`` for
a flow record, ``metric``/``value`` for a sensor reading — because that is the contract doing
its job.

So the split is:

- a **Behaviour** models one lane's *normal*, expresses the faults aimed at a device, and
  reports which of them it actually perturbed. It produces ``NormalizedEvent``s.
- a **Renderer** turns those events into the wire text a customer would send.

## Why a renderer is worth its own object

Because it makes rendering the formal **inverse of a normalizer**, which is checkable: emit
the text, run it through the *real* normalizer, and confirm we get back what we claimed to
write. A renderer that lies about its lane, writes a timestamp that will not parse, or names
a device the normalizer re-attributes to the peer fails that test immediately. The codebase
half-knew this already — ``corpus.to_stub_csv``'s docstring called itself "the inverse of that
normalizer's fan-out" — it was simply never promoted from a comment to a concept.

And because dialect multiplicity is *measured*, not speculative: six wire formats already
exist across three lanes (stub-csv; two syslog dialects; two flow dialects; canonical JSONL).
"How a pump behaves" and "which dialect it is logged in" are different questions, so adding a
dialect is a renderer and nothing else.

Neither is a fifth pipeline port. Ports are the *product's* runtime joints; nothing here runs
at product runtime, which is the same argument ``corpus`` makes about ``Corpus`` itself.
"""

import math
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, TypeAlias

import numpy as np

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.eval.generate.world import Clock, Device, FaultSpec


@dataclass(frozen=True)
class Calibration:
    """A detector boundary a fault's magnitude is expressed **in multiples of**.

    The generalization of the old ``generate.THRESHOLD_HINT``. That constant existed because
    the telemetry corpus only discriminates while its channel scales straddle
    ``stub_threshold``'s default of 80 — change either side and the corpus quietly stops
    teaching anything, with the whole suite still green, because a bench spec pins its own
    threshold and cannot see the drift.

    Declaring it per behaviour lets one meta-test check every corpus's calibration against the
    registry, so the coupling is enforced rather than commented. ``preset`` names the preset
    the value belongs to, for a detector whose thresholds live in a ``PRESETS`` table.

    ## Why it is now load-bearing rather than documentary (ADR-0028 D6)

    It used to be a declaration a test checked and nothing read. Now it is the **unit** every
    fault magnitude is quoted in: a behaviour converts ``FaultSpec.intensity`` into its own
    units by multiplying this boundary, so ``intensity=1.0`` means "exactly at the alarm line"
    on every lane at once.

    That is what makes one scalar honest across three modalities. Before, the same number meant
    "30 network peers" to the flow behaviour and "multiply the thermal rise by 30" to the sensor
    rig, so the trimodal compromise — documented as deliberately *marginal*, so the corroboration
    A/B would measure corroboration rather than an obvious spike — drove the camera's temperature
    to 316.7 degrees against a threshold of 80, and the telemetry arm caught it outright.

    A boundary is therefore declared per *fault kind*, not per behaviour: fan-out's 20 peers and
    the port scan's 15 ports are different alarm lines on one lane, and a fault expressed against
    the wrong one is a magnitude nobody chose.

    ## When ``param`` is None (ADR-0038 D5)

    ``param`` names the registry default this boundary must equal, and a meta-test pins the two
    together — the right law while a detector's threshold is a hyperparameter someone typed.
    ``meter_iforest`` has none: its threshold is an isolation-forest score fitted to a
    false-alarm budget on each meter's own history, so there is no number in the registry for
    ``intensity=1.0`` to equal, and pointing at ``fp_budget`` would be worse than nothing since
    a rate is not a magnitude. So ``param=None`` says "learned, not configured", and ``value``
    is then in units the behaviour itself owns.

    The hatch is narrow on purpose. A meta-test allows ``None`` only for a **Fittable**
    detector, which is exactly the class that learns its own line; and what replaces the
    registry check is a *behavioural* one — the corpus asserts a fault at the declared intensity
    is actually caught — because a boundary in a behaviour's own units can only be validated by
    measuring it, never by comparing it to a constant.
    """

    detector: str
    param: str | None
    value: float
    preset: str | None = None

    def __post_init__(self) -> None:
        if self.preset is not None and self.param is None:
            # A preset names *where* to read the param from, so a preset with no param to read
            # is a boundary pointing at nothing. Refused here rather than left constructable,
            # since the meta-test's `param is None` branch would skip it silently.
            raise ValueError(
                f"{self.detector} declares preset {self.preset!r} with no param: a preset says "
                "which table holds the registered default, so there has to be one to hold"
            )

    def scaled(self, intensity: float) -> float:
        """The magnitude ``intensity`` multiples of this boundary comes to, in its own units.

        The ADR-0028 D6 conversion, written **once**. It was open-coded per lane — a `_target` in
        the sensor rig, a `_lines` in the log source, an `_at_least_one` in the network host —
        which is three chances for the rule that makes ``intensity`` mean one thing to drift.
        The natural home is the object being multiplied, and this class already calls itself
        "the **unit** every fault magnitude is quoted in".
        """
        return self.value * intensity

    def units(self, intensity: float, per_unit: float = 1.0) -> int:
        """``scaled``, as a whole number of records — at least one.

        Rounded **up** because ``intensity=1.0`` means "at the boundary", and a fault producing
        19.6 peers against a threshold of 20 would sit below a line it was asked to sit on.

        ``per_unit`` is how much of the boundary's quantity one record carries, which is 1 for
        the counting boundaries (a peer, a port, a log line) and a chunk size for a volumetric
        one — so an exfiltration of ``intensity`` x 50 MB is however many fixed-size flows that
        takes. Without it the volumetric case would need its own conversion, which is exactly the
        per-lane special-casing this method exists to remove.
        """
        return max(1, math.ceil(self.scaled(intensity) / per_unit))


@dataclass(frozen=True)
class Observed:
    """One device's contribution to one lane of one recording.

    ``expressed`` maps each ``FaultSpec`` this behaviour actually perturbed to **the instants
    at which its output differs from the unfaulted world**. It is what makes both an incident's
    ``lanes`` and its *interval* derived facts. A behaviour with no vocabulary for a fault
    reports nothing for it, and that is not a gap: a compromised camera really does still read
    22 degrees (ADR-0024 D2). The answer key therefore records which modalities carried the
    signal, and when, because the generator observed it — never because someone typed it.

    Keyed by the spec object rather than by ``fault_id``: that id is a readable display string
    (``asset:kind:start_step``), so two faults sharing one would silently merge into a single
    label. ``FaultSpec`` is frozen and therefore hashable, which distinguishes two faults that
    differ in *any* field — including the intensity a shared display id would hide.

    It does **not** distinguish two faults that are equal in every field, because a frozen
    dataclass hashes by value: two identical specs are one key. That is the right answer (they
    describe the same fault twice) but it is not the structural guarantee this docstring used to
    claim, and it is worth being exact about, since the neighbouring collision — one *fault*
    reaching two devices, where the builder's merge would keep only the last — is prevented by
    ``Recording.__post_init__`` making an asset id name exactly one device, not by this key.

    Instants rather than a bare ``True``, because the boolean was answering the wrong
    question. It was set when a *handler existed* for the fault kind, which is a statement
    about the code; the label needs a statement about the data. With only the boolean, a
    fault of ``intensity=0.0`` produced byte-identical output and still shipped labelled, and
    a fault clamped into range by the handler was labelled at its unclamped declared window —
    two computations of one interval, only one of which touched a byte. Reporting the
    perturbed instants collapses them into one.

    Every behaviour already computes this at the line where it used to discard it: the
    telemetry rig knows the span it damaged, and the flow and log sources hold the exact
    steps they injected records at.
    """

    events: tuple[NormalizedEvent, ...]
    expressed: Mapping[FaultSpec, frozenset[datetime]] = field(default_factory=dict)


Streams: TypeAlias = Callable[..., np.random.Generator]
"""Opens further private random streams, already scoped to one device and lane.

A single ``Generator`` would only be insertion-stable down to the device: drawing three
channels from one stream means *adding a fourth channel* shifts the values of the ones after
it, which is the same defect one level down that ``rng`` exists to remove. So a behaviour opens
a stream per independent thing it generates — ``streams("Pressure")`` — and ``streams()`` is
the device-and-lane stream itself.
"""


class Behaviour(Protocol):
    """Models one (lane, payload): what normal looks like, and what a fault does to it."""

    @property
    def lane(self) -> Lane: ...

    @property
    def payload(self) -> str | None:
        """The typed vocabulary this behaviour's events carry in ``metadata``, or ``None``.

        ``None`` means the events fit the contract's own fields. With ``lane`` this is the
        behaviour's ``Shape``, and therefore the key it registers under.
        """
        ...

    @property
    def expresses(self) -> frozenset[str]:
        """``FAULTS`` keys this lane can show — the keys of its own dispatch table.

        Derived from the handlers rather than declared beside them, so a fault this behaviour
        renders and one it claims to render cannot be different sets. ``faults.lanes_of`` reads
        the reverse direction out of the same source.
        """
        ...

    @property
    def calibration(self) -> tuple[Calibration, ...]:
        """The distinct boundaries this lane's magnitudes are quoted against.

        Derived from ``boundary`` over ``expresses`` rather than listed separately, for the same
        reason ``expresses`` is derived from the handler table: two places to say one thing is
        one place to get it wrong.
        """
        ...

    def boundary(self, kind: str) -> Calibration:
        """The alarm line ``intensity`` is measured in multiples of, for one fault kind.

        Every kind in ``expresses`` must have one — a meta-test pins that, because a fault shape
        added without a boundary would silently fall back to whatever magnitude its handler
        happened to hard-code, which is the failure ADR-0028 D6 exists to remove.
        """
        ...

    def observe(
        self,
        device: Device,
        faults: Sequence[FaultSpec],
        clock: Clock,
        streams: Streams,
        source_format: str,
    ) -> Observed:
        """Everything this device was observed doing, as the pipeline will see it.

        ``source_format`` is stamped onto the events rather than filled in afterwards, so these
        are literally the events *this* wire format will produce once ingested — which is the
        claim the round-trip test checks. It must not influence anything else: ``streams`` is
        scoped by **lane**, so the world a behaviour describes is the same whichever dialect
        renders it. Faults are pre-filtered to this device, so an implementation never has to
        check ownership.
        """
        ...


class Renderer(Protocol):
    """Turns events into one wire format's text — the inverse of a normalizer."""

    @property
    def lane(self) -> Lane: ...

    @property
    def payload(self) -> str | None:
        """Which behaviour's events this dialect writes — see ``Behaviour.payload``.

        A renderer and its behaviour must agree, because ``build._resolve`` looks the behaviour
        up by the renderer's shape. Two dialects of one shape still coexist; what this
        separates is two *shapes* on one lane, which is what metering introduced.
        """
        ...

    @property
    def source_format(self) -> str:
        """Must be a format some registered normalizer accepts; a meta-test enforces it."""
        ...

    def header(self) -> tuple[str, ...]:
        """Schema lines written **once per file**, however many devices contribute.

        Separate from the rows because a CSV needs its column row once and Zeek needs its
        ``#fields`` directive once — the same "header lines are schema, not data" distinction
        ``normalize.flow`` makes on the reading side, so writer and reader now agree on it.
        """
        ...

    def render(self, events: Sequence[NormalizedEvent]) -> tuple[str, ...]:
        """The lines these events were parsed from, **in their order**.

        Usually one line per event, and for three of the four shipped dialects exactly that —
        but it cannot be a law, because a normalizer need not be a per-record function. The
        meter normalizer *differences* a cumulative counter, so N consumption events are the
        differences of N+1 counter readings and the file legitimately holds one row more than
        there are events. A renderer inverting one must write that opening row, once per
        contiguous run.

        What remains a law is the ordering. It belongs to the builder, which pools every
        device's events for a lane and sorts them by ``(timestamp, asset_id)`` before rendering
        — so a multi-host capture is one time-ordered file, the way a real one is, and the
        normalizer's per-line statelessness is exercised. A renderer that sorted internally
        would fight that and, worse, would hide a builder that stopped sorting. The meta-test
        therefore asks that the output *depend* on input order and lose nothing in either
        direction, rather than that reversing the input reverse the file line for line — a
        property only a one-line-per-event renderer can have.
        """
        ...


class LaneBehaviour:
    """The half of a ``Behaviour`` every lane implements identically — declared once.

    A behaviour owns two tables keyed by fault kind: what each fault *does* (its handlers) and
    what boundary each fault's magnitude is *quoted against*. Everything else the Protocol asks
    for is a derivation of those two, so this base performs the derivations and the lane modules
    declare only the tables.

    Before, each lane wrote the derivations out. ``expresses`` was ``frozenset(self._handlers)``
    in three files; ``calibration`` was a hand-typed 1-tuple in two of them and derived from a
    third table in the other; and ``boundary`` was implemented twice as ``del kind`` plus a
    constant — the visible symptom of a *function* in the Protocol where every lane really owns
    a *table*. A meta-test then checked that the hand-written `calibration` still matched the
    boundaries it was supposed to be derived from, which is the signature of a mechanism sitting
    one level too low: the test's whole job was to re-establish a law the code could have made
    true by construction.

    The two tables are checked against each other here, which is the part no lane can now forget:
    a fault with a handler and no boundary would silently fall back to whatever magnitude its
    handler hard-coded, and that is the state ADR-0028 D6 exists to remove.
    """

    payload: str | None = None
    """Most lanes carry no typed vocabulary, so this is the default half of a ``Shape``; only a
    behaviour whose events ride behind a record (metering) overrides it."""

    def __init__(self, handlers: Iterable[str], boundaries: Mapping[str, Calibration]) -> None:
        kinds = frozenset(handlers)
        if kinds != frozenset(boundaries):
            raise ValueError(
                f"{type(self).__name__} expresses {sorted(kinds)} but declares boundaries for "
                f"{sorted(boundaries)}; every fault a lane can show must say what its magnitude "
                "is measured against"
            )
        self._boundaries = dict(boundaries)

    @property
    def expresses(self) -> frozenset[str]:
        return frozenset(self._boundaries)

    @property
    def calibration(self) -> tuple[Calibration, ...]:
        # Distinct values, in a stable order: several fault kinds may share one boundary (a scan
        # and a compromise are both fan-out), and a caller pinning these against the registry
        # should check each boundary once rather than once per fault that uses it.
        return tuple(dict.fromkeys(self._boundaries[kind] for kind in sorted(self._boundaries)))

    def boundary(self, kind: str) -> Calibration:
        return self._boundaries[kind]


def one_boundary(handlers: Iterable[str], calibration: Calibration) -> dict[str, Calibration]:
    """Every fault kind measured against the same boundary — a whole-lane alarm line.

    For a lane whose detector compares everything to one value (``stub_threshold`` on telemetry,
    ``log_burst``'s ``min_count``), as opposed to one with a preset table (flow). Spelling it as
    data keeps "one boundary for this whole lane" a visible fact rather than a ``del kind``.
    """
    return dict.fromkeys(handlers, calibration)


Shape: TypeAlias = tuple[Lane, str | None]
"""A behaviour's slot: which modality, and which typed vocabulary within it (ADR-0035).

The generator was written when a lane *was* the whole shape, and encoded that in this key. It
is the same conflation ADR-0035 removed from the product, surviving in the one place that ADR
did not reach — and a metering corpus is where it stops being harmless: a meter behaviour is a
**second telemetry-lane behaviour**, so keyed by lane alone it would collide with the sensor
rig, and ``_resolve`` would hand a meter renderer the rig's plain numeric readings. The
round-trip law would then fail with a mismatch that looks like a bad renderer.
"""


def ramp(samples: int, rise: float) -> np.ndarray:
    """A ramp **ending** at ``rise``, well-defined for every ``samples >= 1``.

    Shared rather than per behaviour: the telemetry drift and the metering leak are the same
    shape in different units, and the subtlety is not obvious enough to re-derive. The obvious
    ``linspace(0, rise, n)`` is wrong at the narrow end — it spends its first endpoint on zero,
    so a ramp covering one sample ended where it started and silently perturbed nothing (the
    ADR-0028 defect that also bit ``_bump``). Dropping the leading zero makes it end at ``rise``
    whatever its width, which is what "a slow ramp ending past the threshold" always claimed,
    and costs nothing at the wide end where the first step is ``rise/n``.
    """
    return np.linspace(0.0, rise, samples + 1)[1:]


def shape_id(shape: Shape) -> str:
    """A shape as one readable token — ``telemetry/meter``, ``flow/plain``.

    Named once because it is also the **sort key** for a registry keyed on these tuples: the raw
    keys hold ``None`` beside a payload name on the telemetry lane, and comparing those raises
    rather than ordering them. Two copies of that workaround (a generator error message and a
    suite's pytest ids) would be two chances to disagree about what a shape is called.
    """
    lane, payload = shape
    return f"{lane.value}/{payload or 'plain'}"


BEHAVIOURS: dict[Shape, Behaviour] = {}
"""One behaviour per (lane, payload). Device *kinds* select profiles inside it, so a fleet of
cameras and gateways needs one network behaviour rather than one per device type."""

RENDERERS: dict[str, Renderer] = {}
"""Keyed by ``source_format``, so several dialects of one lane coexist."""
