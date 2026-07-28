"""The TELEMETRY lane: a device's channels, its four fault shapes, and stub-csv.

Split along the axes ``port`` defines: the channel model and the fault shapes are *behaviour*,
the CSV text is a *renderer*.

Two things are quoted against ``CALIBRATION`` rather than hard-coded, and both are ADR-0028
decisions. A fault's **magnitude** is ``intensity`` multiples of the detector's boundary (D6), so
one scalar means the same thing here as it does on the flow and log lanes. And a device's
**channels and sampling rate** come from a profile chosen by ``Device.kind`` (D7), so a network
appliance stops emitting industrial pressure readings at 1 Hz and stops dominating every pooled
point metric by sheer row count.
"""

import math
from bisect import bisect_left
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import TypeAlias

import numpy as np
import pandas as pd

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.eval.corpus import CaseInput
from iotshield.eval.generate.port import (
    Calibration,
    LaneBehaviour,
    Observed,
    Streams,
    one_boundary,
)
from iotshield.eval.generate.world import Clock, Device, FaultSpec
from iotshield.normalize.stub import SOURCE_FORMAT as STUB_CSV

THRESHOLD = 80.0
CALIBRATION = Calibration(detector="stub_threshold", param="threshold", value=THRESHOLD)
"""The boundary every profile's channel scales straddle, and the unit fault magnitudes are
quoted in (ADR-0028 D6). Pinned against the registry by a meta-test: change the detector's
default or these scales and the corpus stops discriminating, silently."""


@dataclass(frozen=True)
class _Profile:
    """What one kind of device measures, and how often.

    ## Why the telemetry lane has profiles at all (ADR-0028 D7)

    It did not, and ``Device.kind``'s docstring promised it did — "selects a profile *within* a
    behaviour ... an unknown kind falls back to that behaviour's default". True of the flow and
    log behaviours, false here: every device got a sensor rig's pressure, temperature and
    vibration at 1 Hz, so ``device_trimodal``'s uplink gateway and update mirror emitted
    industrial pressure readings.

    That was not only unrealistic, it was the largest single distortion of the pooled point
    metrics: three devices at 900 instants each made telemetry 2,700 of the corpus's 3,597 clock
    entries, so a "corpus-wide" point precision was 76% a telemetry number. Per-lane counts
    (D4) stop that from being *hidden*; giving a gateway a gateway's sampling rate stops it from
    being *true*.

    ``channels`` maps name to ``(mean, spread)``. Every profile must straddle ``THRESHOLD``, or
    the fault shapes stop teaching their lesson on that device — a meta-test pins it, because
    the alternative is a corpus that silently measures nothing on one device class.
    """

    channels: dict[str, tuple[float, float]]
    every_steps: int = 1
    warm: str = "Temperature"
    """The channel a thermal fault raises. Named per profile because "the one that runs hot"
    is a different sensor on a rig than on a network appliance, and a fault must not have to
    know which kind of device it landed on."""

    surge: str = "Pressure"
    """The channel a spike or a freeze acts on — the profile's primary process variable."""


_RIG = {
    "Pressure": (45.0, 1.0),
    "Temperature": (75.0, 0.8),
    "Vibration": (0.05, 0.004),
}
"""The original sensor rig's channels, unchanged: they are what make the shipped ``synthetic``
corpus discriminate, and ADR-0021 D5b chose them."""

_PROFILES: dict[str, _Profile] = {
    "sensor": _Profile(channels=_RIG),
    "camera": _Profile(
        # An IP camera reports board temperature and a frame rate, once every five seconds.
        channels={"Temperature": (62.0, 0.9), "FrameRate": (25.0, 0.4)},
        every_steps=5,
        surge="FrameRate",
    ),
    "gateway": _Profile(
        # An appliance reports CPU load and its own die temperature, twice a minute.
        channels={"CpuLoad": (38.0, 3.0), "Temperature": (58.0, 1.2)},
        every_steps=30,
        surge="CpuLoad",
    ),
    "mirror": _Profile(
        channels={"CpuLoad": (44.0, 4.0), "Temperature": (55.0, 1.1)},
        every_steps=30,
        surge="CpuLoad",
    ),
}
_DEFAULT_PROFILE = "sensor"

_DECIMALS = 6
"""Rounded before rendering, so the text is stable and small. Applied to the *values*, which
is where the old generator applied it too (``frame.round(6)``)."""

_Series: TypeAlias = dict[str, np.ndarray]
"""One recording's channels, mutable in place — what a fault handler damages."""


