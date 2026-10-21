/**
 * The IT and OT overview charts (ADR-0044) — the iotShield prototype's two dashboards,
 * rebuilt as Vega-Lite specs over iotshield's own results.
 *
 * Same conventions as `specs.ts`: a magnitude is one hue (`palette.series`), the status red
 * means "a detector said something" and nothing else, time is UTC on every axis, and every
 * builder returns `null` when there is nothing to draw. One deliberate exception: SensiPro's
 * six anomaly types are six *identities*, not a magnitude, so they get six fixed colours —
 * the Okabe–Ito set, chosen to stay distinguishable under the common colour-vision
 * deficiencies — and every chart that uses them travels with a legend and a table, so no
 * meaning rides on colour alone (V1).
 *
 * A count over a range the server was asked about is a real zero, not a missing value, so
 * the bars and lines here fill the range's empty buckets with 0 — unlike the evaluation
 * charts, where an absent score is undefined and must never be drawn as zero.
 */
import type { NormalizedEvent } from "@/lib/api/types";

import type { Palette } from "./palette";
import type { ChartSpec } from "./specs";

/** SensiPro's six types, in its `anomalyclass` code order (ADR-0043). */
export const QUALITY_ISSUES = ["heartbeat", "valueError", "zero", "negative", "high", "small"] as const;
export type QualityIssueName = (typeof QUALITY_ISSUES)[number];

/** Okabe–Ito: one fixed colour per type, so a type looks the same on every chart. */
export const QUALITY_COLORS: Readonly<Record<QualityIssueName, string>> = {
  heartbeat: "#0072B2",
  valueError: "#999999",
  zero: "#E69F00",
  negative: "#CC79A7",
  high: "#D55E00",
  small: "#009E73",
};

/** What each type means, for legends and the (i) beside them. */
export const QUALITY_MEANING: Readonly<Record<QualityIssueName, string>> = {
  heartbeat: "The meter went silent: readings missing for longer than allowed.",
  valueError: "The meter's counter could not be read (missing, not a number, not finite).",
  zero: "The cumulative counter fell to zero.",
  negative: "The cumulative counter was negative.",
  high: "An hour's consumption was above the configured limit.",
  small: "The cumulative counter went backwards (not to zero).",
};

const SCHEMA = "https://vega.github.io/schema/vega-lite/v5.json";
const DAY_MS = 86_400_000;
const HOUR_MS = 3_600_000;

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
    legend: { labelColor: palette.muted, titleColor: palette.muted, labelFontSize: 11 },
    view: { strokeOpacity: 0 },
  };
}

/** Every UTC day from `start` to `end`, both inclusive, as `YYYY-MM-DD`. */
export function daysOf(start: string, end: string): string[] {
  const days: string[] = [];
  const last = Date.parse(`${end}T00:00:00Z`);
  for (let at = Date.parse(`${start}T00:00:00Z`); at <= last && days.length < 400; at += DAY_MS) {
    days.push(new Date(at).toISOString().slice(0, 10));
  }
  return days;
}

/** Every UTC hour of the days `start`..`end`, as the server spells an hour bucket. */
export function hoursOf(start: string, end: string): string[] {
  const hours: string[] = [];
  const last = Date.parse(`${end}T23:00:00Z`);
  for (let at = Date.parse(`${start}T00:00:00Z`); at <= last && hours.length < 24 * 31; at += HOUR_MS) {
    hours.push(`${new Date(at).toISOString().slice(0, 13)}:00:00+00:00`);
  }
  return hours;
}

/** The buckets of a range, in the server's spelling: days, or every hour of those days. */
export function bucketsOf(range: { start: string; end: string }, bucket: "day" | "hour"): string[] {
  return bucket === "day" ? daysOf(range.start, range.end) : hoursOf(range.start, range.end);
}

/** A bucket as an instant Vega can place: a day is its midnight UTC. */
const instant = (key: string): string => (key.length === 10 ? `${key}T00:00:00Z` : key);

