/**
 * The two APIs, as functions. A pure client: it asks and it renders (H3).
 *
 * Query limits are ported from `dashboard/client.py` rather than re-chosen — the old page
 * asked for 5,000 events/detections and 1,000 alerts/malformed records, and a chart drawn
 * from a different slice of the same database is a different chart.
 */
import { apiPage, apiRequest, PIPELINE_BASE, READ_BASE } from "./http";
import {
  alertFacetsSchema,
  alertSchema,
  alertsSchema,
  assetsSchema,
  benchSpecsSchema,
  corporaSchema,
  connectionsSchema,
  dataSourceSchema,
  dataSourcesSchema,
  devicesSchema,
  detectionsSchema,
  detectorSummariesSchema,
  evaluationReportSchema,
  eventsSchema,
  healthSchema,
  itOverviewSchema,
  malformedListSchema,
  authOptionsSchema,
  meSchema,
  modulesSchema,
  noContentSchema,
  organizationAdminInfoSchema,
  organizationsAdminSchema,
  otOverviewSchema,
  previewSchema,
  pullSchema,
  qualityRecordsSchema,
  progressSchema,
  runSchema,
  savedRunSchema,
  savedRunsSchema,
  executionSchema,
  executionsSchema,
  jobSchema,
  jobsSchema,
  summarySchema,
  userInfoSchema,
  usersSchema,
  validateSchema,
  type EvalReport,
} from "./schemas";
import type { AlertSeverity, DataSourceRequest, JobRequest, Lane, OrgRole, PullRequest } from "./types";

const EVENT_LIMIT = 5000;
const DETECTION_LIMIT = 5000;
const ALERT_LIMIT = 1000;
const MALFORMED_LIMIT = 1000;
/** Anomaly records shown at once: the route's own ceiling is 1,000. */
const RECORDS_LIMIT = 500;
const SAVED_RUNS_LIMIT = 200;
/** Flows listed at once on the IT overview; the charts count all of them server-side. */
const FLOWS_LIMIT = 500;

/** Optional UTC instants bounding a list (`?start=&end=`); each route documents its clock. */
export interface TimeBounds {
  readonly start?: string;
  readonly end?: string;
}

