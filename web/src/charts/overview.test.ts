/**
 * The overview chart builders (ADR-0044), tested by inspection like `specs.test.ts`: what each
 * spec draws, which empty buckets it fills with a real zero, and which colours carry identity.
 */
import { describe, expect, it } from "vitest";

import type { NormalizedEvent } from "@/lib/api/types";

import {
  anomaliesOverTime,
  anomaliesPerMeter,
  anomalyTypes,
  daysOf,
  bucketOf,
  detectionsOverTime,
  flaggedFlows,
  hourlyConsumption,
  hoursOf,
  QUALITY_COLORS,
  typesOverTime,
} from "./overview";
import { LIGHT_PALETTE as palette } from "./palette";

type Spec = Record<string, unknown> & {
  data: { values: Record<string, unknown>[] };
  encoding: Record<string, Record<string, unknown>>;
};
const asSpec = (spec: unknown): Spec => spec as Spec;
const WEEK = { start: "2023-07-01", end: "2023-07-07" };

describe("ranges", () => {
  it("lists every UTC day of a range, both ends included", () => {
    expect(daysOf("2023-06-29", "2023-07-02")).toEqual(["2023-06-29", "2023-06-30", "2023-07-01", "2023-07-02"]);
    expect(daysOf("2023-07-02", "2023-07-01")).toEqual([]);
  });

  it("lists every hour of a day in the server's bucket spelling", () => {
    const hours = hoursOf("2023-07-01", "2023-07-01");
    expect(hours).toHaveLength(24);
    expect(hours[0]).toBe("2023-07-01T00:00:00+00:00");
    expect(hours[23]).toBe("2023-07-01T23:00:00+00:00");
  });
});

describe("IT charts", () => {
  it("draws flows inside a detection in the status red, the rest as the neutral backdrop", () => {
    const spec = asSpec(flaggedFlows(10, 3, palette));
    expect(spec.data.values).toEqual([
      { part: "inside a detection", count: 3 },
      { part: "no detection", count: 7 },
    ]);
    expect((spec.encoding["color"]?.["scale"] as { range: string[] }).range).toEqual([palette.critical, palette.truth]);
    expect(flaggedFlows(0, 0, palette)).toBeNull();
  });

  it("keeps a bar on the range's last day inside the chart", () => {
    const spec = asSpec(detectionsOverTime([{ bucket: "2023-07-07", detections: 9 }], WEEK, "day", palette));
    const domain = (spec.encoding["x"]?.["scale"] as { domain: number[] }).domain;
    expect(domain[1]).toBe(Date.parse("2023-07-08T00:00:00Z"));
    const hourly = asSpec(detectionsOverTime([], { start: "2023-07-07", end: "2023-07-07" }, "hour", palette));
    expect((hourly.encoding["x"]?.["scale"] as { domain: number[] }).domain).toEqual([
      Date.parse("2023-07-07T00:00:00Z"),
      Date.parse("2023-07-08T00:00:00Z"),
    ]);
  });

  it("draws every day of the range, an empty day as zero", () => {
    const spec = asSpec(detectionsOverTime([{ bucket: "2023-07-03", detections: 4 }], WEEK, "day", palette));
    expect(spec.data.values).toHaveLength(7);
    expect(spec.data.values.find((row) => row["when"] === "2023-07-03T00:00:00Z")?.["count"]).toBe(4);
    expect(spec.data.values.find((row) => row["when"] === "2023-07-01T00:00:00Z")?.["count"]).toBe(0);
  });
});

