"""The METERING shape of the TELEMETRY lane: hourly consumption, and a SensiPro export.

Metering is the first *payload* the generator models, and it is on a lane that already had a
behaviour. That is the whole reason ``BEHAVIOURS`` is keyed on ``(lane, payload)``: a lane says
which modality, and a meter is a different vocabulary within one that already has ``SensorRig``.

Two things here are unlike the other three lanes, and both come from the same fact — **a meter
reports a cumulative counter, and the observation is the difference between two of them.**

**The renderer is not one line per event.** N consumption events are the differences of N+1
counter readings, so a contiguous run of hours needs one extra *opening* row. There is no way
to write N rows and read N events back, which is why ``Renderer.render`` no longer promises
one line per event and the ordering meta-test no longer tests the mirror property.

**A label is the instants whose CONSUMPTION changed, not whose bytes changed.** Perturb one
hour of a cumulative series and every later row differs, because the counter carries the
change forward forever. Labelling that would mark the whole remainder of the recording faulty.
What a detector judges is the difference, so that is what ``_expressed`` compares — the one
place where "the instants at which output differs from the unfaulted world" needs reading as
*the observation*, not the file.
"""

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np

from iotshield.contracts import Lane, NormalizedEvent
from iotshield.detect.meter._features import WEEK_HOURS
from iotshield.eval.generate.port import (
    Calibration,
    LaneBehaviour,
    Observed,
    Streams,
    one_boundary,
    ramp,
)
from iotshield.eval.generate.world import Clock, Device, FaultSpec, GenerationError
from iotshield.meter import (
    METADATA_KEY,
    UNIT_BY_TYPE,
    MeterReading,
    MeterType,
    consumption_message,
    metric_for,
)
from iotshield.normalize.meter import INTERVAL, INTERVAL_HOURS, SENSIPRO_HOURLY_CSV

CALIBRATION = Calibration(detector="meter_iforest", param=None, value=3.0)
"""``intensity=1.0`` means consumption at **3x what this hour of the week normally draws**.

``param=None`` because ``meter_iforest`` has no threshold to point at: it fits an
isolation-forest score to a false-alarm budget on each meter's own history, so its alarm line
is learned and lives in no registry default (see ``Calibration``). The boundary is therefore
quoted in units this behaviour owns — multiples of the hour-of-week expectation — and 3x is
where the shipped detector reliably separates a fault from ordinary variation at the default
``fp_budget``. Being measured rather than declared, it is pinned by a *behavioural* test rather
than by equality with a constant: the corpus asserts the fault is caught at this intensity.
"""

_COUNTER_DECIMALS = 3
"""Litres on a cubic-metre counter, and Wh on a kWh one: the resolution these platforms export.

Load-bearing for the round-trip, not cosmetic. The normalizer's consumption is the difference
of two counters **as parsed from the text**, so the behaviour has to claim exactly that: it
quantizes each counter through the same string form the renderer will write, and takes its
event values from those quantized numbers. Claiming the raw float would put the answer key a
rounding error away from what the detector is scored on.
"""

_DIURNAL = (
    0.35, 0.30, 0.28, 0.28, 0.32, 0.55, 0.95, 1.35,
    1.25, 1.00, 0.95, 1.00, 1.05, 0.95, 0.90, 0.95,
    1.15, 1.50, 1.70, 1.55, 1.25, 0.95, 0.65, 0.45,
)  # fmt: skip
"""Consumption by hour of day, as multiples of the daily mean: a trough overnight, a morning
peak, and a larger evening one.

This shape is the entire reason ``meter_iforest`` exists rather than a threshold, so a corpus
without it would measure nothing. 3am at 1.7x the daily mean is extraordinary and 7pm at the
same level is ordinary, and only a model of normal can tell those apart — which is also what
makes ``stuck`` (see below) invisible to a magnitude rule and obvious to this detector.
"""

_WEEKEND_LIFT = 1.2
"""Weekends draw more and later. Kept to one scalar rather than a second 24-tuple: it gives the
hour-of-week profile real structure past hour-of-day without inventing detail nobody checks."""


