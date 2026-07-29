import { describe, expect, it } from "vitest";

import { formatCell, formatNumber, formatUtc, utcInstant } from "./format";

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

  it("feeds Vega the true instant, zone and all", () => {
    // The predecessor stripped the zone so Vega's local parse would reproduce the UTC digits.
    // That is an identity only where the local wall-clock exists, so the chart is fed the real
    // instant now and the specs ask Vega for UTC explicitly.
    expect(utcInstant("2026-07-07T15:00:00+03:00")).toBe("2026-07-07T12:00:00.000Z");
    expect(utcInstant(null)).toBeNull();
  });

  it("survives the DST spring-forward gap, where the wall-clock trick could not", () => {
    // 03:30 UTC on 2026-03-29 is a local time that does not exist in Europe/Nicosia: the
    // clocks jump 03:00 -> 04:00. ECMA-262 resolves a skipped local time with the
    // *pre*-transition offset, so re-reading a zone-less "…T03:30:00.000" came back an hour
    // late and the axis disagreed with the table beside it. An instant has no such gap.
    const instant = utcInstant("2026-03-29T03:30:00Z");
    expect(instant).toBe("2026-03-29T03:30:00.000Z");
    expect(new Date(instant as string).getUTCHours()).toBe(3);
    expect(new Date(instant as string).getUTCMinutes()).toBe(30);
    expect(formatUtc(instant)).toBe("2026-03-29 03:30:00");
  });
});

describe("generic cells", () => {
  it("gives a mapping's undefined values the same em-dash every other undefined gets", () => {
    // `lane_recall` is `{"telemetry": 1.0, "flow": null}`. JSON.stringify printed a literal
    // `null` here, which is the one claim the table's em-dash convention exists to prevent:
    // on a clean recording recall is undefined, not zero and not the word "null".
    expect(formatCell({ telemetry: 1, flow: null })).toBe("telemetry 1, flow —");
    expect(formatCell({})).toBe("");
    // A nested mapping is not cell-shaped and still stringifies; it gets its own table.
    expect(formatCell({ telemetry: { hits: 1 } })).toBe('{"telemetry":{"hits":1}}');
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
