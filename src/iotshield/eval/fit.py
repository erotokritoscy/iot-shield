"""Fit protocols: what "normal" a detector learns from before it is judged.

A detector that learns a baseline (team3's robust z-score) is only as good as the normal
data it learned from, and ADR-0012 D3 measured that a baseline from a *foreign* recording
fails outright — the threshold becomes unreachable. So benchmarking a shipped artifact
across a corpus of other recordings largely measures **domain shift**, and reporting that as
the method's score would be a misleading number.

Hence the protocol is an explicit, reportable choice rather than something implied by how
the harness happens to work (ADR-0021 D4):

- ``none``    — judge with the adapter exactly as configured (the shipped artifact).
- ``prefix``  — relearn from the case's own events before its first incident. This is what a
                real deployment does: learn this asset's recent clean history, then watch it.
                It is ``train.py``'s rule, generalized.
- ``holdout`` — relearn from a separate clean recording the case names.

Running ``none`` against ``prefix`` on the same corpus is therefore a real experiment: it
answers whether an artifact generalizes across recordings of the same rig, which decides
whether the product ships one artifact per device class or must fit per asset.

**Fitting is optional, never enforced.** A detector that does not implement ``Fittable`` is
evaluated as configured and the report says so; nothing fails. That is a deliberate product
stance — a rule-based detector with no baseline to learn is not a second-class detector.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from iotshield.contracts import NormalizedEvent
from iotshield.detect import Detector, Fittable
from iotshield.detect._fitted import is_fitted
from iotshield.eval.truth import GroundTruth


class FitProtocol(StrEnum):
    NONE = "none"
    PREFIX = "prefix"
    HOLDOUT = "holdout"


@dataclass(frozen=True)
class FitOutcome:
    """A detector ready to judge, plus an honest account of how it got there.

    The ``note`` is not decoration: a report that silently mixed fitted and unfitted
    detectors in one table would invite exactly the misreading this module exists to
    prevent.
    """

    detector: Detector
    fitted: bool = False  # every fallback path leaves it False; only a real fit sets it
    note: str | None = None


def normal_prefix(events: Sequence[NormalizedEvent], truth: GroundTruth) -> list[NormalizedEvent]:
    """The case's events strictly before its first incident — its clean history.

    Everything, when the recording is clean; nothing, when it opens mid-fault (there is no
    clean history to learn from, which the caller reports rather than papering over).
    """
    if truth.is_clean:
        return list(events)
    first_start = min(incident.start for incident in truth.incidents)
    return [event for event in events if event.timestamp < first_start]


def _unfitted(detector: Detector, why: str) -> FitOutcome:
    """A detector that was not fitted, with the reason — and whether it can judge at all.

    Two different situations reach here and must not read alike. A detector with a shipped
    baseline is merely *as configured*: it judges, and the note is context. One built from
    hyperparameters alone (no artifact — the ADR-0022 way to say "fit from scratch") has
    nothing to judge with, and its row of zeros would otherwise read as "this detector is
    bad" when the truth is that it never ran. So the note says which, and the caller reads
    ``is_fitted`` to drop it rather than let ``detect()`` raise mid-benchmark.
    """
    tail = (
        "; evaluated as configured"
        if is_fitted(detector)
        else ", and it has no trained state to fall back on; it judged nothing"
    )
    return FitOutcome(detector, note=f"{why}{tail}")


def prepare_detector(
    detector: Detector,
    *,
    protocol: FitProtocol,
    events: Sequence[NormalizedEvent],
    truth: GroundTruth,
    holdout_events: Sequence[NormalizedEvent] | None = None,
) -> FitOutcome:
    """Apply the protocol to one detector for one case, degrading to "as configured".

    Every fallback path is reported rather than raised: an unfittable detector, a case with
    no clean prefix, a missing holdout, or a detector that refuses the data it was given
    (team3 rejects multi-asset or channel-incomplete input). A benchmark that died because
    one of several detectors could not be fitted would be far less useful than one that
    says which ones were.
    """
    if protocol is FitProtocol.NONE:
        if is_fitted(detector):
            return FitOutcome(detector)
        return _unfitted(detector, "fit protocol is 'none' so nothing was learned")
    if not isinstance(detector, Fittable):
        return _unfitted(detector, "not fittable")

    if protocol is FitProtocol.HOLDOUT:
        training = list(holdout_events or ())
        if not training:
            return _unfitted(detector, "no holdout data")
    else:
        training = normal_prefix(events, truth)
        if not training:
            return _unfitted(detector, "no clean prefix")

    try:
        return FitOutcome(detector.fit(training), fitted=True)
    except (ValueError, TypeError) as exc:
        # ValueError: a detector refusing its training data is a statement about the data,
        # not a bug (team3 rejects several assets at once, or a missing trained channel).
        # TypeError: ``runtime_checkable`` matches on method *name* only, so an adapter with
        # a sklearn-shaped ``fit(self, X, y)`` satisfies the isinstance check above and then
        # fails on call. Neither may kill a benchmark twelve cases in — degrade and report.
        return _unfitted(detector, f"fit refused ({exc})")
