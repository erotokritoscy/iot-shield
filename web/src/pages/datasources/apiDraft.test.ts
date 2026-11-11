import { describe, expect, it } from "vitest";

import type { ApiEndpoint } from "@/lib/api/types";
import { apiBody, apiDraftOf, apiProblem, EMPTY_API, parseMetrics } from "@/pages/datasources/apiDraft";

const READY = {
  ...EMPTY_API,
  url: " https://api.example.com/x ",
  assetId: "id",
  metrics: "Temperature = r.t\n\n  Pressure=r.p  ",
};

describe("parseMetrics", () => {
  it("reads name = path lines, skipping blanks", () => {
    expect(parseMetrics(READY.metrics)).toEqual({ metrics: { Temperature: "r.t", Pressure: "r.p" } });
  });

  it("names the first line that is not one, and a duplicate", () => {
    expect(parseMetrics("a = b\nnonsense")).toEqual({ error: "Line 2 is not “name = path”: nonsense" });
    expect(parseMetrics("a = b\na = c")).toEqual({ error: "The metric “a” is named twice." });
  });
});

describe("apiProblem", () => {
  it("is null once the draft can be sent", () => {
    expect(apiProblem(READY)).toBeNull();
  });

  it.each([
    [{ url: "" }, "Enter the API's URL."],
    [{ auth: "basic" as const }, "Enter the user name."],
    [{ metrics: "" }, "Name at least one metric."],
    [{ lane: "log" as const }, "Name the path of the log message."],
    [{ startParam: " " }, "Name the window's start and end parameters."],
  ])("says what is missing: %o", (change, problem) => {
    expect(apiProblem({ ...READY, ...change })).toBe(problem);
  });
});

describe("apiBody", () => {
  it("sends only the secret its scheme uses, a password exactly as typed", () => {
    const basic = apiBody({ ...READY, auth: "basic", username: "ops", password: " p w ", apiKey: "leftover" });
    expect(basic.credentials).toEqual({ password: " p w ", api_key: null });
    expect(basic.api?.username).toBe("ops");
    const none = apiBody({ ...READY, password: "x", apiKey: "y" });
    expect(none.credentials).toEqual({ password: null, api_key: null });
    expect(none.api?.username).toBeNull();
  });

  it("drops the other lane's fields", () => {
    const log = apiBody({ ...READY, lane: "log", message: "msg", severity: "" });
    expect(log.api?.mapping).toMatchObject({ lane: "log", metrics: {}, message: "msg", severity: null });
    expect(apiBody(READY).api?.mapping).toMatchObject({ message: null, metrics: { Temperature: "r.t" } });
  });

  it("round-trips through what the server returns", () => {
    const api = apiBody(READY).api as ApiEndpoint;
    expect(apiBody(apiDraftOf(api)).api).toEqual(api);
    expect(apiDraftOf(api).apiKey).toBe(""); // a secret never comes back
  });
});
