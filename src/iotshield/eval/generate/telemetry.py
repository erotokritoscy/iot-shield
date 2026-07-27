"""The TELEMETRY lane: a sensor rig's normal, its three fault shapes, and stub-csv.

Ported from the original single-file generator, split along the axes ``port`` defines: the
channel model and the fault shapes are *behaviour*, the CSV text is a *renderer*. The
magnitudes are unchanged, because they are what make the corpus discriminate (see
``CALIBRATION``).
"""

import math
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import TypeAlias

import numpy as np
import pandas as pd

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.eval.corpus import CaseInput
from iotshield.eval.generate.port import Calibration, Observed, Streams
from iotshield.eval.generate.world import Clock, Device, FaultSpec
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV

_NORMAL = {
    "Pressure": (45.0, 1.0),
    "Temperature": (75.0, 0.8),
    "Vibration": (0.05, 0.004),
}
"""Per-channel (mean, spread).

Chosen so a *fixed absolute threshold* around 80 is a meaningful rule on this rig — which is
what makes ``stub_threshold`` a fair baseline here rather than a detector mis-parameterized
for the data, and what lets each fault shape teach its intended lesson.
"""

CHANNELS = tuple(_NORMAL)
"""Derived from the scales above rather than restated: two lists to keep in step meant a new
channel was either a silent no-op (absent here) or a KeyError (absent there)."""

THRESHOLD = 80.0
CALIBRATION = (Calibration(detector="stub_threshold", param="threshold", value=THRESHOLD),)
"""The boundary the scales above straddle. Pinned against the registry by a meta-test: change
the detector's default or these scales and the corpus stops discriminating, silently."""

_DECIMALS = 6
"""Rounded before rendering, so the text is stable and small. Applied to the *values*, which
is where the old generator applied it too (``frame.round(6)``)."""

_Series: TypeAlias = dict[str, np.ndarray]
"""One recording's channels, mutable in place — what a fault handler damages."""


class SensorRig:
    """Independent gaussian channels, damaged over the span a fault names."""

    lane = Lane.TELEMETRY
    calibration = CALIBRATION

    def __init__(self) -> None:
        # The dispatch table *is* the declaration of what this lane can show: `expresses` is
        # its keys, so a handler and its claim cannot disagree (`faults.lanes_of` then derives
        # the reverse direction from the same source).
        self._handlers: dict[str, Callable[[_Series, FaultSpec, slice, int], None]] = {
            "spike": self._spike,
            "drift": self._drift,
            "stuck": self._stuck,
            "compromise": self._thermal_rise,
        }
        self.expresses = frozenset(self._handlers)

    def observe(
        self,
        device: Device,
        faults: Sequence[FaultSpec],
        clock: Clock,
        streams: Streams,
        source_format: str,
    ) -> Observed:
        # One stream per channel, so adding a channel cannot move an existing one's values.
        series = {
            channel: streams(channel).normal(mean, spread, clock.steps)
            for channel, (mean, spread) in _NORMAL.items()
        }
        expressed = {
            fault: perturbed for fault in faults if (perturbed := self._apply(series, fault, clock))
        }

        # Hoisted out of the comprehension, for the reason `to_stub_csv` documents: per-cell
        # numpy indexing and a per-cell clock call were measured far more expensive than
        # materializing once. `clock.at` in particular ran three times per instant.
        stamps = [clock.at(index) for index in range(clock.steps)]
        columns = [(channel, np.round(series[channel], _DECIMALS).tolist()) for channel in CHANNELS]
        events = tuple(
            NormalizedEvent(
                timestamp=moment,
                asset_id=device.asset_id,
                source_type=Lane.TELEMETRY,
                source_format=source_format,
                metric=channel,
                value=column[index],
                raw="",  # the pipeline fills this from the rendered line it parsed
            )
            for index, moment in enumerate(stamps)
            for channel, column in columns
        )
        return Observed(events=events, expressed=expressed)

    def _apply(self, series: _Series, fault: FaultSpec, clock: Clock) -> frozenset[datetime]:
        """Damage the span in place; return the instants whose **rendered values changed**.

        Measured, not assumed. This used to return ``True`` whenever a handler existed for the
        fault kind, which answers "can this lane model this shape?" rather than "did anything
        happen?" — so a fault scaled to nothing by ``intensity=0.0`` shipped labelled over
        output identical to a clean recording.

        The comparison is against the **rounded** series, because ``_DECIMALS`` is applied
        before rendering: a perturbation smaller than the sixth decimal never reaches the file,
        so the answer key must not claim it did. Rounding also serves as the copy.

        The span is clamped into the recording here and nowhere else. Deriving the label from
        the instants this returns is what removed the second, unclamped computation of the same
        window in ``build._incident``, which could place a label past the last row.
        """
        handler = self._handlers.get(fault.kind)
        if handler is None:
            return frozenset()
        before = {channel: np.round(values, _DECIMALS) for channel, values in series.items()}
        start = max(0, min(fault.start_step, clock.steps - 1))
        length = max(1, min(fault.length_steps, clock.steps - start))
        handler(series, fault, slice(start, start + length), length)
        changed = np.zeros(clock.steps, dtype=bool)
        for channel, values in series.items():
            changed |= np.round(values, _DECIMALS) != before[channel]
        return frozenset(clock.at(int(index)) for index in np.flatnonzero(changed))

    def _spike(self, series: _Series, fault: FaultSpec, span: slice, length: int) -> None:
        # A violent excursion peaking mid-span, well past any sane threshold. The floor: every
        # detector should catch this, so one that misses it is broken.
        shape = np.sin(np.linspace(0.0, math.pi, length))
        series["Pressure"][span] += 50.0 * fault.intensity * shape
        series["Vibration"][span] += 0.08 * fault.intensity * shape

    def _drift(self, series: _Series, fault: FaultSpec, span: slice, length: int) -> None:
        # A slow ramp ending past the threshold but never locally sudden, so a fixed rule
        # catches it only once it crosses — this shape is where *latency* separates detectors
        # that a hit/miss score would tie.
        series["Temperature"][span] += np.linspace(0.0, 9.0 * fault.intensity, length)

    def _stuck(self, series: _Series, fault: FaultSpec, span: slice, length: int) -> None:
        # The sensor freezes at its last plausible value: in range the entire time, so a fixed
        # threshold is structurally blind. Only a detector that models *normal* can see it.
        # This is the discriminating shape.
        del fault, length
        series["Pressure"][span] = series["Pressure"][span.start]

    def _thermal_rise(self, series: _Series, fault: FaultSpec, span: slice, length: int) -> None:
        # The thermal half of a compromise: the board runs hot while malware works. Kept modest
        # on purpose — the telemetry signal alone should be arguable, so that the multimodal
        # corpus measures corroboration rather than a spike anyone would catch.
        del length
        series["Temperature"][span] += 8.0 * fault.intensity


