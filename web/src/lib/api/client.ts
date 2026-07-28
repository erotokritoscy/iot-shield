/**
 * The two APIs, as functions. A pure client: it asks and it renders (H3).
 *
 * Query limits are ported from `dashboard/client.py` rather than re-chosen — the old page
 * asked for 5,000 events/detections and 1,000 alerts/malformed records, and a chart drawn
 * from a different slice of the same database is a different chart.
 */
import { apiRequest, PIPELINE_BASE, READ_BASE } from "./http";
import {
  alertsSchema,
  assetsSchema,
  benchSpecsSchema,
  corporaSchema,
  detectionsSchema,
  detectorSummariesSchema,
  evaluationReportSchema,
  eventsSchema,
  healthSchema,
  malformedListSchema,
  modulesSchema,
  runSchema,
  summarySchema,
  validateSchema,
  type EvalReport,
} from "./schemas";
import type { Lane } from "./types";

const EVENT_LIMIT = 5000;
const DETECTION_LIMIT = 5000;
const ALERT_LIMIT = 1000;
const MALFORMED_LIMIT = 1000;

export const readApi = {
  health: (signal?: AbortSignal) =>
    apiRequest(READ_BASE, "/health", { schema: healthSchema, ...(signal ? { signal } : {}) }),

  summary: () => apiRequest(READ_BASE, "/summary", { schema: summarySchema }),

  assets: () => apiRequest(READ_BASE, "/assets", { schema: assetsSchema }),

  events: (options: { assetId?: string; lane?: Lane } = {}) =>
    apiRequest(READ_BASE, "/events", {
      schema: eventsSchema,
      query: { asset_id: options.assetId, lane: options.lane, limit: EVENT_LIMIT },
    }),

  detections: (options: { assetId?: string; lane?: Lane } = {}) =>
    apiRequest(READ_BASE, "/detections", {
      schema: detectionsSchema,
      query: { asset_id: options.assetId, lane: options.lane, limit: DETECTION_LIMIT },
    }),

  alerts: () =>
    apiRequest(READ_BASE, "/alerts", { schema: alertsSchema, query: { limit: ALERT_LIMIT } }),

  alertDetections: (alertId: string) =>
    apiRequest(READ_BASE, `/alerts/${encodeURIComponent(alertId)}/detections`, {
      schema: detectionsSchema,
    }),

  malformed: () =>
    apiRequest(READ_BASE, "/malformed", {
      schema: malformedListSchema,
      query: { limit: MALFORMED_LIMIT },
    }),
};

/** One input source's raw payload, aligned to `spec.sources` by position. */
export interface SourceInput {
  readonly content: string;
  readonly origin: string;
}

/** One `/eval/run` entry. `spec` is opaque here on purpose — see `benchSpecSchema`. */
export interface EvalEntry {
  readonly label: string;
  readonly spec: Record<string, unknown>;
  readonly sweep: Record<string, unknown[]>;
}

export interface EvalRunRequest {
  readonly corpus: string;
  readonly specs: EvalEntry[];
  readonly fit_protocol: string;
  readonly include_timeline: boolean;
}

export const pipelineApi = {
  health: (signal?: AbortSignal) =>
    apiRequest(PIPELINE_BASE, "/health", { schema: healthSchema, ...(signal ? { signal } : {}) }),

  modules: () => apiRequest(PIPELINE_BASE, "/modules", { schema: modulesSchema }),

  validate: (spec: unknown, signal?: AbortSignal) =>
    apiRequest(PIPELINE_BASE, "/runs/validate", {
      schema: validateSchema,
      method: "POST",
      body: { spec },
      ...(signal ? { signal } : {}),
    }),

  run: (spec: unknown, inputs: SourceInput[]) =>
    apiRequest(PIPELINE_BASE, "/runs", {
      schema: runSchema,
      method: "POST",
      body: { spec, inputs },
    }),

  // The eval router exists only when IOTSHIELD_EVAL_API is set — absent, not guarded
  // (ADR-0021 D1). That absence is what the mode probes for (D5): 404 here means the plane
  // is off, which is a different fact from "the request failed".
  corpora: (signal?: AbortSignal) =>
    apiRequest(PIPELINE_BASE, "/eval/corpora", {
      schema: corporaSchema,
      ...(signal ? { signal } : {}),
    }),

  evalDetectors: () =>
    apiRequest(PIPELINE_BASE, "/eval/detectors", { schema: detectorSummariesSchema }),

  evalSpecs: () => apiRequest(PIPELINE_BASE, "/eval/specs", { schema: benchSpecsSchema }),

  evalRun: (request: EvalRunRequest): Promise<EvalReport> =>
    apiRequest(PIPELINE_BASE, "/eval/run", {
      schema: evaluationReportSchema,
      method: "POST",
      body: request,
    }),
};

/**
 * Most spans a report records per lane per case.
 *
 * Duplicated from `iotshield/eval/report.py: TIMELINE_SPAN_CAP`, which is the source of
 * truth — the value is not published on the wire and H5 forbids adding an endpoint to
 * publish it. Named here rather than inlined so the truncation warning and this comment
 * travel together; if the harness changes the cap, this line is what has to follow.
 */
export const TIMELINE_SPAN_CAP = 2000;
