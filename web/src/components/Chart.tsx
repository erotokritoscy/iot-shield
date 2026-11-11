import { useMemo } from "react";
import { VegaLite } from "react-vega";

import { useTheme } from "@/components/ThemeToggle";
import { paletteFor, type Palette } from "@/charts/palette";
import type { ChartSpec } from "@/charts/specs";
import { isZoomable, withZoom } from "@/charts/zoom";

/** The palette for the theme currently applied — a Vega spec cannot read a CSS variable. */
export function useChartPalette(): Palette {
  return paletteFor(useTheme());
}

/**
 * A rendered chart plus its caption. The caption is not decoration: identity never rides on
 * colour alone, so every chart in this app travels with the words (or the table) that say
 * what its marks mean (V1).
 */
export function Chart({
  spec,
  caption,
  label,
  zoom = true,
}: {
  readonly spec: ChartSpec;
  readonly caption?: React.ReactNode;
  readonly label: string;
  /** Off for a chart that should stay fixed (Triage's severity counts). */
  readonly zoom?: boolean;
}) {
  // Every chart zooms on whatever continuous axes it has (`charts/zoom.ts`); a pie has none.
  const zoomed = useMemo(() => (zoom ? withZoom(spec) : spec), [spec, zoom]);
  const zoomable = isZoomable(zoomed);
  return (
    <figure className="min-w-0">
      <div className="w-full" role="img" aria-label={label}>
        <VegaLite spec={zoomed} actions={false} renderer="svg" className="w-full" />
      </div>
      {caption || zoomable ? (
        <figcaption className="caption mt-2">
          {caption}
          {zoomable ? (
            <span className="block" data-testid="chart_zoom_hint">
              Scroll to zoom, drag to pan, double-click to reset.
            </span>
          ) : null}
        </figcaption>
      ) : null}
    </figure>
  );
}
