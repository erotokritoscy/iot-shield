/**
 * Cached fits: which artifact ids exist, and which one a form opens on (ADR-0035, ADR-0036).
 *
 * The pure half of the artifact chooser, split from its component exactly as `params.ts` is
 * split from `ParamFields` — and shared because two pages choose weights: Compose (where it
 * is the *only* thing a learning detector can be configured with, since a run never fits) and
 * Evaluate (where it is one of two exclusive modes).
 */
import type { ParamInfo } from "./api/types";
import { initialValue, initialValues, type ParamValues } from "./params";

/**
 * The fields both catalogues publish about a detector that can load a cached fit.
 *
 * `ModuleInfo` (`/modules`) and `DetectorSummary` (`/eval/detectors`) are separate response
 * models on two different APIs. Typing this structurally is what lets one chooser serve both
 * pages without either API's types importing the other's.
 */
export type Artifacted = {
  readonly params?: readonly ParamInfo[] | null;
  readonly artifact_param?: string | null;
};

/**
 * Whether this adapter judges with a cached fit rather than with configuration alone.
 *
 * One spelling, because there were four across three files and two of them disagreed:
 * `artifact_param: ""` is permitted by the response schema, and a truthiness test called it
 * "learns nothing" while an explicit null check called it a learner — so one code path handed
 * back every hyperparameter while another posted "has no fitted artifact" about the same row.
 * Unreachable through today's API and still the wrong number of answers to have.
 */
export const learnsWeights = (
  summary: Artifacted,
): summary is Artifacted & { artifact_param: string } =>
  typeof summary.artifact_param === "string" && summary.artifact_param !== "";

/** The param that names a cached fit, when this detector has one (ADR-0022). */
const artifactParamOf = (summary: Artifacted): ParamInfo | undefined =>
  learnsWeights(summary)
    ? (summary.params ?? []).find((param) => param.name === summary.artifact_param)
    : undefined;

/** The artifact ids the adapter ships, published as that param's closed vocabulary. */
export const artifactIdsOf = (summary: Artifacted): string[] =>
  artifactParamOf(summary)?.choices ?? [];

/**
 * Everything that is *not* the artifact: the hyperparameters a fit would consume.
 *
 * The complement of `artifactIdsOf`, and it lives here rather than in the Evaluate page
 * because the split it encodes — the param named by `artifact_param` is the weights, every
 * other one is a knob — is the load-bearing concept of ADR-0022, and splitting a concept's two
 * halves across two modules is how the next change to it gets made in only one of them.
 */
export const tunablesOf = (summary: Artifacted): ParamInfo[] =>
  (summary.params ?? []).filter((param) => param.name !== summary.artifact_param);

/**
 * The artifact a form opens on: the factory default if it has one, else the first shipped id.
 *
 * **Preselects, where `initialValue` deliberately opens blank.** That rule (a closed vocabulary
 * with no default opens on "not given", so preselecting cannot fabricate a value nobody chose)
 * is right for `meter.meter_type` and wrong here: wherever this is rendered an artifact is
 * *mandatory* — a blank is a reported blocker, not an absent value, and it is submitted
 * explicitly rather than omitted as untouched — so the only thing a blank default buys is a page
 * that opens broken. On Compose it was also incoherent: `Select` renders no empty option, so the
 * box *displayed* the first id while the value was `""`, beside a warning saying none was named.
 *
 * Composed over `initialValue` rather than re-deriving it, because that function has *two*
 * rules and only the blank-vs-preselect one is excepted here. The second — a default outside
 * `choices` falls back to a real option — was silently dropped by the hand-rolled version, which
 * would have handed `Select` a value matching no option and reproduced the very incoherence
 * above one field over. Reads the catalogue, where a hardcoded table used to name one of the two
 * shipped artifacts from memory (ADR-0035 D1).
 *
 * KNOWN LIMIT: with two or more shipped ids, "the first" is whichever filename sorts first
 * (`detect/_artifacts.py` sorts for tidiness and publishes no preference). See ADR-0036 D2.
 */
export function initialArtifact(summary: Artifacted): string {
  const param = artifactParamOf(summary);
  if (param === undefined) return "";
  return String(initialValue(param)) || (param.choices?.[0] ?? "");
}

/**
 * The params a *judge-only* form opens on: the artifact alone, or every knob when there is none.
 *
 * A run never fits, so for a learning adapter an artifact is the one thing that can be set and
 * every hyperparameter beside it is refused at construction (ADR-0036). Carrying no value for
 * them is what keeps `submittedParams` from ever naming one: it skips a param whose value is
 * `undefined`, so the spec cannot acquire a half-configured shape even by accident.
 *
 * Takes an absent adapter like `initialValues` does, so a caller mid-selection passes
 * `catalogue.find(...)` straight through instead of standing an empty object in for a module.
 */
export function judgeOnlyValues(summary: Artifacted | null | undefined): ParamValues {
  if (!summary || !learnsWeights(summary)) return initialValues(summary?.params);
  return { [summary.artifact_param]: initialArtifact(summary) };
}
