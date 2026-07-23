"""Adapter capability vocabulary: how a pipeline stage declares what it connects to.

A *capability* is a stage's plug shape — the set of tokens it ``consumes`` (needs
from upstream) and ``produces`` (offers downstream). Tokens are *typed* values, one
class per connector axis:

- ``FormatToken`` at the ingest joint (which raw format a normalizer accepts),
- ``LaneToken`` at the normalize->detect joint (which modality flows between them),
- the pass-through signals ``DETECTIONS`` / ``ALERTS`` (``SignalToken``) at the later joints.

The compatibility guard (``pipeline/compat.py``) validates a config by comparing
adjacent stages' ``produces``/``consumes`` as sets. Capabilities are declared
co-located with each adapter and surfaced through the registry, so the guard and the
``/modules`` discovery endpoint can read a stage's shape WITHOUT constructing the
(possibly expensive) adapter — the whole reason this is static metadata and not an
instance property.

Tokens are typed rather than bare strings so the type checker catches a mismatched
axis or a mistyped lane, and there is no prefix-string scheme scattered through the
code to keep in sync; each token renders to its stable wire form (``format:<x>`` /
``lane:<x>`` / ``detections`` / ``alerts``) only at the API boundary, via ``render()``.
This module depends only on ``contracts`` (the ``Lane`` vocabulary), so any adapter
can import it without an import cycle through the pipeline package.
"""

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from iotshield.contracts import Lane

_FORMAT_PREFIX = "format:"
_LANE_PREFIX = "lane:"


@dataclass(frozen=True)
class FormatToken:
    """A raw input format accepted at the ingest joint (e.g. ``syslog``)."""

    format: str

    def render(self) -> str:
        return f"{_FORMAT_PREFIX}{self.format}"


@dataclass(frozen=True)
class LaneToken:
    """A modality lane flowing across the normalize->detect joint."""

    lane: Lane

    def render(self) -> str:
        return f"{_LANE_PREFIX}{self.lane.value}"


@dataclass(frozen=True)
class SignalToken:
    """A pass-through stage signal at the later joints (detections, alerts)."""

    name: str

    def render(self) -> str:
        return self.name


Token = FormatToken | LaneToken | SignalToken

# The pass-through signals: every detector produces DETECTIONS, every alert engine
# consumes them and produces ALERTS. Singletons, so a joint is checked by value
# equality on a typed token rather than a re-typed string.
DETECTIONS = SignalToken("detections")
ALERTS = SignalToken("alerts")


@dataclass(frozen=True)
class Capability:
    """One stage's connectors: the tokens it needs in, and the tokens it emits."""

    consumes: frozenset[Token]
    produces: frozenset[Token]

    @classmethod
    def of(cls, *, consumes: Iterable[Token], produces: Iterable[Token]) -> "Capability":
        """Build from any iterables (call sites pass set literals; we freeze them)."""
        return cls(consumes=frozenset(consumes), produces=frozenset(produces))


# A capability that may depend on the adapter's config params (e.g. a detector whose
# lane is a param). Static adapters wrap a fixed Capability with ``static`` below.
CapabilityProvider = Callable[[Mapping[str, Any]], Capability]


def formats_in(tokens: Iterable[Token]) -> set[str]:
    """The formats among a token set (typed: only ``FormatToken``s contribute)."""
    return {token.format for token in tokens if isinstance(token, FormatToken)}


def lanes_in(tokens: Iterable[Token]) -> set[Lane]:
    """The lanes among a token set (typed: only ``LaneToken``s contribute)."""
    return {token.lane for token in tokens if isinstance(token, LaneToken)}


def static(capability: Capability) -> CapabilityProvider:
    """Adapt a fixed capability into a provider, for adapters whose shape ignores params."""

    def provider(_params: Mapping[str, Any]) -> Capability:
        return capability

    return provider