describe("replay", () => {
  const series = [
    { bucket: "2023-07-02", issue: "high", count: 2 },
    { bucket: "2023-07-05", issue: "heartbeat", count: 3 },
  ];

  it("draws only what has arrived by now, with a now line, on an axis fixed to the range", () => {
    const spec = asSpec(anomaliesOverTime(series, WEEK, "day", palette, "2023-07-03")) as unknown as {
      data: { values: { when: string; count: number }[] };
      layer: { mark: { type: string } | string; encoding: { x: { scale?: { domain: number[] } } } }[];
    };
    expect(spec.data.values.map((row) => row.when)).toEqual([
      "2023-07-01T00:00:00Z",
      "2023-07-02T00:00:00Z",
      "2023-07-03T00:00:00Z",
    ]);
    expect(spec.layer).toHaveLength(2);
    expect((spec.layer[1]?.mark as { type: string }).type).toBe("rule");
    // Through the END of the last day, so its bar is drawn inside the chart.
    expect(spec.layer[0]?.encoding.x.scale?.domain).toEqual([
      Date.parse("2023-07-01T00:00:00Z"),
      Date.parse("2023-07-08T00:00:00Z"),
    ]);
  });

  it("keeps the whole range's legend while the lines grow", () => {
    const spec = asSpec(typesOverTime(series, WEEK, "day", palette, "2023-07-02")) as unknown as {
      data: { values: { issue: string }[] };
      layer: { encoding: { color: { scale: { domain: string[] } } } }[];
    };
    expect(spec.layer[0]?.encoding.color.scale.domain).toEqual(["heartbeat", "high"]);
    expect(spec.data.values).toHaveLength(2 * 2);
  });

  it("names an instant's bucket as the server does", () => {
    expect(bucketOf("2023-07-18T09:42:00Z", "hour")).toBe("2023-07-18T09:00:00+00:00");
    expect(bucketOf("2023-07-18T09:42:00+03:00", "day")).toBe("2023-07-18");
  });
});

describe("OT charts", () => {
  const series = [
    { bucket: "2023-07-02", issue: "high", count: 2 },
    { bucket: "2023-07-02", issue: "zero", count: 1 },
    { bucket: "2023-07-05", issue: "heartbeat", count: 3 },
  ];

  it("totals every type per day, empty days at zero, in one hue", () => {
    const spec = asSpec(anomaliesOverTime(series, WEEK, "day", palette));
    const counts = spec.data.values.map((row) => row["count"]);
    expect(counts).toEqual([0, 3, 0, 0, 3, 0, 0]);
    expect((spec["mark"] as { color: string }).color).toBe(palette.series);
  });

  it("draws a single day hour by hour", () => {
    const spec = asSpec(
      anomaliesOverTime(
        [{ bucket: "2023-07-01T08:00:00+00:00", issue: "zero", count: 1 }],
        { start: "2023-07-01", end: "2023-07-01" },
        "hour",
        palette,
      ),
    );
    expect(spec.data.values).toHaveLength(24);
    expect(spec.data.values[8]).toEqual({ when: "2023-07-01T08:00:00+00:00", count: 1 });
  });

  it("draws one line per type that occurred, each in its fixed colour", () => {
    const spec = asSpec(typesOverTime(series, WEEK, "day", palette));
    const scale = spec.encoding["color"]?.["scale"] as { domain: string[]; range: string[] };
    expect(scale.domain).toEqual(["heartbeat", "zero", "high"]);
    expect(scale.range).toEqual([QUALITY_COLORS.heartbeat, QUALITY_COLORS.zero, QUALITY_COLORS.high]);
    expect(spec.data.values).toHaveLength(3 * 7);
    expect(typesOverTime([], WEEK, "day", palette)).toBeNull();
  });

  it("divides by type without drawing the types that have none", () => {
    const perType = [
      { issue: "heartbeat", count: 0 },
      { issue: "high", count: 5 },
      { issue: "small", count: 2 },
    ];
    const spec = asSpec(anomalyTypes(perType, palette));
    expect(spec.data.values.map((row) => row["issue"])).toEqual(["high", "small"]);
    expect(anomalyTypes([{ issue: "high", count: 0 }], palette)).toBeNull();
  });

  it("ranks meters largest first", () => {
    const spec = asSpec(anomaliesPerMeter([{ asset_id: "e1", count: 3 }], palette));
    expect(spec.encoding["y"]?.["sort"]).toBe("-x");
    expect(anomaliesPerMeter([], palette)).toBeNull();
  });

  it("draws a day of consumption in the meter's unit, leaving out readings with no value", () => {
    const event = (hour: number, value: number | null): NormalizedEvent =>
      ({
        timestamp: `2023-07-01T${String(hour).padStart(2, "0")}:00:00Z`,
        asset_id: "w1",
        source_type: "telemetry",
        source_format: "sensipro-hourly-csv",
        metric: "hourly_consumption_m3",
        value,
        raw: "x",
      }) as NormalizedEvent;
    const spec = asSpec(hourlyConsumption([event(1, 0.2), event(2, null), event(3, 0.1)], palette));
    expect(spec.data.values).toHaveLength(2);
    expect(spec.encoding["y"]?.["title"]).toBe("consumption (m³)");
    expect(hourlyConsumption([], palette)).toBeNull();
  });
});

