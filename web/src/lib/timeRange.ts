/**
 * The Monitor pages' optional date-time range, kept in the URL (`?from=&to=`) so a filtered
 * view is linkable and survives a refresh, like every other choice in this app
 * (`useUrlChoice`, `useDayRange`).
 *
 * Both bounds are UTC wall-clock minutes in the `datetime-local` shape (`2026-07-07T12:00`).
 * The browser's `datetime-local` input carries no zone, and every Monitor page states "UTC
 * throughout", so the value is read as UTC by construction — `toInstant` appends the `Z`
 * rather than letting `new Date()` read it in the viewer's zone (golden rule 4 / H4).
 *
 * Each bound is optional: an open side means "no limit", which is also what an unparseable
 * URL value means, so a hand-edited link degrades to a wider view rather than an error.
 *
 * Filtering is asked of the server, never applied to the fetched page: every list is capped,
 * so a client-side filter over the cap would silently miss what lies beyond it.
 */
import { useSearchParams } from "react-router-dom";

const PATTERN = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;

export interface TimeRange {
  /** Inclusive lower bound, `YYYY-MM-DDTHH:MM` UTC, or null for open. */
  readonly from: string | null;
  /** Upper bound, `YYYY-MM-DDTHH:MM` UTC, or null for open. */
  readonly to: string | null;
}

export const OPEN_RANGE: TimeRange = { from: null, to: null };

const valid = (value: string | null): string | null =>
  value !== null && PATTERN.test(value) && !Number.isNaN(Date.parse(`${value}:00Z`)) ? value : null;

/** `2026-07-07T12:00` → `2026-07-07T12:00:00Z` — explicitly UTC, never the viewer's zone. */
export const toInstant = (value: string): string => `${value}:00Z`;

export const isOpen = (range: TimeRange): boolean => range.from === null && range.to === null;

/** Whether the bounds contradict each other, so nothing can match. */
export const isInverted = (range: TimeRange): boolean =>
  range.from !== null && range.to !== null && range.from > range.to;

/** The `start`/`end` query params of the read API, or nothing for an open side. */
export function rangeQuery(range: TimeRange): { start?: string; end?: string } {
  return {
    ...(range.from ? { start: toInstant(range.from) } : {}),
    ...(range.to ? { end: toInstant(range.to) } : {}),
  };
}

export function useTimeRange(): [TimeRange, (range: TimeRange) => void] {
  const [params, setParams] = useSearchParams();
  const range: TimeRange = { from: valid(params.get("from")), to: valid(params.get("to")) };
  const set = (next: TimeRange) => {
    const updated = new URLSearchParams(params);
    for (const [key, value] of [
      ["from", next.from],
      ["to", next.to],
    ] as const) {
      if (valid(value)) updated.set(key, value as string);
      else updated.delete(key);
    }
    setParams(updated, { replace: true });
  };
  return [range, set];
}
