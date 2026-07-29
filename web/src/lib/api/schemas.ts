/**
 * Runtime validation at the API boundary (D4) — the half of ADR-0008 that types cannot do.
 *
 * Two mechanisms, deliberately both:
 *
 * 1. `types.ts` is generated from each app's own OpenAPI document, so a *compile* fails when
 *    the server's schema and this client disagree.
 * 2. These zod schemas run on every response, so a *deployment* fails loudly at one boundary
 *    when a live API drifts from what this bundle was built against — the exact scenario the
 *    old `test_contract_drift_mid_page…` pins.
 *
 * The `Satisfies` assertions below wire the two together: each schema is checked, at compile
 * time, against the generated server type it validates. Adding a field to a contract without
 * teaching this file about it is then a build error rather than a silent runtime strip.
 *
 * Where a table renders *whatever columns the report carries* (the per-case table, the
 * head-to-head summary), the schema is `.passthrough()`: stripping unknown keys would
 * silently drop a metric the harness added, which is the failure mode E12/E15 exist to
 * prevent. Those schemas assert nothing at compile time, because a passthrough index
 * signature is by construction wider than the generated type.
 */
import { z } from "zod";

import type {
  AlertSeverity,
  Asset,
  CorporaResponse,
  Criticality,
  DetectionResult,
  DetectorSummary,
  Lane,
  MalformedRecord,
  ModulesResponse,
  NormalizedEvent,
  PrioritizedAlert,
  RunResponse,
  Summary,
  ValidateResponse,
} from "./types";

/**
 * `true` when the live server type and what we parse are the *same* shape; a type error
 * otherwise.
 *
 * Mutual, not one-way. `Server extends Parsed` alone is satisfied by a server type with
 * EXTRA properties — structural subtyping — so a field added to a contract compiled clean
 * and was then stripped at runtime by zod, which is the precise failure the comment at the
 * top of this file promises to catch. Asking both directions is what makes that promise true.
 *
 * The tuple wrappers are load-bearing: a bare `Server extends Parsed` over a naked type
 * parameter *distributes* across unions, so each member of `Lane` would be tested alone
 * against the whole union and the enum asserts below would all fail. `[Server] extends
 * [Parsed]` compares the union as one thing.
 */
type Drift<Server, Parsed> = {
  drift: "server type and parsed shape have diverged";
  server: Server;
  parsed: Parsed;
};
type Satisfies<Server, Parsed> = [Server] extends [Parsed]
  ? [Parsed] extends [Server]
    ? true
    : Drift<Server, Parsed>
  : Drift<Server, Parsed>;
type Assert<T extends true> = T;

const jsonObject = z.record(z.unknown());

/**
 * The closed vocabularies, restated once and *checked* against the server's own enums by the
 * asserts beside them. The old typed client validated every response against the pydantic
 * contracts, so an out-of-vocabulary severity failed loudly rather than rendering as an
 * unstyled badge — keeping these as enums rather than bare strings is what preserves that.
 * Adding a value server-side without adding it here is a build error, not a runtime surprise.
 */
const laneSchema = z.enum(["telemetry", "log", "flow"]);
const severitySchema = z.enum(["low", "medium", "high", "critical", "emergency"]);
const statusSchema = z.enum(["new", "acknowledged", "closed"]);
const criticalitySchema = z.enum(["low", "medium", "high", "critical"]);
const eventSeveritySchema = z.enum(["debug", "info", "warning", "error", "critical"]);
const eventKindSchema = z.enum(["point", "contextual", "collective"]);

export type _LaneOk = Assert<Satisfies<Lane, z.infer<typeof laneSchema>>>;
export type _SeverityOk = Assert<Satisfies<AlertSeverity, z.infer<typeof severitySchema>>>;
export type _CriticalityOk = Assert<Satisfies<Criticality, z.infer<typeof criticalitySchema>>>;

// --- Read plane ---------------------------------------------------------------

export const healthSchema = z.record(z.string());

