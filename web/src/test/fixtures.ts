/**
 * The seeded world, mirroring `tests/dashboard/conftest.py`.
 *
 * Same pump-02, same T0, one detection, one HIGH alert, one malformed record — so the React
 * suite and the Python suite assert the *same story* and a divergence between the two shows
 * up as a disagreement rather than as two independent green runs (brief §8).
 */
export const T0 = "2026-07-07T12:00:00Z";

const at = (seconds: number): string =>
  new Date(Date.parse(T0) + seconds * 1000).toISOString().replace(".000Z", "Z");

export const asset = {
  asset_id: "pump-02",
  name: "Feed pump 2",
  asset_type: "pump",
  location: null,
  criticality: "high",
  metadata: {},
};

export const discoveredAsset = {
  asset_id: "cam-07",
  name: "cam-07",
  asset_type: "unknown",
  location: null,
  criticality: "medium",
  metadata: { auto_discovered: true },
};

export const events = [45.0, 96.0, 47.0].map((value, index) => ({
  event_id: `00000000-0000-0000-0000-00000000000${index}`,
  timestamp: at(30 * index),
  asset_id: "pump-02",
  source_type: "telemetry",
  source_format: "stub-csv",
  metric: "Pressure",
  value,
  severity: "info",
  message: null,
  raw: `line-${index}`,
  metadata: {},
}));

export const detection = {
  detection_id: "11111111-1111-1111-1111-111111111111",
  asset_id: "pump-02",
  lane: "telemetry",
  method: "stub_threshold",
  score: 96.0,
  threshold: 80.0,
  window_start: at(30),
  window_end: at(30),
  event_kind: "point",
  // `explanation` is on every detection because ADR-0032 D7 makes it a port law, enforced by
  // `tests/ports/test_detector_port.py`. The fixture carries one so the mocked world matches
  // what the real stack now produces; the rest of evidence stays per-detector.
  evidence: {
    explanation: "Pressure 96 is above the configured threshold of 80",
    peak: 96.0,
    metric: "Pressure",
  },
  metadata: {},
};

/**
 * Two rule detections on ONE lane, which is the shape `method` cannot name.
 *
 * The Detector port gives an adapter one lane and the port suite pins `detection.method` to
 * it, so every rule on the flow lane reports `rules:flow` and the firing rule rides in
 * `Enrichment.origin` (ADR-0032 D3). Both rows below are therefore `rules:flow`, exactly as
 * the demo walkthrough produces — its camera raises telnet propagation and a bulk upload,
 * two different findings that report the same `method`.
 */
export const ruleDetections = [
  {
    ...detection,
    detection_id: "33333333-3333-3333-3333-333333333333",
    lane: "flow",
    method: "rules:flow",
    score: 2.55,
    threshold: 1.0,
    evidence: {
      explanation: "The device originated a connection to port 23 (telnet).",
      rule_id: "telnet-outbound-propagation",
      matched_events: 6,
      enrichment: {
        threat_class: "Remote Services (T1021)",
        source: "rule",
        origin: "rule:telnet-outbound-propagation",
        is_heuristic: false,
        mitre: [
          { tactic: "Lateral Movement", technique: "Remote Services", technique_id: "T1021" },
        ],
        suggested_actions: ["Isolate the device.", "Block outbound telnet."],
      },
    },
  },
  {
    ...detection,
    detection_id: "44444444-4444-4444-4444-444444444444",
    lane: "flow",
    method: "rules:flow",
    score: 1.7,
    threshold: 1.0,
    evidence: {
      explanation: "Outbound bytes on one flow exceed 10000.",
      rule_id: "high-upload-volume",
      matched_events: 1,
      enrichment: {
        threat_class: "Exfiltration Over C2 Channel (T1041)",
        source: "rule",
        origin: "rule:high-upload-volume",
        is_heuristic: false,
        mitre: [
          {
            tactic: "Exfiltration",
            technique: "Exfiltration Over C2 Channel",
            technique_id: "T1041",
          },
        ],
        suggested_actions: ["Review the destination.", "Investigate possible exfiltration."],
      },
    },
  },
];

