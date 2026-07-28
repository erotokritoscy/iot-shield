import { describe, expect, it } from "vitest";

import { LIGHT_PALETTE as palette } from "./palette";
import {
  caseTimeline,
  parseSweep,
  severityBar,
  specScores,
  sweepCurve,
  telemetryTimeline,
  type ScoreRow,
} from "./specs";
import type { DetectionResult, NormalizedEvent, PrioritizedAlert } from "@/lib/api/types";

const rows = (...labels: string[]): ScoreRow[] =>
  labels.map((label) => ({ spec: label, event_f1: 0.5 }));

/**
 * A sweep gets a curve; anything else falls back to bars (V4) — the port of
 * `tests/dashboard/test_charts.py::TestSweepCurve`, condition for condition.
 *
 * The judgment being pinned: a swept hyperparameter is a *quantitative* axis, and a bar
 * chart of `team3[window_size=20]`, `…=60`, `…=120` makes the reader decode the ordering out
 * of the labels. The fallback conditions matter as much as the happy path — a chart that
 * silently drew unrelated specs as a "curve" would invent a trend.
 */
describe("the curve-or-bars decision", () => {
  it("makes a single-parameter sweep a curve", () => {
    expect(parseSweep(rows("team3[window_size=20]", "team3[window_size=60]"))).not.toBeNull();
    expect(sweepCurve(rows("team3[window_size=20]", "team3[window_size=60]"), palette)).not.toBeNull();
  });

  it("still makes a detector-qualified sweep a curve", () => {
    // A qualified key puts a dot in the parameter name and the entry label contains spaces
    // and a `+`, so a parser that worked for `team3[window_size=20]` could degrade to bars.
    expect(
      parseSweep(
        rows(
          "stub_threshold + flow_burst[flow_burst.threshold=10]",
          "stub_threshold + flow_burst[flow_burst.threshold=30]",
        ),
      ),
    ).not.toBeNull();
  });

  it("does not treat an ordinary head-to-head as a sweep", () => {
    expect(parseSweep(rows("team3", "stub"))).toBeNull();
  });

  it("does not treat a mix of swept and plain specs as a sweep", () => {
    expect(parseSweep(rows("team3[window_size=20]", "stub"))).toBeNull();
  });

  it("refuses two swept parameters: that is a surface, not a line", () => {
    expect(parseSweep(rows("t[window_size=20,percentile=99]", "t[window_size=60,percentile=99]"))).toBeNull();
  });

  it("refuses a non-numeric sweep, which has no meaningful axis order", () => {
    expect(parseSweep(rows("log[preset=auth]", "log[preset=sev]"))).toBeNull();
  });

  it("refuses one point, which is not a curve", () => {
    expect(parseSweep(rows("team3[window_size=20]"))).toBeNull();
  });

  it("refuses two different sweeps in one report: those are two series", () => {
    expect(parseSweep(rows("a[window_size=20]", "b[window_size=60]"))).toBeNull();
  });

  it("drops undefined scores rather than drawing them as zero", () => {
    // ADR-0021 D2: a bar at zero claims a detector scored nothing when the truth is that
    // nothing was measurable.
    const undefinedRows: ScoreRow[] = [
      { spec: "team3[window_size=20]", event_f1: null },
      { spec: "team3[window_size=60]", event_f1: null },
    ];
    expect(parseSweep(undefinedRows)).toBeNull();
    expect(sweepCurve(undefinedRows, palette)).toBeNull();
    expect(specScores(undefinedRows, palette)).toBeNull();
  });

  it("keeps only the defined scores when a report mixes them", () => {
    const spec = specScores(
      [
        { spec: "kept", event_f1: 0.7 },
        { spec: "dropped", event_f1: null },
      ],
      palette,
    );
    const values = (spec?.["data"] as { values: { spec: string }[] }).values;
    expect(values.map((row) => row.spec)).toEqual(["kept"]);
  });
});

