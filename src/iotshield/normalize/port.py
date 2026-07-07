"""Normalizer port: raw text in, events AND rejects out.

A port is a promise, not a component: the pipeline runner is written against this
Protocol and never imports an implementation. Any class with this shape IS a
Normalizer (structural typing); its behavior is pinned by the parametrized suite
in tests/ports/test_normalizer_port.py, which every adapter must join.

Semantics every implementation must honor (enforced by the suite):
1. Nothing is silently dropped: every non-blank input line either contributes
   events or produces a MalformedRecord. Failure is data, not logs.
2. The original record text is preserved verbatim in ``raw`` (events and rejects).
3. An unsupported ``source_format`` is a configuration error, not a data error:
   raise ValueError instead of returning everything as malformed.

Format ownership (deliberate asymmetry with Detector.lane): a lane rides on event
data, so the Detector port exposes it; a text blob has no intrinsic format, so the
pipeline *config* asserts which format an input is and the adapter double-checks
(rule 3). Adapters advertise their format(s) as module constants (e.g.
``stub.SOURCE_FORMAT``), not on the port.
"""

from dataclasses import dataclass, field
from typing import Protocol

from iotshield.contracts import MalformedRecord, NormalizedEvent


@dataclass
class NormalizeResult:
    """Return shape of one normalize() call — NOT a contract: never persisted,
    never serialized; it lives for exactly one hop (normalizer -> runner)."""

    events: list[NormalizedEvent] = field(default_factory=list)
    malformed: list[MalformedRecord] = field(default_factory=list)


class Normalizer(Protocol):
    def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult:
        """Normalize raw input text into events plus captured rejects."""
        ...