class StubCsvRenderer:
    """``timestamp,asset_id,metric,value`` — one line per reading, no header.

    The inverse of the stub normalizer's fan-out: that adapter turns one wide row into one
    event per channel (the contract's one-value-per-event rule, ADR-0003 D1), and this turns
    each event back into its line.
    """

    lane = Lane.TELEMETRY
    source_format = STUB_CSV

    def header(self) -> tuple[str, ...]:
        return ()

    def render(self, events: Sequence[NormalizedEvent]) -> tuple[str, ...]:
        return tuple(
            line(event.timestamp.isoformat(), event.asset_id, str(event.metric), event.value)
            for event in events
        )


def line(stamp: str, asset_id: str, metric: str, value: object) -> str:
    """One stub-csv reading. **The only place this dialect's line layout is written.**

    Two callers need it from different shapes — the renderer from an event, ``to_stub_csv``
    from a dataframe row — and the previous arrangement gave each its own f-string a hundred
    lines apart, held together by a docstring claiming a test pinned them. No such test
    existed. One function is cheaper than the test would have been.
    """
    return f"{stamp},{asset_id},{metric},{value}"


def to_stub_csv(frame: pd.DataFrame, timestamps: Sequence[datetime], asset_id: str) -> CaseInput:
    """Render a wide channel frame as stub-csv — the path a *converted* corpus takes.

    A third-party converter (``fetch``) has a dataframe rather than events, so it needs a
    frame-shaped entry point; the layout it emits is ``line``'s, by construction.

    Values are materialized once via ``to_numpy``; the obvious per-cell ``frame[c].iloc[i]``
    costs a pandas column selection *and* a scalar lookup per cell, measured at 22x slower
    over a SKAB recording (100ms vs 4.5ms). ``isoformat()`` runs once per row, not once per
    cell — it sat in the inner clause and was 36% of generating a corpus.
    """
    channels = list(frame.columns)
    values = frame.to_numpy()
    stamps = [moment.isoformat() for moment in timestamps]
    lines = [
        line(stamp, asset_id, channels[column], row[column])
        for stamp, row in zip(stamps, values, strict=True)
        for column in range(len(channels))
    ]
    return CaseInput(STUB_CSV, "\n".join(lines) + "\n")
