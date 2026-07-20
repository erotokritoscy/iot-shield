"""Altair chart builders, following the dataviz method: form first, color by
job, marks thin, hover tooltips by default, identity never on color alone.

Palette: the validated reference instance (series blue + reserved status
critical for detection windows; pair CVD-validated, worst ΔE 75.7). Every
chart is accompanied by a table/caption in the app, so nothing depends on
color perception alone.
"""

from datetime import UTC, datetime, timedelta
from typing import TypeVar, cast

import altair as alt
import pandas as pd

from iotshield.contracts import DetectionResult, NormalizedEvent, PrioritizedAlert

SERIES = "#2a78d6"  # categorical slot 1 (blue) — the single data series
CRITICAL = "#d03b3b"  # status: critical — reserved for detection windows, never a series
GRID = "#e1e0d9"
MUTED = "#898781"
BASELINE = "#c3c2b7"


def _utc_wallclock(ts: datetime) -> datetime:
    # Vega-Lite renders a naive datetime as-is (no timezone conversion), while a
    # tz-aware one is shifted into the viewer's local time — which would disagree
    # with the UTC timestamps in the tables beside the chart. Feed UTC wall-clock
    # so the axis and tooltips stay in UTC for every viewer (golden rule 4).
    return ts.astimezone(UTC).replace(tzinfo=None)


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
            "timestamp": [_utc_wallclock(e.timestamp) for e in plotted],
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
                "start": [_utc_wallclock(d.window_start) for d in spans],
                "end": [_utc_wallclock(d.window_end) for d in spans],
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
                "timestamp": [_utc_wallclock(d.window_start) for d in points],
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


# --- Evaluation charts (ADR-0021) --------------------------------------------
#
# These introduce NO new colours. Identity rides on the y-axis (one row per spec, one lane
# per stream), so nothing here is a categorical palette needing CVD validation — the one
# real pair was measured and rejected for the right reason: a deliberate neutral fails the
# chroma floor because it *is* gray, which means truth is a reference backdrop (like the
# grid), not a series. So: SERIES for magnitude, CRITICAL for detections and alerts (the
# same meaning it carries in telemetry_timeline, so red never changes meaning between
# pages), and a recessive neutral for ground truth.

TRUTH = "#b8b6ac"  # ground-truth band: a reference element, deliberately not a series hue


def spec_scores(rows: list[dict]) -> alt.Chart | None:
    """Event F1 per spec: a magnitude comparison, so one hue and identity on the axis.

    ``rows`` are ``{"spec": str, "event_f1": float | None}``. Undefined scores are dropped
    rather than drawn as zero — a bar at zero would claim a detector scored nothing when
    the truth is that nothing was measurable (ADR-0021 D2).
    """
    scored = [row for row in rows if row.get("event_f1") is not None]
    if not scored:
        return None
    frame = pd.DataFrame(scored)
    chart = (
        alt.Chart(frame)
        .mark_bar(color=SERIES, cornerRadiusTopRight=4, cornerRadiusBottomRight=4, height=18)
        .encode(
            x=alt.X("event_f1:Q", title="event F1", scale=alt.Scale(domain=[0, 1])),
            y=alt.Y("spec:N", title=None, sort="-x"),
            tooltip=["spec:N", alt.Tooltip("event_f1:Q", format=".3f")],
        )
        .properties(height=alt.Step(26))
    )
    return _recessive(chart)


def case_variance(rows: list[dict]) -> alt.Chart | None:
    """One dot per case, per spec — the view a mean hides.

    ``rows`` are ``{"spec": str, "case": str, "event_f1": float | None}``. The reason this
    exists: SKAB's fitted run scored a mean event F1 of 0.47, which reads as "mediocre
    everywhere" and is wrong — thirteen recordings scored near 1.0 and three collapsed to 0.
    A mean cannot show that; a strip shows it instantly. Identity is on the y-axis, so this
    is a single-hue chart however many specs are compared.
    """
    scored = [row for row in rows if row.get("event_f1") is not None]
    if not scored:
        return None
    frame = pd.DataFrame(scored)
    chart = (
        alt.Chart(frame)
        .mark_circle(size=110, color=SERIES, opacity=0.55, stroke="#fcfcfb", strokeWidth=2)
        .encode(
            x=alt.X("event_f1:Q", title="event F1 per case", scale=alt.Scale(domain=[0, 1])),
            y=alt.Y("spec:N", title=None),
            tooltip=["spec:N", "case:N", alt.Tooltip("event_f1:Q", format=".3f")],
        )
        .properties(height=alt.Step(46))
    )
    return _recessive(chart)


def case_timeline(
    truth: list[tuple[datetime, datetime]],
    detections: list[tuple[datetime, datetime]],
    alerts: list[tuple[datetime, datetime]],
) -> alt.Chart | None:
    """Truth, detections and alerts against one clock — fragmentation made visible.

    The chart a table cannot replace. "det/inc 300" is a number a reader must decode; three
    lanes on one axis show it as texture: a solid band of real fault, a dense comb of
    detections, and however many alert bars the correlation stage compressed them into. The
    gap between the band's left edge and the first detection *is* the latency.

    Zero-length spans (point anomalies, ADR-0003) would be invisible as rects, so every span
    is drawn with a minimum width by nudging its end — the alternative is a chart that
    silently omits exactly the detections that fragment worst.
    """
    lanes = (("truth", truth), ("detections", detections), ("alerts", alerts))
    if not any(spans for _, spans in lanes):
        return None

    everything = [moment for _, spans in lanes for span in spans for moment in span]
    # A visible floor for zero-length spans: 1/300th of the window, so a point anomaly is a
    # thin tick rather than nothing at all.
    total = (max(everything) - min(everything)).total_seconds() or 1.0
    floor = timedelta(seconds=max(total / 300.0, 0.5))

    records = [
        {
            "lane": lane,
            "start": _utc_wallclock(start),
            "end": _utc_wallclock(max(end, start + floor)),
            "span_s": (end - start).total_seconds(),
        }
        for lane, spans in lanes
        for start, end in spans
    ]
    frame = pd.DataFrame(records)
    chart = (
        alt.Chart(frame)
        .mark_bar(cornerRadius=2, height=22)
        .encode(
            x=alt.X("start:T", title=None),
            x2="end:T",
            y=alt.Y("lane:N", title=None, sort=["truth", "detections", "alerts"]),
            # Truth is the reference backdrop; our output wears the status colour it wears
            # everywhere else in the app. Identity is on the axis, never on colour alone.
            color=alt.Color(
                "lane:N",
                sort=["truth", "detections", "alerts"],
                scale=alt.Scale(
                    domain=["truth", "detections", "alerts"], range=[TRUTH, CRITICAL, CRITICAL]
                ),
                legend=None,
            ),
            tooltip=["lane:N", "start:T", "end:T", alt.Tooltip("span_s:Q", title="seconds")],
        )
        .properties(height=alt.Step(34))
    )
    return _recessive(chart)
