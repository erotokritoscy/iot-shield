/**
 * What every Compose field expects — the text behind each (i).
 *
 * Kept in one table rather than beside each control so the copy can be read, reviewed and
 * corrected as a whole. The catalogue (`/modules`) publishes a param's name, type, default and
 * choices but no prose, so the *meaning* of a param lives here, keyed by adapter; `paramHelp`
 * appends what the catalogue does know (default, optional), so a param this table has not
 * caught up with still gets an (i) that states its type rather than none.
 */
import type { ParamInfo } from "@/lib/api/types";

export const FIELD_HELP = {
  sample:
    "A small ready-made input file from the sample library. Picking one fills in the content, the normalizer and the format together, so they always match.",
  normalizer:
    "The reader that turns this input into events. It must match what the data is: log lines need syslog, a Zeek conn.log or flow CSV needs flow, sensor CSV needs stub.",
  format:
    "The exact dialect of the input. This normalizer reads more than one, and the format is never guessed from the content, so pick the one your file is written in.",
  upload: "A text file from your computer, up to 8 MB per source. It replaces anything pasted below.",
  origin:
    "Where these records came from: the uploaded file's name, or source-N for pasted text. It is stored with every record, including the rejected ones on Data health.",
  paste:
    "The records themselves, one per line. Blank lines are ignored; a line the normalizer cannot read is kept as a malformed record, never dropped.",
  detector:
    "The method that looks for problems in the events. Each detector reads one lane (telemetry, log or flow); the ones your sources can feed are listed first.",
  artifact:
    "The saved, already-trained model this detector judges with. A run never trains, so this detector can only run from an artifact, and an artifact only judges the device it was trained on.",
  engine:
    "Turns detections into alerts: it groups detections on the same device and time, then scores and ranks each group 0–100.",
  sinks:
    "Where the results are written. Pick postgres to see them on Monitor → Triage; file writes a JSONL copy on the server. You can pick both.",
} as const;

/** What each input format looks like on the wire, keyed by `source_format`. */
export const FORMAT_HELP: Readonly<Record<string, string>> = {
  "stub-csv": "stub-csv: one reading per line, timestamp,asset_id,metric,value — e.g. 2026-07-07T12:00:00+00:00,pump-02,Pressure,96.0. The timestamp must carry a timezone.",
  syslog: "syslog: RFC 5424 or RFC 3164 lines, starting with a <PRI> such as <34>. RFC 3164 has no timezone, so it is read as UTC.",
  "zeek-conn": "zeek-conn: a Zeek conn.log — tab-separated, with the #fields header line Zeek writes.",
  "flow-csv": "flow-csv: CSV with a header row: start,src_ip,src_port,dst_ip,dst_port,proto,service,duration,src_bytes,dst_bytes,src_packets,dst_packets,state.",
  "normalized-jsonl": "normalized-jsonl: one already-normalized event per line, as JSON (the shape iotshield itself stores).",
  "eoa-meter-csv": "eoa-meter-csv: the EOA water-platform export — dataTime, flow (the cumulative counter) and deviceId columns, Cyprus local time.",
  "sensipro-hourly-csv": "sensipro-hourly-csv: SensiPro's hourly export — from_timestamp, last_value (the cumulative counter), device_id and unit columns.",
};

/** What a whole adapter does, appended to the detector / engine / sink (i). */
export const ADAPTER_HELP: Readonly<Record<string, string>> = {
  "detector:stub_threshold": "stub_threshold flags every reading above a fixed value.",
  "detector:team3_zscore": "team3_zscore flags readings far from a device's learned normal behaviour (z-score over rolling features).",
  "detector:log_burst": "log_burst flags too many matching log lines in a short window.",
  "detector:flow_burst": "flow_burst flags a device contacting too many hosts or ports, or sending too much data, too fast.",
  "detector:meter_iforest": "meter_iforest flags unusual hourly consumption on a utility meter (needs meter data).",
  "detector:rules": "rules matches authored signatures of known-bad behaviour — one match is enough, no rate needed.",
  "alert_engine:stub": "stub gives every alert a simple template score — the reference engine.",
  "alert_engine:weighted":
    "weighted multiplies explained factors: how far the strongest detection crossed its line, cross-lane agreement, how many detections, and the device's criticality.",
  "sink:postgres": "postgres: the database the Monitor pages read.",
  "sink:file": "file: a JSONL file in the server's results directory.",
};

