import { describe, expect, it } from "vitest";

import { modules } from "@/test/fixtures";
import type { ModuleInfo } from "@/lib/api/types";

import { composedSpec, labelled, normalizerFor, runCount, sweepValues } from "./lib";

const normalizers = modules.normalizers as unknown as ModuleInfo[];
const detector = (name: string) => ({ name, params: {} });

describe("sweep values", () => {
  it("parses a comma-separated list into narrow types", () => {
    // Ported from `eval/sweep.py: _scalar`/`values`. Typing each value is what makes a label
    // read `window_size=20` rather than `window_size='20'`.
    expect(sweepValues("20, 60,120")).toEqual([20, 60, 120]);
    expect(sweepValues("0.5, 1.5")).toEqual([0.5, 1.5]);
    expect(sweepValues("true, false")).toEqual([true, false]);
    expect(sweepValues("fan_out, exfiltration")).toEqual(["fan_out", "exfiltration"]);
  });

  it("drops blank items rather than sending empty strings", () => {
    expect(sweepValues("20, ,60,")).toEqual([20, 60]);
    expect(sweepValues("")).toEqual([]);
  });

  it("counts one benchmark per swept value, and one when nothing is swept", () => {
    expect(runCount({})).toBe(1);
    expect(runCount({ threshold: [40, 80, 120] })).toBe(3);
  });
});

describe("composing a spec for a corpus (E5)", () => {
  it("builds one source per declared format, in the corpus's order", () => {
    // A benchmark checks a case's inputs against the spec's `sources` positionally and by
    // format, so a hardcoded single stub-csv source would offer a flow corpus in the picker
    // and then be refused on run.
    const spec = composedSpec(
      [detector("flow_burst")],
      "weighted",
      ["stub-csv", "syslog", "flow-csv"],
      normalizers,
    );
    expect((spec["sources"] as { source_format: string }[]).map((s) => s.source_format)).toEqual([
      "stub-csv",
      "syslog",
      "flow-csv",
    ]);
  });

  it("resolves each normalizer from the registry, by declared capability", () => {
    // Not by name — so a newly registered normalizer works here with no change, and a
    // rename cannot silently break the page.
    expect(normalizerFor("flow-csv", normalizers)).toBe("flow");
    expect(normalizerFor("syslog", normalizers)).toBe("syslog");
    expect(normalizerFor("nothing-reads-this", normalizers)).toBe("");
    const spec = composedSpec([detector("flow_burst")], "weighted", ["flow-csv"], normalizers);
    expect(spec["sources"]).toEqual([{ normalizer: "flow", source_format: "flow-csv" }]);
  });

  it("carries every detector of one entry into ONE spec", () => {
    // Several detectors in one spec run together, which is what lets their detections pool
    // into a single alert-engine run and corroborate across lanes.
    const spec = composedSpec(
      [detector("stub_threshold"), detector("flow_burst")],
      "weighted",
      ["stub-csv", "flow-csv"],
      normalizers,
    );
    expect((spec["detectors"] as { name: string }[]).map((d) => d.name)).toEqual([
      "stub_threshold",
      "flow_burst",
    ]);
  });
});

describe("labelling entries (E8)", () => {
  it("drops entries with no detectors", () => {
    expect(
      labelled([
        { label: "a", detectors: [1] },
        { label: "", detectors: [] },
      ]),
    ).toEqual([{ label: "a", detectors: [1] }]);
  });

  it("makes duplicate labels unique so two rows are tellable apart", () => {
    expect(
      labelled([
        { label: "x", detectors: [1] },
        { label: "x", detectors: [1] },
      ]).map((entry) => entry.label),
    ).toEqual(["x", "x (2)"]);
  });
});
