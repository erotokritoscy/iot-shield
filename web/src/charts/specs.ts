/**
 * The six chart builders, ported from `dashboard/charts.py` (D3).
 *
 * Altair compiles to Vega-Lite, so these are the same specs the Streamlit app produced —
 * which is the point: `charts.py` is not styling, it encodes validated dataviz decisions
 * (single-hue magnitude, reserved status red, truth as a neutral backdrop, UTC wall-clock
 * feeding, a visible floor for zero-length spans, undefined-never-drawn-as-zero). Porting
 * the spec preserves those by construction; a native-library restyle would have to re-earn
 * every one of them.
 *
 * Every builder returns `null` for "there is nothing to draw", exactly as the Python ones
 * returned `None`, so the caller renders its empty state instead of an axis with no marks.
 */
import { utcWallclock } from "@/lib/format";
import type { DetectionResult, NormalizedEvent, PrioritizedAlert } from "@/lib/api/types";
import { SEVERITY_ORDER } from "@/lib/api/types";

import type { Palette } from "./palette";

/**
 * A Vega-Lite spec, as data.
 *
 * Deliberately not `vega-lite`'s own `TopLevelSpec`: these builders assemble layer arrays
 * conditionally, and the library's discriminated unions reject a `layer` built by pushing.
 * The specs are unit-tested by *inspection* (the curve/bars decision, the zero-span floor,
 * UTC feeding), which is a stronger check than the nominal type, and Vega itself validates
 * the spec at render time.
 */
export type ChartSpec = Record<string, unknown>;

/** The recessive frame (V5): light grid, muted labels, no view stroke. */
function recessive(palette: Palette): Record<string, unknown> {
  return {
    background: "transparent",
    axis: {
      gridColor: palette.grid,
      labelColor: palette.muted,
      titleColor: palette.muted,
      tickColor: palette.baseline,
      domainColor: palette.baseline,
      labelFontSize: 11,
      titleFontSize: 11,
      titleFontWeight: 500,
    },
    view: { strokeOpacity: 0 },
  };
}

// --- Monitor -----------------------------------------------------------------

/**
 * Value-over-time line with detection windows overlaid in status colour.
 *
 * Zero-length windows (point anomalies) draw as 2 px rules; longer windows as translucent
 * bands. Events with no numeric value are not plotted at all — plotting them at zero would
 * invent a reading (A2). The caption and the detections table beside it carry the identity
 * of the overlay, so meaning never rides on the red alone.
 */
export function telemetryTimeline(
  events: readonly NormalizedEvent[],
  detections: readonly DetectionResult[],
  palette: Palette,
): ChartSpec | null {
  const plotted = events.filter((event) => event.value !== null && event.value !== undefined);
  if (plotted.length === 0) return null;

  const line = {
    data: {
      values: plotted.map((event) => ({
        timestamp: utcWallclock(event.timestamp),
        value: event.value,
        metric: event.metric ?? "",
      })),
    },
    mark: { type: "line", strokeWidth: 2, color: palette.series },
    encoding: {
      x: { field: "timestamp", type: "temporal", title: null },
      y: { field: "value", type: "quantitative", title: null },
      tooltip: [
        { field: "timestamp", type: "temporal" },
        { field: "value", type: "quantitative" },
        { field: "metric", type: "nominal" },
      ],
    },
  };

  const layer: Record<string, unknown>[] = [line];
  const spans = detections.filter((item) => item.window_end > item.window_start);
  const points = detections.filter((item) => item.window_end === item.window_start);

  if (spans.length > 0) {
    layer.push({
      data: {
        values: spans.map((item) => ({
          start: utcWallclock(item.window_start),
          end: utcWallclock(item.window_end),
          method: item.method,
        })),
      },
      mark: { type: "rect", color: palette.critical, opacity: 0.15 },
      encoding: {
        x: { field: "start", type: "temporal" },
        x2: { field: "end" },
        tooltip: [
          { field: "start", type: "temporal" },
          { field: "end", type: "temporal" },
          { field: "method", type: "nominal" },
        ],
      },
    });
  }
  if (points.length > 0) {
    layer.push({
      data: {
        values: points.map((item) => ({
          timestamp: utcWallclock(item.window_start),
          method: item.method,
          score: item.score,
        })),
      },
      mark: { type: "rule", color: palette.critical, strokeWidth: 2, opacity: 0.6 },
      encoding: {
        x: { field: "timestamp", type: "temporal" },
        tooltip: [
          { field: "timestamp", type: "temporal" },
          { field: "method", type: "nominal" },
          { field: "score", type: "quantitative" },
        ],
      },
    });
  }

  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    width: "container",
    height: 280,
    layer,
    config: recessive(palette),
  } as ChartSpec;
}

/**
 * Alert counts per severity: a magnitude comparison, so one hue — the axis labels carry
 * identity (multi-hue would add nothing but noise). The x order is fixed, so the axis does
 * not silently re-scale between runs.
 */
