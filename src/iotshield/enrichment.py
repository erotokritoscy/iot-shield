"""Threat-explanation vocabulary: what a detection claims about *what happened*.

Why this module exists (ADR-0032 D4)
------------------------------------
A ``DetectionResult`` says "this asset, this lane, this window, this far past the
threshold". It deliberately says nothing about *what kind of thing* that is, because for
four of our five detectors there is nothing to say — a robust z-score knows a reading is
far from normal and knows nothing else.

Two of our producers do know. A **rule** carries the classification its author wrote down;
a **preset** (``flow_burst:fan_out``, ``log_burst:auth_failure``) names a phenomenon we
chose deliberately. That knowledge has to reach ``PrioritizedAlert.threat_class`` and
``recommended_action`` — fields that have been reserved since contracts v1 and have never
once been populated.

Getting it there means crossing the detect -> alert seam, and the only channel is
``DetectionResult.evidence``, which is ``dict[str, Any]`` and therefore unchecked. That is
the *third* time this project has met the same problem: the FLOW lane met it at
normalize -> detect (``flow.py``), the METER lane met it again (``meter.py``), and the
answer both times was to make the untyped hop **exactly one function call wide** — a typed
record with ``to_evidence`` / ``from_evidence``, so neither side ever names a key. This is
that answer a third time, which is now enough repetition to call it the house solution for
structured data crossing an untyped seam.

It lives at the top level, beside ``flow`` and ``meter``, for the same reason they do: it
is shared between two ports and owned by neither. (Contrast ``detect/rules/fields.py``,
which is also a published vocabulary but crosses no port boundary and therefore stays
inside the adapter that owns it.)

Precedence: three tiers, and the middle one is the point (ADR-0032 D5)
----------------------------------------------------------------------
The weeks 3-4 plan proposed two sources — authored enrichment vs a label inferred from the
anomaly's *shape* — with authored winning. Working through the actual detectors showed a
third sitting between them, and it is the one that makes the queue coherent:

1. ``RULE`` — the specific rule that fired says what this is. Most specific, wins.
2. ``DETECTOR`` — the detector's named preset maps to a known threat pattern. "A device
   contacted 41 hosts on the telnet port in one minute" *is* remote system discovery; the
   label is as authored as a rule's, merely authored in code rather than in YAML.
3. ``HEURISTIC`` — inferred from shape, for detectors that genuinely find only "this is
   statistically unusual". **Not implemented yet** (ADR-0032 D6 defers it); the tier exists
   so the ladder is decided rather than discovered later.

``is_heuristic`` is a *product* requirement, not bookkeeping: a security tool that presents
a guess as a diagnosis is worse than one that says nothing, because the first time the guess
is wrong an analyst stops trusting the whole column.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace
from enum import StrEnum
from typing import Any

from iotshield.contracts import DetectionResult

EVIDENCE_KEY = "enrichment"
"""The single ``evidence`` key the whole record lives under.

Namespaced rather than flattened so a detector may put whatever else it likes in evidence
without any chance of colliding with an enrichment field.
"""

_MAX_ACTIONS = 6
"""Cap on the actions promoted onto one alert. A bucket can gather several enriched
detections and each carries up to three or four actions; past half a dozen the field stops
being a next step and becomes a wall of text nobody reads."""


class EnrichmentSource(StrEnum):
    """Who made this claim — which decides who wins when two claims meet.

    Values are wire format: they are serialized into ``evidence`` and reach the API.
    """

    RULE = "rule"
    DETECTOR = "detector"
    HEURISTIC = "heuristic"


_PRECEDENCE: dict[EnrichmentSource, int] = {
    EnrichmentSource.RULE: 0,
    EnrichmentSource.DETECTOR: 1,
    EnrichmentSource.HEURISTIC: 2,
}
"""Written out rather than derived from ``EnrichmentSource``'s member order.

