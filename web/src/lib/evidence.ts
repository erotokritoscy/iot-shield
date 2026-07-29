/**
 * The read side of `enrichment.py` — the typed records that ride inside `evidence`.
 *
 * `DetectionResult.evidence` is `dict[str, Any]` server-side and therefore `unknown` here:
 * it is the one payload in this app with no generated type, because its *shape is
 * per-detector by design* (ADR-0032 D7 — a z-score, a feature attribution and a peer count
 * are genuinely different things, and flattening them into one schema would serve this
 * dashboard and nobody else). So the app dumps it as JSON, which is right for the
 * per-detector half.
 *
 * Two keys are not per-detector, and those are what this module reads:
 *
 * - **`explanation`** — one analyst-readable sentence, which the *port suite* requires of
 *   every detector. That law is what makes it safe to render as a heading.
 * - **`enrichment`** — the threat claim, whose schema `enrichment.py` owns.
 *
 * Mirroring the Python side's discipline: the untyped hop is exactly one module wide, the
 * key names appear here and nowhere else in `web/`, and a payload that does not parse reads
 * as *absent* rather than throwing — this is a drill-down panel, and a detector that
 * predates ADR-0032 (or a future one that writes something else) must degrade to the older
 * rendering rather than blanking an analyst's evidence view.
 */
import { z } from "zod";

import type { DetectionResult } from "@/lib/api/types";

/** Mirrors `enrichment.EVIDENCE_KEY`. */
const ENRICHMENT_KEY = "enrichment";

/** Mirrors the key ADR-0032 D7 makes a port law. */
const EXPLANATION_KEY = "explanation";

const mitreSchema = z.object({
  tactic: z.string().min(1),
  technique: z.string().min(1),
  technique_id: z.string().min(1),
});

/** One MITRE ATT&CK mapping. Enterprise and ICS ids share the vocabulary (`T1041`, `T0869`). */
export type MitreTechnique = z.infer<typeof mitreSchema>;

const enrichmentSchema = z.object({
  threat_class: z.string().min(1),
  source: z.enum(["rule", "detector", "heuristic"]),
  origin: z.string().min(1),
  is_heuristic: z.boolean().optional(),
  mitre: z.array(mitreSchema).default([]),
  suggested_actions: z.array(z.string()).default([]),
});

export type Enrichment = z.infer<typeof enrichmentSchema>;

/**
 * The sentence this detection would use to describe itself, or `null` if it carries none.
 *
 * Null is reachable in practice even though the port suite requires the key: the law binds
 * what we *produce*, and a store populated before ADR-0032 holds detections that predate it.
 */
export function explanationOf(detection: DetectionResult): string | null {
  const raw = detection.evidence?.[EXPLANATION_KEY];
  return typeof raw === "string" && raw.trim() !== "" ? raw.trim() : null;
}

/** The threat claim this detection carries, or `null` — the normal case for four of six adapters. */
export function enrichmentOf(detection: DetectionResult): Enrichment | null {
  const raw = detection.evidence?.[ENRICHMENT_KEY];
  if (raw === undefined || raw === null) return null;
  const parsed = enrichmentSchema.safeParse(raw);
  return parsed.success ? parsed.data : null;
}

/**
 * Every technique cited across an alert's detections, deduplicated, in the order met.
 *
 * A *summary* of what is already on screen, not a re-derivation of the server's judgment.
 * Promotion (`alert/_correlate.py:promote_enrichment`) picks a single winning claim by
 * precedence, and that decision stays server-side where both engines share it — this only
 * answers "which techniques does this alert implicate", where the honest answer is all of
 * them and no ranking is implied.
 */
export function mitreOf(detections: readonly DetectionResult[]): MitreTechnique[] {
  const seen = new Map<string, MitreTechnique>();
  for (const detection of detections) {
    for (const technique of enrichmentOf(detection)?.mitre ?? []) {
      if (!seen.has(technique.technique_id)) seen.set(technique.technique_id, technique);
    }
  }
  return [...seen.values()];
}

/**
 * Split a `recommended_action` back into the steps that were joined to build it.
 *
 * The inverse of `Enrichment.action_text`, mirroring `enrichment.action_steps` — see that
 * docstring for why the list is recovered rather than carried, and note that the Python
 * side round-trips every shipped rule and preset through the pair, which is what keeps both
 * copies honest about our own generated text.
 */
export function actionSteps(text: string | null | undefined): string[] {
  if (!text) return [];
  const parts = text.trim().split(/\.\s+/);
  // The period is the delimiter, so it belongs to the step it terminated: every part but the
  // last had one consumed. The last keeps whatever it had — nothing invents punctuation.
  const steps = parts.map((part, index) => (index < parts.length - 1 ? `${part}.` : part));
  return steps.map((step) => step.trim()).filter((step) => step !== "");
}