export const alert = {
  alert_id: "22222222-2222-2222-2222-222222222222",
  asset_id: "pump-02",
  severity: "high",
  priority_score: 62.0,
  threat_class: "unknown",
  rationale: "seeded incident on Feed pump 2",
  score_components: { base: 52.0, asset_criticality: 10.0 },
  detection_ids: [detection.detection_id],
  recommended_action: "Inspect pump-02.",
  status: "new",
  window_start: at(30),
  window_end: at(30),
  created_at: T0,
};

export const malformed = {
  record_id: "33333333-3333-3333-3333-333333333333",
  raw: "garbage-line",
  error: "unparseable",
  source_format: "stub-csv",
  origin: "test://seed",
  created_at: T0,
};

export const summary = {
  counts: { events: 3, detections: 1, alerts: 1, malformed_records: 1 },
  top_alert: alert,
};

export const emptySummary = {
  counts: { events: 0, detections: 0, alerts: 0, malformed_records: 0 },
  top_alert: null,
};

// --- Pipeline catalogue -------------------------------------------------------
//
// Shaped exactly like `/modules`, including the two facts the compose page's preselection
// turns on: `meter_iforest` sorts first *and* declares an `artifact_param`, so a page that
// picked the alphabetically-first detector would open on a chain a run refuses.

const param = (
  name: string,
  type: string | null,
  value: unknown,
  choices: string[] | null = null,
) => ({ name, type, default: value, required: false, choices });

export const modules = {
  normalizers: [
    {
      name: "stub",
      kind: "normalizer",
      consumes: ["format:stub-csv"],
      produces: ["lane:telemetry"],
      formats: ["stub-csv"],
      params: [],
      artifact_param: null,
    },
    {
      name: "syslog",
      kind: "normalizer",
      consumes: ["format:syslog"],
      produces: ["lane:log"],
      formats: ["syslog"],
      params: [],
      artifact_param: null,
    },
    {
      name: "flow",
      kind: "normalizer",
      consumes: ["format:flow-csv", "format:zeek-conn"],
      produces: ["lane:flow", "payload:flow"],
      formats: ["flow-csv", "zeek-conn"],
      params: [],
      artifact_param: null,
    },
  ],
  detectors: [
    {
      name: "meter_iforest",
      kind: "detector",
      // Two tokens, and no fixture normalizer produces `payload:meter` — so this detector is
      // not feedable by any chain the compose page can build here, which is the shape that
      // used to be indistinguishable from `stub_threshold` (ADR-0035 D2).
      consumes: ["lane:telemetry", "payload:meter"],
      produces: ["detections"],
      formats: [],
      params: [param("artifact", "str", null, ["demo_meter"])],
      artifact_param: "artifact",
    },
    {
      name: "flow_burst",
      kind: "detector",
      consumes: ["lane:flow", "payload:flow"],
      produces: ["detections"],
      formats: [],
      params: [
        param("preset", "str", "fan_out", ["fan_out", "exfiltration"]),
        param("window_minutes", "int", null),
        param("threshold", "float", null),
      ],
      artifact_param: null,
    },
    {
      name: "log_burst",
      kind: "detector",
      consumes: ["lane:log"],
      produces: ["detections"],
      formats: [],
      params: [
        param("preset", "str", "severity_spike", ["severity_spike", "auth_failure"]),
        param("window_seconds", "int", null),
        param("min_count", "int", null),
      ],
      artifact_param: null,
    },
    {
      name: "stub_threshold",
      kind: "detector",
      consumes: ["lane:telemetry"],
      produces: ["detections"],
      formats: [],
      params: [param("threshold", "float", 80.0)],
      artifact_param: null,
    },
    {
      name: "team3_zscore",
      kind: "detector",
      consumes: ["lane:telemetry"],
      produces: ["detections"],
      formats: [],
      params: [
        param("artifact", "str", null, ["skab_zscore"]),
        param("window_size", "int", 60),
        param("percentile", "float", 99.0),
      ],
      artifact_param: "artifact",
    },
    {
      // A learning adapter that declares an artifact param and ships NO ids. Both real ones
      // ship exactly one, but a deployment that removes the committed demo artifact has this
      // shape, and it is what ADR-0035 D1's free-text fallback exists for — a dropdown over an
      // empty vocabulary offers no way forward. It is also the only shape where Compose's "no
      // fitted artifact" warning is still reachable, now that a shipped id preselects
      // (ADR-0036), so it is what keeps that warning under test.
      name: "unshipped_learner",
      kind: "detector",
      consumes: ["lane:telemetry"],
      produces: ["detections"],
      formats: [],
      params: [param("artifact", "str", null)],
      artifact_param: "artifact",
    },
  ],
  alert_engines: [
    {
      name: "stub",
      kind: "alert_engine",
      consumes: ["detections"],
      produces: ["alerts"],
      formats: [],
      params: [],
      artifact_param: null,
    },
    {
      name: "weighted",
      kind: "alert_engine",
      consumes: ["detections"],
      produces: ["alerts"],
      formats: [],
      params: [param("volume", "bool", true)],
      artifact_param: null,
    },
  ],
  sinks: [
    { name: "file", params: [param("filename", "str", "iotshield-results.jsonl")] },
    { name: "postgres", params: [] },
  ],
};