export const assetSchema = z.object({
  asset_id: z.string(),
  name: z.string(),
  asset_type: z.string(),
  location: z.string().nullish(),
  criticality: criticalitySchema,
  metadata: jsonObject.optional(),
});
export type _AssetOk = Assert<Satisfies<Asset, z.infer<typeof assetSchema>>>;

export const eventSchema = z.object({
  event_id: z.string().optional(),
  timestamp: z.string(),
  asset_id: z.string(),
  source_type: laneSchema,
  source_format: z.string(),
  metric: z.string().nullish(),
  value: z.number().nullish(),
  severity: eventSeveritySchema,
  message: z.string().nullish(),
  raw: z.string(),
  metadata: jsonObject.optional(),
});
export type _EventOk = Assert<Satisfies<NormalizedEvent, z.infer<typeof eventSchema>>>;

export const detectionSchema = z.object({
  detection_id: z.string().optional(),
  asset_id: z.string(),
  lane: laneSchema,
  method: z.string(),
  score: z.number(),
  threshold: z.number(),
  window_start: z.string(),
  window_end: z.string(),
  event_kind: eventKindSchema.nullish(),
  evidence: jsonObject.optional(),
  metadata: jsonObject.optional(),
});
export type _DetectionOk = Assert<Satisfies<DetectionResult, z.infer<typeof detectionSchema>>>;

export const alertSchema = z.object({
  alert_id: z.string().optional(),
  asset_id: z.string(),
  severity: severitySchema,
  priority_score: z.number(),
  threat_class: z.string(),
  rationale: z.string(),
  score_components: jsonObject.optional(),
  detection_ids: z.array(z.string()),
  recommended_action: z.string().nullish(),
  status: statusSchema,
  window_start: z.string(),
  window_end: z.string(),
  created_at: z.string().optional(),
});
export type _AlertOk = Assert<Satisfies<PrioritizedAlert, z.infer<typeof alertSchema>>>;

export const malformedSchema = z.object({
  record_id: z.string().optional(),
  raw: z.string(),
  error: z.string(),
  source_format: z.string(),
  origin: z.string(),
  created_at: z.string().optional(),
});
export type _MalformedOk = Assert<Satisfies<MalformedRecord, z.infer<typeof malformedSchema>>>;

export const summarySchema = z.object({
  counts: z.record(z.number()),
  top_alert: alertSchema.nullable(),
});
export type _SummaryOk = Assert<Satisfies<Summary, z.infer<typeof summarySchema>>>;

export const assetsSchema = z.array(assetSchema);
export const eventsSchema = z.array(eventSchema);
export const detectionsSchema = z.array(detectionSchema);
export const alertsSchema = z.array(alertSchema);
export const malformedListSchema = z.array(malformedSchema);

// --- Pipeline plane -----------------------------------------------------------

export const paramInfoSchema = z.object({
  name: z.string(),
  type: z.string().nullish(),
  default: z.unknown(),
  required: z.boolean(),
  choices: z.array(z.string()).nullish(),
});

export const moduleInfoSchema = z.object({
  name: z.string(),
  kind: z.string(),
  consumes: z.array(z.string()),
  produces: z.array(z.string()),
  formats: z.array(z.string()),
  params: z.array(paramInfoSchema),
  artifact_param: z.string().nullish(),
});

export const sinkInfoSchema = z.object({
  name: z.string(),
  params: z.array(paramInfoSchema),
});

export const modulesSchema = z.object({
  normalizers: z.array(moduleInfoSchema),
  detectors: z.array(moduleInfoSchema),
  alert_engines: z.array(moduleInfoSchema),
  sinks: z.array(sinkInfoSchema),
});
export type _ModulesOk = Assert<Satisfies<ModulesResponse, z.infer<typeof modulesSchema>>>;

export const validateSchema = z.object({
  ok: z.boolean(),
  issues: z.array(z.object({ level: z.string(), stage: z.string(), message: z.string() })),
});
export type _ValidateOk = Assert<Satisfies<ValidateResponse, z.infer<typeof validateSchema>>>;

