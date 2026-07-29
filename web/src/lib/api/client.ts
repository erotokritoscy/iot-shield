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

/**
 * The server's own ceiling on `limit` (`api/app.py: LimitParam`, `le=10_000`).
 *
 * Named here because `cappedList` asks for `limit + 1`: a client limit set to the documented
 * maximum would request 10,001 and take a 422, turning "this list is long" into "this page is
 * broken". The assert below is the one place that knows both numbers.
 */
const SERVER_LIMIT_CEILING = 10_000;

/**
 * A capped list, and whether the cap actually bit.
 *
 * Every list endpoint that takes a `limit` can return a *prefix* of the truth and look
 * identical to the whole of it. That is not a hypothetical: `/alerts` is
 * ordered priority-descending, so a queue capped at 1,000 drops precisely the low-priority
 * tail — and a severity chart drawn from it is skewed toward the top of the queue while the
 * KPI tile beside it reports the real total. Rendering a subset as if it were everything is
 * the one thing an operational console must not do.
 */
export interface Capped<T> {
  readonly rows: readonly T[];
  /** The server had more than we render. */
  readonly truncated: boolean;
  readonly limit: number;
}

/**
 * Fetch a list by asking for one more row than we intend to show.
 *
 * The alternative — comparing `rows.length` against `/summary.counts` — is wrong for the
 * filtered lists: `counts.events` is a global row count while `/events` is per asset and per
 * lane, so every asset with fewer than 5,000 readings would falsely report truncation.
 * `limit + 1` asks the only question that generalises, of the same query, with no second
 * request: *is there a row beyond the ones I am about to draw?*
 *
 * The `+1` and the slice live here together on purpose. Spelled at each call site they are
 * two numbers that must agree, and an endpoint that asked for `limit` while slicing at
 * `limit` would report `truncated: false` forever — the exact defect this exists to fix,
 * invisible to inspection and to any test that does not seed past the cap.
 */
async function cappedList<T>(
  limit: number,
  request: (ask: number) => Promise<readonly T[]>,
): Promise<Capped<T>> {
  if (limit >= SERVER_LIMIT_CEILING) throw new Error(`limit ${limit} leaves no room for the +1 probe`);
  const rows = await request(limit + 1);
  return { rows: rows.slice(0, limit), truncated: rows.length > limit, limit };
}

export const readApi = {
  health: (signal?: AbortSignal) =>
    apiRequest(READ_BASE, "/health", { schema: healthSchema, ...(signal ? { signal } : {}) }),

  summary: () => apiRequest(READ_BASE, "/summary", { schema: summarySchema }),

  assets: () => apiRequest(READ_BASE, "/assets", { schema: assetsSchema }),

  events: (options: { assetId?: string; lane?: Lane } = {}) =>
    cappedList(EVENT_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/events", {
        schema: eventsSchema,
        query: { asset_id: options.assetId, lane: options.lane, limit },
      }),
    ),

  detections: (options: { assetId?: string; lane?: Lane } = {}) =>
    cappedList(DETECTION_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/detections", {
        schema: detectionsSchema,
        query: { asset_id: options.assetId, lane: options.lane, limit },
      }),
    ),

  alerts: () =>
    cappedList(ALERT_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/alerts", { schema: alertsSchema, query: { limit } }),
    ),

  // Uncapped on purpose, and the only list here that is: an alert's evidence is the set of
  // detections that justify it, so a partial answer would be an incomplete argument rather
  // than a long list. The route takes no `limit` (`api/app.py`), so there is none to pass.
  alertDetections: (alertId: string) =>
    apiRequest(READ_BASE, `/alerts/${encodeURIComponent(alertId)}/detections`, {
      schema: detectionsSchema,
    }),

  malformed: () =>
    cappedList(MALFORMED_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/malformed", { schema: malformedListSchema, query: { limit } }),
    ),
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
  readonly specs: readonly EvalEntry[];
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
