/**
 * "From / To (UTC)" for the Monitor pages — see `lib/timeRange.ts` for the semantics.
 *
 * `basis` names which clock the range is applied to, because it differs per page and the
 * difference matters: an alert is matched on its window start, a reading on its timestamp, a
 * malformed record on when it was ingested (it has no timestamp of its own).
 */
import { Button, Field, TextInput } from "@/components/ui/controls";
import { isInverted, isOpen, OPEN_RANGE, type TimeRange } from "@/lib/timeRange";

export function TimeRangeFilter({
  range,
  onChange,
  basis,
  idPrefix,
}: {
  readonly range: TimeRange;
  readonly onChange: (range: TimeRange) => void;
  /** What the bounds are compared against, shown behind the (i). */
  readonly basis: React.ReactNode;
  /** Keeps input ids unique where a page also renders another filter row. */
  readonly idPrefix: string;
}) {
  return (
    <div className="flex flex-wrap items-end gap-3" data-testid="time_range">
      <Field label="From (UTC)" htmlFor={`${idPrefix}_from`} info={basis}>
        <TextInput
          id={`${idPrefix}_from`}
          type="datetime-local"
          value={range.from ?? ""}
          max={range.to ?? undefined}
          onChange={(event) => onChange({ ...range, from: event.target.value || null })}
        />
      </Field>
      <Field label="To (UTC)" htmlFor={`${idPrefix}_to`} info={basis}>
        <TextInput
          id={`${idPrefix}_to`}
          type="datetime-local"
          value={range.to ?? ""}
          min={range.from ?? undefined}
          onChange={(event) => onChange({ ...range, to: event.target.value || null })}
        />
      </Field>
      <Button size="sm" variant="ghost" disabled={isOpen(range)} onClick={() => onChange(OPEN_RANGE)}>
        Clear range
      </Button>
      {isInverted(range) ? (
        <p className="caption pb-2" role="status">
          “From” is after “To”, so nothing can match.
        </p>
      ) : null}
    </div>
  );
}
