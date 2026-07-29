/**
 * The chart palette, ported from `dashboard/charts.py` (V1).
 *
 * These are not styling choices to re-make: single-hue for magnitude comparisons (identity
 * rides on the axis, so a second hue would add noise, not information), a **reserved**
 * status red that means "our detector said something" and never a data series, and ground
 * truth as a deliberate neutral — a reference backdrop like the grid, not a series colour.
 * The pair was CVD-validated (worst ΔE 75.7) and every chart keeps a caption or table
 * beside it, so nothing depends on colour perception alone.
 *
 * The light values are the Python file's exactly. Dark is the same *roles* re-instanced for
 * a dark ramp — #2a78d6 on near-black paper falls under WCAG AA, and X6 requires both
 * themes to be legible. Roles, not hexes, are what the invariant is about.
 */
export interface Palette {
  /** Categorical slot 1 — the single data series. */
  readonly series: string;
  /** Status: critical. Reserved for detections/alerts. Never a series. */
  readonly critical: string;
  /** Ground truth: a reference element, deliberately not a series hue. */
  readonly truth: string;
  readonly grid: string;
  readonly muted: string;
  readonly baseline: string;
  /** Point halo — the paper colour, so overlapping dots stay countable. */
  readonly halo: string;
}

export const LIGHT_PALETTE: Palette = {
  series: "#2a78d6",
  critical: "#d03b3b",
  truth: "#b8b6ac",
  grid: "#e1e0d9",
  muted: "#898781",
  baseline: "#c3c2b7",
  halo: "#fcfcfb",
};

export const DARK_PALETTE: Palette = {
  series: "#5aa0f0",
  critical: "#e66c6c",
  truth: "#6b6862",
  grid: "#383630",
  muted: "#8d8a82",
  baseline: "#4a4842",
  halo: "#1d1c19",
};

export const paletteFor = (theme: "light" | "dark"): Palette =>
  theme === "dark" ? DARK_PALETTE : LIGHT_PALETTE;
