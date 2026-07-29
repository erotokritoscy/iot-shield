"""Hyperparameter sweeps: one spec in, N labelled specs out (ADR-0022).

The second half of "choose your own hyperparameters". ADR-0022 makes every knob reachable
from a spec's ``params``; this makes *varying* one a single flag instead of N hand-written
YAML files. It is deliberately nothing more than spec-rewriting: a sweep produces the same
``list[tuple[label, PipelineSpec]]`` that ``evaluate`` already takes, so the harness, the
report, the console table and the dashboard all render a sweep with no knowledge that one
happened. Four ``window_size`` values are four ordinary rows in the head-to-head table.

It lives here rather than in the CLI because the dashboard needs it too, and a sweep that
existed only for one caller would be the kind of research affordance nobody uses.

A swept key is ``[<detector>.]<param>``::

    window_size                 every detector in the spec that declares it
    team3_zscore.window_size    exactly that detector

The qualified form exists because a parameter name is only unique *inside* a detector, which
stopped being a corner case once one spec could hold several: ``threshold`` is 80.0 on the
telemetry lane and 20 distinct peers on the flow lane. Qualifying resolves that rather than
routing around it, so every knob of every detector in a multi-detector spec is reachable. The
unqualified form is unchanged, and the grammar is confined to this module — the key travels as
a plain string through the CLI flag, the API request and the dashboard alike.
"""

import itertools
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from iotshield.pipeline import PipelineSpec
from iotshield.pipeline.registry import detector_param_names

MAX_EXPANSION = 24
"""Most specs one sweep may expand into.

A cap rather than a warning because the caller is often a button: a UI that let a stray
comma launch three hundred benchmark runs would be a denial-of-service on its own server,
and the honest cross-product size is something a researcher should see before it runs.
"""


def _scalar(text: str) -> Any:
    """Read one swept value as the narrowest type it fits: bool, int, float, then string."""
    lowered = text.strip().lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    for cast in (int, float):
        try:
            return cast(text)
        except ValueError:
            continue
    return text.strip()


def values(text: str) -> list[Any]:
    """Parse a comma-separated sweep list: ``"20, 60,120"`` -> ``[20, 60, 120]``.

    Shared by the CLI's ``--sweep`` and the dashboard's values box so the two cannot
    disagree about whitespace, empty items, or types. Typing each value is what makes a
    label read ``window_size=20`` rather than ``window_size='20'``, and what lets a bool
    actually reach a bool parameter — the adapters coerce anyway.
    """
    return [_scalar(part) for part in text.split(",") if part.strip()]


QUALIFIER = "."
"""Separates a detector name from a parameter name in a sweep key.

Safe as a separator because no registered adapter name and no hyperparameter name contains a
dot — they are Python identifiers.
"""


@dataclass(frozen=True)
class _Knob:
    """What one swept key addresses: a parameter, optionally scoped to one detector.

    Qualification exists because a parameter name is only unique *within* a detector.
    ``threshold`` means 80.0 on the telemetry lane and 20 distinct peers on the flow lane, so
    an unqualified sweep of it across a spec holding both would vary two incomparable knobs
    under a single label reporting one change — refused (see ``_check``). Naming the detector
    resolves that instead of routing around it, which is what makes every knob of every
    detector reachable in a multi-detector spec.

    ``detector is None`` is the unqualified form and keeps its original meaning: apply to
    every detector in the spec that declares the parameter. That is the right default when
    only one does, so nothing that worked before has to change.
    """

    detector: str | None
    param: str

    @classmethod
    def parse(cls, key: str) -> "_Knob":
        detector, separator, param = key.partition(QUALIFIER)
        return cls(detector, param) if separator else cls(None, detector)

    def matches(self, detector_name: str) -> bool:
        """Whether this knob addresses the named detector."""
        if self.detector is not None:
            return self.detector == detector_name
        return self.param in detector_param_names(detector_name)

    def render(self) -> str:
        """Back to the key as written — labels quote it verbatim, so a swept row says which
        detector moved rather than only which parameter did."""
        if self.detector is None:
            return self.param
        return f"{self.detector}{QUALIFIER}{self.param}"


def _render(assignment: Mapping[_Knob, Any]) -> str:
    return ",".join(f"{knob.render()}={value}" for knob, value in assignment.items())


def _applied(spec: PipelineSpec, assignment: Mapping[_Knob, Any]) -> PipelineSpec:
    """A copy of ``spec`` with each assignment merged into the detectors its knob addresses.

    Per-detector rather than positional: a sweep names a *hyperparameter*, and which
    detectors have one is the registry's answer, not the caller's. Detectors a knob does not
    address are left exactly as configured, so sweeping ``window_size`` across a spec holding
    both team3 and the stub tunes the one and leaves the other as the control.
    """
    detectors = [
        entry.model_copy(
            update={
                "params": {
                    **entry.params,
                    **{
                        knob.param: value
                        for knob, value in assignment.items()
                        if knob.matches(entry.name)
                        and knob.param in detector_param_names(entry.name)
                    },
                }
            }
        )
        for entry in spec.detectors
    ]
    return spec.model_copy(update={"detectors": detectors})


