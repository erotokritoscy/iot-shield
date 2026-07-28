/**
 * UTC formatting — engineered, not hoped for (golden rule 4 / brief H4).
 *
 * `Date#toString`, `toLocaleString` and friends render in the *viewer's* zone, so a
 * dashboard opened in Nicosia would show 15:00 for the same instant a table in the API
 * calls 12:00. Every timestamp the app renders goes through `formatUtc`, which reads the
 * UTC components explicitly; nothing formats a Date any other way.
 */

const pad = (value: number, width = 2): string => String(value).padStart(width, "0");

/** Parse an API timestamp. Returns `null` for anything unparseable, never throws. */
export function parseInstant(value: string | number | Date | null | undefined): Date | null {
  if (value === null || value === undefined) return null;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

export type UtcPrecision = "minute" | "second" | "millisecond";

/** `2026-07-07 12:00:30` — UTC, always, whoever is looking. */
export function formatUtc(
  value: string | number | Date | null | undefined,
  precision: UtcPrecision = "second",
): string {
  const date = parseInstant(value);
  if (date === null) return "";
  const head =
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    ` ${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}`;
  if (precision === "minute") return head;
  const withSeconds = `${head}:${pad(date.getUTCSeconds())}`;
  return precision === "second"
    ? withSeconds
    : `${withSeconds}.${pad(date.getUTCMilliseconds(), 3)}`;
}

/**
 * The `_utc_wallclock` port (charts.py).
 *
 * Vega-Lite renders a *naive* datetime as-is and shifts a zoned one into the viewer's local
 * time — which would disagree with the UTC timestamps in the tables beside the chart. So we
 * hand it the UTC wall-clock with no zone: an ISO string without `Z` is parsed as local
 * time by ECMAScript, so the digits Vega draws are the digits UTC has. Same trick as the
 * Python original, same reason.
 */
export function utcWallclock(value: string | number | Date | null | undefined): string | null {
  const date = parseInstant(value);
  if (date === null) return null;
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    `T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}` +
    `.${pad(date.getUTCMilliseconds(), 3)}`
  );
}

/** A number for a dense table cell; `-` for undefined, never a fabricated 0. */
export function formatNumber(value: number | null | undefined, places = 2): string {
  return value === null || value === undefined || Number.isNaN(value)
    ? "-"
    : value.toFixed(places);
}

/** A value of unknown type for a generic table cell (per-case reports, score components). */
export function formatCell(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "number") return Number.isInteger(value) ? String(value) : value.toFixed(4);
  if (typeof value === "boolean") return value ? "true" : "false";
  if (typeof value === "string") return value;
  // A list cell reads as its contents, not as its literal. The per-case table's `notes`
  // column rendered `[]` on every clean row (pandas' repr), which is noise where the whole
  // point of the column is that a row of zeros arrives with its reason.
  if (Array.isArray(value)) return value.length === 0 ? "" : value.map(formatCell).join("; ");
  return JSON.stringify(value);
}
