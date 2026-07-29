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
 * The instant a chart is fed — a real point in time, offset and all.
 *
 * The predecessor (`_utc_wallclock`, ported from charts.py) handed Vega the UTC wall-clock
 * with the zone *stripped*, relying on ECMAScript parsing a zone-less ISO string as local
 * time so the digits drawn would be the digits UTC has. That round trip is an identity only
 * where the local wall-clock time exists — and once a year, it does not. In the DST
 * spring-forward gap ECMA-262 resolves a skipped local time using the *pre*-transition
 * offset, so a Europe/Nicosia viewer looking at `2026-03-29T03:30:00Z` saw a chart axis
 * reading 04:30 beside a table reading 03:30. One hour a year, and silent.
 *
 * So the lie is gone: charts get the true instant, and the specs ask Vega for UTC explicitly
 * — `scale: {type: "utc"}` for axes, `timeUnit: "utc…"` for tooltips, because a UTC scale
 * governs the axis and does not reach tooltip formatting. H4 says every rendered timestamp
 * is UTC *by construction*, and that is what construction looks like here.
 */
export function utcInstant(value: string | number | Date | null | undefined): string | null {
  const date = parseInstant(value);
  if (date === null) return null;
  // Hand-built rather than `toISOString()`, which produces the identical string ~50% slower
  // in V8 — measurable at 5,000 telemetry points per spec build, and free to avoid.
  return (
    `${date.getUTCFullYear()}-${pad(date.getUTCMonth() + 1)}-${pad(date.getUTCDate())}` +
    `T${pad(date.getUTCHours())}:${pad(date.getUTCMinutes())}:${pad(date.getUTCSeconds())}` +
    `.${pad(date.getUTCMilliseconds(), 3)}Z`
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
  if (Array.isArray(value)) return value.map(formatCell).join("; ");
  // A flat mapping — `lane_recall` is `{"telemetry": 1.0, "flow": null}` — reads as its
  // pairs. `JSON.stringify` would print a literal `null` here, which is exactly the claim
  // the em-dash convention exists to avoid: on a clean recording recall is *undefined*, not
  // zero and not the word "null". Nested mappings still stringify; they are not cell-shaped
  // and belong in their own table.
  if (isFlatRecord(value)) {
    return Object.entries(value)
      .map(([key, item]) => `${key} ${item === null || item === undefined ? "—" : formatCell(item)}`)
      .join(", ");
  }
  return JSON.stringify(value);
}

/** A one-level object of scalars — the shape a table cell can honestly hold. */
export function isFlatRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  return Object.values(value).every(
    (item) => item === null || ["number", "string", "boolean", "undefined"].includes(typeof item),
  );
}
