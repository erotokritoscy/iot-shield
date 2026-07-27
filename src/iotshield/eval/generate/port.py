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

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol, TypeAlias

import numpy as np

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.eval.generate.world import Clock, Device, FaultSpec


@dataclass(frozen=True)
class Calibration:
    """A detector boundary this behaviour's magnitudes were deliberately chosen against.

    The generalization of the old ``generate.THRESHOLD_HINT``. That constant existed because
    the telemetry corpus only discriminates while its channel scales straddle
    ``stub_threshold``'s default of 80 — change either side and the corpus quietly stops
    teaching anything, with the whole suite still green, because a bench spec pins its own
    threshold and cannot see the drift.

    Declaring it per behaviour lets one meta-test check every corpus's calibration against the
    registry, so the coupling is enforced rather than commented. ``preset`` names the preset
    the value belongs to, for a detector whose thresholds live in a ``PRESETS`` table.
    """

    detector: str
    param: str
    value: float
    preset: str | None = None


@dataclass(frozen=True)
class Observed:
    """One device's contribution to one lane of one recording.

    ``expressed`` holds the ``fault_id``s this behaviour actually perturbed — and it is what
    makes an incident's ``lanes`` a *derived fact*. A behaviour with no vocabulary for a fault
    returns without expressing it, and that is not a gap: a compromised camera really does
    still read 22 degrees (ADR-0024 D2). The answer key therefore records which modalities
    carried the signal because the generator observed it, never because someone typed it.
    """

    events: tuple[NormalizedEvent, ...]
    expressed: frozenset[str] = frozenset()


Streams: TypeAlias = Callable[..., np.random.Generator]
"""Opens further private random streams, already scoped to one device and lane.

A single ``Generator`` would only be insertion-stable down to the device: drawing three
channels from one stream means *adding a fourth channel* shifts the values of the ones after
it, which is the same defect one level down that ``rng`` exists to remove. So a behaviour opens
a stream per independent thing it generates — ``streams("Pressure")`` — and ``streams()`` is
the device-and-lane stream itself.
"""


class Behaviour(Protocol):
    """Models one lane: what normal looks like, and what a fault does to it."""

    @property
    def lane(self) -> Lane: ...

    @property
    def expresses(self) -> frozenset[str]:
        """``FAULTS`` keys this lane can show — the keys of its own dispatch table.

        Derived from the handlers rather than declared beside them, so a fault this behaviour
        renders and one it claims to render cannot be different sets. ``faults.lanes_of`` reads
        the reverse direction out of the same source.
        """
        ...

    @property
    def calibration(self) -> tuple[Calibration, ...]: ...

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
        """One line per event, **order-preserving**.

        Ordering belongs to the builder, which pools every device's events for a lane and
        sorts them by ``(timestamp, asset_id)`` before rendering — so a multi-host capture is
        one time-ordered file, the way a real one is, and the normalizer's per-line
        statelessness is exercised. A renderer that sorted internally would fight that.
        """
        ...


BEHAVIOURS: dict[Lane, Behaviour] = {}
"""One behaviour per lane. Device *kinds* select profiles inside it, so a fleet of cameras and
gateways needs one network behaviour rather than one per device type."""

RENDERERS: dict[str, Renderer] = {}
"""Keyed by ``source_format``, so several dialects of one lane coexist."""
