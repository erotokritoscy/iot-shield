import { describe, expect, it } from "vitest";

import { detection } from "@/test/fixtures";
import type { DetectionResult } from "@/lib/api/types";
import { actionSteps, enrichmentOf, explanationOf, mitreOf } from "./evidence";

const withEvidence = (evidence: Record<string, unknown>): DetectionResult =>
  ({ ...detection, evidence }) as DetectionResult;

const TELNET = {
  threat_class: "Remote Services (T1021)",
  source: "rule",
  origin: "rule:telnet-outbound-propagation",
  is_heuristic: false,
  mitre: [{ tactic: "Lateral Movement", technique: "Remote Services", technique_id: "T1021" }],
  suggested_actions: ["Isolate the device.", "Block outbound telnet."],
};

describe("reading the promoted keys out of evidence", () => {
  it("reads the explanation every detector is required to carry", () => {
    expect(explanationOf(withEvidence({ explanation: "  Port 23 was dialled.  " }))).toBe(
      "Port 23 was dialled.",
    );
  });

  it("reads a detection that predates the law, or says nothing, as absent", () => {
    // The port suite binds what we PRODUCE; a store populated before ADR-0032 holds rows
    // with no such key, and the UI must fall back rather than render a blank heading.
    expect(explanationOf(withEvidence({ peak: 96 }))).toBeNull();
    expect(explanationOf(withEvidence({ explanation: "   " }))).toBeNull();
    expect(explanationOf(withEvidence({ explanation: 42 }))).toBeNull();
  });

  it("parses a well-formed enrichment", () => {
    const parsed = enrichmentOf(withEvidence({ enrichment: TELNET }));
    expect(parsed?.threat_class).toBe("Remote Services (T1021)");
    expect(parsed?.mitre[0]?.tactic).toBe("Lateral Movement");
    expect(parsed?.suggested_actions).toHaveLength(2);
  });

  it("treats an unenriched or malformed payload as absent, never as a throw", () => {
    // Four of six shipped adapters carry no enrichment at all, so absence is the normal
    // case; a malformed one reads the same way because this is a drill-down panel, and
    // failing it closed would blank an analyst's evidence over a cosmetic drift.
    expect(enrichmentOf(withEvidence({ peak: 96 }))).toBeNull();
    expect(enrichmentOf(withEvidence({ enrichment: { threat_class: "" } }))).toBeNull();
    expect(enrichmentOf(withEvidence({ enrichment: { ...TELNET, mitre: {} } }))).toBeNull();
  });

  it("defaults the optional lists so a caller never guards them", () => {
    const parsed = enrichmentOf(
      withEvidence({ enrichment: { threat_class: "X", source: "detector", origin: "preset:y" } }),
    );
    expect(parsed?.mitre).toEqual([]);
    expect(parsed?.suggested_actions).toEqual([]);
  });
});

describe("summarising an alert's techniques", () => {
  it("unions across detections and deduplicates by technique id", () => {
    const other = {
      ...TELNET,
      mitre: [
        { tactic: "Lateral Movement", technique: "Remote Services", technique_id: "T1021" },
        { tactic: "Exfiltration", technique: "Exfiltration Over C2", technique_id: "T1041" },
      ],
    };
    const techniques = mitreOf([
      withEvidence({ enrichment: TELNET }),
      withEvidence({ enrichment: other }),
      withEvidence({ peak: 96 }),
    ]);
    expect(techniques.map((t) => t.technique_id)).toEqual(["T1021", "T1041"]);
  });

  it("is empty when nothing in the bucket claims a technique", () => {
    expect(mitreOf([withEvidence({ peak: 96 })])).toEqual([]);
  });
});

describe("recovering the action checklist", () => {
  it("splits the joined paragraph back into steps", () => {
    // The camera alert's real shape: six imperatives unioned from four rules, which as one
    // paragraph is the wall of text this exists to undo.
    expect(actionSteps("Isolate the device. Block outbound telnet. Review the source IP.")).toEqual(
      ["Isolate the device.", "Block outbound telnet.", "Review the source IP."],
    );
  });

  it("leaves the generated fallback as the single instruction it is", () => {
    // One sentence carrying a period-free ISO instant and a semicolon: splitting it would
    // invent two half-instructions out of one.
    const fallback = "Inspect pump-02 around 2026-07-07T12:01:00+00:00; review flow_burst evidence.";
    expect(actionSteps(fallback)).toEqual([fallback]);
  });

  it("does not give a trailing step a period it never had", () => {
    expect(actionSteps("First step. Second with no stop")).toEqual([
      "First step.",
      "Second with no stop",
    ]);
  });

  it("renders nothing for an engine that recommends nothing", () => {
    expect(actionSteps(null)).toEqual([]);
    expect(actionSteps("")).toEqual([]);
    expect(actionSteps("   ")).toEqual([]);
  });
});
