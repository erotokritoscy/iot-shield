import { describe, expect, it } from "vitest";

import { newProgressId, progressFraction, progressText } from "@/pages/compose/RunProgress";

describe("run progress (ADR-0051)", () => {
  it("moves through reading and judging by their counts and holds while saving", () => {
    expect(progressFraction(null)).toBeNull();
    expect(progressFraction({ phase: "starting", done: 0, total: null, elapsed_s: 0 })).toBe(0);
    expect(progressFraction({ phase: "reading", done: 0, total: 100, elapsed_s: 1 })).toBeCloseTo(0.02);
    expect(progressFraction({ phase: "reading", done: 100, total: 100, elapsed_s: 1 })).toBeCloseTo(0.6);
    expect(progressFraction({ phase: "judging", done: 50, total: 100, elapsed_s: 1 })).toBeCloseTo(0.775);
    expect(progressFraction({ phase: "saving", done: 0, total: null, elapsed_s: 1 })).toBeCloseTo(0.95);
  });

  it("never runs past a phase's end, and stays at its start without a total", () => {
    expect(progressFraction({ phase: "reading", done: 500, total: 100, elapsed_s: 1 })).toBeCloseTo(0.6);
    expect(progressFraction({ phase: "reading", done: 500, total: null, elapsed_s: 1 })).toBeCloseTo(0.02);
  });

  it("says the phase, its count and the time", () => {
    expect(progressText(null)).toBe("Starting…");
    expect(progressText({ phase: "reading", done: 1234, total: null, elapsed_s: 61.9 })).toBe(
      "Reading 1,234 rows… · 1:01 elapsed",
    );
    expect(progressText({ phase: "judging", done: 10, total: 20, elapsed_s: 5 })).toBe(
      "Judging 10 / 20 events · 0:05 elapsed",
    );
    expect(progressText({ phase: "saving", done: 0, total: null, elapsed_s: 600 })).toBe(
      "Saving the results… · 10:00 elapsed",
    );
  });

  it("mints a version-4 uuid, fresh each time", () => {
    const id = newProgressId();
    expect(id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
    expect(newProgressId()).not.toBe(id);
  });
});