export function severityBar(
  alerts: readonly PrioritizedAlert[],
  palette: Palette,
): ChartSpec | null {
  if (alerts.length === 0) return null;
  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    data: { values: alerts.map((alert) => ({ severity: alert.severity })) },
    mark: {
      type: "bar",
      color: palette.series,
      cornerRadiusTopLeft: 4,
      cornerRadiusTopRight: 4,
    },
    encoding: {
      x: {
        field: "severity",
        type: "nominal",
        sort: [...SEVERITY_ORDER],
        title: null,
        // Horizontal: five short words fit, and Vega's default rotation turned the one thing
        // this chart's identity rides on (the axis labels) into vertical text.
        axis: { labelAngle: 0 },
      },
      y: {
        aggregate: "count",
        type: "quantitative",
        title: "alerts",
        // Whole alerts only: with two in the tallest bar Vega ticks at 0.5, and half an
        // alert is not a thing this axis can mean.
        axis: { tickMinStep: 1, format: "d" },
      },
      tooltip: [
        { field: "severity", type: "nominal" },
        { aggregate: "count", type: "quantitative" },
      ],
    },
    height: 180,
    width: 360,
    config: recessive(palette),
  } as ChartSpec;
}

// --- Evaluation --------------------------------------------------------------

export interface ScoreRow {
  readonly spec: string;
  readonly event_f1: number | null | undefined;
}

/**
 * Drop rows whose score is undefined — never plot them as zero (ADR-0021 D2).
 *
 * A bar at zero claims a detector scored nothing when the truth is that nothing was
 * measurable. Shared so the two charts cannot disagree about it.
 */
function scored(rows: readonly ScoreRow[]): { spec: string; event_f1: number }[] {
  return rows.filter(
    (row): row is { spec: string; event_f1: number } =>
      row.event_f1 !== null && row.event_f1 !== undefined,
  );
}

/** Event F1 per spec: a magnitude comparison, so one hue and identity on the axis. */
export function specScores(rows: readonly ScoreRow[], palette: Palette): ChartSpec | null {
  const values = scored(rows);
  if (values.length === 0) return null;
  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    data: { values },
    mark: {
      type: "bar",
      color: palette.series,
      cornerRadiusTopRight: 4,
      cornerRadiusBottomRight: 4,
      height: 18,
    },
    encoding: {
      x: {
        field: "event_f1",
        type: "quantitative",
        title: "event F1",
        scale: { domain: [0, 1] },
      },
      y: { field: "spec", type: "nominal", title: null, sort: "-x" },
      tooltip: [
        { field: "spec", type: "nominal" },
        { field: "event_f1", type: "quantitative", format: ".3f" },
      ],
    },
    width: "container",
    height: { step: 26 },
    config: recessive(palette),
  } as ChartSpec;
}

/**
 * Recognises the one label shape `eval.sweep` generates: `team3[window_size=20]`.
 *
 * Parsing the label rather than threading sweep metadata through the report keeps the report
 * a flat list of specs — which is what lets a sweep, a hand-written head-to-head and a saved
 * CLI report all render through the same path. A label that does not match is simply not
 * part of a sweep, so the fallback is the ordinary bar chart.
 */
export const SWEEP_LABEL = /^(?<base>.+)\[(?<name>[^=[\]]+)=(?<value>[^,[\]]+)\]$/;

export interface ParsedSweepPoint {
  readonly base: string;
  readonly name: string;
  readonly value: number;
  readonly event_f1: number;
}

/**
 * The curve-or-bars decision, as data (V4). Returns the parsed points when *every* scored
 * row is one single-parameter numeric sweep of the same knob, and `null` otherwise — which
 * is the caller's signal to draw bars.
 */
export function parseSweep(rows: readonly ScoreRow[]): ParsedSweepPoint[] | null {
  const parsed: ParsedSweepPoint[] = [];
  for (const row of scored(rows)) {
    const match = SWEEP_LABEL.exec(String(row.spec));
    if (match?.groups === undefined) return null;
    const value = Number(match.groups.value);
    // A non-numeric sweep has no meaningful axis order.
    if (!Number.isFinite(value)) return null;
    parsed.push({
      base: match.groups.base as string,
      name: match.groups.name as string,
      value,
      event_f1: row.event_f1,
    });
  }
  // One point is not a curve, and two different single-parameter sweeps in one report are
  // two series — neither is a line over one axis.
  const distinct = new Set(parsed.map((item) => `${item.base} ${item.name}`));
  if (parsed.length < 2 || distinct.size !== 1) return null;
  return parsed;
}

/**
 * Event F1 against a swept hyperparameter — the shape of an experiment.
 *
 * A bar chart of `team3[window_size=20]`, `…=60`, `…=120` is a bar chart of three unrelated
 * things; the reader has to decode the ordering out of the labels. A line over a
 * *quantitative* x-axis says the one thing the sweep was run to learn: whether the parameter
 * matters at all, and where it peaks.
 */