/**
 * Rendered, not just inspected: the specs above can be correct as data and still draw nothing.
 * `bandPosition: 0` on a time-unit bar did exactly that — every bar had zero width, so the
 * detections chart was empty in the browser while every inspection test passed.
 */
describe("what the charts actually draw", () => {
  async function shapes(spec: unknown, role: string): Promise<string[]> {
    const { View, parse } = await import("vega");
    const { compile } = await import("vega-lite");
    const sized = { ...(spec as Record<string, unknown>), width: 600 };
    const view = new View(parse(compile(sized as never).spec), { renderer: "none" });
    const svg = await view.toSVG();
    return [...svg.matchAll(new RegExp(`aria-roledescription="${role}"[^>]*d="([^"]+)"`, "g"))].map(
      (match) => match[1] ?? "",
    );
  }
  const widthOf = (path: string) => Number(/h(-?[\d.]+)/.exec(path)?.[1] ?? 0);

  it("draws the detections bars with a width, hourly and daily", async () => {
    const hourly = detectionsOverTime(
      [{ bucket: "2023-07-22T14:00:00+00:00", detections: 2 }],
      { start: "2023-07-22", end: "2023-07-22" },
      "hour",
      palette,
    );
    const daily = detectionsOverTime(
      [{ bucket: "2023-07-22", detections: 7 }],
      { start: "2023-07-01", end: "2023-07-22" },
      "day",
      palette,
    );
    for (const spec of [hourly, daily]) {
      const bars = await shapes(spec, "bar");
      expect(bars.length).toBeGreaterThan(0);
      expect(bars.every((bar) => widthOf(bar) > 0)).toBe(true);
    }
  });

  it("draws the consumption bars with a width", async () => {
    const event = (hour: number) =>
      ({
        timestamp: `2023-07-01T${String(hour).padStart(2, "0")}:00:00Z`,
        asset_id: "w1",
        source_type: "telemetry",
        source_format: "sensipro-hourly-csv",
        metric: "hourly_consumption_kwh",
        value: hour + 1,
        raw: "x",
      }) as NormalizedEvent;
    const bars = await shapes(hourlyConsumption([event(1), event(2), event(3)], palette), "bar");
    expect(bars).toHaveLength(3);
    expect(bars.every((bar) => widthOf(bar) > 0)).toBe(true);
  });

  it("draws the anomaly line across the range, not at one point", async () => {
    const spec = anomaliesOverTime(
      [{ bucket: "2023-07-03", issue: "high", count: 2 }],
      { start: "2023-07-01", end: "2023-07-07" },
      "day",
      palette,
    );
    const [line] = await shapes(spec, "line mark");
    const xs = [...(line ?? "").matchAll(/[ML](-?[\d.]+),/g)].map((match) => Number(match[1]));
    expect(Math.max(...xs) - Math.min(...xs)).toBeGreaterThan(100);
  });
});
