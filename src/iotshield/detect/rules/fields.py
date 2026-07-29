"""The rule field vocabulary: what a rule author is allowed to name (ADR-0032 D1).

What a "field" is
-----------------
A rule is one sentence — ``IF <field> <operator> <value> THEN raise a finding``. Only two
of those three parts are written by a human: the operator and the value are the author's
judgment ("greater than 10000"), while ``<field>`` is the *name of a slot the data fills*.
``flow.bytes_out`` is not something anyone types into a form; it is a label for "however
many bytes this device sent in this conversation", a different number on every record.

This module is the published dictionary of legal names. It is a phone book, not a filing
cabinet: it stores no data and writes nothing. Each entry is a name, the kind of value it
holds, a one-line description, and instructions for *reading* it back out.

Why a table, and why it reads through the typed records
-------------------------------------------------------
``FlowRecord`` closed the untyped hop between normalize and detect by making it exactly one
function call wide, so a name mismatch is caught by the type checker before anything runs.
That trick is unavailable here: a rule's field name arrives as **text in a YAML file at
runtime**, and no type checker will ever see it. So the same principle takes a different
shape — the untyped hop becomes exactly one *table* wide.

The load-bearing detail is the ``read`` column: it goes **through** ``FlowRecord``, never
around it into ``metadata``. Three consequences, and they are the whole justification:

1. If ``FlowRecord`` loses or renames a field, this table stops type-checking and CI catches
   it. The vocabulary cannot silently drift from the data it describes.
2. Because the table is *data*, it can be enumerated — published to the docs and printable
   with ``python -m iotshield.detect.rules``. Documentation generated from the mechanism
   cannot go stale, which a hand-maintained "field list" always eventually does.
3. Every rule is checked when it *loads*: an unknown name is refused with suggestions, and
   an operator that does not suit the field's kind (asking whether a service name is greater
   than 10000) is refused too, rather than raising ``TypeError`` mid-run as the upstream
   engine does.

The vocabulary is deliberately **closed** — there is no escape hatch for addressing
arbitrary metadata paths. An open path would re-open the silent-failure hole this design
exists to close, and re-open it for the least-defended audience: someone editing YAML, with
no type checker, no tests, and no error message. A source needing fields we do not model is
a request to extend this table (one reviewed entry), not to bypass it.

Placement (ADR-0032 D1): unlike ``flow``/``meter``/``enrichment``, this vocabulary crosses
no port boundary — YAML in, events read, findings out, all inside one adapter. So it lives
with the adapter that owns it rather than at the top level, which would imply other stages
depend on it.
"""

from __future__ import annotations

import difflib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from iotshield.contracts import Lane, NormalizedEvent, Severity
from iotshield.detect._severity import SEVERITY_RANK
from iotshield.flow import METADATA_KEY as FLOW_METADATA_KEY
from iotshield.flow import FlowRecord


class FieldKind(StrEnum):
    """What kind of value a field holds — which decides the operators it accepts."""

    NUMBER = "number"
    TEXT = "text"
    TIER = "tier"  # an ordered category (severity): comparable, but not arithmetic


_UNDECODED: Any = object()
"""Distinguishes "not looked at yet" from a decode that legitimately returned ``None`` —
which a plain ``None`` cache could not, and which is the common case on a FLOW event a
foreign normalizer produced."""


class EventView:
    """One event, with its lane record decoded at most once.

    A rule may name several flow fields in one selection, and each rule in the set re-asks;
    ``FlowRecord.from_event`` parses the whole record every call. Measured on the flow lane,
    this memo saves more than the rest of ``detect()`` costs. ``__slots__`` rather than
    ``functools.cached_property`` because one of these is built per event on the
    highest-volume input we ingest, and a per-instance ``__dict__`` is the thing to avoid
    there.
    """

    __slots__ = ("_flow", "event")

    def __init__(self, event: NormalizedEvent) -> None:
        self.event = event
        self._flow: FlowRecord | None = _UNDECODED

    @property
    def flow(self) -> FlowRecord | None:
        if self._flow is _UNDECODED:
            self._flow = FlowRecord.from_event(self.event)
        return self._flow


