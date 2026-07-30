"""The rule language: what a rule file may say, and what it compiles to (ADR-0032 D2).

The grammar is a faithful port of the upstream Sigma-style DSL
(``AntoniaVoutouri/iot-ot-alert-correlation``, ``src/rule_matcher.py``) — a rule holds named
*selections*, each a set of field/operator/value triples ANDed together, combined by a flat
``condition``. The grammar is the part worth keeping: it is small, it is what its author is
fluent in, and it is the interface a future contributor learns. Only the *vocabulary* moved
(``fields.py``), because her field names describe her canonical event model and ours
describes ours.

Four behaviours are deliberately stricter than upstream, each fixing a failure we would
otherwise inherit:

1. **A missing value never matches, whatever the operator.** Upstream, ``!=`` against an
   absent field returns True, so ``flow.service != "smb"`` fires on every event that has no
   service at all — an absence being read as a positive claim. Every operator here reads
   absence as "cannot say", which is what a SIEM predicate should mean.
2. **Type mismatches are refused at load, not raised mid-run.** Upstream, ``>`` between a
   service name and a number raises an uncaught ``TypeError`` and takes the run down. Here
   the field's kind is known, so the rule never loads.
3. **A condition naming a selection that does not exist is refused at load.** Upstream it
   raises ``KeyError`` on the first event, i.e. in production.
4. **Rule files load in sorted order**, because "same input, same output" is a port law and
   an unsorted directory listing is not a guarantee of anything.

No regular-expression operator, deliberately. ``contains`` covers every signature we
actually need (the OpenSSH break-in warning is a substring), and ADR-0020 D3 already
established the reason to stop there: an operator-authored regex in YAML is a config typo
away from being pathologically slow or wildly over-matching, with nothing between it and
the run.
"""

from __future__ import annotations

import operator
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from iotshield.contracts import Lane
from iotshield.detect.rules.fields import (
    EventView,
    FieldKind,
    FieldSpec,
    coerce_value,
    read_comparable,
    resolve,
)
from iotshield.enrichment import (
    Enrichment,
    EnrichmentSource,
    MitreTechnique,
    validate_action_step,
)


class Operator(StrEnum):
    """The comparison vocabulary — exactly the upstream engine's seven, no more."""

    EQ = "=="
    NE = "!="
    GT = ">"
    LT = "<"
    GE = ">="
    LE = "<="
    CONTAINS = "contains"


_ORDERED = frozenset({Operator.GT, Operator.LT, Operator.GE, Operator.LE})
_EQUALITY = frozenset({Operator.EQ, Operator.NE})

_ALLOWED_OPERATORS: dict[FieldKind, frozenset[Operator]] = {
    # A number is fully ordered; text is not (">" on a service name means nothing);
    # a tier is ordered but not arithmetic, which is exactly what makes `severity >= error`
    # expressible and `severity + 1` not.
    FieldKind.NUMBER: _EQUALITY | _ORDERED,
    FieldKind.TEXT: _EQUALITY | {Operator.CONTAINS},
    FieldKind.TIER: _EQUALITY | _ORDERED,
}


class SeverityHint(StrEnum):
    """What the rule's author thinks this is worth. A hint: the pipeline computes priority."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ConfidenceTier(StrEnum):
    """How sure the author is that a match means what the rule claims."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


# --- Severity x confidence -> the margin the alert engine reads (ADR-0032 D3) ----------
#
# A rule match is a boolean, but the Detector port requires `score > threshold` and the
# alert engine reads `score / threshold` as its severity factor. So the pair has to be
# synthesised, and the honest synthesis is the author's own two judgments: severity sets
# the base, confidence scales it. The margin then *is* the authored severity, which is
# precisely how the integration letter framed "severity becomes a hint that feeds the
# score".
#
# The table is calibrated against `alert/weighted.py`, whose severity factor is clamped to
# [1.0, 4.0]: the weakest combination sits just above the floor and the strongest exactly at
# the cap, so the scale is used end to end with nothing clipped. Two properties a test pins:
# every product is strictly greater than THRESHOLD (the port's strict `>`), and no product
# exceeds the engine's cap (a rule cannot claim more severity than the scale allows).
THRESHOLD = 1.0
_SEVERITY_SCORE: dict[SeverityHint, float] = {
    SeverityHint.LOW: 1.5,
    SeverityHint.MEDIUM: 2.0,
    SeverityHint.HIGH: 3.0,
    SeverityHint.CRITICAL: 4.0,
}
_CONFIDENCE_FACTOR: dict[ConfidenceTier, float] = {
    ConfidenceTier.LOW: 0.7,
    ConfidenceTier.MEDIUM: 0.85,
    ConfidenceTier.HIGH: 1.0,
}


