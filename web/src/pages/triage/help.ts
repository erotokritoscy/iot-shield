/**
 * What every part of the Triage page shows — the text behind each (i).
 *
 * One table, like `compose/help.ts`, so the copy can be read and corrected as a whole. The
 * count tiles reuse Compose's `RESULT_HELP`: they count the same four things, here for the
 * whole organization rather than one run.
 */
import { RESULT_HELP } from "@/pages/compose/help";
import type { AlertSeverity, SUMMARY_KEYS } from "@/lib/api/types";

export const COUNT_HELP: Readonly<Record<(typeof SUMMARY_KEYS)[number], string>> = {
  events: RESULT_HELP.events,
  detections: RESULT_HELP.detections,
  alerts: RESULT_HELP.alerts,
  malformed_records: RESULT_HELP.malformed,
};

export const TRIAGE_HELP = {
  runs:
    "Pipeline runs saved for this organization: from the Compose page, saved jobs, the command line and the demo seed. Each one produced some of the events, detections and alerts counted beside it; open them under Operate → Runs.",
  severity:
    "Show only alerts of one severity. Severity is a band of the 0–100 priority: medium from 30, high from 50, critical from 70, emergency from 90, and low below 30.",
  chartSeverities:
    "Which severities the chart draws. It changes the chart only, not the queue below (use its Severity filter for that). Hiding a band re-scales the chart to what is left, e.g. hide low to see a handful of critical alerts beside thousands of low ones.",
  asset: "Show only one device's alerts. Lists the devices that appear in the alerts loaded here.",
  run: "Show only the alerts one saved run produced (Operate → Runs). Any shows the alerts of every run.",
  queue: [
    "Alerts in priority order, highest first. The order is the product's ranking, so the table cannot be re-sorted. Click a row to inspect it.",
    "severity — the priority's band. priority — 0–100, how urgently to look. asset — the device. window start — when its first detection began (UTC). threat class — what the evidence says this is, or unclassified when nothing claims to know. status — new, acknowledged or closed.",
  ],
  inspector:
    "Everything behind one alert: why it scored what it did, what to do next, and the detections it was built from.",
  inspect: "The alert shown below. Clicking a row in the queue picks it too.",
  rationale:
    "How the priority was computed: the factors, how many detections across how many lanes, and the strongest detection against its threshold.",
  actions:
    "Next steps to take, collected from every rule or preset that fired on this alert, so a device showing two problems gets both sets of steps.",
  mitre:
    "The MITRE ATT&CK techniques the detections point to, each with its tactic. The threat class names only the strongest one.",
  components: [
    "The factors multiplied into the priority. Each engine records its own; with the weighted engine:",
    "severity — how far the strongest detection went past its threshold. corroboration — how many lanes agree. volume — how many distinct detections. asset_weight — the device's criticality. raw_score — their product, which is mapped onto 0–100.",
  ],
  evidence: [
    "The detections grouped into this alert, one row each: which detector (method) on which lane, its score against its threshold, and when.",
    "Expand a finding below for its full evidence — peers, ports, matched log lines, feature values.",
  ],
} as const;

/**
 * The severity bands, highest first, for the table under the severity chart.
 *
 * `floor` MUST equal `_SEVERITY_FLOOR` in `src/iotshield/alert/_correlate.py` — the server
 * decides an alert's band, this only explains it. `tests/adapters/test_severity_guide.py` reads
 * these floors out of this file and fails when the two disagree.
 */
export interface SeverityBand {
  readonly severity: AlertSeverity;
  readonly floor: number;
  readonly meaning: string;
  readonly response: string;
}

export const SEVERITY_GUIDE: readonly SeverityBand[] = [
  {
    severity: "emergency",
    floor: 90,
    meaning:
      "The strongest evidence the scale can express: many detections agreeing on one device. The weighted engine cannot reach it (its highest priority is 88.7), so it appears only with other engines.",
    response: "Act now: contain or isolate the device, then investigate.",
  },
  {
    severity: "critical",
    floor: 70,
    meaning:
      "Strong, corroborated evidence — typically independent detections on two or more lanes (flow, log, telemetry) of the same device at the same time, well past their thresholds.",
    response: "Investigate immediately and follow the recommended actions.",
  },
  {
    severity: "high",
    floor: 50,
    meaning:
      "A clear finding on one device: a detection far past its threshold, or a known-bad signature (a single critical rule at high confidence scores 63).",
    response: "Investigate today; check the device's other lanes for corroboration.",
  },
  {
    severity: "medium",
    floor: 30,
    meaning: "Something unusual but not yet convincing: a detection moderately past its threshold, with little or no corroboration.",
    response: "Review once the higher bands are clear; watch for it recurring.",
  },
  {
    severity: "low",
    floor: 0,
    meaning: "Weak evidence, just over a detector's line. Often noise or an expected change.",
    response: "No action needed on its own; useful as context for other alerts.",
  },
];

/** `90–100`, `70–89.9`: each band runs from its floor up to the next one, exclusive. */
export function bandRange(index: number): string {
  const band = SEVERITY_GUIDE[index]!;
  const above = SEVERITY_GUIDE[index - 1];
  return above === undefined ? `${band.floor}–100` : `${band.floor}–${(above.floor - 0.1).toFixed(1)}`;
}
