import { describe, expect, it } from "vitest";

import { formatNumber, formatUtc, utcWallclock } from "./format";

describe("UTC rendering", () => {
  it("formats an instant in UTC whatever the viewer's zone", () => {
    // 15:00 in a +03:00 zone is 12:00 UTC — the digits every table in this app must show
    // (golden rule 4). `Date#toLocaleString` would answer with the viewer's zone instead.
    expect(formatUtc("2026-07-07T15:00:00+03:00")).toBe("2026-07-07 12:00:00");
    expect(formatUtc("2026-07-07T15:00:00+03:00", "minute")).toBe("2026-07-07 12:00");
  });

  it("renders nothing for a missing or unparseable timestamp", () => {
    expect(formatUtc(null)).toBe("");
    expect(formatUtc("not a date")).toBe("");
  });

  it("feeds Vega the UTC wall-clock with no zone", () => {
    // The `_utc_wallclock` port: Vega-Lite renders a naive datetime as-is and shifts a zoned
    // one into local time, which would disagree with the tables beside the chart.
    const wall = utcWallclock("2026-07-07T15:00:00+03:00");
    expect(wall).toBe("2026-07-07T12:00:00.000");
    expect(wall).not.toMatch(/Z$/);
    // ECMAScript parses a zone-less date-time as *local*, so the digits Vega draws are the
    // digits UTC has, whatever zone the browser is in.
    const parsed = new Date(wall as string);
    expect(parsed.getHours()).toBe(12);
    expect(parsed.getMinutes()).toBe(0);
  });
});

describe("numbers", () => {
  it("renders an undefined value as a dash, never as zero", () => {
    expect(formatNumber(null)).toBe("-");
    expect(formatNumber(undefined)).toBe("-");
    expect(formatNumber(0)).toBe("0.00");
    expect(formatNumber(62, 1)).toBe("62.0");
  });
});