def _multiplier(stamp: datetime) -> float:
    """This hour's share of the daily mean, by hour of week."""
    lift = _WEEKEND_LIFT if stamp.weekday() >= 5 else 1.0
    return _DIURNAL[stamp.hour] * lift


@dataclass(frozen=True)
class _Profile:
    """What one kind of meter draws, and which utility it bills.

    Two kinds ship for the same reason the telemetry lane has profiles (ADR-0028 D7): a water
    meter and an electricity meter are different devices, and one detector serving both is the
    property ADR-0030 built the adapter around.
    """

    mean_per_hour: float
    spread: float
    meter_type: MeterType


_PROFILES: dict[str, _Profile] = {
    "water_meter": _Profile(mean_per_hour=0.045, spread=0.18, meter_type=MeterType.WATER),
    "power_meter": _Profile(mean_per_hour=0.62, spread=0.22, meter_type=MeterType.ELECTRICITY),
}
_DEFAULT_PROFILE = "water_meter"

_START_COUNTER = 1_000.0
"""Where a counter starts. Non-zero because a real meter has been running for years, and a
counter near zero would make the ``:g`` counter in each event's message change width mid-file."""


def _counter_text(counter: float) -> str:
    """A counter as this dialect writes it. **The only place that format is spelled.**

    Both the renderer and the behaviour's quantization go through here, which is what makes the
    behaviour's claimed value and the normalizer's computed value the same float rather than two
    floats that agree to three decimals.
    """
    return f"{counter:.{_COUNTER_DECIMALS}f}"


def _quantize(counter: float) -> float:
    return float(_counter_text(counter))


def _counters(consumption: np.ndarray) -> np.ndarray:
    """The cumulative counter a file of this consumption would carry, quantized per row.

    Sequential because each row is written from the previous row's *rounded* value, so the
    rounding is part of the series rather than applied to a cumulative sum afterwards. A
    ``cumsum`` then rounded would drift from what the text actually says.
    """
    counters = np.empty(consumption.size, dtype=float)
    counters[0] = _quantize(_START_COUNTER)
    for index in range(1, consumption.size):
        # max(0, ...) because a cumulative counter cannot go backwards: the normalizer
        # quarantines a decrease as a rollback, which would fail the round-trip law with a
        # MalformedRecord rather than a mismatch. `stuck` legitimately sits at exactly zero.
        counters[index] = _quantize(counters[index - 1] + max(0.0, float(consumption[index])))
    return counters