// --- Evaluation plane ---------------------------------------------------------

// `payloads` mirrors what `_shape_fed_by` computes server-side: a `stub-csv` bundle carries
// no typed lane vocabulary, which is what makes `meter_iforest` ineligible for every corpus
// here despite matching on the telemetry lane (ADR-0035 D2).
export const corpora = {
  root: "data/eval",
  corpora: [
    {
      name: "synthetic",
      description: "Synthetic rig telemetry: 11 recordings, 9 injected faults",
      provenance: "generated",
      licence: "",
      cases: 11,
      source_formats: ["stub-csv"],
      lanes: ["telemetry"],
      payloads: [],
      error: "",
    },
    {
      name: "fleet_scan",
      description: "Multi-device flow captures",
      provenance: "generated",
      licence: "",
      cases: 4,
      source_formats: ["flow-csv"],
      lanes: ["flow"],
      payloads: ["flow"],
      error: "",
    },
    {
      name: "device_trimodal",
      description: "One device on all three lanes, for the corroboration A/B",
      provenance: "generated",
      licence: "",
      cases: 1,
      source_formats: ["stub-csv", "syslog", "flow-csv"],
      lanes: ["telemetry", "log", "flow"],
      payloads: ["flow"],
      error: "",
    },
    {
      name: "skab_valve1",
      description: "SKAB valve1 recordings",
      provenance: "https://github.com/waico/SKAB",
      licence: "GPL-3.0",
      cases: 16,
      source_formats: ["stub-csv"],
      lanes: ["telemetry"],
      payloads: [],
      error: "",
    },
    {
      name: "stale_bundle",
      description: "",
      provenance: "",
      licence: "",
      cases: 0,
      source_formats: [],
      lanes: [],
      payloads: [],
      error: "labels schema v4 is stale; regenerate with `iotshield.eval generate`",
    },
  ],
  fetchable: { skab: "SKAB is GPLv3: run `uv run python -m iotshield.eval fetch skab`" },
};

