"""Fit protocols: what "normal" a detector learns from before it is judged.

A detector that learns a baseline (team3's robust z-score) is only as good as the normal
data it learned from, and ADR-0012 D3 measured that a baseline from a *foreign* recording
fails outright — the threshold becomes unreachable. So benchmarking a shipped artifact
across a corpus of other recordings largely measures **domain shift**, and reporting that as
the method's score would be a misleading number.

Hence the protocol is an explicit, reportable choice rather than something implied by how
the harness happens to work (ADR-0021 D4):

- ``none``    — judge with the adapter exactly as configured (the shipped artifact).
- ``prefix``  — relearn from a reserved head of each asset's own clock, which is then **not
                scored**. This is what a real deployment does: learn this asset's recent clean
                history, then watch what comes next.

Running ``none`` against ``prefix`` on the same corpus is therefore a real experiment: it
answers whether an artifact generalizes across recordings of the same rig, which decides
whether the product ships one artifact per device class or must fit per asset.

## Why the reserved head is excluded from scoring

Because otherwise the experiment measures nothing. The rule used to be "everything strictly
before this asset's first incident", which sounds disciplined and is not: an asset with *no*
incidents has no first incident, so it contributed **100% of its events** to training and was
then scored on the very same rows. Measured on ``fleet_scan/scan-one``, 1515 of 1580 scored
events were also training events, and all fourteen asserted-clean assets were trained on
everything they were judged against — so a detector that merely memorises normal reported a
near-perfect false-alarm rate on precisely the assets ADR-0026's roster exists to measure.

Reserving a fixed leading fraction and dropping it from the answer key makes "clean history"
and "test set" disjoint *by construction* rather than by luck of where a fault happened to
land. The reservation is also capped at the asset's first incident, because training on a
fault would be the other half of the same mistake.

Detection still runs over the whole recording — a windowed detector needs its warm-up — but
nothing inside the reserved head is scored, in either direction. That is the same rule the
harness already applies to an asset outside the roster: not measured is not the same as
measured and correct.

**Fitting is optional, never enforced.** A detector that does not implement ``Fittable`` is
evaluated as configured and the report says so; nothing fails. That is a deliberate product
stance — a rule-based detector with no baseline to learn is not a second-class detector.

Note there is no ``holdout``. It was offered by the CLI, the API and the dashboard, and no
bundle could ever satisfy it: ``CaseManifest`` has no field to carry a holdout recording, so
the option could only ever print sixteen rows of zeros and one line of explanation. An option
that cannot succeed is worse than an absent one. Reinstating it means a manifest field plus a
generator that emits a clean companion recording, at which point it is a labels-schema change
and belongs in an ADR rather than in a dropdown (ADR-0022, ADR-0026 both record it deferred).
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from iotshield.contracts import NormalizedEvent
from iotshield.detect import Detector, Fittable
from iotshield.detect._fitted import is_fitted
from iotshield.eval.truth import AssetTruth, FindingT, GroundTruth

TRAIN_FRACTION = 0.2
"""How much of each asset's clock ``prefix`` reserves for training.