/** The bucket an ISO instant falls in, spelled as the server spells it. */
export function bucketOf(iso: string, bucket: "day" | "hour"): string {
  const utc = new Date(iso).toISOString();
  return bucket === "day" ? utc.slice(0, 10) : `${utc.slice(0, 13)}:00:00+00:00`;
}

/**
 * A UTC time axis fixed to the whole range, so a replay *grows* across it instead of the
 * axis rescaling under every new bucket.
 */
const temporalX = (field: string, title: string | null, hourly: boolean, domain: readonly string[]) => ({
  field,
  type: "temporal",
  // Ends at the END of the last bucket, not its start: a bar is drawn across its whole hour or
  // day, so ending at the last bucket's start cut the last bar off the chart entirely — on a
  // range whose detections all fall on its last day, the chart showed no bars at all.
  // As epoch milliseconds: Vega-Lite reads a string domain on a time scale as-is rather than
  // parsing it, so ISO strings here mapped every mark to x = 0 — bars of no width, a flat line.
  scale: {
    type: "utc",
    domain: [Date.parse(instant(domain[0] ?? "")), Date.parse(bucketEnd(domain[domain.length - 1] ?? ""))],
  },
  title,
  axis: { format: hourly ? "%b %d %H:%M" : "%b %d", labelAngle: 0, labelOverlap: true },
});

/** The instant a bucket ends: the next midnight for a day, the next hour for an hour. */
function bucketEnd(key: string): string {
  const start = Date.parse(instant(key));
  return Number.isNaN(start) ? "" : new Date(start + (key.length === 10 ? DAY_MS : HOUR_MS)).toISOString();
}

const temporalTip = (field: string, hourly: boolean) => ({
  field,
  type: "temporal",
  timeUnit: hourly ? "utcyearmonthdatehours" : "utcyearmonthdate",
  title: hourly ? "hour (UTC)" : "day (UTC)",
});

/**
 * Lay a "now" line over a time chart during a replay; without `now`, the spec as it is.
 * The line is muted and dashed: it is a reading aid, not data and not a status.
 */
function withNow(
  spec: ChartSpec & { mark: unknown; encoding: unknown },
  now: string | null | undefined,
  palette: Palette,
): ChartSpec {
  if (!now) return spec;
  const { mark, encoding, ...rest } = spec;
  return {
    ...rest,
    layer: [
      { mark, encoding },
      {
        data: { values: [{ now: instant(now) }] },
        mark: { type: "rule", color: palette.muted, strokeDash: [4, 3], strokeWidth: 1.5 },
        encoding: { x: { field: "now", type: "temporal" } },
      },
    ],
  };
}

// --- IT ------------------------------------------------------------------------------

/** Flows a detection covered, against the rest: the prototype's "attack vs normal". */
export function flaggedFlows(flows: number, flagged: number, palette: Palette): ChartSpec | null {
  if (flows === 0) return null;
  const rows = [
    { part: "inside a detection", count: flagged },
    { part: "no detection", count: flows - flagged },
  ];
  return {
    $schema: SCHEMA,
    data: { values: rows },
    mark: { type: "arc", innerRadius: 50, stroke: palette.halo, strokeWidth: 2 },
    encoding: {
      theta: { field: "count", type: "quantitative", stack: true },
      color: {
        field: "part",
        type: "nominal",
        // The status red is what "a detector said something" looks like everywhere; the
        // rest is the neutral backdrop, not a second series.
        scale: { domain: rows.map((row) => row.part), range: [palette.critical, palette.truth] },
        legend: { title: null, orient: "bottom" },
      },
      tooltip: [
        { field: "part", type: "nominal", title: "flows" },
        { field: "count", type: "quantitative", format: "," },
      ],
    },
    height: 200,
    width: 200,
    config: recessive(palette),
  };
}

/**
 * Flow-lane detections per bucket across the whole range, empty buckets at zero; during a
 * replay only the buckets up to `now`.
 */
