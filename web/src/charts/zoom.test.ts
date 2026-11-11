import { compile, type TopLevelSpec } from "vega-lite";
import { describe, expect, it } from "vitest";

import { anomalyTypes, detectionsOverTime } from "./overview";
import { LIGHT_PALETTE as palette } from "./palette";
import { caseTimeline, severityBar, specScores, sweepCurve, telemetryTimeline, type ChartSpec } from "./specs";
import { isZoomable, withZoom, zoomChannels, ZOOM_PARAM } from "./zoom";
import * as seed from "@/test/fixtures";
import type { DetectionResult, NormalizedEvent } from "@/lib/api/types";

/** Compile through Vega-Lite itself: a binding it refuses is a warning, so collect those too. */
function compiled(spec: ChartSpec) {
  const warnings: string[] = [];
  const logger = {
    level: () => logger,
    error: (...m: unknown[]) => { throw new Error(m.join(" ")); },
    warn: (...m: unknown[]) => { warnings.push(m.join(" ")); return logger; },
    info: () => logger,
    debug: () => logger,
  };
  // @ts-expect-error -- the logger's full interface is wider than what compile calls.
  const { spec: vega } = compile(spec as TopLevelSpec, { logger });
  return { vega, warnings };
}

const events = seed.events as unknown as NormalizedEvent[];
const detections = [seed.detection] as unknown as DetectionResult[];

describe("which axes zoom", () => {
  it("zooms a time series on both axes, across its layers", () => {
    const spec = telemetryTimeline(events, detections, palette) as ChartSpec;
    expect(zoomChannels(spec)).toEqual(["x", "y"]);
    const zoomed = withZoom(spec);
    expect(isZoomable(zoomed)).toBe(true);
    // On the first layer, where Vega-Lite wants a selection; the bands share its scales.
    const layer = zoomed.layer as { params?: { name: string }[] }[];
    expect(layer[0]?.params?.map((p) => p.name)).toEqual([ZOOM_PARAM]);
    expect(layer.slice(1).every((l) => l.params === undefined)).toBe(true);
  });

  it("zooms a bar chart of counts per category on its count axis alone", () => {
    expect(zoomChannels(severityBar({ high: 3, critical: 1 }, palette) as ChartSpec)).toEqual(["y"]);
    expect(zoomChannels(specScores([{ spec: "a", event_f1: 0.4 }], palette) as ChartSpec)).toEqual(["x"]);
  });

  it("leaves a pie alone: it has no position axis to zoom", () => {
    const pie = anomalyTypes([{ issue: "zero", count: 2 }], palette) as ChartSpec;
    expect(withZoom(pie)).toBe(pie);
    expect(isZoomable(pie)).toBe(false);
  });

  it("never adds the selection twice", () => {
    const once = withZoom(severityBar({ high: 3 }, palette) as ChartSpec);
    expect(withZoom(once)).toBe(once);
  });

  it("does not zoom a binned axis, which Vega-Lite cannot bind", () => {
    const spec = { mark: "bar", encoding: { x: { field: "v", type: "quantitative", bin: true }, y: { aggregate: "count", type: "quantitative" } } };
    expect(zoomChannels(spec)).toEqual(["y"]);
  });
});

describe("Vega-Lite accepts every zoomed chart", () => {
  const charts: [string, ChartSpec | null][] = [
    ["telemetry timeline", telemetryTimeline(events, detections, palette)],
    ["severity bar", severityBar({ high: 3, critical: 1 }, palette)],
    ["spec scores", specScores([{ spec: "a", event_f1: 0.4 }, { spec: "b", event_f1: 0.6 }], palette)],
    ["sweep curve", sweepCurve([{ spec: "team3[window_size=20]", event_f1: 0.4 }, { spec: "team3[window_size=60]", event_f1: 0.6 }], palette)],
    ["case timeline", caseTimeline([["2026-07-07T12:00:00Z", "2026-07-07T12:05:00Z"]], [], [], palette)],
    [
      "detections over time",
      detectionsOverTime([{ bucket: "2026-07-07", detections: 2 }], { start: "2026-07-06", end: "2026-07-08" }, "day", palette),
    ],
  ];

  it.each(charts)("%s compiles with its scales bound, adding no warning", (_, spec) => {
    expect(spec).not.toBeNull();
    // Compared with the chart unzoomed: a step-height chart already warns about "fit-y",
    // which is its own layout and not something zoom introduced or can fix.
    const before = compiled(spec as ChartSpec).warnings;
    const { vega, warnings } = compiled(withZoom(spec as ChartSpec));
    expect(warnings).toEqual(before);
    expect(JSON.stringify(vega)).toContain(`"${ZOOM_PARAM}_`);
  });
});
