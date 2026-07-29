import { VegaLite } from "react-vega";

import { useTheme } from "@/components/ThemeToggle";
import { paletteFor, type Palette } from "@/charts/palette";
import type { ChartSpec } from "@/charts/specs";

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
}: {
  readonly spec: ChartSpec;
  readonly caption?: React.ReactNode;
  readonly label: string;
}) {
  return (
    <figure className="min-w-0">
      <div className="w-full" role="img" aria-label={label}>
        <VegaLite spec={spec} actions={false} renderer="svg" className="w-full" />
      </div>
      {caption ? <figcaption className="caption mt-2">{caption}</figcaption> : null}
    </figure>
  );
}