# One entry per operator, so adding one to the enum without teaching it to compare fails
# loudly at the single lookup below rather than falling through to a wrong answer — which
# is what the previous if-chain's bare final `return` would have done.
_APPLY: dict[Operator, Callable[[Any, Any], bool]] = {
    Operator.EQ: operator.eq,
    Operator.NE: operator.ne,
    Operator.GT: operator.gt,
    Operator.LT: operator.lt,
    Operator.GE: operator.ge,
    Operator.LE: operator.le,
    # Case-insensitive: `contains` is a search, and text arriving from different exporters
    # differs only in casing far more often than it differs in meaning. `==` stays exact,
    # because equality that quietly ignores case is a surprise. The needle is already
    # case-folded at compile time (`Comparison.value`); only the haystack folds per event.
    Operator.CONTAINS: lambda actual, needle: needle in str(actual).lower(),
}


@dataclass(frozen=True)
class Comparison:
    """One field/operator/value triple, compiled against a known field."""

    spec: FieldSpec
    operator: Operator
    value: Any  # already coerced into the field kind's comparable form

    def matches(self, view: EventView) -> bool:
        actual = read_comparable(self.spec, view)
        if actual is None:
            # Absence is not a claim. See the module docstring, point 1.
            return False
        return bool(_APPLY[self.operator](actual, self.value))


@dataclass(frozen=True)
class Selection:
    """A named group of comparisons, ALL of which must hold (the upstream semantics)."""

    name: str
    comparisons: tuple[Comparison, ...]

    def matches(self, view: EventView) -> bool:
        return all(comparison.matches(view) for comparison in self.comparisons)


class Combine(StrEnum):
    ALL = "AND"
    ANY = "OR"


@dataclass(frozen=True)
class Rule:
    """A compiled rule: a predicate over one event, plus everything the finding needs."""

    id: str
    title: str
    lane: Lane
    description: str
    explanation: str
    severity_hint: SeverityHint
    confidence: ConfidenceTier
    selections: tuple[Selection, ...]
    combine: Combine
    enrichment: Enrichment
    source: str  # provenance: which file this came from, for error messages

    def matches(self, view: EventView) -> bool:
        predicate = all if self.combine is Combine.ALL else any
        return predicate(selection.matches(view) for selection in self.selections)

    @property
    def score(self) -> float:
        return _SEVERITY_SCORE[self.severity_hint] * _CONFIDENCE_FACTOR[self.confidence]

    @property
    def threshold(self) -> float:
        return THRESHOLD


_UPSTREAM_CLAIMS: dict[str, tuple[str, str]] = {
    "signal_basis": (
        "observable",
        "only 'observable' rules may be loaded. A label-based rule matches a dataset's "
        "answer key, which does not exist on live data, so it can never fire in production.",
    ),
    "rule_type": (
        "single_event",
        "this detector evaluates single-event signatures only. Count-over-time patterns are "
        "the burst detectors' job (ADR-0020, ADR-0024) and multi-step sequences are not "
        "implemented.",
    ),
    "integration_status": ("production", "only 'production' rules may be loaded."),
}
"""Upstream metadata keys that make a claim: the one value each may hold, and why."""


