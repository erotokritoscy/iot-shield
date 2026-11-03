import { describe, expect, it } from "vitest";

import { isInverted, isOpen, rangeQuery, toInstant } from "./timeRange";

describe("the Monitor time range", () => {
  it("reads a datetime-local value as UTC, never the viewer's zone", () => {
    // `new Date("2026-07-07T12:00")` would be local time; the `Z` is what makes it UTC.
    expect(toInstant("2026-07-07T12:00")).toBe("2026-07-07T12:00:00Z");
  });

  it("sends only the sides that are set", () => {
    expect(rangeQuery({ from: null, to: null })).toEqual({});
    expect(rangeQuery({ from: "2026-07-07T12:00", to: null })).toEqual({
      start: "2026-07-07T12:00:00Z",
    });
    expect(rangeQuery({ from: null, to: "2026-07-08T00:00" })).toEqual({
      end: "2026-07-08T00:00:00Z",
    });
  });

  it("knows an open range and an inverted one", () => {
    expect(isOpen({ from: null, to: null })).toBe(true);
    expect(isOpen({ from: "2026-07-07T12:00", to: null })).toBe(false);
    expect(isInverted({ from: "2026-07-08T00:00", to: "2026-07-07T00:00" })).toBe(true);
    expect(isInverted({ from: "2026-07-07T00:00", to: "2026-07-07T00:00" })).toBe(false);
    expect(isInverted({ from: "2026-07-08T00:00", to: null })).toBe(false);
  });
});