class SensorRig(LaneBehaviour):
    """Independent gaussian channels per device kind, damaged over the span a fault names."""

    lane = Lane.TELEMETRY

    def __init__(self) -> None:
        # The dispatch table *is* the declaration of what this lane can show: `expresses` is
        # its keys, so a handler and its claim cannot disagree (`faults.lanes_of` then derives
        # the reverse direction from the same source).
        self._handlers: dict[str, Callable[[_Series, _Profile, float, slice], None]] = {
            "spike": self._spike,
            "drift": self._drift,
            "stuck": self._stuck,
            "compromise": self._thermal_rise,
        }
        # One boundary for the whole lane: `stub_threshold` compares every channel of every
        # device against a single absolute value, so unlike the flow lane there is nothing to
        # select on. Said as data rather than as a `boundary()` that ignores its argument.
        super().__init__(self._handlers, one_boundary(self._handlers, CALIBRATION))

    def observe(
        self,
        device: Device,
        faults: Sequence[FaultSpec],
        clock: Clock,
        streams: Streams,
        source_format: str,
    ) -> Observed:
        profile = _PROFILES.get(device.kind, _PROFILES[_DEFAULT_PROFILE])
        # A device is sampled on its own schedule, so an appliance reporting twice a minute no
        # longer contributes 900 instants to a corpus alongside a 1 Hz rig (ADR-0028 D7).
        indices = list(range(0, clock.steps, profile.every_steps))
        # One stream per channel, so adding a channel cannot move an existing one's values.
        series = {
            channel: streams(channel).normal(mean, spread, len(indices))
            for channel, (mean, spread) in profile.channels.items()
        }
        expressed = {
            fault: perturbed
            for fault in faults
            if (perturbed := self._apply(series, profile, fault, clock, indices))
        }

        # Hoisted out of the comprehension, for the reason `to_stub_csv` documents: per-cell
        # numpy indexing and a per-cell clock call were measured far more expensive than
        # materializing once. `clock.at` in particular ran once per channel per instant.
        stamps = [clock.at(index) for index in indices]
        columns = [
            (channel, np.round(values, _DECIMALS).tolist()) for channel, values in series.items()
        ]
        events = tuple(
            NormalizedEvent(
                timestamp=moment,
                asset_id=device.asset_id,
                source_type=Lane.TELEMETRY,
                source_format=source_format,
                metric=channel,
                value=column[position],
                raw="",  # the pipeline fills this from the rendered line it parsed
            )
            for position, moment in enumerate(stamps)
            for channel, column in columns
        )
        return Observed(events=events, expressed=expressed)

    def _apply(
        self,
        series: _Series,
        profile: _Profile,
        fault: FaultSpec,
        clock: Clock,
        indices: Sequence[int],
    ) -> frozenset[datetime]:
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
        window in ``build._incidents``, which could place a label past the last instant.

        The span is expressed in *sample positions*, not clock steps, because a profile may
        sample every thirtieth second: a fault declared over 40 steps covers one or two samples
        on a gateway and forty on a rig, which is the honest rendering of a slow sensor.
        """
        handler = self._handlers.get(fault.kind)
        if handler is None:
            return frozenset()
        before = {channel: np.round(values, _DECIMALS) for channel, values in series.items()}
        # `bisect_left` already returns 0 below the first sample, so the only clamping that
        # does anything is keeping the span non-empty and inside the array.
        first = min(bisect_left(indices, fault.start_step), len(indices) - 1)
        last = max(bisect_left(indices, fault.start_step + max(1, fault.length_steps)), first + 1)
        handler(series, profile, fault.intensity, slice(first, last))
        changed = np.zeros(len(indices), dtype=bool)
        for channel, values in series.items():
            changed |= np.round(values, _DECIMALS) != before[channel]
        return frozenset(clock.at(indices[int(pos)]) for pos in np.flatnonzero(changed))

    def _spike(self, series: _Series, profile: _Profile, intensity: float, span: slice) -> None:
        # A violent excursion peaking mid-span. The floor: every detector should catch this at a
        # reasonable intensity, so one that misses it is broken.
        shape = np.sin(np.linspace(0.0, math.pi, span.stop - span.start))
        surge = series[profile.surge]
        # The peak reaches the target; the amplitude is whatever it takes from this channel's
        # own baseline, which is what lets one intensity mean the same thing on a rig's pressure
        # and an appliance's CPU load.
        amplitude = CALIBRATION.scaled(intensity) - float(np.median(surge))
        surge[span] += amplitude * shape
        for channel, values in series.items():
            # Co-perturbation on the other channels, so a spike reads as a correlated event
            # rather than one lonely column. Deliberately small: it must not itself cross the
            # boundary, or every fault shape would be catchable on every channel.
            if channel is not profile.surge:
                values[span] += 0.1 * float(np.std(values)) * shape

    def _drift(self, series: _Series, profile: _Profile, intensity: float, span: slice) -> None:
        # A slow ramp ending at the target but never locally sudden, so a fixed rule catches it
        # only once it crosses — this shape is where *latency* separates detectors that a
        # hit/miss score would tie.
        warm = series[profile.warm]
        rise = CALIBRATION.scaled(intensity) - float(np.median(warm))
        warm[span] += np.linspace(0.0, rise, span.stop - span.start)

    def _stuck(self, series: _Series, profile: _Profile, intensity: float, span: slice) -> None:
        # The sensor freezes at its last plausible value: in range the entire time, so a fixed
        # threshold is structurally blind. Only a detector that models *normal* can see it.
        # This is the discriminating shape, and the one with no magnitude to scale.
        del intensity
        series[profile.surge][span] = series[profile.surge][span.start]

    def _thermal_rise(
        self, series: _Series, profile: _Profile, intensity: float, span: slice
    ) -> None:
        # The thermal half of a compromise: the board runs hot while malware works. A step, not
        # a ramp — the name said "rise" while the code added a constant, and the constant is the
        # honest shape for "it is working hard and stays hot".
        warm = series[profile.warm]
        warm[span] += CALIBRATION.scaled(intensity) - float(np.median(warm))


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
