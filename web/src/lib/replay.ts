/**
 * Replay: play a range of stored results forward in time, one bucket per step (ADR-0044
 * amendment). Not a live feed — nothing here asks the server anything; it reveals counts the
 * page already holds, in time order, so a past week can be watched as it happened. The app
 * polls nothing (H5), and a replay is what that rule leaves room for.
 *
 * `cursor` is the index of the last bucket shown, or null when not replaying — which is the
 * ordinary static page, everything shown. A replay that reaches the end lingers one step on the
 * full picture and then returns to null, so a finished replay leaves the normal page behind.
 */
import * as React from "react";

export const SPEEDS = [1, 2, 4] as const;
export type Speed = (typeof SPEEDS)[number];

/** One step at 1×. A week by hour is 168 steps: about 50 s at 1×, 13 s at 4×. */
export const STEP_MS = 300;

export interface Replay {
  /** Index of the last bucket shown; null when not replaying (the full, static view). */
  readonly cursor: number | null;
  readonly playing: boolean;
  readonly speed: Speed;
  /** The bucket the replay has reached, or null when not replaying. */
  readonly now: string | null;
  readonly steps: number;
  readonly play: () => void;
  readonly pause: () => void;
  readonly reset: () => void;
  readonly setSpeed: (speed: Speed) => void;
}

function prefersReducedMotion(): boolean {
  return typeof window !== "undefined" && window.matchMedia?.("(prefers-reduced-motion: reduce)").matches === true;
}

export function useReplay(buckets: readonly string[]): Replay {
  const [cursor, setCursor] = React.useState<number | null>(null);
  const [playing, setPlaying] = React.useState(false);
  const [speed, setSpeed] = React.useState<Speed>(1);
  const key = buckets.join("|");

  // A new range is a new story: stop and show it whole.
  React.useEffect(() => {
    setPlaying(false);
    setCursor(null);
  }, [key]);

  React.useEffect(() => {
    if (!playing) return;
    // Reduced motion: the same replay, in fewer, calmer steps.
    const interval = (STEP_MS * (prefersReducedMotion() ? 3 : 1)) / speed;
    const timer = setInterval(() => {
      setCursor((current) => {
        const next = current === null ? 0 : current + 1;
        if (next >= buckets.length) {
          setPlaying(false);
          return null; // finished: back to the ordinary, complete page
        }
        return next;
      });
    }, interval);
    return () => clearInterval(timer);
  }, [playing, speed, buckets.length]);

  return {
    cursor,
    playing,
    speed,
    now: cursor === null ? null : (buckets[cursor] ?? null),
    steps: buckets.length,
    play: () => {
      if (buckets.length === 0) return;
      setCursor((current) => current ?? 0);
      setPlaying(true);
    },
    pause: () => setPlaying(false),
    reset: () => {
      setPlaying(false);
      setCursor(null);
    },
    setSpeed,
  };
}

/** Whether a bucket has "arrived" at this point of the replay (always, when not replaying). */
export const arrived = (bucket: string, now: string | null): boolean => now === null || bucket <= now;