/** A range of whole UTC days, both inclusive, as `YYYY-MM-DD`. */
export interface DayRange {
  readonly start: string;
  readonly end: string;
}

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

  // `runId` narrows any of the four lists to one run's rows (ADR-0045) — the Run page.
  // `start`/`end` are UTC instants (`lib/timeRange.ts: rangeQuery`), each optional.
  events: (options: { assetId?: string; lane?: Lane; runId?: string } & TimeBounds = {}) =>
    cappedList(EVENT_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/events", {
        schema: eventsSchema,
        query: {
          asset_id: options.assetId,
          lane: options.lane,
          run_id: options.runId,
          start: options.start,
          end: options.end,
          limit,
        },
      }),
    ),

  detections: (options: { assetId?: string; lane?: Lane; runId?: string } & TimeBounds = {}) =>
    cappedList(DETECTION_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/detections", {
        schema: detectionsSchema,
        query: {
          asset_id: options.assetId,
          lane: options.lane,
          run_id: options.runId,
          start: options.start,
          end: options.end,
          limit,
        },
      }),
    ),

  alerts: (options: { runId?: string } & TimeBounds = {}) =>
    cappedList(ALERT_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/alerts", {
        schema: alertsSchema,
        query: { run_id: options.runId, start: options.start, end: options.end, limit },
      }),
    ),

  /**
   * One page of the ranked queue, and how many alerts the same filters match in all. The
   * triage page pages through the whole queue this way rather than loading a capped prefix
   * of it, so `severity` and `assetId` are asked of the server: filtering one page here would
   * hide every match on the pages not fetched.
   */
  alertPage: (
    options: {
      runId?: string;
      severity?: AlertSeverity;
      assetId?: string;
      limit: number;
      offset: number;
    } & TimeBounds,
  ) =>
    apiPage(READ_BASE, "/alerts", {
      schema: alertsSchema,
      query: {
        run_id: options.runId,
        severity: options.severity,
        asset_id: options.assetId,
        start: options.start,
        end: options.end,
        limit: options.limit,
        offset: options.offset,
      },
    }),

  alert: (alertId: string) =>
    apiRequest(READ_BASE, `/alerts/${encodeURIComponent(alertId)}`, { schema: alertSchema }),

  /** The whole queue's severity counts and assets, under the same run and time scope. */
  alertFacets: (options: { runId?: string } & TimeBounds = {}) =>
    apiRequest(READ_BASE, "/alerts/facets", {
      schema: alertFacetsSchema,
      query: { run_id: options.runId, start: options.start, end: options.end },
    }),

  // Uncapped on purpose, and the only list here that is: an alert's evidence is the set of
  // detections that justify it, so a partial answer would be an incomplete argument rather
  // than a long list. The route takes no `limit` (`api/app.py`), so there is none to pass.
  alertDetections: (alertId: string) =>
    apiRequest(READ_BASE, `/alerts/${encodeURIComponent(alertId)}/detections`, {
      schema: detectionsSchema,
    }),

  malformed: (options: { runId?: string } & TimeBounds = {}) =>
    cappedList(MALFORMED_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/malformed", {
        schema: malformedListSchema,
        query: { run_id: options.runId, start: options.start, end: options.end, limit },
      }),
    ),

  // --- The IT and OT overviews (ADR-0044): counted on the server, whole UTC days ---------

  itOverview: (range: DayRange, bucket: "day" | "hour" = "day") =>
    apiRequest(READ_BASE, "/overview/it", { schema: itOverviewSchema, query: { ...range, bucket } }),

  otOverview: (range: DayRange, options: { assetId?: string; bucket?: "day" | "hour" } = {}) =>
    apiRequest(READ_BASE, "/overview/ot", {
      schema: otOverviewSchema,
      query: { ...range, asset_id: options.assetId, bucket: options.bucket },
    }),

  otRecords: (range: DayRange, options: { assetId?: string; issue?: string } = {}) =>
    cappedList(RECORDS_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/overview/ot/records", {
        schema: qualityRecordsSchema,
        query: { ...range, asset_id: options.assetId, issue: options.issue, limit },
      }),
    ),

  /** The flow events of a range of UTC days, newest last — the IT overview's table. */
  flows: (range: DayRange) =>
    cappedList(FLOWS_LIMIT, (limit) =>
      apiRequest(READ_BASE, "/events", {
        schema: eventsSchema,
        query: { lane: "flow", start: `${range.start}T00:00:00Z`, end: `${range.end}T23:59:59.999999Z`, limit },
      }),
    ),

  /** One meter's hourly consumption events for one UTC day. */
  meterDay: (assetId: string, day: string) =>
    apiRequest(READ_BASE, "/events", {
      schema: eventsSchema,
      query: {
        asset_id: assetId,
        lane: "telemetry",
        start: `${day}T00:00:00Z`,
        end: `${day}T23:59:59Z`,
        limit: 200,
      },
    }),
};

/**
 * One source's input, aligned to `spec.sources` by position: its text, or a window of a data
 * source for the server to read itself during the run (ADR-0047) — never both.
 */
export type SourceInput =
  | { readonly content: string; readonly origin: string }
  | {
      readonly pull: {
        readonly source_id: string;
        readonly start: string;
        readonly end: string;
        readonly params: Readonly<Record<string, unknown>>;
      };
      readonly origin: string;
    };

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