@dataclass(frozen=True)
class FieldSpec:
    """A published field: its name, what it holds, and how to read it.

    ``read`` returns ``None`` when the event carries no value for this field (a flow event
    with no service, a log line on a non-flow lane). A missing value never matches any
    operator — see ``rules/model.py`` for why that is stricter than the upstream engine.
    """

    name: str
    kind: FieldKind
    description: str
    read: Callable[[EventView], Any]
    payload: str | None = None
    """The typed lane vocabulary ``read`` goes through, when it goes through one.

    Set by the ``_field`` helper that creates the dependency, never by hand, so
    ``PAYLOAD_BY_LANE`` is *read off* this table rather than restated beside it (ADR-0035 D2).
    """


def _flow_field(
    name: str, kind: FieldKind, description: str, pick: Callable[[FlowRecord], Any]
) -> FieldSpec:
    """A field read off the decoded ``FlowRecord`` — never off raw ``metadata``."""

    def read(view: EventView) -> Any:
        record = view.flow
        return None if record is None else pick(record)

    return FieldSpec(
        name=name, kind=kind, description=description, read=read, payload=FLOW_METADATA_KEY
    )


# --- The vocabulary itself, one table per lane. -------------------------------------
#
# Lanes have separate tables because lanes are separate: nothing converges into a shared
# shape. Telemetry and log fields read straight off the event, where they already live;
# flow fields read through FlowRecord. That separation is what keeps FlowRecord from
# becoming a dumping ground for things that are not facts about a network conversation.

_COMMON_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        name="severity",
        kind=FieldKind.TIER,
        description="Normalized event severity (debug < info < warning < error < critical)",
        read=lambda view: view.event.severity,
    ),
    FieldSpec(
        name="source_format",
        kind=FieldKind.TEXT,
        description='Fine-grained origin of the record, e.g. "syslog-rfc5424"',
        read=lambda view: view.event.source_format,
    ),
)
"""Fields every lane carries, because they are the contract's own."""

_TELEMETRY_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        name="telemetry.metric",
        kind=FieldKind.TEXT,
        description='Channel name of the reading, e.g. "Pressure"',
        read=lambda view: view.event.metric,
    ),
    FieldSpec(
        name="telemetry.value",
        kind=FieldKind.NUMBER,
        description="The numeric reading itself",
        read=lambda view: view.event.value,
    ),
)

_LOG_FIELDS: tuple[FieldSpec, ...] = (
    FieldSpec(
        name="log.message",
        kind=FieldKind.TEXT,
        description="The log line's message text (the parsed body, not the raw line)",
        read=lambda view: view.event.message,
    ),
)

_FLOW_FIELDS: tuple[FieldSpec, ...] = (
    _flow_field(
        "flow.bytes_out", FieldKind.NUMBER, "Bytes sent BY the asset", lambda r: r.bytes_out
    ),
    _flow_field(
        "flow.bytes_in", FieldKind.NUMBER, "Bytes received by the asset", lambda r: r.bytes_in
    ),
    _flow_field(
        "flow.packets_out", FieldKind.NUMBER, "Packets sent by the asset", lambda r: r.packets_out
    ),
    _flow_field(
        "flow.packets_in", FieldKind.NUMBER, "Packets received by the asset", lambda r: r.packets_in
    ),
    _flow_field(
        "flow.duration_s",
        FieldKind.NUMBER,
        "Conversation duration in seconds",
        lambda r: r.duration_s,
    ),
    # Ports are asset-RELATIVE, like every other flow field, and that catches rule authors
    # out: a service port lands on `peer_port` for a connection the device made and on
    # `asset_port` for one it received. "Telnet" is `peer_port == 23` outbound and
    # `asset_port == 23` inbound. The descriptions say so because the alternative is a rule
    # that looks obviously right and can never fire.
    _flow_field(
        "flow.peer_port",
        FieldKind.NUMBER,
        "Port on the OTHER end — the service the device DIALLED, on an outbound connection",
        lambda r: r.peer_port,
    ),
    _flow_field(
        "flow.asset_port",
        FieldKind.NUMBER,
        "Port on the asset's end — the service the device EXPOSED, on an inbound connection",
        lambda r: r.asset_port,
    ),
    _flow_field(
        "flow.peer_ip", FieldKind.TEXT, "Address of the other endpoint", lambda r: r.peer_ip
    ),
    _flow_field(
        "flow.protocol", FieldKind.TEXT, 'Transport protocol, e.g. "tcp"', lambda r: r.protocol
    ),
    _flow_field(
        "flow.service",
        FieldKind.TEXT,
        'Application service as seen, e.g. "smb"',
        lambda r: r.service,
    ),
    _flow_field(
        "flow.state",
        FieldKind.TEXT,
        'Connection state as the exporter saw it, e.g. "S0"',
        lambda r: r.state,
    ),
    _flow_field(
        "flow.direction",
        FieldKind.TEXT,
        "Where the conversation went, relative to the asset: outbound / inbound / internal",
        lambda r: r.direction.value,
    ),
)