export const evalDetectors = [
  // Sorted as `/eval/detectors` sorts, so `meter_iforest` is alphabetically first among the
  // telemetry detectors — the position that made it the Evaluate page's default entry on
  // every telemetry corpus, including the ones it cannot read (ADR-0035 D2).
  {
    name: "meter_iforest",
    lanes: ["telemetry"],
    payloads: ["meter"],
    fittable: true,
    params: [param("artifact", "str", null, ["demo_meter"])],
    artifact_param: "artifact",
  },
  {
    name: "flow_burst",
    lanes: ["flow"],
    payloads: ["flow"],
    fittable: false,
    params: [
      param("preset", "str", "fan_out", ["fan_out", "exfiltration"]),
      param("window_minutes", "int", null),
      param("threshold", "float", null),
    ],
    artifact_param: null,
  },
  {
    name: "log_burst",
    lanes: ["log"],
    payloads: [],
    fittable: false,
    params: [
      param("preset", "str", "severity_spike", ["severity_spike", "auth_failure"]),
      param("window_seconds", "int", null),
      param("min_count", "int", null),
    ],
    artifact_param: null,
  },
  {
    name: "stub_threshold",
    lanes: ["telemetry"],
    payloads: [],
    fittable: false,
    params: [param("threshold", "float", 80.0)],
    artifact_param: null,
  },
  {
    name: "team3_zscore",
    lanes: ["telemetry"],
    payloads: [],
    fittable: true,
    params: [
      param("artifact", "str", null, ["skab_zscore"]),
      param("window_size", "int", 60),
      param("percentile", "float", 99.0),
    ],
    artifact_param: "artifact",
  },
];

export const benchSpecs = [
  {
    name: "stub",
    spec: {
      sources: [{ normalizer: "stub", source_format: "stub-csv" }],
      detectors: [{ name: "stub_threshold", params: {} }],
      alert_engine: { name: "weighted", params: {} },
      assets: [{ asset_id: "rig-01", name: "rig-01", asset_type: "rig", criticality: "high" }],
    },
    source_formats: ["stub-csv"],
    detectors: ["stub_threshold"],
    corpora: ["synthetic"],
  },
  {
    name: "flow",
    spec: {
      sources: [{ normalizer: "flow", source_format: "flow-csv" }],
      detectors: [{ name: "flow_burst", params: { preset: "fan_out" } }],
      alert_engine: { name: "weighted", params: {} },
    },
    source_formats: ["flow-csv"],
    detectors: ["flow_burst"],
    corpora: ["fleet_scan"],
  },
];

const caseReport = (name: string, f1: number | null, notes: string[] = []) => ({
  case: name,
  incidents: f1 === null ? 0 : 1,
  detections: 62,
  alerts: 1,
  fitted: false,
  notes,
  point_precision: 0.4,
  point_recall: 0.9,
  point_f1: 0.55,
  event_precision: 1.0,
  event_recall: 1.0,
  event_f1: f1,
  detections_per_incident: 62.0,
  alerts_per_incident: 1.0,
  incident_coverage: 1.0,
  mean_latency_seconds: 28.0,
  mean_rank_of_true: 1.0,
  mean_true_detection_overlap: 0.04,
  timeline: {
    truth: [[T0, at(300)]],
    detections: [[at(30), at(30)]],
    alerts: [[at(30), at(90)]],
    truncated: false,
  },
});

export const report = {
  corpus: "synthetic",
  corpus_description: "Synthetic rig telemetry: 11 recordings, 9 injected faults",
  fit_protocol: "none",
  cases: 2,
  incidents: 1,
  faults: 1,
  generated_at: T0,
  specs: [
    {
      spec: "stub_threshold",
      detectors: ["stub_threshold"],
      alert_engine: "weighted",
      cases: [
        caseReport("spike-0", 1.0),
        caseReport("clean-0", null, ["! stub_threshold rejected 100% of input"]),
      ],
      pooled: {
        event_f1: 1.0,
        event_precision: 1.0,
        event_recall: 1.0,
        point_f1: 0.55,
        detections: 124.0,
        alerts: 2.0,
        compression_ratio: 62.0,
      },
      pooled_by_lane: {},
    },
  ],
};