describe("UTC feeding (V2)", () => {
  const event = (timestamp: string, value: number | null): NormalizedEvent => ({
    timestamp,
    asset_id: "pump-02",
    source_type: "telemetry",
    source_format: "stub-csv",
    metric: "Pressure",
    value,
    severity: "info",
    raw: "line",
  });

  it("feeds the timeline UTC wall-clock, not the viewer's zone", () => {
    const spec = telemetryTimeline([event("2026-07-07T15:00:00+03:00", 45)], [], palette);
    const layer = spec?.["layer"] as { data: { values: { timestamp: string }[] } }[];
    expect(layer[0]?.data.values[0]?.timestamp).toBe("2026-07-07T12:00:00.000");
  });

  it("never plots an event whose value is undefined", () => {
    // Events with `value=None` are not plotted; plotting them at zero would invent a reading.
    expect(telemetryTimeline([event("2026-07-07T12:00:00Z", null)], [], palette)).toBeNull();
  });

  it("draws a zero-length detection window as a rule and a span as a band", () => {
    const detection = (start: string, end: string): DetectionResult => ({
      asset_id: "pump-02",
      lane: "telemetry",
      method: "stub_threshold",
      score: 96,
      threshold: 80,
      window_start: start,
      window_end: end,
    });
    const spec = telemetryTimeline(
      [event("2026-07-07T12:00:00Z", 45)],
      [
        detection("2026-07-07T12:00:30Z", "2026-07-07T12:00:30Z"),
        detection("2026-07-07T12:01:00Z", "2026-07-07T12:02:00Z"),
      ],
      palette,
    );
    const layer = spec?.["layer"] as { mark: { type: string; color?: string } }[];
    expect(layer.map((item) => item.mark.type)).toEqual(["line", "rect", "rule"]);
    // The status red is reserved for our output and never used as a data series (V1).
    expect(layer[1]?.mark.color).toBe(palette.critical);
    expect(layer[2]?.mark.color).toBe(palette.critical);
    expect(layer[0]?.mark.color).toBe(palette.series);
  });
});

describe("the case timeline", () => {
  const spans = (start: string, end: string) => [[start, end]] as [string, string][];

  it("gives a zero-length span a visible floor rather than omitting it", () => {
    // 1/300th of the window, floored at half a second: point anomalies are exactly the
    // detections that fragment worst, so a chart that silently dropped them would hide the
    // finding.
    const spec = caseTimeline(
      spans("2026-07-07T12:00:00Z", "2026-07-07T13:00:00Z"),
      spans("2026-07-07T12:30:00Z", "2026-07-07T12:30:00Z"),
      [],
      palette,
    );
    const values = (spec?.["data"] as { values: { lane: string; start: string; end: string; span_s: number }[] })
      .values;
    const point = values.find((row) => row.lane === "detections");
    expect(point?.span_s).toBe(0);
    // 3600 s window / 300 = 12 s of drawn width.
    expect(point?.start).toBe("2026-07-07T12:30:00.000");
    expect(point?.end).toBe("2026-07-07T12:30:12.000");
  });

  it("draws nothing when there are no faults, detections or alerts", () => {
    expect(caseTimeline([], [], [], palette)).toBeNull();
  });

  it("keeps truth a neutral backdrop and our output the status red", () => {
    const spec = caseTimeline(spans("2026-07-07T12:00:00Z", "2026-07-07T12:10:00Z"), [], [], palette);
    const range = (
      spec?.["encoding"] as { color: { scale: { range: string[] } } }
    ).color.scale.range;
    expect(range).toEqual([palette.truth, palette.critical, palette.critical]);
  });
});

describe("the severity bar", () => {
  const alert = (severity: string): PrioritizedAlert =>
    ({
      asset_id: "pump-02",
      severity,
      priority_score: 62,
      threat_class: "unknown",
      rationale: "x",
      detection_ids: ["a"],
      status: "new",
      window_start: "2026-07-07T12:00:00Z",
      window_end: "2026-07-07T12:00:00Z",
    }) as PrioritizedAlert;

  it("fixes the x order so the axis cannot re-scale between runs", () => {
    const spec = severityBar([alert("high")], palette);
    const sort = (spec?.["encoding"] as { x: { sort: string[] } }).x.sort;
    expect(sort).toEqual(["low", "medium", "high", "critical", "emergency"]);
  });

  it("is hidden entirely when there are no alerts", () => {
    expect(severityBar([], palette)).toBeNull();
  });

  it("uses one hue: identity is on the axis labels", () => {
    const spec = severityBar([alert("high")], palette);
    expect((spec?.["mark"] as { color: string }).color).toBe(palette.series);
  });
});