export function sweepCurve(rows: readonly ScoreRow[], palette: Palette): ChartSpec | null {
  const parsed = parseSweep(rows);
  if (parsed === null) return null;
  const points = [...parsed].sort((a, b) => a.value - b.value);
  const axisTitle = points[0]?.name ?? "";
  const encoding = {
    x: { field: "value", type: "quantitative", title: axisTitle },
    y: {
      field: "event_f1",
      type: "quantitative",
      title: "event F1",
      scale: { domain: [0, 1] },
    },
    tooltip: [
      { field: "value", type: "quantitative", title: axisTitle },
      { field: "event_f1", type: "quantitative", format: ".3f" },
    ],
  };
  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    data: { values: points },
    layer: [
      { mark: { type: "line", strokeWidth: 2, color: palette.series }, encoding },
      {
        mark: {
          type: "point",
          size: 90,
          filled: true,
          color: palette.series,
          stroke: palette.halo,
          strokeWidth: 2,
        },
        encoding,
      },
    ],
    width: "container",
    height: 240,
    config: recessive(palette),
  } as ChartSpec;
}

export interface CaseScoreRow extends ScoreRow {
  readonly case: string;
}

/**
 * One dot per case, per spec — the view a mean hides. SKAB's fitted run scored a mean event
 * F1 of 0.47, which reads as "mediocre everywhere" and is wrong: thirteen recordings scored
 * near 1.0 and three collapsed to 0. Identity is on the y-axis, so this is a single-hue
 * chart however many specs are compared.
 */
export function caseVariance(
  rows: readonly CaseScoreRow[],
  palette: Palette,
): ChartSpec | null {
  const values = rows.filter((row) => row.event_f1 !== null && row.event_f1 !== undefined);
  if (values.length === 0) return null;
  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    data: { values: values.map((row) => ({ spec: row.spec, case: row.case, event_f1: row.event_f1 })) },
    mark: {
      type: "circle",
      size: 110,
      color: palette.series,
      opacity: 0.55,
      stroke: palette.halo,
      strokeWidth: 2,
    },
    encoding: {
      x: {
        field: "event_f1",
        type: "quantitative",
        title: "event F1 per case",
        scale: { domain: [0, 1] },
      },
      y: { field: "spec", type: "nominal", title: null },
      tooltip: [
        { field: "spec", type: "nominal" },
        { field: "case", type: "nominal" },
        { field: "event_f1", type: "quantitative", format: ".3f" },
      ],
    },
    width: "container",
    height: { step: 46 },
    config: recessive(palette),
  } as ChartSpec;
}

export type Span = readonly [string, string];

/**
 * Truth, detections and alerts against one clock — fragmentation made visible.
 *
 * The chart a table cannot replace. "det/inc 300" is a number a reader must decode; three
 * lanes on one axis show it as texture. The gap between the truth band's left edge and the
 * first detection *is* the latency.
 *
 * Zero-length spans (point anomalies, ADR-0003) would be invisible as rects, so every span
 * is drawn with a minimum width by nudging its end — 1/300th of the window, floored at half
 * a second. The alternative is a chart that silently omits exactly the detections that
 * fragment worst.
 */
export function caseTimeline(
  truth: readonly Span[],
  detections: readonly Span[],
  alerts: readonly Span[],
  palette: Palette,
): ChartSpec | null {
  const lanes: readonly [string, readonly Span[]][] = [
    ["truth", truth],
    ["detections", detections],
    ["alerts", alerts],
  ];
  if (!lanes.some(([, spans]) => spans.length > 0)) return null;

  const moments = lanes.flatMap(([, spans]) => spans.flatMap((span) => span.map((at) => Date.parse(at))));
  const totalSeconds = (Math.max(...moments) - Math.min(...moments)) / 1000 || 1.0;
  const floorSeconds = Math.max(totalSeconds / 300.0, 0.5);

  const values = lanes.flatMap(([lane, spans]) =>
    spans.map(([start, end]) => {
      const startMs = Date.parse(start);
      const endMs = Date.parse(end);
      const drawnEnd = Math.max(endMs, startMs + floorSeconds * 1000);
      return {
        lane,
        start: utcWallclock(start),
        end: utcWallclock(new Date(drawnEnd)),
        span_s: (endMs - startMs) / 1000,
      };
    }),
  );

  return {
    $schema: "https://vega.github.io/schema/vega-lite/v5.json",
    data: { values },
    mark: { type: "bar", cornerRadius: 2, height: 22 },
    encoding: {
      x: { field: "start", type: "temporal", title: null },
      x2: { field: "end" },
      y: {
        field: "lane",
        type: "nominal",
        title: null,
        sort: ["truth", "detections", "alerts"],
      },
      // Truth is the reference backdrop; our output wears the status colour it wears
      // everywhere else in the app. Identity is on the axis, never on colour alone.
      color: {
        field: "lane",
        type: "nominal",
        sort: ["truth", "detections", "alerts"],
        scale: {
          domain: ["truth", "detections", "alerts"],
          range: [palette.truth, palette.critical, palette.critical],
        },
        legend: null,
      },
      tooltip: [
        { field: "lane", type: "nominal" },
        { field: "start", type: "temporal" },
        { field: "end", type: "temporal" },
        { field: "span_s", type: "quantitative", title: "seconds" },
      ],
    },
    width: "container",
    height: { step: 34 },
    config: recessive(palette),
  } as ChartSpec;
}
