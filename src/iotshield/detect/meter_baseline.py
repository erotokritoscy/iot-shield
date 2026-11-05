"""Meter baseline: interpretable checks that need no fit and no artifact (ADR-0048 D4).

The other half of what a meter detector should be. ``meter_iforest`` is near-perfect on a
sustained rise and blind to a stuck meter (upstream measured recall 0.98 against 0.02 on its
synthetic harness; the baseline's stuck recall was 0.88), because a forest learns what a row
looks like and a run of zeros in an hour that is often zero looks like nothing. It also needs a
fitted model per meter, so this is the only anomaly judgment a meter gets with no artifact.

Three checks run by default, each its own detection with its own threshold:

- **stuck** — ``stuck_min_hours`` or more of zero in a row, during hours whose hour-of-week
  profile says the meter is normally active.
- **zscore** — a robust, lagging, floored rolling z-score. The floor is a high percentile of the
  meter's non-zero readings, so a meter idle most of the week cannot have a collapsed scale turn a
  sip into a spike. It reports a fall as readily as a rise.
- **profile** — use well above what this hour of the week normally draws, in units of that same
  floor. It catches normal-sized use at an abnormal time, which the z-score cannot.

An hour belongs to at most one check, most specific first, as upstream's event layer does.

**The floor assumes a meter that idles.** Upstream's 95th percentile protects a household, and on a
building with a constant base load that percentile sits near peak demand, so ``zscore`` and
``profile`` can barely fire: on a 20-building LEAD sample the largest z anywhere was 1.73 against a
threshold of 3.5, and neither check spoke once. Lowering the percentile barely helps, since every
percentile of a constant load sits near that load, so ``floor_percentile: 0`` removes the floor and
leaves the rolling MAD as the only scale. Both checks then share it, which on the default floor
changes nothing unless a meter's MAD exceeds that floor.

**Leak rule: off unless asked for.** Upstream's manufacturer rule is flow above 10 litres in both
the 02:00 and 03:00 hours. On a household meter that is a leak; on a district main it is the job,
and it produced 30,782 of 31,521 baseline events on WBN. Pass ``leak_m3`` to enable it for a fleet
where it means something. It reads those hours on the source's own clock (``leak_timezone``),
where upstream read them in UTC and so checked 04:00-05:00 in Cyprus for half the year.

**What is judged against what.** The profile and the floor come from the batch being judged, so a
long anomaly contributes to its own normal. Medians and a 95th percentile resist that, and it is
the price of needing no fit, but it is why a meter with under ``min_history_hours`` is skipped
and named by ``why_silent``: a few days cannot say what a week looks like.

Unlike upstream, consecutive flagged hours merge into one detection: the alert engine counts
judgments, and one rise is one finding (see ``_runs``). Provenance: Panayiotis Cyprus's
``meterdetect/baseline.py`` (see ADR-0048 on why no commit can be cited).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import numpy as np
import pandas as pd

from iotshield.capabilities import DETECTIONS, Capability, LaneToken, PayloadToken
from iotshield.contracts import DetectionResult, Lane, NormalizedEvent
from iotshield.detect._group import group_by_asset
from iotshield.detect._runs import classify_run, merge_runs
from iotshield.detect.meter._features import WEEK_HOURS, regrid
from iotshield.detect.meter._profile import (
    as_datetime_index,
    build_profile,
    expected_from_profile,
)
from iotshield.detect.meter.detector import consumption_series
from iotshield.enrichment import EVIDENCE_KEY, Enrichment, EnrichmentSource
from iotshield.meter import METADATA_KEY, SIGNATURE_KEY, Signature, reading_of

METHOD = "meter_baseline"

CAPABILITY = Capability.of(
    consumes={LaneToken(Lane.TELEMETRY), PayloadToken(METADATA_KEY)},
    produces={DETECTIONS},
)

_MAD_SCALE = 1.4826
_EPSILON = 1e-9
_LEAK_HOURS = (2, 3)
_NIGHT_END_HOUR = 6

_INSPECT = "Inspect the meter and its register around this time."
ENRICHMENT: dict[str, Enrichment] = {
    "stuck": Enrichment(
        threat_class="Meter fault: stuck reading",
        source=EnrichmentSource.DETECTOR,
        origin="meter_baseline:stuck",
        suggested_actions=(
            "Check whether the premises were really idle during these hours.",
            "Inspect the meter for a jammed register or a failed pulse output.",
        ),
    ),
    "zscore": Enrichment(
        threat_class="Abnormal consumption level",
        source=EnrichmentSource.DETECTOR,
        origin="meter_baseline:zscore",
        suggested_actions=("Compare with the site's activity at this time.", _INSPECT),
    ),
    "profile": Enrichment(
        threat_class="Consumption at an unusual time",
        source=EnrichmentSource.DETECTOR,
        origin="meter_baseline:profile",
        suggested_actions=("Check for a leak or an unscheduled load.", _INSPECT),
    ),
    "leak": Enrichment(
        threat_class="Possible leak: continuous night flow",
        source=EnrichmentSource.DETECTOR,
        origin="meter_baseline:leak",
        suggested_actions=("Inspect the premises for a leak.", _INSPECT),
    ),
}


def _floor(values: np.ndarray, percentile: float = 0.95) -> float:
    """The scale below which a deviation is not worth reporting, whatever the MAD says."""
    if percentile == 0.0:
        return _EPSILON
    positive = values[np.isfinite(values) & (values > 0)]
    floor = float(np.quantile(positive, percentile)) if positive.size else _EPSILON
    return floor if floor > 0 else _EPSILON


def robust_zscore(
    consumption: pd.Series, window: int, floor: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(z, rolling median, scale), lagged an hour so the hour judged is not its own normal."""
    minimum = max(24, window // 4)
    lagged = consumption.shift(1)
    median = lagged.rolling(window, min_periods=minimum).median()
    mad = (lagged - median).abs().rolling(window, min_periods=minimum).median()
    scale = np.maximum(_MAD_SCALE * mad.to_numpy(dtype=float), floor)
    centre = median.to_numpy(dtype=float)
    return (consumption.to_numpy(dtype=float) - centre) / scale, centre, scale


class MeterBaselineDetector:
    def __init__(
        self,
        *,
        z_threshold: float = 3.5,
        window_hours: int = WEEK_HOURS,
        profile_k: float = 3.0,
        stuck_min_hours: int = 12,
        min_history_hours: int = 2 * WEEK_HOURS,
        floor_percentile: float = 0.95,
        leak_m3: float | None = None,
        leak_timezone: str = "Europe/Nicosia",
    ) -> None:
        for name, value in (("z_threshold", z_threshold), ("profile_k", profile_k)):
            if not np.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be a positive, finite number, got {value!r}")
        for name, whole in (
            ("window_hours", window_hours),
            ("stuck_min_hours", stuck_min_hours),
            ("min_history_hours", min_history_hours),
        ):
            if whole < 1:
                raise ValueError(f"{name} must be at least 1, got {whole!r}")
        if not 0.0 <= floor_percentile <= 1.0:
            raise ValueError(f"floor_percentile must be in [0, 1], got {floor_percentile!r}")
        if leak_m3 is not None and not (np.isfinite(leak_m3) and leak_m3 > 0):
            raise ValueError(f"leak_m3 must be a positive, finite number, got {leak_m3!r}")
        try:
            self._leak_zone = ZoneInfo(leak_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown leak_timezone {leak_timezone!r}") from exc
        self._z_threshold = z_threshold
        self._window = window_hours
        self._profile_k = profile_k
        self._stuck_min = stuck_min_hours
        self._min_history = min_history_hours
        self._floor_percentile = floor_percentile
        self._leak_m3 = leak_m3

    @property
    def lane(self) -> Lane:
        return Lane.TELEMETRY

    @property
    def method(self) -> str:
        return METHOD

    def detect(self, events: Sequence[NormalizedEvent]) -> list[DetectionResult]:
        detections: list[DetectionResult] = []
        for asset_id, asset_events in group_by_asset(
            events, lane=self.lane, keep=_is_meter_reading
        ):
            series, interval = consumption_series(asset_events)
            if len(series) < self._min_history:
                continue
            detections += self._judge(asset_id, series, interval, asset_events)
        return detections

    def why_silent(self, events: Sequence[NormalizedEvent]) -> str | None:
        """The optional ``Diagnosable`` capability: name the meters too short to judge."""
        short = sorted(
            asset_id
            for asset_id, asset_events in group_by_asset(
                events, lane=self.lane, keep=_is_meter_reading
            )
            if len(asset_events) < self._min_history
        )
        if not short:
            return None
        return (
            f"{METHOD} needs {self._min_history} hours of a meter's history to know its normal "
            f"week; skipped {', '.join(short)}"
        )

    def _judge(
        self,
        asset_id: str,
        series: pd.Series,
        interval: float,
        events: list[NormalizedEvent],
    ) -> list[DetectionResult]:
        unit = next(
            (reading.unit for reading in map(_reading, events) if reading is not None), "units"
        )
        gridded = regrid(series, interval)
        stamps = as_datetime_index(gridded.index)
        values = gridded.to_numpy(dtype=float)
        expected = expected_from_profile(stamps, build_profile(series))
        floor = _floor(values, self._floor_percentile)

        taken = np.zeros(values.size, dtype=bool)
        found: list[DetectionResult] = []

        for start, end in merge_runs(((values == 0) & (expected > 0)).tolist()):
            hours = end - start + 1
            if hours < self._stuck_min:
                continue
            taken[start : end + 1] = True
            found.append(
                self._detection(
                    "stuck",
                    asset_id,
                    stamps,
                    start,
                    end,
                    score=float(hours),
                    threshold=float(self._stuck_min),
                    signature=Signature.STUCK,
                    explanation=(
                        f"meter {asset_id} read zero for {hours} hours during hours when it is "
                        "normally active"
                    ),
                    extra={"hours": hours},
                )
            )

        if self._leak_m3 is not None:
            found += self._leaks(asset_id, stamps, values, taken, unit)

        z, median, scale = robust_zscore(gridded, self._window, floor)
        flagged = (np.abs(z) > self._z_threshold) & ~taken
        for start, end in merge_runs(flagged.tolist()):
            taken[start : end + 1] = True
            peak = start + int(np.nanargmax(np.abs(z[start : end + 1])))
            hours = end - start + 1
            direction = "above" if z[peak] > 0 else "below"
            found.append(
                self._detection(
                    "zscore",
                    asset_id,
                    stamps,
                    start,
                    end,
                    score=float(abs(z[peak])),
                    threshold=self._z_threshold,
                    signature=Signature.SPIKE if hours == 1 else Signature.SUSTAINED,
                    explanation=(
                        f"meter {asset_id} used {values[peak]:.6g} {unit} in an hour, far "
                        f"{direction} its recent normal of {median[peak]:.6g} {unit} "
                        f"(robust z {z[peak]:.1f})"
                    ),
                    extra={"peak_z": float(z[peak]), "hours": hours},
                )
            )

        deviation = (values - expected) / scale
        for start, end in merge_runs(((deviation > self._profile_k) & ~taken).tolist()):
            peak = start + int(np.nanargmax(deviation[start : end + 1]))
            moment = "at night" if _at(stamps, peak).hour < _NIGHT_END_HOUR else "for this time"
            found.append(
                self._detection(
                    "profile",
                    asset_id,
                    stamps,
                    start,
                    end,
                    score=float(deviation[peak]),
                    threshold=self._profile_k,
                    signature=Signature.PROFILE,
                    explanation=(
                        f"meter {asset_id} used {values[peak]:.6g} {unit} {moment}, against "
                        f"{expected[peak]:.6g} {unit} normally at this hour of the week"
                    ),
                    extra={"hours": end - start + 1},
                )
            )
        found.sort(key=lambda detection: detection.window_start)
        return found

    def _leaks(
        self,
        asset_id: str,
        stamps: pd.DatetimeIndex,
        values: np.ndarray,
        taken: np.ndarray,
        unit: str,
    ) -> list[DetectionResult]:
        limit = self._leak_m3
        assert limit is not None  # checked by the caller
        hours = np.asarray(stamps.tz_convert(self._leak_zone).hour, dtype=int)  # type: ignore[attr-defined]
        first, second = _LEAK_HOURS
        found: list[DetectionResult] = []
        # Adjacent grid rows, so a gap or a daylight-saving jump between them cannot pass.
        for index in range(values.size - 1):
            if hours[index] != first or hours[index + 1] != second:
                continue
            if not (values[index] > limit and values[index + 1] > limit):
                continue
            taken[index : index + 2] = True
            peak = float(max(values[index], values[index + 1]))
            found.append(
                self._detection(
                    "leak",
                    asset_id,
                    stamps,
                    index,
                    index + 1,
                    score=peak,
                    threshold=limit,
                    signature=Signature.SUSTAINED,
                    explanation=(
                        f"meter {asset_id} ran above {limit:.6g} {unit} through both the "
                        f"{first:02d}:00 and {second:02d}:00 hours on "
                        f"{_at(stamps, index).astimezone(self._leak_zone).date()}, "
                        f"peaking at {peak:.6g} {unit}"
                    ),
                    extra={"peak": peak},
                )
            )
        return found

    def _detection(
        self,
        check: str,
        asset_id: str,
        stamps: pd.DatetimeIndex,
        start: int,
        end: int,
        *,
        score: float,
        threshold: float,
        signature: Signature,
        explanation: str,
        extra: dict[str, Any],
    ) -> DetectionResult:
        return DetectionResult(
            asset_id=asset_id,
            window_start=_at(stamps, start),
            window_end=_at(stamps, end),
            lane=self.lane,
            method=self.method,
            score=score,
            threshold=threshold,
            event_kind=classify_run(end - start + 1),
            evidence={
                "explanation": explanation,
                "check": check,
                SIGNATURE_KEY: signature.value,
                **extra,
                EVIDENCE_KEY: ENRICHMENT[check].to_evidence(),
            },
        )


def _at(index: pd.DatetimeIndex, position: int) -> datetime:
    return index[position].to_pydatetime()  # type: ignore[attr-defined]


def _reading(event: NormalizedEvent) -> Any:
    return reading_of(event.metadata)


def _is_meter_reading(event: NormalizedEvent) -> bool:
    return event.value is not None and reading_of(event.metadata) is not None