The order is a *product* decision about which claim an analyst should see, and the enum's
order is a wire vocabulary someone may one day reorder for an unrelated reason. Deriving
one from the other would let that edit silently re-rank the queue."""

_MITRE_FIELDS = ("tactic", "technique", "technique_id")

_STEP_BOUNDARY = re.compile(r"\.\s+")


def validate_action_step(step: str) -> str:
    """Refuse a step ``action_text``/``action_steps`` could not round-trip. Returns it.

    The pair is a claim about our own prose, and prose is exactly what a contributor writes:
    the authoring guide invites rules onto the deployment's own shelf, which no test
    of the packaged library can reach. So the invariant is enforced where the text is
    *authored* rather than hoped about downstream, and the message quotes the offender
    because the audience is someone editing YAML with no type checker in front of them.

    Four refusals, each a measured way the round trip loses data (ADR-0033):

    - **empty** — vanishes entirely on the way back.
    - **a newline** — ``\\n`` is whitespace, so a YAML block scalar (``- >`` / ``- |``) would
      split one step into two.
    - **an interior ``". "``** — the separator itself; ``"Check the I.P. address."`` and
      ``"Escalate (e.g. the plant engineer)."`` both come back as two half-steps.
    - **no terminal ``.``/``!``/``?``** — the dangerous one, because it does not split a step,
      it *merges* two: ``("Block the source", "Review the device.")`` returns as the single
      string ``"Block the source Review the device."`` and an entire instruction is lost with
      nothing to indicate it.
    """
    cleaned = step.strip()
    if not cleaned:
        raise ValueError("an action step must not be empty")
    if "\n" in step or "\r" in step:
        raise ValueError(f"an action step must be one line, got {step!r}")
    if _STEP_BOUNDARY.search(cleaned[:-1]):
        raise ValueError(
            f"an action step must not contain '. ' — it is the step separator, so {step!r}"
            " would come back as two steps; rephrase or drop the interior period"
        )
    if cleaned[-1] not in ".!?":
        raise ValueError(
            f"an action step must end with '.', '!' or '?', got {step!r};"
            " without it this step and the next merge into one on the way back"
        )
    return cleaned


def action_steps(text: str | None) -> list[str]:
    """Split a ``recommended_action`` back into the steps ``action_text`` joined.

    The **inverse of** ``Enrichment.action_text``, and written beside it so the pair cannot
    drift. It exists because ``PrioritizedAlert.recommended_action`` is a single ``str``
    while the thing it holds is a *checklist*: up to six steps unioned from every rule that
    fired on the alert (ADR-0032 D5), which an analyst works through one at a time. Rendered
    as one paragraph — which is what both dashboards did — six imperatives from four
    different rules read as an undifferentiated wall, and the field stops being a next step.

    Recovering the list rather than carrying it keeps this a *display* concern: the contract
    is untouched, so no stored alert, API schema or generated client moves. The price is that
    the split is a claim about our own generated text, which is why a test round-trips every
    shipped rule and preset through the pair. That turns a guess into a checked property: an
    action written with a mid-sentence ``". "`` fails the test rather than silently splitting
    into two half-steps in front of an analyst.

    Splits after a period followed by whitespace, so text with no such boundary — the
    generated fallback, ``"Inspect pump-02 around ...; review flow_burst evidence."`` — comes
    back as the single step it already is. A trailing step that did not end in a period keeps
    not ending in one: nothing here invents punctuation.
    """
    if not text:
        return []
    parts = _STEP_BOUNDARY.split(text.strip())
    # The period is the delimiter, so it belongs to the step it terminated — every part but
    # the last had one consumed. The last kept whatever it had.
    steps = [f"{part}." for part in parts[:-1]] + parts[-1:]
    return [stripped for step in steps if (stripped := step.strip())]


class EnrichmentFieldError(ValueError):
    """Evidence that does not carry a well-formed enrichment record."""


@dataclass(frozen=True)
class MitreTechnique:
    """One MITRE ATT&CK mapping, in the structured shape (never a bare technique id).

    Both enterprise ATT&CK and ATT&CK for ICS ids live here; the vocabulary is identical
    and the id itself says which matrix it came from (``T1041`` vs ``T0869``).
    """

    tactic: str
    technique: str
    technique_id: str

    def label(self) -> str:
        """The analyst-facing rendering — what becomes ``PrioritizedAlert.threat_class``."""
        return f"{self.technique} ({self.technique_id})"

    def to_dict(self) -> dict[str, str]:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: Any) -> MitreTechnique:
        if not isinstance(raw, dict):
            raise EnrichmentFieldError(
                f"a mitre entry must be a mapping of tactic/technique/technique_id, "
                f"got {type(raw).__name__}"
            )
        try:
            values = {field: str(raw[field]).strip() for field in _MITRE_FIELDS}
        except KeyError as exc:
            raise EnrichmentFieldError(f"mitre entry is missing {exc.args[0]!r}") from exc
        empty = [field for field, value in values.items() if not value]
        if empty:
            raise EnrichmentFieldError(f"mitre entry has empty {', '.join(sorted(empty))}")
        return cls(**values)


def _list_field(raw: dict[str, Any], key: str) -> list[Any]:
    """Read an optional list out of stored evidence, or raise naming the key.

    ``is None`` rather than a truthiness check: an empty dict is falsy, so ``{"mitre": {}}``
    — a real schema drift — would otherwise read clean as "no techniques".
    """
    value = raw.get(key)
    if value is None:
        return []
    if not isinstance(value, list):
        raise EnrichmentFieldError(f"enrichment {key!r} must be a list, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class Enrichment:
    """An authored claim about what a detection means, and what to do about it.

    ``origin`` is what makes the claim attributable in the alert rationale — ``rule:
    high-upload-volume`` rather than an anonymous label. It is also the answer to a
    constraint the Detector port imposes: ``method`` is a property of the *detector*, and
    the port suite requires every detection to carry it, so a rule detector holding six
    rules cannot name the firing rule in ``method``. It names it here instead, which is the
    field the analyst actually reads (ADR-0032 D3).
    """

    threat_class: str
    source: EnrichmentSource
    origin: str
    mitre: tuple[MitreTechnique, ...] = ()
    suggested_actions: tuple[str, ...] = ()

    @property
    def is_heuristic(self) -> bool:
        return self.source is EnrichmentSource.HEURISTIC

    @property
    def precedence(self) -> int:
        """Lower wins. Exposed so the alert layer sorts by a number, not by an enum name."""
        return _PRECEDENCE[self.source]

    def action_text(self) -> str | None:
        """The actions as one paragraph — ``PrioritizedAlert.recommended_action``'s shape.

        ``action_steps`` (module level) is the inverse, and is what a UI should render:
        the contract carries one string, but the thing is a checklist.
        """
        return " ".join(self.suggested_actions) if self.suggested_actions else None

    def rationale_clause(self) -> str:
        """The sentence appended to an alert's rationale, naming who made the claim.

        Marked ``(inferred)`` for a heuristic source, so a guess is never presented in the
        same voice as a fact.
        """
        marker = " (inferred)" if self.is_heuristic else ""
        return f"Threat class: {self.threat_class}{marker}, per {self.origin}."

    def to_evidence(self) -> dict[str, Any]:
        """Render for ``DetectionResult.evidence[EVIDENCE_KEY]`` (JSON-safe throughout)."""
        return {
            "threat_class": self.threat_class,
            "source": self.source.value,
            "origin": self.origin,
            "is_heuristic": self.is_heuristic,
            "mitre": [technique.to_dict() for technique in self.mitre],
            "suggested_actions": list(self.suggested_actions),
        }

    @classmethod
    def from_evidence(cls, raw: Any) -> Enrichment:
        """Rebuild from stored evidence, raising ``EnrichmentFieldError`` on anything malformed.

        Strict on purpose: this is the one place the untyped hop is crossed, so a drift
        between writer and reader surfaces here with a field name rather than as an alert
        that silently reads ``unknown`` forever — which is indistinguishable from a detector
        that had nothing to say.
        """
        if not isinstance(raw, dict):
            raise EnrichmentFieldError(
                f"enrichment evidence must be a mapping, got {type(raw).__name__}"
            )
        try:
            threat_class = str(raw["threat_class"]).strip()
            source = EnrichmentSource(raw["source"])
            origin = str(raw["origin"]).strip()
        except KeyError as exc:
            raise EnrichmentFieldError(f"enrichment is missing {exc.args[0]!r}") from exc
        except ValueError as exc:  # an unknown EnrichmentSource value
            raise EnrichmentFieldError(f"enrichment has an unreadable source: {exc}") from exc
        if not threat_class:
            raise EnrichmentFieldError("enrichment has an empty 'threat_class'")
        if not origin:
            raise EnrichmentFieldError("enrichment has an empty 'origin'")

        return cls(
            threat_class=threat_class,
            source=source,
            origin=origin,
            mitre=tuple(MitreTechnique.from_dict(e) for e in _list_field(raw, "mitre")),
            suggested_actions=tuple(
                str(action) for action in _list_field(raw, "suggested_actions")
            ),
        )

    @classmethod
    def from_detection(cls, detection: DetectionResult) -> Enrichment | None:
        """The alert layer's read path: the record, or None if this detection carries none.

        ``None`` rather than an exception because an unenriched detection is the normal case
        for four of five shipped detectors — the alert layer's job is to fall back, not to
        fail the run. A *malformed* record is also read as absent here for the same reason,
        which is why ``from_evidence`` exists separately and is what the tests assert on.
        """
        raw = detection.evidence.get(EVIDENCE_KEY)
        if raw is None:
            return None
        try:
            return cls.from_evidence(raw)
        except EnrichmentFieldError:
            return None

    def with_union_of(self, contributing: Iterable[Enrichment]) -> Enrichment:
        """This claim, carrying every contributing enrichment's actions (deduped, capped).

        Unioned rather than taken from the winner alone because an analyst looking at a
        device that shows both failed logins *and* a bulk outbound transfer wants both sets
        of next steps; the combination is more informative than either alone (ADR-0032 D5).
        """
        actions = list(
            dict.fromkeys(
                action for enrichment in contributing for action in enrichment.suggested_actions
            )
        )
        if len(actions) > _MAX_ACTIONS:
            # Say that the checklist was trimmed, rather than trimming it in silence. The cap
            # is a real product decision, but an analyst cannot tell a complete checklist from
            # a truncated one, and on a camera alert unioned across four rules six of twelve steps
            # were dropped — including the whole exfiltration checklist for a device that had
            # just uploaded 4.2 MB. Carried as a real step so `action_steps` recovers it and
            # both dashboards render it with no UI change (ADR-0033).
            omitted = len(actions) - (_MAX_ACTIONS - 1)
            actions = [
                *actions[: _MAX_ACTIONS - 1],
                f"{omitted} further step(s) from other findings on this alert were omitted;"
                " see the detections.",
            ]
        return replace(self, suggested_actions=tuple(actions))