export function detectionsOverTime(
  series: readonly { bucket: string; detections: number }[],
  range: { start: string; end: string },
  bucket: "day" | "hour",
  palette: Palette,
  now?: string | null,
): ChartSpec | null {
  const counts = new Map(series.map((point) => [point.bucket, point.detections]));
  const buckets = bucketsOf(range, bucket);
  if (buckets.length === 0) return null;
  const hourly = bucket === "hour";
  const rows = buckets
    .filter((key) => !now || key <= now)
    .map((key) => ({ when: instant(key), count: counts.get(key) ?? 0 }));
  return withNow(
    {
      $schema: SCHEMA,
      data: { values: rows },
      mark: { type: "bar", color: palette.critical, cornerRadiusTopLeft: 2, cornerRadiusTopRight: 2 },
      encoding: {
        x: {
          ...temporalX("when", null, hourly, buckets),
          timeUnit: hourly ? "utcyearmonthdatehours" : "utcyearmonthdate",
        },
        y: { field: "count", type: "quantitative", title: "detections", axis: { tickMinStep: 1, format: "d" } },
        tooltip: [temporalTip("when", hourly), { field: "count", type: "quantitative", title: "detections" }],
      },
      height: 200,
      width: "container",
      config: recessive(palette),
    },
    now,
    palette,
  );
}

// --- OT ------------------------------------------------------------------------------

type SeriesPoint = { readonly bucket: string; readonly issue: string; readonly count: number };

/** All anomalies per bucket, empty buckets drawn at zero; during a replay, up to `now`. */
export function anomaliesOverTime(
  series: readonly SeriesPoint[],
  range: { start: string; end: string },
  bucket: "day" | "hour",
  palette: Palette,
  now?: string | null,
): ChartSpec | null {
  const totals = new Map<string, number>();
  for (const point of series) totals.set(point.bucket, (totals.get(point.bucket) ?? 0) + point.count);
  const buckets = bucketsOf(range, bucket);
  if (buckets.length === 0) return null;
  const hourly = bucket === "hour";
  const rows = buckets
    .filter((key) => !now || key <= now)
    .map((key) => ({ when: instant(key), count: totals.get(key) ?? 0 }));
  return withNow(
    {
      $schema: SCHEMA,
      data: { values: rows },
      mark: { type: "line", color: palette.series, point: { filled: true, size: 22, color: palette.series } },
      encoding: {
        x: temporalX("when", null, hourly, buckets),
        y: { field: "count", type: "quantitative", title: "anomalies", axis: { tickMinStep: 1, format: "d" } },
        tooltip: [temporalTip("when", hourly), { field: "count", type: "quantitative", title: "anomalies" }],
      },
      height: 200,
      width: "container",
      config: recessive(palette),
    },
    now,
    palette,
  );
}

/**
 * One line per type over time — the prototype's per-device anomaly lines. The legend is the
 * whole range's, so it does not change while a replay draws the lines.
 */
export function typesOverTime(
  series: readonly SeriesPoint[],
  range: { start: string; end: string },
  bucket: "day" | "hour",
  palette: Palette,
  now?: string | null,
): ChartSpec | null {
  const present = QUALITY_ISSUES.filter((issue) => series.some((point) => point.issue === issue));
  if (present.length === 0) return null;
  const hourly = bucket === "hour";
  const buckets = bucketsOf(range, bucket);
  const counts = new Map(series.map((point) => [`${point.bucket}|${point.issue}`, point.count]));
  const shown = buckets.filter((key) => !now || key <= now);
  const rows = present.flatMap((issue) =>
    shown.map((key) => ({ when: instant(key), issue, count: counts.get(`${key}|${issue}`) ?? 0 })),
  );
  return withNow(
    {
      $schema: SCHEMA,
      data: { values: rows },
      mark: { type: "line", point: { filled: true, size: 20 } },
      encoding: {
        x: temporalX("when", null, hourly, buckets),
        y: { field: "count", type: "quantitative", title: "anomalies", axis: { tickMinStep: 1, format: "d" } },
        color: {
          field: "issue",
          type: "nominal",
          scale: { domain: [...present], range: present.map((issue) => QUALITY_COLORS[issue]) },
          legend: { title: null, orient: "bottom" },
        },
        tooltip: [
          temporalTip("when", hourly),
          { field: "issue", type: "nominal", title: "type" },
          { field: "count", type: "quantitative", title: "anomalies" },
        ],
      },
      height: 200,
      width: "container",
      config: recessive(palette),
    },
    now,
    palette,
  );
}

