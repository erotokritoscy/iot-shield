import { describe, expect, it } from "vitest";

import { param } from "@/test/param";

import { initialValue, initialValues, isUntouched, submittedParams } from "./params";

describe("a form that can say 'not given' (C3/C4)", () => {
  it("opens a numeric with no default EMPTY, and omits it", () => {
    // flow_burst's window and threshold have preset-owned real defaults, so the factory
    // reports None. Coercing that to 0 submits a value the adapter never offered — and it is
    // refused at construction, turning "pick a detector and press run" into a failed run.
    const threshold = param("threshold", "float", null);
    expect(initialValue(threshold)).toBe("");
    expect(submittedParams([threshold], { threshold: "" })).toEqual({});
  });

  it("omits every parameter of an untouched learning detector", () => {
    // The costly case: an untouched team3_zscore row submitted its whole prefilled
    // hyperparameter block *plus* its artifact — the half-configured request ADR-0022 D4
    // refuses.
    const params = [
      param("artifact", "str", null),
      param("window_size", "int", 60),
      param("percentile", "float", 99.0),
    ];
    expect(submittedParams(params, initialValues(params))).toEqual({});
  });

  it("still sends what the operator actually moved", () => {
    const params = [param("window_size", "int", 60), param("threshold", "float", 80.0)];
    const values = { ...initialValues(params), window_size: "20" };
    expect(submittedParams(params, values)).toEqual({ window_size: 20 });
  });

  it("sends numbers as numbers, not as widget text", () => {
    const params = [param("threshold", "float", 80.0)];
    expect(submittedParams(params, { threshold: "40.5" })).toEqual({ threshold: 40.5 });
  });

  it("preselects the reported default of a closed vocabulary", () => {
    const preset = param("preset", "str", "severity_spike", ["severity_spike", "auth_failure"]);
    expect(initialValue(preset)).toBe("severity_spike");
    expect(submittedParams([preset], { preset: "severity_spike" })).toEqual({});
    expect(submittedParams([preset], { preset: "auth_failure" })).toEqual({
      preset: "auth_failure",
    });
  });

  it("compares like the Python original, not like JavaScript", () => {
    // `False == ""` is False in Python but `false == ""` is true in JS, so a loose
    // comparison here would silently change which parameters a run declares.
    const flag = param("volume", "bool", null);
    expect(isUntouched(false, flag)).toBe(false);
    expect(isUntouched("", flag)).toBe(true);
    // ...and the string-compare fallback still catches a value equal to its default.
    expect(isUntouched("60", param("window_size", "int", 60))).toBe(true);
  });
});
