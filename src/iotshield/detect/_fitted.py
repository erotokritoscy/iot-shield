"""Hyperparameters vs fitted state: the rule every learning adapter follows (ADR-0022).

A trained model on disk carries two different kinds of thing in one file — the knobs a
human chose (window size, tree depth, a percentile) and the numbers the fitting procedure
computed (a baseline, a booster, a threshold). Every serialization format encourages
mixing them: a pickled estimator, a torch checkpoint, and our own JSON artifact all do.
Mixed, ``fit()`` silently inherits stale hyperparameters from whatever file was loaded,
and config can never reach them.

So the split is made explicit, once, here:

- **Hyperparameters** are exactly the adapter's *construction* parameters — a frozen
  pydantic model, declared on its ``AdapterSpec`` as ``params_type``, so ``/modules`` can
  report them and a config or a benchmark can set them.
- **Fitted state** is whatever ``fit()`` computes: learned parameters *and* the schema
  learned from the training data's shape (which channels, which feature columns).
- A **cached fit** (an "artifact") is the two together, and is therefore **atomic**.
  Loading one supplies both; overriding one of its hyperparameters while keeping its
  weights is refused, because the weights are aligned to the features the *original*
  hyperparameters produced. Scoring one feature set against another's baseline yields
  numbers, all of them meaningless — the failure mode this module exists to prevent.

The consequence a caller sees: ``build_detector(name, params)`` is fully determined by
``params``, and ``fit()`` by construction plus data. That is what makes a hyperparameter
sweep in the evaluation harness mean what it says.
"""

import textwrap
from collections.abc import Mapping
from typing import Any, Protocol, TypeVar, runtime_checkable

import yaml
from pydantic import BaseModel

P = TypeVar("P", bound=BaseModel)


class NotFitted(ValueError):
    """``detect()`` on a detector built from hyperparameters alone.

    Raised rather than returning ``[]`` deliberately: an empty list is a *successful*
    judgment meaning "no anomalies here", which is the most dangerous wrong answer a
    detector can give. A run configured with an untrained model is refused before ingest
    (``pipeline.runner``), and a benchmark reports it as a note (``eval.fit``).
    """


@runtime_checkable
class Stateful(Protocol):
    """OPTIONAL: an adapter that can exist in an unfitted state and says which it is in.

    A separate one-member protocol rather than a new member on ``Fittable``, deliberately.
    ``Fittable`` is ``runtime_checkable`` and matches on attribute *presence*, so adding
    ``is_fitted`` to it would silently reclassify an adapter implementing only ``fit`` as
    "not fittable" — a wrong answer delivered quietly. Splitting it keeps ``Fittable``'s
    meaning intact while still *declaring* the member, so it is type-checked rather than
    discovered by ``getattr`` (golden rule 2: adapters declare their shape through ports).
    """

    @property
    def is_fitted(self) -> bool: ...


def is_fitted(detector: object) -> bool:
    """Whether a detector is ready to judge. **Not declaring it means fitted.**

    The default is the right one: a rule-based detector has no state to learn and is
    always ready, so only adapters that can be constructed *without* a cached fit need to
    say anything.
    """
    return detector.is_fitted if isinstance(detector, Stateful) else True


def _yaml_block(params: BaseModel) -> str:
    """The params as a copy-pasteable YAML mapping, for the error below.

    Turning the refusal into two seconds of editing rather than a hunt through a JSON
    artifact is most of what makes the atomicity rule tolerable in practice — so it goes
    through the same YAML writer the configs are read with, rather than a hand-rolled
    emitter that would quietly produce invalid YAML for the first value needing quotes.
    ``mode="json"`` turns tuples into lists (safe_dump refuses tuples) and
    ``default_flow_style=None`` keeps short sequences inline.
    """
    dumped = yaml.safe_dump(
        params.model_dump(mode="json"), sort_keys=False, default_flow_style=None
    )
    return textwrap.indent(dumped.rstrip(), " " * 6)


def resolve_params(params_type: type[P], overrides: Mapping[str, Any], cached: P | None) -> P:
    """Settle an adapter's hyperparameters: from a cached fit, or from config, never half.

    ``overrides`` is the factory's keyword arguments, where ``None`` means "not given" —
    which is why every hyperparameter in a registry factory's signature defaults to
    ``None`` and takes its real default from ``params_type``.
    """
    given = {name: value for name, value in overrides.items() if value is not None}
    if cached is None:
        return params_type(**given)
    if given:
        # ASCII-only: this reaches a console, and Windows terminals mangle UTF-8
        # punctuation (the same reason RunReport and the eval report avoid it).
        raise ValueError(
            f"cannot override {sorted(given)} while loading a trained artifact - its "
            "weights are aligned to the features its own hyperparameters produced. "
            "It was fitted with:\n"
            f"{_yaml_block(cached)}\n"
            "    Drop the artifact to fit from scratch (and pick a fit protocol), or "
            "drop the overrides."
        )
    return cached
