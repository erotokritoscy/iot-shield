"""Pass-through normalizer: input that already arrives as NormalizedEvent JSONL (ADR-0025).

Not every source needs parsing. Some upstream systems already emit our canonical event
shape — a prior iotshield run, a partner that adopted the contract, an ETL job that did the
normalization elsewhere. For those, "normalization" is not parsing a wire format but
**validation and enveloping**: every record is still checked against the contract
(timezone-aware UTC timestamps, finite values, required fields, no unknown fields) and every
failure is still captured as a ``MalformedRecord``, never dropped (ADR-0004). The four-port
model is untouched — the pipeline still *always* normalizes; this is simply the smallest
normalizer that honours every port law, which also makes it the hello-world for a new
contributor (docs/design/weeks-3-4.md §5.2/§5.6).

Format ("normalized-jsonl"): one JSON object per line, each matching ``NormalizedEvent``'s
fields. Blank lines are skipped (no content is not data).

The one design point (ADR-0025): the produced lane is a *parameter*
------------------------------------------------------------------
Every other normalizer produces a *fixed* lane (stub -> telemetry, syslog -> log,
flow -> flow), declared statically so the compatibility guard can read it without
constructing the adapter. But a pass-through record carries its *own* lane in
``source_type`` — the lane is in the data, which the static guard cannot see. Declaring
"produces all three lanes" would make the guard's coverage check (``pipeline/compat.py``)
warn about every lane no detector happens to consume, on every run, eroding the guard's
trustworthiness. So the lane a given source carries is a **config parameter** — this is the
first param-driven *normalizer* capability, mirroring the ``stub_threshold`` detector. The
source declares its lane; the capability reports exactly that lane; and each record's
``source_type`` is *filled from* the parameter when absent and *checked against* it when
present — turning the parameter into a guard that quarantines a misrouted source.

Provenance. ``raw`` is set by the shared loop to the verbatim JSON line (the port requires
the event's ``raw`` to be this stage's verbatim input; any ``raw`` inside the JSON is still
visible within that line). The event's own ``source_format`` is *preserved* when the record
carries one — it records where the data originally came from (e.g. ``"partner-scada"``),
which is distinct from the pipeline ``source_format`` that selected this normalizer — and
defaults to ours only when absent, so the required field is always satisfied.
"""

import json
from collections.abc import Mapping
from typing import Any

from iotshield.capabilities import Capability, FormatToken, LaneToken
from iotshield.contracts import Lane
from iotshield.normalize._linewise import LineParseError, normalize_linewise
from iotshield.normalize.port import NormalizeResult

SOURCE_FORMAT = "normalized-jsonl"


def capability(params: Mapping[str, Any]) -> Capability:
    """Consumes the JSONL format; produces exactly the one lane this source declares.

    Param-driven (not ``static``) because the produced lane is the ``lane`` parameter, not a
    property of the parser — mirrors ``detect/stub.py``. The default matches the factory's
    (``registry._passthrough_normalizer``); the meta-test in
    tests/pipeline/test_capabilities.py pins the two together against a built instance. A bad
    lane value raises ``ValueError``, which the guard reports as a structured issue.
    """
    lane = Lane(params.get("lane", Lane.TELEMETRY))
    return Capability.of(consumes={FormatToken(SOURCE_FORMAT)}, produces={LaneToken(lane)})


class PassThroughNormalizer:
    def __init__(self, lane: Lane = Lane.TELEMETRY) -> None:
        self._lane = lane

    def _parse_line(self, line: str) -> dict[str, Any]:
        """Parse one JSON line into the NormalizedEvent field mapping (all but ``raw``).

        Contract well-formedness (aware timestamp, finite value, required/unknown fields) is
        left to ``normalize_linewise``'s ``model_validate`` so it is never re-implemented
        here; this handles only what is specific to the JSON envelope and the lane parameter.
        """
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            # JSONDecodeError is a ValueError, which normalize_linewise does NOT catch; left
            # uncaught it would abort the whole run. A bad *record* is data, not an exception
            # (the port's first law), so translate it into a reject.
            raise LineParseError(f"not valid JSON: {exc.msg}") from exc
        if not isinstance(obj, dict):
            # A JSON array/number/string is valid JSON but not an event; ``{**non_dict}``
            # would raise TypeError and escape, so reject it as data instead.
            raise LineParseError(f"expected a JSON object, got {type(obj).__name__}")

        declared = obj.get("source_type")
        if declared is None:
            # A source configured for one lane may omit the redundant field; fill it in. The
            # member itself, not ``.value``: ``Lane`` is a StrEnum, so a member *is* its wire
            # string — the same thing stub.py and flow.py put in their field mappings, and it
            # avoids a descriptor call on every line.
            obj["source_type"] = self._lane
        elif declared != self._lane:
            # A record whose own lane disagrees with the source's configured lane is a
            # misrouted source, caught here rather than silently entering the wrong lane.
            raise LineParseError(
                f"record declares lane {declared!r} but this source is configured for "
                f"{self._lane.value!r}"
            )
        # Preserve upstream provenance; default to ours only when absent (see module docstring).
        obj.setdefault("source_format", SOURCE_FORMAT)
        return obj

    def normalize(self, text: str, *, source_format: str, origin: str) -> NormalizeResult:
        # normalize_linewise owns the port's laws: the source_format guard, the blank skip,
        # verbatim-``raw`` capture (it sets ``raw=line``, overriding any incoming ``raw``), and
        # the ValidationError -> MalformedRecord translation for every contract rule.
        return normalize_linewise(
            text,
            source_format=source_format,
            expected_format=SOURCE_FORMAT,
            origin=origin,
            parse_line=self._parse_line,
        )