class RuleFile(BaseModel):
    """The on-disk shape of a rule file, validated before anything is compiled.

    ``extra="forbid"`` like every contract in this repo, so a typo in a key name fails
    loudly instead of being silently ignored — which for a *detection* rule means silently
    not detecting.

    Several keys exist only because upstream rule files carry them. They are accepted rather
    than forbidden, so a rule can move between the two repos with only its field addresses
    changed; where they encode a claim we care about, the accepted values are constrained,
    which turns each one into a safety gate (a label-based demo rule cannot be dropped into
    the production library by accident).
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1, description="Stable identifier; appears in the alert rationale")
    title: str = Field(min_length=1)
    lane: Lane
    description: str = Field(min_length=1)
    severity_hint: SeverityHint
    confidence: ConfidenceTier
    detection: dict[str, Any]
    mitre: list[dict[str, Any]] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)

    @field_validator("suggested_actions")
    @classmethod
    def _steps_survive_the_round_trip(cls, value: list[str]) -> list[str]:
        """Refuse at LOAD what ``action_text``/``action_steps`` could not carry (ADR-0033).

        The pair is documented as each other's inverse, but the only test of that iterates
        the packaged library — and this file's own guide tells contributors to drop rules
        into a rule set on the deployment's shelf. Checking here covers both, and turns a
        silent loss (a step that merges into its neighbour and disappears from the alert)
        into a load error naming the file.
        """
        return [validate_action_step(step) for step in value]

    one_line: str | None = Field(
        default=None, description="The analyst-facing headline; falls back to description"
    )
    references: list[str] = Field(default_factory=list)

    # --- Accepted from upstream files, constrained where they make a claim. ------------
    signal_basis: str | None = None
    rule_type: str | None = None
    integration_status: str | None = None
    severity: str | None = Field(
        default=None, description="Legacy alias for severity_hint upstream; ignored here"
    )
    correlation: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Upstream grouping key. Inert here: our normalizers decide which asset an event "
            "belongs to at ingest (flow orientation, ADR-0023 D2), so a rule does not choose."
        ),
    )

    @field_validator("signal_basis", "rule_type", "integration_status")
    @classmethod
    def _upstream_claim_is_accepted(cls, value: str | None, info: ValidationInfo) -> str | None:
        """Each of these keys makes a claim, so only one value of it may be loaded.

        That is what turns "accepted from upstream" into a safety gate rather than mere
        tolerance: a label-based demo rule, a correlation rule, or a rule its author has not
        promoted cannot reach the production library by being copied into the folder.
        """
        # `.get` on a possibly-None field name rather than an index: pydantic types
        # `field_name` as optional, and this validator is only ever registered on the three
        # keys below, so an absent claim is simply nothing to check.
        claim = _UPSTREAM_CLAIMS.get(info.field_name or "")
        if value is None or claim is None:
            return value
        allowed, reason = claim
        if value != allowed:
            raise ValueError(f"{info.field_name} is {value!r}; {reason}")
        return value


def _parse_condition(condition: Any, names: Mapping[str, Any]) -> Combine:
    """Read the flat ``condition`` grammar: one name, or ``A OR B``, or ``A AND B``.

    Flat on purpose — no parentheses, no NOT, no precedence — because that is the upstream
    grammar and because every extension is a new way for a YAML file to be subtly wrong. If
    a rule needs more, it is two rules.

    Returns only the combiner: the two checks below leave ``referenced`` and ``names`` equal
    as sets, and ``all``/``any`` do not care about order, so the caller compiles the
    selections straight from the file.
    """
    if not isinstance(condition, str) or not condition.strip():
        raise ValueError("detection.condition must be a non-empty string")
    text = condition.strip()
    has_or = " OR " in text
    has_and = " AND " in text
    if has_or and has_and:
        raise ValueError(
            f"condition {text!r} mixes AND and OR; the grammar is flat, so use one or the "
            "other (split the rule if you need both)"
        )
    if has_or:
        combine, parts = Combine.ANY, text.split(" OR ")
    elif has_and:
        combine, parts = Combine.ALL, text.split(" AND ")
    else:
        combine, parts = Combine.ALL, [text]

    referenced = tuple(part.strip() for part in parts)
    if any(not name for name in referenced):
        raise ValueError(f"condition {text!r} has an empty operand")
    unknown = [name for name in referenced if name not in names]
    if unknown:
        known = ", ".join(sorted(names)) or "(none)"
        raise ValueError(
            f"condition {text!r} names selection(s) {', '.join(repr(n) for n in unknown)} "
            f"that this rule does not define. Defined selections: {known}"
        )
    # A selection nobody references is dead code, and dead code in a detection rule means
    # the author believes something is being checked that is not — the same silent narrowing
    # a mistyped field would cause, arrived at from the other direction. Upstream evaluates
    # the condition and ignores the rest; we refuse, because "I forgot the OR" is a far more
    # likely explanation than "I meant to leave that there".
    unreferenced = [name for name in names if name not in referenced]
    if unreferenced:
        raise ValueError(
            f"selection(s) {', '.join(repr(n) for n in sorted(unreferenced))} are defined but "
            f"never named by condition {text!r}, so they would never be evaluated. Reference "
            f"them in the condition or remove them."
        )
    return combine


def _compile_selection(lane: Lane, name: str, body: Any) -> Selection:
    if not isinstance(body, dict) or not body:
        raise ValueError(f"selection {name!r} must be a non-empty mapping of field -> condition")
    comparisons: list[Comparison] = []
    for field_name, raw in body.items():
        if not isinstance(raw, dict):
            raise ValueError(
                f"selection {name!r} field {field_name!r} must be a mapping with "
                f"'operator' and 'value', got {type(raw).__name__}"
            )
        unexpected = set(raw) - {"operator", "value"}
        if unexpected:
            raise ValueError(
                f"selection {name!r} field {field_name!r} has unexpected key(s): "
                f"{', '.join(sorted(unexpected))}"
            )
        if "operator" not in raw or "value" not in raw:
            raise ValueError(
                f"selection {name!r} field {field_name!r} needs both 'operator' and 'value'"
            )
        spec = resolve(lane, str(field_name))
        try:
            op = Operator(str(raw["operator"]))
        except ValueError:
            known = ", ".join(known_op.value for known_op in Operator)
            raise ValueError(
                f"selection {name!r} field {field_name!r} uses unknown operator "
                f"{raw['operator']!r}; known operators: {known}"
            ) from None
        if op not in _ALLOWED_OPERATORS[spec.kind]:
            allowed = ", ".join(sorted(a.value for a in _ALLOWED_OPERATORS[spec.kind]))
            raise ValueError(
                f"field {spec.name!r} holds a {spec.kind.value}, so operator "
                f"{op.value!r} does not apply to it; allowed: {allowed}"
            )
        value = coerce_value(spec, raw["value"])
        if op is Operator.CONTAINS:
            # Fold the needle once here rather than on every event; only the haystack
            # (the event's own text) has to be folded per comparison.
            value = str(value).lower()
        comparisons.append(Comparison(spec=spec, operator=op, value=value))
    return Selection(name=name, comparisons=tuple(comparisons))


def _build_enrichment(parsed: RuleFile) -> Enrichment:
    """The authored claim this rule carries onto every finding it produces.

    ``threat_class`` is the first MITRE technique's label when there is one, and the rule's
    title otherwise. ``mitre`` is optional because not every worthwhile signature is an
    attack — "a critical log line was written" is a real operational signal with no
    technique — and a title is a far more honest classification than ``unknown``.
    """
    techniques = tuple(MitreTechnique.from_dict(entry) for entry in parsed.mitre)
    threat_class = techniques[0].label() if techniques else parsed.title
    return Enrichment(
        threat_class=threat_class,
        source=EnrichmentSource.RULE,
        origin=f"rule:{parsed.id}",
        mitre=techniques,
        suggested_actions=tuple(parsed.suggested_actions),
    )


def compile_rule(raw: Mapping[str, Any], *, source: str) -> Rule:
    """Validate and compile one rule file's contents, naming ``source`` on any failure."""
    try:
        parsed = RuleFile.model_validate(dict(raw))
        detection = parsed.detection
        if "condition" not in detection:
            raise ValueError("detection is missing 'condition'")
        bodies = {name: body for name, body in detection.items() if name != "condition"}
        if not bodies:
            raise ValueError("detection defines no selections")
        combine = _parse_condition(detection["condition"], bodies)
        selections = tuple(
            _compile_selection(parsed.lane, name, body) for name, body in bodies.items()
        )
    except Exception as exc:  # pydantic ValidationError or any ValueError above
        raise ValueError(f"invalid rule in {source}: {exc}") from exc

    return Rule(
        id=parsed.id,
        title=parsed.title,
        lane=parsed.lane,
        description=parsed.description,
        explanation=parsed.one_line or parsed.description,
        severity_hint=parsed.severity_hint,
        confidence=parsed.confidence,
        selections=selections,
        combine=combine,
        enrichment=_build_enrichment(parsed),
        source=source,
    )
