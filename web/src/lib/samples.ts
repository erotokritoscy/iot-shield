/**
 * The sample library the Compose wizard offers (`scripts/make_sample_data.py`).
 *
 * Static files served beside the bundle at `/samples/`, not an API: they are inputs for the
 * operator to try, not something the pipeline computes, so the dashboard still computes
 * nothing (ADR-0031). The manifest is parsed with zod like any wire response, because a stale
 * or hand-edited manifest should fail here rather than prefill a wizard with nonsense.
 *
 * A deployment without the files (a 404) simply offers no samples; the wizard works as before.
 */
import { z } from "zod";

export const SAMPLES_BASE = "/samples";

const sampleSchema = z.object({
  id: z.string(),
  file: z.string(),
  title: z.string(),
  description: z.string(),
  normalizer: z.string(),
  source_format: z.string(),
});

const configuredSchema = z.object({
  name: z.string(),
  params: z.record(z.unknown()).default({}),
});

const scenarioSchema = z.object({
  id: z.string(),
  title: z.string(),
  description: z.string(),
  samples: z.array(z.string()).min(1),
  detectors: z.array(configuredSchema).min(1),
  alert_engine: configuredSchema,
  expected: z.object({
    events: z.number(),
    malformed: z.number(),
    detections: z.number(),
    alerts: z.number(),
  }),
});

export const manifestSchema = z.object({
  samples: z.array(sampleSchema),
  scenarios: z.array(scenarioSchema),
});

export type Sample = z.infer<typeof sampleSchema>;
export type Scenario = z.infer<typeof scenarioSchema>;
export type SampleManifest = z.infer<typeof manifestSchema>;

const EMPTY: SampleManifest = { samples: [], scenarios: [] };

/** The manifest, or an empty one when this deployment ships no samples. */
export async function fetchManifest(): Promise<SampleManifest> {
  const response = await fetch(`${SAMPLES_BASE}/manifest.json`);
  if (response.status === 404) return EMPTY;
  if (!response.ok) throw new Error(`the sample manifest returned HTTP ${response.status}`);
  const parsed = manifestSchema.safeParse(await response.json());
  if (!parsed.success) {
    const issue = parsed.error.issues[0];
    throw new Error(`the sample manifest is malformed at ${issue?.path.join(".") ?? "?"}: ${issue?.message ?? ""}`);
  }
  // A scenario naming a sample that is not listed cannot be loaded; drop it rather than offer
  // a button that fails.
  const known = new Set(parsed.data.samples.map((sample) => sample.id));
  return {
    samples: parsed.data.samples,
    scenarios: parsed.data.scenarios.filter((scenario) => scenario.samples.every((id) => known.has(id))),
  };
}

export async function fetchSampleText(sample: Sample): Promise<string> {
  const response = await fetch(`${SAMPLES_BASE}/${encodeURIComponent(sample.file)}`);
  if (!response.ok) throw new Error(`could not load ${sample.file} (HTTP ${response.status})`);
  return response.text();
}
