/**
 * The Evaluate page's pure logic: sweep values, spec composition, labelling.
 *
 * Everything here is a port of a named function in `dashboard/app.py`, kept out of the
 * components so the load-bearing semantics can be unit-tested directly.
 */
import type { EvalEntry } from "@/lib/api/client";
import type { ModuleInfo } from "@/lib/api/types";

export const NO_SWEEP = "(no sweep)";

/**
 * Modes for a detector that can load a cached fit (ADR-0022). Two exclusive choices rather
 * than one form, because an artifact is atomic: its weights are aligned to the features its
 * own hyperparameters produced, so editing one knob while keeping the weights is refused at
 * construction. Offering the modes separately means the UI never builds that request.
 */
export const TRAINED = "Use trained weights";
export const SCRATCH = "Fit from scratch with my hyperparameters";

/**
 * Which artifact to prefill per detector, so the page is runnable on arrival.
 *
 * A UI convenience, not a contract: the *factory* default is deliberately `null` (no
 * artifact means untrained), so there is nothing on the wire to prefill from. A blank box is
 * still a legal state — it is reported as a blocker rather than silently building an
 * untrained detector. This table disappears the day `/eval/detectors` enumerates each
 * adapter's shipped artifacts, which is the real fix.
 */
export const DEFAULT_ARTIFACT: Record<string, string> = { team3_zscore: "skab_zscore" };

/**
 * The fit protocols, in the order the radio shows them, with their help text verbatim.
 *
 * There is deliberately no `holdout`: no bundle format can carry a holdout recording, so the
 * option could only ever produce a table of zeros. Keyed off this table rather than off the
 * server's enum so the page keeps its own help text, and a protocol added to the enum and
 * not here is simply not offered — the safe direction.
 */
export const PROTOCOL_HELP: Record<string, string> = {
  none: "Judge with each detector exactly as configured (its shipped artifact).",
  prefix:
    "Relearn 'normal' from a reserved head of each asset's clock, which is then NOT scored — " +
    "what a real deployment does. Detectors that cannot train are used as configured and say so.",
};

export type SweptValue = boolean | number | string;

/**
 * Read one swept value as the narrowest type it fits: bool, int, float, then string.
 *
 * Ported from `eval/sweep.py: _scalar`. Typing each value is what makes a label read
 * `window_size=20` rather than `window_size='20'`, and what lets a bool actually reach a
 * bool parameter. The Python original is *shared* between the CLI and the old page so the
 * two could not disagree; across languages that sharing is impossible, so the port is
 * deliberately literal and pinned by tests.
 */
function scalar(text: string): SweptValue {
  const lowered = text.trim().toLowerCase();
  if (lowered === "true" || lowered === "false") return lowered === "true";
  const trimmed = text.trim();
  if (/^[+-]?\d+$/.test(trimmed)) return Number.parseInt(trimmed, 10);
  const asNumber = Number(trimmed);
  if (trimmed !== "" && Number.isFinite(asNumber)) return asNumber;
  return trimmed;
}

/** `"20, 60,120"` -> `[20, 60, 120]`; blank items are dropped, not sent as empty strings. */
export function sweepValues(text: string): SweptValue[] {
  return text
    .split(",")
    .filter((part) => part.trim() !== "")
    .map(scalar);
}

/** How many benchmarks one entry costs: one per swept value, else one. */
export function runCount(sweep: Record<string, readonly unknown[]>): number {
  const lengths = Object.values(sweep).map((values) => values.length);
  return lengths.length === 0 ? 1 : lengths.reduce((total, length) => total * length, 1);
}

/**
 * Which registered normalizer accepts this format — asked of the registry, not assumed.
 *
 * Follows the capability declarations the same way the compose page does, so a new
 * normalizer becomes usable here the moment it is registered.
 */
export function normalizerFor(sourceFormat: string, normalizers: readonly ModuleInfo[]): string {
  const sorted = [...normalizers].sort((a, b) => a.name.localeCompare(b.name));
  return sorted.find((item) => item.formats.includes(sourceFormat))?.name ?? "";
}

/**
 * One spec from one *entry* — whose detectors run together, in a single pipeline.
 *
 * The unit of comparison is the entry, not the detector. One detector per entry is
 * head-to-head; several detectors in ONE spec pool their detections into a single
 * alert-engine run, which is what produces cross-lane corroboration — a shape that was
 * unreachable from the UI at any setting while a spec held exactly one detector.
 *
 * The sources come from **the corpus's own declared shape**, one per position. Hardcoding a
 * single stub-csv source was correct only while every corpus was single-lane telemetry: a
 * benchmark checks a case's inputs against the spec's `sources` positionally and by format,
 * so a flow or multi-lane corpus would appear in the picker and then be refused on run.
 *
 * This composes a spec from what is on screen, which is right for an exploratory comparison
 * and *wrong* for reproducing a committed benchmark: it carries no `assets:` block and no
 * source or engine parameters. Reproducing a committed benchmark is its own mode, fed by
 * `/eval/specs` — this stays what it always was, and the page shows the spec it built either
 * way.
 */
export function composedSpec(
  detectors: readonly { name: string; params: Record<string, unknown> }[],
  engine: string,
  sourceFormats: readonly string[],
  normalizers: readonly ModuleInfo[],
): Record<string, unknown> {
  return {
    sources: sourceFormats.map((sourceFormat) => ({
      normalizer: normalizerFor(sourceFormat, normalizers),
      source_format: sourceFormat,
    })),
    detectors: detectors.map((detector) => ({ ...detector })),
    alert_engine: { name: engine },
  };
}

/**
 * Drop empty entries and make the surviving labels unique (E8).
 *
 * A label is a spec's identity in the report, and two entries holding the same detectors
 * would otherwise produce two rows with one name — legitimate to want (the same detector
 * under two settings is a real experiment) but unreadable unless they are told apart. An
 * entry with nothing selected is a transient UI state, not an error, so it is dropped rather
 * than blocking the run.
 */
export function labelled<T extends { label: string; detectors: unknown[] }>(entries: readonly T[]): T[] {
  const kept = entries.filter((entry) => entry.detectors.length > 0);
  const seen = new Map<string, number>();
  return kept.map((entry) => {
    const count = (seen.get(entry.label) ?? 0) + 1;
    seen.set(entry.label, count);
    return count > 1 ? { ...entry, label: `${entry.label} (${count})` } : entry;
  });
}

/** The stable identity of a benchmark request — the latch key and the cache key (E10). */
export function selectionKey(corpus: string, entries: readonly EvalEntry[], protocol: string): string {
  return JSON.stringify([corpus, entries, protocol]);
}
