"""Detector port: normalized events in, positive judgments out.

One detector per modality lane. The pipeline routes events by lane, but
implementations must be defensive: events from a foreign lane are ignored,
never judged. Batch semantics (sequences in, lists out) by ADR-0004 — a
streaming variant would be an additional port, not a change to this one.

Semantics every implementation must honor (enforced by the suite in
tests/ports/test_detector_port.py):
1. Emitted detections carry this detector's ``lane`` and ``method``.
2. Detection windows lie within the time range of the input events.
3. ``score > threshold`` for every emitted detection ("no anomaly" is a
   successful judgment with zero detections, not a failure).
4. Same input, same output (deterministic apart from generated ids).

Forming judgments (guidance, not an enforced law — ADR-0020, ADR-0021)
----------------------------------------------------------------------
A ``DetectionResult`` is *one judgment over a window*, not a row label — the contract has
no row-label shape, so a detector that scores rows must decide how flagged rows become
windows. Prefer **one detection per distinct judgment**: merge consecutive flagged rows
into a span rather than emitting one detection per row (``detect/_runs.py: merge_runs``
does this, and both shipped row-scoring adapters use it).

The reason is downstream and worth knowing before you choose. The alert engine's
``volume`` factor counts detections in a bucket as *corroborating evidence*, which is only
meaningful if each one is a separate judgment. Restating a single finding as many
detections reports the same evidence twice — once through ``score``, again through the
count — and measurably inflates the resulting alert's priority (ADR-0020 D4).

This is guidance rather than a suite-enforced law because "distinct" has no general test:
a multi-metric detector may legitimately emit overlapping detections for genuinely
different channels. The evaluation harness *measures* it (detections-per-incident) so the
cost is visible; nothing rejects an adapter for it.
"""

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from iotshield.contracts import DetectionResult, Lane, NormalizedEvent


class Detector(Protocol):
    @property
    def lane(self) -> Lane:
        """The modality this detector judges; the pipeline routes events by it."""
        ...

    @property
    def method(self) -> str:
        """Name of the judging method, stamped on every emitted detection, e.g. "lstm_ae".

        It names *what judged*, not how strictly: a param that selects which rule or model
        decides belongs here (and may render as ``<adapter>:<variant>``, e.g.
        ``log_burst:auth_failure``), while a param that only tunes the same rule's
        sensitivity (a threshold, a window) does not. ``method`` is the only detection field
        that reaches the alert rationale and the triage views, so two instances of one
        adapter that judge differently must not share a name.
        """
        ...

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        """Judge a batch of events; return only positive detections."""
        ...


@runtime_checkable
class Fittable(Protocol):
    """OPTIONAL capability: a detector that can learn its baseline from normal data.

    **The pipeline never calls this.** Fitting is an offline/dev-time concern; ``compute_run``
    builds detectors from config and judges with them as-is. It exists so an *evaluation* can
    follow the realistic deployment protocol — learn "normal" from an asset's own clean
    history, then judge that asset — instead of measuring domain shift and misreporting it as
    detector quality (ADR-0021 D3).

    Implementing it is opt-in and costs a detector nothing else: a rule-based detector with no
    baseline to learn (``stub_threshold``, ``log_burst``) simply does not implement it and
    remains a first-class ``Detector``. The harness checks with ``isinstance(d, Fittable)`` and
    falls back to evaluating as-configured, reporting that it did so — never failing the run.

    ``fit`` **returns a new detector rather than mutating self**, so the port's determinism law
    survives (no hidden fitted state), and so one evaluation case's fit can never leak into
    another's.

    Hyperparameters vs fitted state (LAW — ADR-0022)
    ------------------------------------------------
    Three rules, binding on every adapter that learns anything. They exist because a
    serialized model carries a human's choices and a fitting procedure's output in one
    file, and mixing them makes ``fit()`` inherit stale settings while config can never
    reach them (``detect/_fitted.py`` has the full argument):

    1. **Hyperparameters are exactly the adapter's construction parameters** — declared as
       a frozen pydantic model on its ``AdapterSpec.params_type``, so config sets them,
       ``/modules`` reports them, and the evaluation harness can sweep them.
    2. **``fit(events)`` recomputes learned parameters and learned schema from ``events``,
       using the hyperparameters the instance was CONSTRUCTED with.** It must never read a
       hyperparameter out of its own previous fitted state.
    3. **A persisted artifact is an atomic cached fit**, not a source of configuration.
       It records its hyperparameters, but an explicit override that contradicts them is a
       construction error (``_fitted.resolve_params``) — its weights are aligned to the
       features those hyperparameters produced.

    The suite enforces rule 2 behaviourally: build twice differing in one hyperparameter,
    fit both on identical data, and the results must differ. An adapter that read its
    hyperparameters from a stale artifact would produce two identical detectors.

    An adapter constructed from hyperparameters alone has nothing to judge with. It
    reports ``is_fitted = False`` (see ``_fitted.is_fitted``; absent means fitted) and
    ``detect()`` raises ``NotFitted`` rather than returning an empty list, which would be
    a *successful* "no anomalies" verdict — the dangerous wrong answer.
    """

    def fit(self, events: Sequence[NormalizedEvent]) -> Detector: ...