class MeterFleet(LaneBehaviour):
    """Hourly consumption with an hour-of-week profile, and three ways for it to go wrong."""

    lane = Lane.TELEMETRY
    payload = METADATA_KEY

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[np.ndarray, np.ndarray, FaultSpec, slice], None]] = {
            "spike": self._surge,
            "drift": self._creep,
            "stuck": self._frozen,
        }
        # One boundary for the whole shape: the detector scores every hour of every meter
        # against one learned line, so there is nothing to select on — the same situation the
        # telemetry lane is in, for a different reason.
        super().__init__(self._handlers, one_boundary(self._handlers, CALIBRATION))

    def observe(
        self,
        device: Device,
        faults: Sequence[FaultSpec],
        clock: Clock,
        streams: Streams,
        source_format: str,
    ) -> Observed:
        if clock.step != INTERVAL:
            # A precondition rather than a resampling: a meter's row IS an hour, so a clock
            # ticking every second would either fabricate 3,600 readings per row or discard
            # 3,599 of them, and both are a corpus whose labels describe something else.
            raise GenerationError(
                f"a metering recording ticks once per reading, but this clock steps every "
                f"{clock.step}; declare Clock(step=timedelta(hours=1)) for {source_format!r}"
            )
        profile = _PROFILES.get(device.kind, _PROFILES[_DEFAULT_PROFILE])
        stamps = [clock.at(index) for index in range(clock.steps)]
        expected = np.array([profile.mean_per_hour * _multiplier(stamp) for stamp in stamps])
        consumption = np.clip(
            expected * streams("consumption").normal(1.0, profile.spread, clock.steps), 0.0, None
        )
        expressed = {
            fault: perturbed
            for fault in faults
            if (perturbed := self._apply(consumption, expected, fault, clock))
        }
        return Observed(
            events=self._events(device, profile, consumption, stamps, source_format),
            expressed=expressed,
        )

    def _events(
        self,
        device: Device,
        profile: _Profile,
        consumption: np.ndarray,
        stamps: Sequence[datetime],
        source_format: str,
    ) -> tuple[NormalizedEvent, ...]:
        """One event per *closed interval*, so the first reading opens and never scores.

        The reading at index 0 establishes the baseline the next difference is taken against,
        which is exactly what the normalizer does with it (its state 1, "opens an interval").
        """
        counters = _counters(consumption)
        unit = UNIT_BY_TYPE[profile.meter_type]
        metric = metric_for(unit)
        # Differences and closing counters materialized once: they are each read twice below
        # (the value and the message), and `float()` on a numpy scalar is not free per event.
        values = [float(value) for value in np.diff(counters)]
        closing = [float(counter) for counter in counters[1:]]
        return tuple(
            NormalizedEvent(
                timestamp=stamps[index + 1],
                asset_id=device.asset_id,
                source_type=Lane.TELEMETRY,
                source_format=source_format,
                metric=metric,
                value=value,
                raw="",  # the pipeline fills this from the rendered line it parsed
                message=consumption_message(value, unit, counter),
                metadata={
                    METADATA_KEY: MeterReading(
                        meter_type=profile.meter_type,
                        unit=unit,
                        counter=counter,
                        interval_hours=INTERVAL_HOURS,
                    ).to_metadata()
                },
            )
            for index, (value, counter) in enumerate(zip(values, closing, strict=True))
        )

    def _apply(
        self,
        consumption: np.ndarray,
        expected: np.ndarray,
        fault: FaultSpec,
        clock: Clock,
    ) -> frozenset[datetime]:
        """Damage the span in place; return the instants whose **consumption** changed.

        Compared on the differences rather than on the counters, and that is the whole subtlety
        of a cumulative lane. A counter carries a perturbation forward forever, so comparing
        counters would mark every hour after the fault as faulty — the label would describe the
        bookkeeping instead of the world. The difference is what the file yields as a value and
        what the detector scores, so it is what the answer key is entitled to claim.
        """
        handler = self._handlers.get(fault.kind)
        if handler is None:
            return frozenset()
        before = np.diff(_counters(consumption))
        first = min(max(fault.start_step, 1), clock.steps - 1)
        last = max(min(fault.start_step + max(1, fault.length_steps), clock.steps), first + 1)
        handler(consumption, expected, fault, slice(first, last))
        changed = np.flatnonzero(np.diff(_counters(consumption)) != before)
        # `diff[i]` is the interval closing at index i+1, which is the event's own instant.
        return frozenset(clock.at(int(position) + 1) for position in changed)

    def _surge(
        self, consumption: np.ndarray, expected: np.ndarray, fault: FaultSpec, span: slice
    ) -> None:
        # A burst far past what this hour ever draws: a leak that runs, or metered theft. A
        # target rather than a delta, so one intensity means the same thing on a water meter
        # drawing 0.045 and an electricity meter drawing 0.62 (ADR-0028 D6).
        consumption[span] = expected[span] * CALIBRATION.scaled(fault.intensity)

    def _creep(
        self, consumption: np.ndarray, expected: np.ndarray, fault: FaultSpec, span: slice
    ) -> None:
        # A developing leak: each hour draws a little more than the last, ending at the target.
        # Never locally sudden, so it is the shape where *latency* separates detectors.
        width = span.stop - span.start
        target = CALIBRATION.scaled(fault.intensity)
        consumption[span] = expected[span] * (1.0 + ramp(width, target - 1.0))

    def _frozen(
        self, consumption: np.ndarray, expected: np.ndarray, fault: FaultSpec, span: slice
    ) -> None:
        # The counter stops advancing: a stalled register, a tampered meter, a dead radio
        # backfilling nothing. Consumption reads exactly zero, which is IN RANGE for every hour
        # of the week and therefore invisible to any magnitude rule — and glaring to a model
        # that knows this meter draws 1.7x the daily mean at 7pm. The discriminating shape, and
        # the one with no magnitude, so it reads no intensity.
        del expected, fault
        consumption[span] = 0.0


