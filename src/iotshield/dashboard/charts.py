"""Altair chart builders, following the dataviz method: form first, color by
job, marks thin, hover tooltips by default, identity never on color alone.

Palette: the validated reference instance (series blue + reserved status
critical for detection windows; pair CVD-validated, worst ΔE 75.7). Every
chart is accompanied by a table/caption in the app, so nothing depends on
color perception alone.
"""

from typing import TypeVar, cast

import altair as alt
import pandas as pd

from iotshield.contracts import DetectionResult, NormalizedEvent, PrioritizedAlert

SERIES = "#2a78d6"  # categorical slot 1 (blue) — the single data series
CRITICAL = "#d03b3b"  # status: critical — reserved for detection windows, never a series
GRID = "#e1e0d9"
MUTED = "#898781"
BASELINE = "#c3c2b7"


_ChartT = TypeVar("_ChartT", alt.Chart, alt.LayerChart)


def _recessive(chart: _ChartT) -> _ChartT:
    return chart.configure_axis(
        gridColor=GRID, labelColor=MUTED, tickColor=BASELINE, domainColor=BASELINE
    ).configure_view(strokeOpacity=0)


def telemetry_timeline(
    events: list[NormalizedEvent], detections: list[DetectionResult]
) -> alt.LayerChart | alt.Chart | None:
    """Value-over-time line with detection windows overlaid in status color.

    Zero-length windows (point anomalies) draw as 2px rules; longer windows as
    translucent bands. The caption + detections table in the app carry the
    identity of the overlay, so meaning never rides on the red alone.
    """
    plotted = [e for e in events if e.value is not None]
    if not plotted:
        return None
    frame = pd.DataFrame(
        {
            "timestamp": [e.timestamp for e in plotted],
            "value": [e.value for e in plotted],
            "metric": [e.metric or "" for e in plotted],
        }
    )
    line = (
        alt.Chart(frame)
        .mark_line(strokeWidth=2, color=SERIES)
        .encode(
            x=alt.X("timestamp:T", title=None),
            y=alt.Y("value:Q", title=None),
            tooltip=["timestamp:T", "value:Q", "metric:N"],  # hover layer, by default
        )
    )
    layers: list[alt.Chart] = [line]
    spans = [d for d in detections if d.window_end > d.window_start]
    points = [d for d in detections if d.window_end == d.window_start]
    if spans:
        bands = pd.DataFrame(
            {
                "start": [d.window_start for d in spans],
                "end": [d.window_end for d in spans],
                "method": [d.method for d in spans],
            }
        )
        layers.append(
            alt.Chart(bands)
            .mark_rect(color=CRITICAL, opacity=0.15)
            .encode(x="start:T", x2="end:T", tooltip=["start:T", "end:T", "method:N"])
        )
    if points:
        rules = pd.DataFrame(
            {
                "timestamp": [d.window_start for d in points],
                "method": [d.method for d in points],
                "score": [d.score for d in points],
            }
        )
        layers.append(
            alt.Chart(rules)
            .mark_rule(color=CRITICAL, strokeWidth=2, opacity=0.6)
            .encode(x="timestamp:T", tooltip=["timestamp:T", "method:N", "score:Q"])
        )
    combined = cast(alt.LayerChart, alt.layer(*layers)).properties(height=280)
    return _recessive(combined)


def severity_bar(alerts: list[PrioritizedAlert]) -> alt.Chart | None:
    """Alert counts per severity: a magnitude comparison, so one hue — the
    axis labels carry identity (multi-hue would add nothing but noise)."""
    if not alerts:
        return None
    frame = pd.DataFrame({"severity": [a.severity.value for a in alerts]})
    order = ["low", "medium", "high", "critical", "emergency"]
    chart = (
        alt.Chart(frame)
        .mark_bar(color=SERIES, cornerRadiusTopLeft=4, cornerRadiusTopRight=4)
        .encode(
            x=alt.X("severity:N", sort=order, title=None),
            y=alt.Y("count():Q", title="alerts"),
            tooltip=["severity:N", "count():Q"],
        )
        .properties(height=180, width=360)
    )
    return _recessive(chart)
