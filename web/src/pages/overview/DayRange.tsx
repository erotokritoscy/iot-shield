/**
 * The overview pages' range of whole UTC days, kept in the URL (`?start=&end=`) so a view is
 * linkable and survives a refresh, like every other choice in this app (`useUrlChoice`).
 *
 * It opens on the last seven days. Historical data (SensiPro's export is 2023) would then be
 * an empty page with no hint where to look, so when the range holds nothing the server's
 * `latest_day` is offered as a one-click jump to the week ending there. Only then: offered
 * beside a range that has data, it reads as "you are looking at the wrong thing".
 */
import { useSearchParams } from "react-router-dom";

import { Button, Field, TextInput } from "@/components/ui/controls";
import type { DayRange } from "@/lib/api/client";

const DAY_MS = 86_400_000;
const PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export const isoDay = (at: number): string => new Date(at).toISOString().slice(0, 10);

/** The week ending on `day` (inclusive). */
export function weekEnding(day: string): DayRange {
  const end = Date.parse(`${day}T00:00:00Z`);
  return { start: isoDay(end - 6 * DAY_MS), end: day };
}

export function useDayRange(): [DayRange, (range: DayRange) => void] {
  const [params, setParams] = useSearchParams();
  const fallback = weekEnding(isoDay(Date.now()));
  const start = params.get("start") ?? "";
  const end = params.get("end") ?? "";
  const range = PATTERN.test(start) && PATTERN.test(end) && start <= end ? { start, end } : fallback;
  const set = (next: DayRange) => {
    const updated = new URLSearchParams(params);
    updated.set("start", next.start);
    updated.set("end", next.end);
    setParams(updated, { replace: true });
  };
  return [range, set];
}

export function DayRangePicker({
  range,
  onChange,
  latestDay,
  empty,
  info,
  children,
}: {
  readonly range: DayRange;
  readonly onChange: (range: DayRange) => void;
  /** The most recent day holding any data, from the server; offered when outside the range. */
  readonly latestDay: string | null | undefined;
  /** Whether the chosen range holds no data at all — the only time the jump is offered. */
  readonly empty: boolean;
  readonly info: React.ReactNode;
  /** More filters for the same row, drawn right after "To" (the OT overview's meter). */
  readonly children?: React.ReactNode;
}) {
  const outside = empty && latestDay && (latestDay < range.start || latestDay > range.end);
  return (
    <div className="flex flex-wrap items-end gap-3" data-testid="day_range">
      <Field label="From (UTC)" htmlFor="range_start" info={info}>
        <TextInput
          id="range_start"
          data-testid="range_start"
          type="date"
          value={range.start}
          max={range.end}
          onChange={(event) => PATTERN.test(event.target.value) && onChange({ ...range, start: event.target.value })}
        />
      </Field>
      <Field label="To (UTC)" htmlFor="range_end" info={info}>
        <TextInput
          id="range_end"
          data-testid="range_end"
          type="date"
          value={range.end}
          min={range.start}
          onChange={(event) => PATTERN.test(event.target.value) && onChange({ ...range, end: event.target.value })}
        />
      </Field>
      {children}
      {outside ? (
        <Button size="sm" data-testid="range_latest" onClick={() => onChange(weekEnding(latestDay))}>
          Show the week ending {latestDay}, the latest data
        </Button>
      ) : null}
    </div>
  );
}
