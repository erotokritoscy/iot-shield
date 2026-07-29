/**
 * Adapter parameters: how a form says **"not given"** (C3, C4).
 *
 * A form must be able to say "not given", and a widget cannot — it hands back a value
 * whether or not anyone touched it. Sending all of them makes every run an explicit request
 * for every parameter, which is wrong wherever "not given" carries meaning, and it does, in
 * three different ways: a preset owns the real default (ADR-0024 D3), a learning adapter
 * reads it from its artifact, or an absent value means "learn it from the data" (ADR-0022).
 *
 * The clearest case: an untouched `team3_zscore` row that submitted its whole prefilled
 * hyperparameter block alongside the artifact id is exactly the half-configured request
 * ADR-0022 D4 refuses — so picking that detector and pressing run could not succeed.
 *
 * Comparing against the *reported* default is what lets the page express absence, and it is
 * faithful: a value equal to the default produces an identical adapter either way.
 */
import type { ParamInfo } from "./api/types";

/** What a widget holds. Numerics are kept as raw text so "empty" stays representable. */
export type ParamValue = string | boolean;
export type ParamValues = Readonly<Record<string, ParamValue>>;

export const isNumeric = (param: ParamInfo): boolean =>
  param.type === "int" || param.type === "float";

/**
 * The value a widget opens on, from what the catalogue reported.
 *
 * A numeric whose own default is `None` is *optional*: it renders **empty** and is omitted
 * when untouched. Coercing `None` to 0 instead would silently submit a value the adapter
 * never offered — and for a param whose real default is preset-owned (flow_burst's window
 * and threshold), that fabricated 0 is refused at construction, turning "pick a detector and
 * press run" into a failed run.
 */
export function initialValue(param: ParamInfo): ParamValue {
  if (param.choices && param.choices.length > 0) {
    const wanted = param.default === null || param.default === undefined ? null : String(param.default);
    // An OPTIONAL closed vocabulary opens on "not given", like every other optional param.
    // Preselecting choices[0] fabricated a value the operator never chose and — because
    // `isUntouched` only recognises "" as untouched when the default is null — submitted it.
    // Live since /modules started reporting choices for optional Literals: `meter.meter_type`
    // publishes ["water","electricity"] with default null, and picking a source dialect then
    // silently pinned the utility. Harmless only while both dialects happen to default to
    // water; the first adapter whose optional default is not choices[0] would be overridden
    // with nothing on screen saying so (ADR-0033).
    if (wanted === null) return "";
    return param.choices.includes(wanted) ? wanted : (param.choices[0] as string);
  }
  if (param.type === "bool") return Boolean(param.default);
  if (param.default === null || param.default === undefined) return "";
  return String(param.default);
}

export function initialValues(params: readonly ParamInfo[] | null | undefined): ParamValues {
  const values: Record<string, ParamValue> = {};
  for (const param of params ?? []) values[param.name] = initialValue(param);
  return values;
}

/**
 * Whether the operator left this widget at what the catalogue reported.
 *
 * Ported from `_untouched`, including its string-compare fallback. Strict `===` rather than
 * JS loose equality on purpose: `false == ""` is `true` in JavaScript and `False == ""` is
 * `False` in Python, so a loose comparison here would silently change which parameters a
 * run declares.
 */
export function isUntouched(value: unknown, param: ParamInfo): boolean {
  if (param.default === null || param.default === undefined) {
    return value === null || value === undefined || value === "";
  }
  return value === param.default || String(value) === String(param.default);
}

/**
 * The params block a spec carries: only what the operator actually moved.
 *
 * Numerics arrive as widget text and leave as numbers, so `40` reaches the API as `40` and
 * not `"40"`; an empty numeric is omitted entirely rather than becoming `0`.
 */
export function submittedParams(
  params: readonly ParamInfo[] | null | undefined,
  values: ParamValues,
): Record<string, unknown> {
  const submitted: Record<string, unknown> = {};
  for (const param of params ?? []) {
    const raw = values[param.name];
    if (raw === undefined) continue;
    if (isNumeric(param)) {
      const text = String(raw).trim();
      if (text === "") continue; // optional numeric left empty: omitted, never coerced to 0
      const parsed = param.type === "int" ? Number.parseInt(text, 10) : Number.parseFloat(text);
      if (!Number.isFinite(parsed)) continue;
      if (!isUntouched(parsed, param)) submitted[param.name] = parsed;
      continue;
    }
    if (!isUntouched(raw, param)) submitted[param.name] = raw;
  }
  return submitted;
}

/** The numeric knobs a sweep can vary: a preset or a channel list has no axis (E7). */
export const sweepable = (params: readonly ParamInfo[]): ParamInfo[] => params.filter(isNumeric);
