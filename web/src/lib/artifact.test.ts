import { describe, expect, it } from "vitest";

import { param } from "@/test/param";

import { artifactIdsOf, initialArtifact, judgeOnlyValues, learnsWeights } from "./artifact";
import { submittedParams } from "./params";

const learner = {
  params: [param("artifact", "str", null, ["skab_zscore"]), param("window_size", "int", 60)],
  artifact_param: "artifact",
};

describe("artifact ids", () => {
  it("reads the shipped vocabulary off the param, not a table of names", () => {
    // A hardcoded map used to name one of the two shipped artifacts from memory (ADR-0035 D1).
    expect(artifactIdsOf(learner)).toEqual(["skab_zscore"]);
    expect(artifactIdsOf({ params: [param("threshold", "float", 80)] })).toEqual([]);
  });
});

describe("learnsWeights", () => {
  it("answers once, for a value the response schema permits but no adapter sends", () => {
    // `artifact_param` is `z.string().nullish()`, so `""` is legal wire data. There used to be
    // four spellings of this question across three files and two of them answered it
    // oppositely: one handed back every hyperparameter, another posted "has no fitted
    // artifact" about the same row.
    expect(learnsWeights(learner)).toBe(true);
    expect(learnsWeights({ params: [], artifact_param: "" })).toBe(false);
    expect(learnsWeights({ params: [], artifact_param: null })).toBe(false);
    expect(learnsWeights({ params: [] })).toBe(false);
  });
});

describe("initialArtifact", () => {
  it("preselects the first shipped id where an optional vocabulary would open blank", () => {
    // The deliberate exception to ADR-0033's rule. Wherever this is rendered an artifact is
    // mandatory, so opening on "not given" only buys a page that opens broken — and on a
    // `<select>` with no blank option it *displayed* the first id while holding "".
    expect(initialArtifact(learner)).toBe("skab_zscore");
  });

  it("prefers the factory default when the adapter declares one", () => {
    const withDefault = {
      params: [param("artifact", "str", "shipped_default", ["skab_zscore", "shipped_default"])],
      artifact_param: "artifact",
    };
    expect(initialArtifact(withDefault)).toBe("shipped_default");
  });

  it("falls back to a real id when the declared default is not one the adapter ships", () => {
    // The rule inherited by composing over `initialValue` rather than re-deriving it. A
    // hand-rolled version returned the stale id, and `Select` renders no option for it — so the
    // box would display `new_id` while holding `old_id`, which is the display-vs-value
    // incoherence this module exists to remove, one field over.
    const stale = {
      params: [param("artifact", "str", "old_id", ["new_id"])],
      artifact_param: "artifact",
    };
    expect(initialArtifact(stale)).toBe("new_id");
  });

  it("is empty when the adapter declares an artifact param and ships nothing", () => {
    // The free-text fallback's case: there is no id to preselect, so the page must be able to
    // say so rather than invent one, and the untrained warning is what says it.
    expect(initialArtifact({ params: [param("artifact", "str", null)], artifact_param: "artifact" }))
      .toBe("");
  });
});

describe("judgeOnlyValues", () => {
  it("carries the artifact alone, so a run's spec cannot name a hyperparameter", () => {
    // The knobs are not merely hidden: holding no value for them is what stops
    // `submittedParams` naming one, since it skips a param whose value is undefined. Naming one
    // beside an artifact is refused at construction (ADR-0022 D4, ADR-0036).
    expect(judgeOnlyValues(learner)).toEqual({ artifact: "skab_zscore" });
    // ...and the preselected id survives into the spec, which is not automatic: `isUntouched`
    // omits a value equal to the catalogue default, and this param's default is null.
    expect(submittedParams(learner.params, judgeOnlyValues(learner))).toEqual({
      artifact: "skab_zscore",
    });
  });

  it("takes an absent adapter, for a caller mid-selection", () => {
    expect(judgeOnlyValues(undefined)).toEqual({});
    expect(judgeOnlyValues(null)).toEqual({});
  });

  it("falls back to every knob for an adapter that learns nothing", () => {
    const rule = { params: [param("threshold", "float", 80), param("lane", "str", "telemetry")] };
    expect(judgeOnlyValues(rule)).toEqual({ threshold: "80", lane: "telemetry" });
  });
});