/** The meaning of each param, keyed `<kind>:<adapter>.<param>`. */
const PARAM_HELP: Readonly<Record<string, string>> = {
  "normalizer:flow.home_networks":
    "Your own network ranges, as comma-separated CIDRs (e.g. 10.0.0.0/8, 192.168.1.0/24). Decides which end of each flow is the monitored device. Leave empty for the private + link-local defaults, which suit almost every site.",
  "normalizer:passthrough.lane":
    "Which lane these events belong to. A record that names a different lane is rejected as malformed.",
  "normalizer:passthrough.payload":
    "The typed data the events carry in metadata, if any: meter or flow. Set it only when your events include it — meter_iforest needs meter, flow_burst and rules need flow.",
  "normalizer:meter.meter_type":
    "What the meter measures: water or electricity. Leave empty to use the export's own default (water).",
  "normalizer:meter.source_timezone":
    "The IANA timezone the export's local times are in, e.g. Europe/Nicosia. Used to convert them to UTC; a time that is ambiguous at a DST change is rejected rather than guessed.",
  "detector:stub_threshold.threshold":
    "The value above which a reading is flagged, in the reading's own units (°C, bar, …).",
  "detector:stub_threshold.lane": "Which lane's numeric values to compare against the threshold.",
  "detector:log_burst.preset":
    "What counts as a matching line: severity_spike = error-or-worse lines (an error storm); auth_failure = failed-login lines (a brute-force attempt).",
  "detector:log_burst.window_minutes": "How long the sliding window is, in minutes.",
  "detector:log_burst.min_count": "How many matching lines inside one window raise a detection.",
  "detector:flow_burst.preset":
    "What to count per device: fan_out = distinct hosts contacted (20/min); port_scan = distinct ports probed (15/min); exfiltration = bytes sent (50 MB).",
  "detector:flow_burst.window_minutes": "How long the sliding window is, in minutes. Leave empty for the preset's own window.",
  "detector:flow_burst.threshold":
    "The alarm line, in the preset's own unit (hosts, ports or bytes). Leave empty for the preset's own value.",
  "detector:rules.lane":
    "Which lane the rule set judges: flow (network rules) or log (log rules). To judge both, add a second rules detector.",
  "detector:rules.ruleset":
    "Which rule set to load: packaged = the rules that ship with iotshield; any other name is a rule set on the server's rule shelf.",
  "detector:rules.merge_gap_minutes":
    "Matches of the same rule on the same device closer together than this merge into one detection.",
  "alert_engine:stub.bucket_minutes":
    "Detections on the same device within this many minutes are grouped into one alert. Larger means fewer, bigger alerts.",
  "alert_engine:weighted.bucket_minutes":
    "Detections on the same device within this many minutes are grouped into one alert. Larger means fewer, bigger alerts.",
  "sink:file.path":
    "The output file's name. Only the name is used — it is always written inside the server's results directory, never anywhere else.",
};

export type AdapterKind = "normalizer" | "detector" | "alert_engine" | "sink";

function typeText(param: ParamInfo): string {
  if (param.choices && param.choices.length > 0) return `one of ${param.choices.join(", ")}`;
  switch (param.type) {
    case "int":
      return "a whole number";
    case "float":
      return "a number";
    case "bool":
      return "on or off";
    case "list":
      return "a comma-separated list";
    default:
      return "text";
  }
}

/** The (i) text for one param: its meaning when known, then what the catalogue says of it. */
export function paramHelp(kind: AdapterKind, adapter: string, param: ParamInfo): string {
  const meaning = PARAM_HELP[`${kind}:${adapter}.${param.name}`] ?? `A setting of ${adapter}; expects ${typeText(param)}.`;
  const hasDefault = param.default !== null && param.default !== undefined;
  const tail = hasDefault ? `Default: ${String(param.default)}.` : param.required ? "Required." : "Optional.";
  return `${meaning} ${tail}`;
}