export const runSchema = z.object({
  events: z.number(),
  malformed: z.number(),
  detections: z.number(),
  alerts: z.number(),
  warnings: z.array(z.string()),
  destinations: z.array(z.string()),
  top_alerts: z.array(alertSchema),
});
export type _RunOk = Assert<Satisfies<RunResponse, z.infer<typeof runSchema>>>;

// --- Evaluation plane ---------------------------------------------------------

export const corpusSummarySchema = z.object({
  name: z.string(),
  description: z.string(),
  provenance: z.string(),
  licence: z.string(),
  cases: z.number(),
  source_formats: z.array(z.string()).optional(),
  lanes: z.array(z.string()).optional(),
  // Always present (empty for a healthy corpus): a broken bundle is published rather than
  // filtered out, so it is visibly broken instead of silently absent from the picker (E2).
  error: z.string(),
});

export const corporaSchema = z.object({
  root: z.string(),
  corpora: z.array(corpusSummarySchema),
  fetchable: z.record(z.string()).optional(),
});
export type _CorporaOk = Assert<Satisfies<CorporaResponse, z.infer<typeof corporaSchema>>>;

export const detectorSummarySchema = z.object({
  name: z.string(),
  lanes: z.array(z.string()),
  fittable: z.boolean(),
  params: z.array(paramInfoSchema).optional(),
  artifact_param: z.string().nullish(),
});
export type _DetectorOk = Assert<Satisfies<DetectorSummary, z.infer<typeof detectorSummarySchema>>>;

export const detectorSummariesSchema = z.array(detectorSummarySchema);

export const benchSpecSchema = z.object({
  name: z.string(),
  // The spec travels **verbatim** to `/eval/run` (E4), so it is carried as opaque JSON
  // rather than re-modelled: re-modelling it is precisely how the old page came to send a
  // lookalike that dropped `assets:` and every source parameter.
  spec: jsonObject,
  source_formats: z.array(z.string()).optional(),
  detectors: z.array(z.string()).optional(),
  corpora: z.array(z.string()).optional(),
});
export const benchSpecsSchema = z.array(benchSpecSchema);
export type BenchSpec = z.infer<typeof benchSpecSchema>;

const spanSchema = z.tuple([z.string(), z.string()]);

export const caseTimelineSchema = z.object({
  truth: z.array(spanSchema),
  detections: z.array(spanSchema),
  alerts: z.array(spanSchema),
  truncated: z.boolean(),
});

/** Passthrough: the per-case table renders every column the report carries (E15). */
export const caseReportSchema = z
  .object({
    case: z.string(),
    incidents: z.number(),
    detections: z.number(),
    alerts: z.number(),
    fitted: z.boolean(),
    notes: z.array(z.string()),
    event_f1: z.number().nullable(),
    timeline: caseTimelineSchema.nullish(),
  })
  .passthrough();

/** Passthrough for the same reason: `pooled` is whatever `summary()` serialized (E12). */
export const specReportSchema = z
  .object({
    spec: z.string(),
    detectors: z.array(z.string()),
    alert_engine: z.string(),
    cases: z.array(caseReportSchema),
    pooled: z.record(z.number().nullable()),
  })
  .passthrough();

export const evaluationReportSchema = z
  .object({
    corpus: z.string(),
    corpus_description: z.string(),
    fit_protocol: z.string(),
    cases: z.number(),
    incidents: z.number(),
    faults: z.number().optional(),
    generated_at: z.string(),
    specs: z.array(specReportSchema),
  })
  .passthrough();

export type EvalReport = z.infer<typeof evaluationReportSchema>;
export type EvalSpecReport = z.infer<typeof specReportSchema>;
export type EvalCaseReport = z.infer<typeof caseReportSchema>;
export type EvalCaseTimeline = z.infer<typeof caseTimelineSchema>;