class SensiproHourlyRenderer:
    """``from_timestamp,last_value,device_id,unit`` — the hour-end cumulative reading.

    The inverse of the meter normalizer's differencing, and the first renderer that is not a
    per-event function: it writes the **opening** reading of each contiguous run as well as
    every closing one, because a difference needs both of its operands present in the file.

    Timestamps carry their UTC offset. Both shipped dialects export naive Cyprus wall-clock and
    the normalizer localizes it, but a naive local file cannot represent the two hours daylight
    saving breaks — the normalizer quarantines them with a reason — so writing the offset keeps
    a generated corpus meaningful across a DST boundary instead of losing an hour of it once a
    year. ``_parse_naive_or_aware`` honours an offset when one is present, for exactly this.
    """

    lane = Lane.TELEMETRY
    payload = METADATA_KEY
    source_format = SENSIPRO_HOURLY_CSV.name

    def header(self) -> tuple[str, ...]:
        return ("from_timestamp,last_value,device_id,unit",)

    def render(self, events: Sequence[NormalizedEvent]) -> tuple[str, ...]:
        lines: list[str] = []
        # The last instant written per meter, so the opening row is emitted once per contiguous
        # run rather than once per event. Keyed per meter because the builder interleaves a
        # fleet's events by time, so "the previous line" is not "the previous line of this
        # meter" (and a gap in one meter must not suppress another's opener).
        closed_at: dict[str, datetime] = {}
        for event in events:
            reading = MeterReading.from_metadata(event.metadata[METADATA_KEY])
            if event.value is None:
                # The opening counter is `closing - consumption`, so an event with no value
                # cannot be inverted at all. The normalizer never emits one (the value *is* the
                # difference it computed), which makes this a wrong-events guard rather than a
                # data case: it fires only if something hands this renderer another lane's work.
                raise GenerationError(
                    f"meter event at {event.timestamp.isoformat()} carries no consumption "
                    "value, so the reading that opened its interval cannot be reconstructed"
                )
            opened_at = event.timestamp - timedelta(hours=reading.interval_hours)
            if closed_at.get(event.asset_id) != opened_at:
                lines.append(
                    _row(opened_at, reading.counter - event.value, event.asset_id, reading.unit)
                )
            lines.append(_row(event.timestamp, reading.counter, event.asset_id, reading.unit))
            closed_at[event.asset_id] = event.timestamp
        return tuple(lines)


def _row(stamp: datetime, counter: float, device_id: str, unit: str) -> str:
    return f"{stamp.isoformat()},{_counter_text(counter)},{device_id},{unit}"


def hours(count: int) -> Clock:
    """A clock of ``count`` hourly readings — what every metering recording declares.

    Named rather than spelled at each call site because ``step=timedelta(hours=1)`` is the
    precondition ``observe`` refuses without, and a scenario author should not have to know
    that from an exception. ``math`` is imported for the ceiling in ``fitting_hours``.
    """
    return Clock(steps=count, step=INTERVAL)


def fitting_hours(reserved_fraction: float, min_train_hours: int) -> int:
    """The shortest recording whose reserved prefix can actually fit a meter.

    ``--fit-protocol prefix`` trains on the first fraction of each asset's clock, and
    ``meter_iforest`` refuses a meter with less than ``min_train_hours`` of clean history — a
    figure *derived* from ``fp_budget`` (the quantile needs 715 rows, plus a week to warm the
    rolling window). Sizing the corpus from those two numbers rather than typing a length keeps
    it correct if either moves, instead of silently producing a corpus that cannot be fitted and
    a table of skips that reads like a broken detector.

    Two corrections on top of the division, both measured rather than guessed:

    - **+1 reading.** A recording of N readings yields N-1 events, because the first one opens
      an interval and closes nothing. The reserved fraction is taken against the *event* clock,
      so the readings have to be one longer than the events need.
    - **A week of headroom.** Without it the corpus lands on exactly the gate, and anything that
      shifts the arithmetic by one row — a rounding change in ``Reservation.leading``, a warm-up
      row the features drop — turns the whole benchmark into skips. A week is the natural unit,
      being the rolling window the features already warm up over.
    """
    return math.ceil((min_train_hours + WEEK_HOURS) / reserved_fraction) + 1