_BY_LANE: dict[Lane, tuple[FieldSpec, ...]] = {
    Lane.TELEMETRY: _COMMON_FIELDS + _TELEMETRY_FIELDS,
    Lane.LOG: _COMMON_FIELDS + _LOG_FIELDS,
    Lane.FLOW: _COMMON_FIELDS + _FLOW_FIELDS,
}

PAYLOAD_BY_LANE: dict[Lane, frozenset[str]] = {
    lane: frozenset(spec.payload for spec in specs if spec.payload is not None)
    for lane, specs in _BY_LANE.items()
}
"""Which typed lane vocabularies a rule on this lane needs an event to carry (ADR-0035 D2).

**Derived, not declared.** A field that reads through a typed record says so on itself, and
this reads that off the table above — so adding a ``_meter_field`` to the telemetry lane
declares the meter payload automatically. Written by hand it would be a restatement of
``_flow_field``'s dependency one level up, and the first lane whose fields gained a
vocabulary would keep declaring none: the guard would pass the chain and every such rule
would evaluate to "missing" forever, which is a quiet night rather than an error. That is
exactly the defect ADR-0035 exists to fix, reintroduced one lane over.

A set rather than a single value, because nothing says one lane carries one vocabulary.
Lanes whose fields read the contract's own columns map to an empty set.
"""


def fields_for(lane: Lane) -> Mapping[str, FieldSpec]:
    """The vocabulary a rule on this lane may address, keyed by published name."""
    return {spec.name: spec for spec in _BY_LANE[lane]}


def resolve(lane: Lane, name: str) -> FieldSpec:
    """Look a field up, or raise naming the closest matches and then the whole vocabulary.

    The error is the entire user interface for a mistyped field, so it does the work: a
    near-miss gets a "did you mean", and everything else gets the list. Without this the
    rule would load clean and match nothing forever, which reads exactly like a quiet night.
    """
    vocabulary = fields_for(lane)
    spec = vocabulary.get(name)
    if spec is not None:
        return spec
    suggestions = difflib.get_close_matches(name, vocabulary, n=3, cutoff=0.6)
    hint = f" Did you mean {', '.join(repr(s) for s in suggestions)}?" if suggestions else ""
    known = ", ".join(sorted(vocabulary))
    raise ValueError(f"unknown field {name!r} for lane {lane.value!r}.{hint} Known fields: {known}")


def coerce_value(spec: FieldSpec, value: Any) -> Any:
    """Convert a YAML literal into the comparable form this field's kind uses.

    Done at LOAD time so a rule written as ``value: "10000"`` (quoted by a YAML habit)
    compares as a number rather than silently comparing as text and never firing, and so a
    severity tier written as ``critical`` becomes a rank rather than a string that would
    order alphabetically.
    """
    if spec.kind is FieldKind.NUMBER:
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"field {spec.name!r} holds a number, so its value must be numeric; got {value!r}"
            ) from exc
    if spec.kind is FieldKind.TIER:
        try:
            return SEVERITY_RANK[Severity(str(value).lower())]
        except (KeyError, ValueError) as exc:
            known = ", ".join(level.value for level in SEVERITY_RANK)
            raise ValueError(
                f"field {spec.name!r} holds a tier; {value!r} is not one of: {known}"
            ) from exc
    return str(value)


def read_comparable(spec: FieldSpec, view: EventView) -> Any:
    """Read a field from an event in the same form ``coerce_value`` produced.

    The two must agree or a comparison is meaningless, so they are defined next to each
    other and a test pins them together for every published field.
    """
    raw = spec.read(view)
    if raw is None:
        return None
    if spec.kind is FieldKind.NUMBER:
        return float(raw)
    if spec.kind is FieldKind.TIER:
        return SEVERITY_RANK[raw]
    return str(raw)


def describe() -> dict[str, list[dict[str, str]]]:
    """The whole vocabulary as plain data — for the docs, the CLI, and the authoring guide.

    This is what makes the published field list impossible to get out of step with the
    code: the guide is checked against this, rather than being a promise to keep two things
    in sync by hand forever.
    """
    return {
        lane.value: [
            {"name": spec.name, "kind": spec.kind.value, "description": spec.description}
            for spec in specs
        ]
        for lane, specs in _BY_LANE.items()
    }