const runPath = (runId: string) => `/runs/${encodeURIComponent(runId)}`;
const jobPath = (jobId: string) => `/jobs/${encodeURIComponent(jobId)}`;

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

  /** Read one source with its normalizer only: nothing is detected or stored. */
  preview: (source: unknown, content: string, signal?: AbortSignal) =>
    apiRequest(PIPELINE_BASE, "/runs/preview", {
      schema: previewSchema,
      method: "POST",
      body: { source, content },
      ...(signal ? { signal } : {}),
    }),

  /** Run and save the chain; a blank `name` lets the server name it after its inputs (ADR-0046). */
  run: (spec: unknown, inputs: SourceInput[], name?: string, progressId?: string) =>
    apiRequest(PIPELINE_BASE, "/runs", {
      schema: runSchema,
      method: "POST",
      body: {
        spec,
        inputs,
        ...(name?.trim() ? { name: name.trim() } : {}),
        ...(progressId ? { progress_id: progressId } : {}),
      },
    }),

  /** How far the run posted with this `progressId` has got; 404 until it has started (ADR-0051). */
  runProgress: (progressId: string) =>
    apiRequest(PIPELINE_BASE, `/runs/progress/${encodeURIComponent(progressId)}`, {
      schema: progressSchema,
    }),

  // --- Saved runs (ADR-0046) -------------------------------------------------------------

  savedRuns: () =>
    cappedList(SAVED_RUNS_LIMIT, (limit) =>
      apiRequest(PIPELINE_BASE, "/runs", { schema: savedRunsSchema, query: { limit } }),
    ),

  savedRun: (runId: string) => apiRequest(PIPELINE_BASE, runPath(runId), { schema: savedRunSchema }),

  renameRun: (runId: string, name: string) =>
    apiRequest(PIPELINE_BASE, runPath(runId), { schema: savedRunSchema, method: "PATCH", body: { name } }),

  /** Admins only: deletes the run and every result it stored. */
  deleteRun: (runId: string) =>
    apiRequest(PIPELINE_BASE, runPath(runId), { schema: noContentSchema, method: "DELETE" }),

  // --- Saved jobs (ADR-0050) -------------------------------------------------------------

  jobs: () => apiRequest(PIPELINE_BASE, "/jobs", { schema: jobsSchema }),

  job: (jobId: string) => apiRequest(PIPELINE_BASE, jobPath(jobId), { schema: jobSchema }),

  /** Save a chain of pulls as a job; refused (422) for whatever would refuse its run. */
  createJob: (body: JobRequest) =>
    apiRequest(PIPELINE_BASE, "/jobs", { schema: jobSchema, method: "POST", body }),

  updateJob: (jobId: string, body: JobRequest) =>
    apiRequest(PIPELINE_BASE, jobPath(jobId), { schema: jobSchema, method: "PUT", body }),

  /** Admins only: the job and its history go; the runs it made stay. */
  deleteJob: (jobId: string) =>
    apiRequest(PIPELINE_BASE, jobPath(jobId), { schema: noContentSchema, method: "DELETE" }),

  /** Queue the job for the worker; 409 while it is already queued or running. */
  runJob: (jobId: string) =>
    apiRequest(PIPELINE_BASE, `${jobPath(jobId)}/run`, { schema: executionSchema, method: "POST" }),

  jobExecutions: (jobId: string) =>
    apiRequest(PIPELINE_BASE, `${jobPath(jobId)}/executions`, { schema: executionsSchema }),

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
 * The account routes (ADR-0039), on the Pipeline API — the plane that writes.
 *
 * Mounted only when the deployment sets IOTSHIELD_AUTH, absent otherwise, exactly like the
 * eval router: `me()` answering 404 is how the dashboard learns there is no login here.
 */
export const authApi = {
  me: () => apiRequest(PIPELINE_BASE, "/auth/me", { schema: meSchema }),

  /** Public: whether to offer registration (IOTSHIELD_OPEN_REGISTRATION, ADR-0040). */
  options: () => apiRequest(PIPELINE_BASE, "/auth/options", { schema: authOptionsSchema }),

  login: (username: string, password: string) =>
    apiRequest(PIPELINE_BASE, "/auth/login", {
      schema: meSchema,
      method: "POST",
      body: { username, password },
    }),

  register: (organization: string, username: string, password: string) =>
    apiRequest(PIPELINE_BASE, "/auth/register", {
      schema: meSchema,
      method: "POST",
      body: { organization, username, password },
    }),

  logout: () =>
    apiRequest(PIPELINE_BASE, "/auth/logout", { schema: noContentSchema, method: "POST" }),

  changePassword: (currentPassword: string, newPassword: string) =>
    apiRequest(PIPELINE_BASE, "/auth/password", {
      schema: noContentSchema,
      method: "POST",
      body: { current_password: currentPassword, new_password: newPassword },
    }),

  users: () => apiRequest(PIPELINE_BASE, "/auth/users", { schema: usersSchema }),

  addUser: (username: string, password: string, role: OrgRole) =>
    apiRequest(PIPELINE_BASE, "/auth/users", {
      schema: userInfoSchema,
      method: "POST",
      body: { username, password, role },
    }),

  removeUser: (userId: string) =>
    apiRequest(PIPELINE_BASE, `/auth/users/${encodeURIComponent(userId)}`, {
      schema: noContentSchema,
      method: "DELETE",
    }),
};

