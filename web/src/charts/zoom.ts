import type { ChartSpec } from "@/charts/specs";

/**
 * Zoom and pan for any chart, added in one place (`components/Chart.tsx`) rather than in each
 * builder, so a chart written tomorrow is zoomable without anyone remembering to make it so.
 *
 * Vega-Lite's own mechanism: an interval selection bound to the scales. Scroll zooms, drag
 * pans, double-click resets, and Vega-Lite clips the marks to the plot once a scale is bound.
 * It works only on CONTINUOUS position axes — a time or a number — so the zoomable channels
 * are read off the spec: a time series zooms on both axes, a bar chart of counts per category
 * zooms on its count axis alone, and a pie has no position axis and is returned unchanged.
 */

/** The selection's name; `zoomable` checks for it so a spec is never zoomed twice. */
export const ZOOM_PARAM = "zoom";

type Channel = "x" | "y";
type Unit = Record<string, unknown>;

function isContinuous(definition: unknown): boolean {
  if (typeof definition !== "object" || definition === null) return false;
  const { type, bin } = definition as { type?: unknown; bin?: unknown };
  // A binned axis is drawn as discrete bins; Vega-Lite refuses to bind its scale.
  return (type === "temporal" || type === "quantitative") && !bin;
}

function encodingOf(unit: Unit): Record<string, unknown> {
  const encoding = unit.encoding;
  return typeof encoding === "object" && encoding !== null ? (encoding as Record<string, unknown>) : {};
}

/** The continuous x/y channels of a unit spec or of the first layer of a layered one. */
export function zoomChannels(spec: ChartSpec): Channel[] {
  const shared = encodingOf(spec);
  const first = Array.isArray(spec.layer) ? (spec.layer[0] as Unit | undefined) : undefined;
  const own = first ? encodingOf(first) : {};
  return (["x", "y"] as const).filter((channel) => isContinuous(own[channel] ?? shared[channel]));
}

/** The spec with zoom/pan on its continuous axes, or the spec itself when it has none. */
export function withZoom(spec: ChartSpec): ChartSpec {
  const encodings = zoomChannels(spec);
  if (encodings.length === 0 || isZoomable(spec)) return spec;
  const param = { name: ZOOM_PARAM, select: { type: "interval", encodings }, bind: "scales" };
  // A selection lives on a unit. In a layer it goes on the first, and the layers' shared
  // scales carry the zoom to the detection bands and rules drawn over the line.
  if (Array.isArray(spec.layer)) {
    const [first, ...rest] = spec.layer as Unit[];
    if (first === undefined) return spec;
    const params = Array.isArray(first.params) ? first.params : [];
    return { ...spec, layer: [{ ...first, params: [...params, param] }, ...rest] };
  }
  const params = Array.isArray(spec.params) ? spec.params : [];
  return { ...spec, params: [...params, param] };
}

/** Whether a spec already carries the zoom selection (at the top or on its first layer). */
export function isZoomable(spec: ChartSpec): boolean {
  const named = (params: unknown) =>
    Array.isArray(params) && params.some((p) => (p as { name?: unknown }).name === ZOOM_PARAM);
  const first = Array.isArray(spec.layer) ? (spec.layer[0] as Unit | undefined) : undefined;
  return named(spec.params) || (first !== undefined && named(first.params));
}
