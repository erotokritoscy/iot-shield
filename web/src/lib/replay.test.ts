/** The replay's clock: play, pause, reset, speed, and the return to the full page (ADR-0044). */
import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { arrived, STEP_MS, useReplay } from "./replay";

const BUCKETS = ["2023-07-01", "2023-07-02", "2023-07-03"];

describe("useReplay", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("starts still, showing everything", () => {
    const { result } = renderHook(() => useReplay(BUCKETS));
    expect(result.current.cursor).toBeNull();
    expect(result.current.now).toBeNull();
    expect(arrived("2023-07-03", result.current.now)).toBe(true);
  });

  it("steps one bucket at a time, then returns to the full page", () => {
    const { result } = renderHook(() => useReplay(BUCKETS));
    act(() => result.current.play());
    expect(result.current.now).toBe("2023-07-01");
    act(() => vi.advanceTimersByTime(STEP_MS));
    expect(result.current.now).toBe("2023-07-02");
    expect(arrived("2023-07-03", result.current.now)).toBe(false);
    act(() => vi.advanceTimersByTime(STEP_MS * 2));
    expect(result.current.playing).toBe(false);
    expect(result.current.now).toBeNull();
  });

  it("pauses, resumes where it was, and resets", () => {
    const { result } = renderHook(() => useReplay(BUCKETS));
    act(() => result.current.play());
    act(() => result.current.pause());
    act(() => vi.advanceTimersByTime(STEP_MS * 5));
    expect(result.current.now).toBe("2023-07-01");
    act(() => result.current.play());
    act(() => vi.advanceTimersByTime(STEP_MS));
    expect(result.current.now).toBe("2023-07-02");
    act(() => result.current.reset());
    expect(result.current.now).toBeNull();
    expect(result.current.playing).toBe(false);
  });

  it("goes faster at a higher speed", () => {
    const { result } = renderHook(() => useReplay(BUCKETS));
    act(() => result.current.setSpeed(4));
    act(() => result.current.play());
    act(() => vi.advanceTimersByTime(STEP_MS / 4));
    expect(result.current.now).toBe("2023-07-02");
  });

  it("stops when the range changes", () => {
    const { result, rerender } = renderHook(({ buckets }) => useReplay(buckets), {
      initialProps: { buckets: BUCKETS },
    });
    act(() => result.current.play());
    rerender({ buckets: ["2023-08-01"] });
    expect(result.current.now).toBeNull();
    expect(result.current.playing).toBe(false);
  });
});