const sourcePath = (sourceId: string) => `/data-sources/${encodeURIComponent(sourceId)}`;

/** The organization's IT/OT data sources (ADR-0041): anyone lists, admins write. */
export const dataSourcesApi = {
  list: () => apiRequest(PIPELINE_BASE, "/data-sources", { schema: dataSourcesSchema }),

  add: (body: DataSourceRequest) =>
    apiRequest(PIPELINE_BASE, "/data-sources", { schema: dataSourceSchema, method: "POST", body }),

  update: (sourceId: string, body: DataSourceRequest) =>
    apiRequest(PIPELINE_BASE, sourcePath(sourceId), { schema: dataSourceSchema, method: "PUT", body }),

  remove: (sourceId: string) =>
    apiRequest(PIPELINE_BASE, sourcePath(sourceId), { schema: noContentSchema, method: "DELETE" }),

  /** The source databases this organization may pull from (ADR-0042). */
  connections: () => apiRequest(PIPELINE_BASE, "/data-sources/connections", { schema: connectionsSchema }),

  /** What a pull can be narrowed to (meters, hosts); `params` filter it, e.g. the utility. */
  devices: (sourceId: string, params: Record<string, string>) => {
    const query = new URLSearchParams(params).toString();
    return apiRequest(PIPELINE_BASE, `${sourcePath(sourceId)}/devices${query ? `?${query}` : ""}`, {
      schema: devicesSchema,
    });
  },

  /** A date range of the source's database, as text its normalizer reads. */
  pull: (sourceId: string, body: PullRequest) =>
    apiRequest(PIPELINE_BASE, `${sourcePath(sourceId)}/pull`, { schema: pullSchema, method: "POST", body }),
};

const orgPath = (orgId: string) => `/admin/organizations/${encodeURIComponent(orgId)}`;
const userPath = (orgId: string, userId: string) =>
  `${orgPath(orgId)}/users/${encodeURIComponent(userId)}`;

/**
 * The super admin's routes (ADR-0040). Every one answers 403 to anybody else, so the pages
 * that call these are only ever rendered for a super admin.
 */
export const adminApi = {
  organizations: () =>
    apiRequest(PIPELINE_BASE, "/admin/organizations", { schema: organizationsAdminSchema }),

  organization: (orgId: string) =>
    apiRequest(PIPELINE_BASE, orgPath(orgId), { schema: organizationAdminInfoSchema }),

  createOrganization: (organization: string, username: string, password: string) =>
    apiRequest(PIPELINE_BASE, "/admin/organizations", {
      schema: organizationAdminInfoSchema,
      method: "POST",
      body: { organization, username, password },
    }),

  setSuspended: (orgId: string, suspended: boolean) =>
    apiRequest(PIPELINE_BASE, `${orgPath(orgId)}/${suspended ? "suspend" : "reactivate"}`, {
      schema: organizationAdminInfoSchema,
      method: "POST",
    }),

  /** Irreversible. `confirm` must repeat the id; the server checks it too. */
  deleteOrganization: (orgId: string, confirm: string) =>
    apiRequest(PIPELINE_BASE, orgPath(orgId), {
      schema: noContentSchema,
      method: "DELETE",
      query: { confirm },
    }),

  users: (orgId: string) =>
    apiRequest(PIPELINE_BASE, `${orgPath(orgId)}/users`, { schema: usersSchema }),

  addUser: (orgId: string, username: string, password: string, role: OrgRole) =>
    apiRequest(PIPELINE_BASE, `${orgPath(orgId)}/users`, {
      schema: userInfoSchema,
      method: "POST",
      body: { username, password, role },
    }),

  setRole: (orgId: string, userId: string, role: OrgRole) =>
    apiRequest(PIPELINE_BASE, userPath(orgId, userId), {
      schema: userInfoSchema,
      method: "PATCH",
      body: { role },
    }),

  resetPassword: (orgId: string, userId: string, password: string) =>
    apiRequest(PIPELINE_BASE, `${userPath(orgId, userId)}/password`, {
      schema: noContentSchema,
      method: "POST",
      body: { password },
    }),

  removeUser: (orgId: string, userId: string) =>
    apiRequest(PIPELINE_BASE, userPath(orgId, userId), { schema: noContentSchema, method: "DELETE" }),
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