/** How the anomalies divide among SensiPro's six types. Types with none are not drawn. */
export function anomalyTypes(perType: readonly { issue: string; count: number }[], palette: Palette): ChartSpec | null {
  const rows = perType.filter(
    (row): row is { issue: QualityIssueName; count: number } =>
      row.count > 0 && (QUALITY_ISSUES as readonly string[]).includes(row.issue),
  );
  if (rows.length === 0) return null;
  return {
    $schema: SCHEMA,
    data: { values: rows },
    mark: { type: "arc", innerRadius: 50, stroke: palette.halo, strokeWidth: 2 },
    encoding: {
      theta: { field: "count", type: "quantitative", stack: true },
      color: {
        field: "issue",
        type: "nominal",
        scale: { domain: rows.map((row) => row.issue), range: rows.map((row) => QUALITY_COLORS[row.issue]) },
        legend: { title: null, orient: "bottom" },
      },
      tooltip: [
        { field: "issue", type: "nominal", title: "type" },
        { field: "count", type: "quantitative", format: "," },
      ],
    },
    height: 200,
    width: 200,
    config: recessive(palette),
  };
}

/** The meters with the most anomalies, largest first: a magnitude, so one hue. */
export function anomaliesPerMeter(
  perMeter: readonly { asset_id: string; count: number }[],
  palette: Palette,
): ChartSpec | null {
  if (perMeter.length === 0) return null;
  return {
    $schema: SCHEMA,
    data: { values: perMeter.map((row) => ({ meter: row.asset_id, count: row.count })) },
    mark: { type: "bar", color: palette.series, cornerRadiusEnd: 3 },
    encoding: {
      y: { field: "meter", type: "nominal", sort: "-x", title: null },
      x: { field: "count", type: "quantitative", title: "anomalies", axis: { tickMinStep: 1, format: "d" } },
      tooltip: [
        { field: "meter", type: "nominal" },
        { field: "count", type: "quantitative", title: "anomalies" },
      ],
    },
    height: { step: 18 },
    width: "container",
    config: recessive(palette),
  };
}

/** One meter's consumption, hour by hour, for one UTC day — the prototype's measurement bar. */
export function hourlyConsumption(events: readonly NormalizedEvent[], palette: Palette): ChartSpec | null {
  // An event with no value is not a reading of zero (A2): it is simply not drawn.
  const rows = events
    .filter((event) => event.value !== null && event.value !== undefined)
    .map((event) => ({ at: event.timestamp, value: event.value, metric: event.metric ?? "consumption" }));
  if (rows.length === 0) return null;
  const unit = rows[0]?.metric.endsWith("_m3") ? "m³" : rows[0]?.metric.endsWith("_kwh") ? "kWh" : "";
  return {
    $schema: SCHEMA,
    data: { values: rows },
    mark: { type: "bar", color: palette.series, cornerRadiusTopLeft: 2, cornerRadiusTopRight: 2 },
    encoding: {
      x: {
        field: "at",
        type: "temporal",
        scale: { type: "utc" },
        title: "hour ending (UTC)",
        axis: { format: "%H:%M", labelAngle: 0 },
        timeUnit: "utcyearmonthdatehours",
      },
      y: { field: "value", type: "quantitative", title: unit ? `consumption (${unit})` : "consumption" },
      tooltip: [temporalTip("at", true), { field: "value", type: "quantitative", title: unit || "value" }],
    },
    height: 200,
    width: "container",
    config: recessive(palette),
  };
}
