"""Hyperparameter sweeps: one spec in, N labelled specs out (ADR-0022).

The second half of "choose your own hyperparameters". ADR-0022 makes every knob reachable
from a spec's ``params``; this makes *varying* one a single flag instead of N hand-written
YAML files. It is deliberately nothing more than spec-rewriting: a sweep produces the same
``list[tuple[label, PipelineSpec]]`` that ``evaluate`` already takes, so the harness, the
report, the console table and the dashboard all render a sweep with no knowledge that one
happened. Four ``window_size`` values are four ordinary rows in the head-to-head table.

It lives here rather than in the CLI because the dashboard needs it too, and a sweep that
existed only for one caller would be the kind of research affordance nobody uses.
"""

import itertools
import math
from collections.abc import Mapping, Sequence
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


def _render(assignment: Mapping[str, Any]) -> str:
    return ",".join(f"{name}={value}" for name, value in assignment.items())


def _applied(spec: PipelineSpec, assignment: Mapping[str, Any]) -> PipelineSpec:
    """A copy of ``spec`` with the assignment merged into every detector that accepts it.

    Per-detector rather than positional: a sweep names a *hyperparameter*, and which
    detectors have one is the registry's answer, not the caller's. Detectors that do not
    declare it are left exactly as configured, so sweeping ``window_size`` across a spec
    holding both team3 and the stub tunes the one and leaves the other as the control.
    """
    detectors = [
        entry.model_copy(
            update={
                "params": {
                    **entry.params,
                    **{
                        name: value
                        for name, value in assignment.items()
                        if name in detector_param_names(entry.name)
                    },
                }
            }
        )
        for entry in spec.detectors
    ]
    return spec.model_copy(update={"detectors": detectors})


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
    for name, values in sweep.items():
        if name not in known:
            raise ValueError(
                f"no detector accepts a {name!r} parameter; the specs declare: "
                f"{', '.join(sorted(known))}"
            )
        if not values:
            raise ValueError(f"sweep {name!r} has no values")

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
    tunable = _tunables(spec)
    relevant = {name: values for name, values in sweep.items() if name in tunable}
    if not relevant:
        return [(label, spec)]

    names = list(relevant)
    expanded = []
    for combination in itertools.product(*(relevant[name] for name in names)):
        assignment = dict(zip(names, combination, strict=True))
        expanded.append((f"{label}[{_render(assignment)}]", _applied(spec, assignment)))
    return expanded