A judgment, and the trade is legible in both directions: too small and a windowed detector has
no baseline to compute, too large and the measurement rests on a short tail. A fifth leaves
SKAB's ~1000-row recordings ~200 rows to learn from, which clears team3's default window by an
order of magnitude, and still scores 80% of every recording.
"""


class FitProtocol(StrEnum):
    NONE = "none"
    PREFIX = "prefix"


@dataclass(frozen=True)
class FitOutcome:
    """A detector ready to judge, plus an honest account of how it got there.

    The ``note`` is not decoration: a report that silently mixed fitted and unfitted
    detectors in one table would invite exactly the misreading this module exists to
    prevent.

    ``attempted`` records whether the protocol actually *asked* this detector to fit, which is
    a different question from whether it succeeded. Without it, one rule-based detector in a
    spec dragged the whole run's fitted count to zero — a team3-plus-control run under
    ``prefix`` reported ``0/11`` while scoring 0.705, a number only reachable because team3
    fitted eleven times.
    """

    detector: Detector
    fitted: bool = False  # every fallback path leaves it False; only a real fit sets it
    attempted: bool = False
    note: str | None = None


@dataclass(frozen=True)
class Reservation:
    """The train-only head of each asset's clock, and the answer key that survives it.

    One object rather than two returns because the two are the same decision seen from either
    end: what a protocol took for training is exactly what scoring must not look at. Splitting
    them is how the leak got in — the cut was made in the fit path and the scorer never heard
    about it.
    """

    cutoffs: Mapping[str, datetime]
    """Per asset, the first instant that is *scored*. Everything before it is training."""

    truth: GroundTruth
    """The answer key with the reserved rows removed, for scoring."""

    @classmethod
    def leading(cls, truth: GroundTruth, fraction: float = TRAIN_FRACTION) -> "Reservation":
        """Reserve the first ``fraction`` of each asset's clock, never past its first fault.

        The cut is per asset (ADR-0026) and taken against that asset's *pooled* clock rather
        than per lane, so one device's modalities are trained and scored over the same stretch
        of time — a lane-specific cutoff would train the flow model on a window the telemetry
        model was judged on.

        An asset whose reservation would be empty (a very short clock, or a recording that
        opens mid-fault) gets none: it contributes nothing to training and is scored whole.
        Both halves matter — leaving it out of ``cutoffs`` while still training on it would
        restore the leak this class exists to close.
        """
        cutoffs: dict[str, datetime] = {}
        assets: list[AssetTruth] = []
        for asset in truth.assets:
            clock = asset.clock
            index = int(len(clock) * fraction)
            limit = clock[index] if index < len(clock) else None
            first_fault = min((incident.start for incident in asset.incidents), default=None)
            if first_fault is not None:
                limit = first_fault if limit is None else min(limit, first_fault)
            if limit is None or not clock or limit <= clock[0]:
                assets.append(asset)
                continue
            cutoffs[asset.asset_id] = limit
            assets.append(
                AssetTruth(
                    asset_id=asset.asset_id,
                    timelines={
                        lane: tuple(row for row in rows if row[0] >= limit)
                        for lane, rows in asset.timelines.items()
                    },
                    # Every incident survives: the cut is capped at the first fault, so none
                    # of them can lie inside the reserved head.
                    incidents=asset.incidents,
                )
            )
        return cls(cutoffs=cutoffs, truth=GroundTruth.from_assets(assets))

    def training(self, events: Sequence[NormalizedEvent]) -> list[NormalizedEvent]:
        """The events a detector may learn from: each asset's reserved head, and nothing else.

        An event on an asset *outside* the roster is kept — no claim of a fault is no reason to
        withhold it from training, and it can never leak because it is never scored. An asset
        inside the roster with no reservation contributes nothing, which is the case that used
        to hand a detector the entire test set.

        ``labelled_assets`` is bound once rather than read per event: it is an uncached property
        that rebuilds a frozenset from every asset, and asking it inside the loop made this
        O(events x assets) on the hot path of every ``prefix`` fit (measured 370 ms against
        28 ms over 163k events and 30 assets).
        """
        labelled = self.truth.labelled_assets
        return [
            event
            for event in events
            if event.asset_id not in labelled
            or ((limit := self.cutoffs.get(event.asset_id)) is not None and event.timestamp < limit)
        ]

    def scoreable(self, findings: Sequence[FindingT]) -> list[FindingT]:
        """Findings that reach into the scored region — the rest are on training data.

        A finding is kept when any part of its window is at or after its asset's cutoff, so a
        detector that fires *across* the boundary is judged on the part that counts rather than
        discarded whole. With no reservation every ``get`` returns ``None`` and everything is
        kept, which is what makes this safe to call unconditionally.
        """
        return [
            finding
            for finding in findings
            if (limit := self.cutoffs.get(finding.asset_id)) is None or finding.window_end >= limit
        ]


def reserve(truth: GroundTruth, protocol: FitProtocol) -> Reservation:
    """What this protocol takes out of the recording before anything is judged.

    ``none`` reserves nothing, which is an empty ``cutoffs`` and the untouched answer key —
    so every caller can apply a reservation unconditionally and get the identity.
    """
    if protocol is FitProtocol.PREFIX:
        return Reservation.leading(truth)
    return Reservation(cutoffs={}, truth=truth)


def _unfitted(detector: Detector, why: str, *, attempted: bool = False) -> FitOutcome:
    """A detector that was not fitted, with the reason — and whether it can judge at all.

    Two different situations reach here and must not read alike. A detector with a shipped
    baseline is merely *as configured*: it judges, and the note is context. One built from
    hyperparameters alone (no artifact — the ADR-0022 way to say "fit from scratch") has
    nothing to judge with, and its row of zeros would otherwise read as "this detector is
    bad" when the truth is that it never ran. So the note says which, and the caller reads
    ``is_fitted`` to drop it rather than let ``detect()`` raise mid-benchmark.

    The note leads with ``method`` because a spec holds several detectors and
    ``CaseReport.notes`` is a *set*: unnamed, three unfittable detectors deduplicated into
    one line reading "not fittable", from which a reader cannot tell whether one control
    opted out or the whole spec did — and those say opposite things about whether the fit
    protocol did anything. ``method`` rather than the registry name because it distinguishes
    two presets of one adapter, which is exactly when the collapse was most confusing.
    """
    tail = (
        "; evaluated as configured"
        if is_fitted(detector)
        else ", and it has no trained state to fall back on; it judged nothing"
    )
    return FitOutcome(detector, attempted=attempted, note=f"{detector.method}: {why}{tail}")


def prepare_detector(
    detector: Detector,
    *,
    protocol: FitProtocol,
    training: Sequence[NormalizedEvent],
) -> FitOutcome:
    """Apply the protocol to one detector, degrading to "as configured" rather than raising.

    ``training`` is chosen by the caller (``Reservation.training``) rather than derived here,
    because the same cut has to be applied to scoring and one function cannot own both ends of
    a benchmark. It also means this stays a statement about *a detector*, testable without a
    corpus.

    Every fallback path is reported rather than raised: an unfittable detector, a case with no
    reserved history, or a detector that refuses the data it was given (team3 rejects
    channel-incomplete input). A benchmark that died because one of several detectors could
    not be fitted would be far less useful than one that says which ones were.
    """
    if protocol is FitProtocol.NONE:
        if is_fitted(detector):
            return FitOutcome(detector)
        return _unfitted(detector, "fit protocol is 'none' so nothing was learned")
    if not isinstance(detector, Fittable):
        return _unfitted(detector, "not fittable")
    if not training:
        return _unfitted(detector, "no clean prefix to learn from", attempted=True)

    try:
        return FitOutcome(detector.fit(training), fitted=True, attempted=True)
    except (ValueError, TypeError) as exc:
        # ValueError: a detector refusing its training data is a statement about the data,
        # not a bug (team3 rejects a missing pinned channel, or a fleet where no asset had
        # enough history to fit).
        # TypeError: ``runtime_checkable`` matches on method *name* only, so an adapter with
        # a sklearn-shaped ``fit(self, X, y)`` satisfies the isinstance check above and then
        # fails on call. Neither may kill a benchmark twelve cases in — degrade and report.
        return _unfitted(detector, f"fit refused ({exc})", attempted=True)