def _accepts(spec: PipelineSpec, knob: _Knob) -> bool:
    """Whether this spec has a detector the knob can actually set.

    A qualified knob needs that detector *present* and declaring the parameter; an
    unqualified one needs any detector declaring it.
    """
    return any(
        knob.matches(entry.name) and knob.param in detector_param_names(entry.name)
        for entry in spec.detectors
    )


def _tunables(spec: PipelineSpec) -> frozenset[str]:
    return frozenset(
        param for entry in spec.detectors for param in detector_param_names(entry.name)
    )


def _check(specs: Sequence[PipelineSpec], sweep: Mapping[str, Sequence[Any]]) -> None:
    """Refuse an incoherent sweep before anything runs, against ALL the specs at once.

    Validated over the union rather than per spec, because sweeping one detector's
    hyperparameter while a rule-based baseline rides along as the control is the normal
    shape of an experiment — ``--spec team3 --spec stub --sweep window_size=…`` must not
    fail merely because the stub has no window. A name *no* spec accepts is still a typo,
    and is named against the real alternatives.
    """
    known = frozenset().union(*(_tunables(spec) for spec in specs)) if specs else frozenset()
    present = {entry.name for spec in specs for entry in spec.detectors}
    for key, chosen in sweep.items():
        knob = _Knob.parse(key)
        if knob.detector is not None and knob.detector not in present:
            raise ValueError(
                f"sweep {key!r} names detector {knob.detector!r}, which is not in any spec; "
                f"they hold: {', '.join(sorted(present))}"
            )
        if knob.detector is not None and knob.param not in detector_param_names(knob.detector):
            raise ValueError(
                f"detector {knob.detector!r} does not accept a {knob.param!r} parameter; "
                f"it declares: {', '.join(sorted(detector_param_names(knob.detector)))}"
            )
        if knob.detector is None and knob.param not in known:
            raise ValueError(
                f"no detector accepts a {knob.param!r} parameter; the specs declare: "
                f"{', '.join(sorted(known))}"
            )
        if not chosen:
            raise ValueError(f"sweep {key!r} has no values")

        for spec in specs:
            accepting = [
                entry.name
                for entry in spec.detectors
                if knob.matches(entry.name) and knob.param in detector_param_names(entry.name)
            ]
            if len(accepting) <= 1:
                continue
            # An unqualified name several detectors in ONE spec accept would be applied to all
            # of them under a single label reporting one change. `threshold` means 80.0 on the
            # telemetry lane and 20 peers on the flow lane, so that label would be a lie.
            # Refusing is right, but the remedy is now nameable: qualify it.
            if knob.detector is None:
                alternatives = ", ".join(f"{name}{QUALIFIER}{knob.param}" for name in accepting)
                raise ValueError(
                    f"sweep {key!r} is accepted by more than one detector in a spec "
                    f"({', '.join(accepting)}); it would vary all of them at once under one "
                    f"label. Qualify it to vary one: {alternatives}"
                )
            # Qualified and *still* ambiguous: the same adapter appears twice in one spec,
            # which is legal and deliberate (two presets in one run, ADR-0023 D9). A name
            # cannot separate two instances of one adapter, so refuse rather than vary both.
            raise ValueError(
                f"sweep {key!r} is ambiguous: {knob.detector!r} appears "
                f"{len(accepting)} times in a spec. Put the instances in separate specs so "
                f"each carries its own sweep."
            )

    total = math.prod(len(values) for values in sweep.values())
    if total > MAX_EXPANSION:
        raise ValueError(
            f"this sweep expands to {total} runs per spec (limit {MAX_EXPANSION}); "
            "narrow the value lists"
        )


def expand(
    specs: Sequence[tuple[str, PipelineSpec]], sweep: Mapping[str, Sequence[Any]]
) -> list[tuple[str, PipelineSpec]]:
    """Cross-product each spec over the swept values, labelling every combination.

    Labels read ``team3[window_size=20,percentile=99]`` — the identity of the run, carried
    into the report so a table of six near-identical rows is legible. A spec none of whose
    detectors accept any swept name comes back unchanged and unlabelled: it is the control
    row, and it appears once rather than N identical times. An empty sweep returns the
    specs untouched, so callers need no special case.
    """
    if sweep:
        _check([spec for _, spec in specs], sweep)
    return [pair for label, spec in specs for pair in _one(label, spec, sweep)]


def _one(
    label: str, spec: PipelineSpec, sweep: Mapping[str, Sequence[Any]]
) -> list[tuple[str, PipelineSpec]]:
    relevant = {
        knob: chosen
        for knob, chosen in ((_Knob.parse(key), chosen) for key, chosen in sweep.items())
        if _accepts(spec, knob)
    }
    if not relevant:
        return [(label, spec)]

    knobs = list(relevant)
    expanded = []
    for combination in itertools.product(*(relevant[knob] for knob in knobs)):
        assignment = dict(zip(knobs, combination, strict=True))
        expanded.append((f"{label}[{_render(assignment)}]", _applied(spec, assignment)))
    return expanded
