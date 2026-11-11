/**
 * Both APIs, mocked at the network boundary (D8).
 *
 * The moral equivalent of the old suite injecting FastAPI's `TestClient` into the typed
 * client: the app under test makes real `fetch` calls over its real client module, and only
 * the wire is faked — so the fetch wrapper, the zod validation and the error mapping are all
 * exercised rather than stubbed past.
 */
import { HttpResponse, http, type RequestHandler } from "msw";
import { setupServer } from "msw/node";

import * as seed from "./fixtures";

export const API = "http://localhost/api";
export const PIPELINE = "http://localhost/pipeline";

type Alert = typeof seed.alert;

/**
 * `/alerts` and `/alerts/facets` the way the read API answers them: severity, asset and run
 * filtered, paged by `limit`/`offset`, with `X-Total-Count`. Faithful because the triage page
 * now asks the server for every filter and page — a mock that ignored them would let a
 * client-side filter pass. A row's `run_id` is the test's to set (`{ ...alert, run_id }`).
 */
export function alertHandlers(alerts: readonly (Alert & { run_id?: string })[]): RequestHandler[] {
  const scoped = (params: URLSearchParams) =>
    alerts.filter((row) => {
      const run = params.get("run_id");
      // A row the test gave no run belongs to every run, so the seeded world answers a run.
      return run === null || row.run_id === undefined || row.run_id === run;
    });
  return [
    http.get(`${API}/alerts`, ({ request }) => {
      const params = new URL(request.url).searchParams;
      const severity = params.get("severity");
      const asset = params.get("asset_id");
      const matched = scoped(params).filter(
        (row) => (severity === null || row.severity === severity) && (asset === null || row.asset_id === asset),
      );
      const offset = Number(params.get("offset") ?? 0);
      const limit = Number(params.get("limit") ?? 1000);
      return HttpResponse.json(matched.slice(offset, offset + limit), {
        headers: { "X-Total-Count": String(matched.length) },
      });
    }),
    http.get(`${API}/alerts/facets`, ({ request }) => {
      const rows = scoped(new URL(request.url).searchParams);
      const bySeverity: Record<string, number> = {};
      for (const row of rows) bySeverity[row.severity] = (bySeverity[row.severity] ?? 0) + 1;
      return HttpResponse.json({
        total: rows.length,
        by_severity: bySeverity,
        assets: [...new Set(rows.map((row) => row.asset_id))].sort(),
      });
    }),
    http.get(`${API}/alerts/:id`, ({ params }) => {
      const found = alerts.find((row) => row.alert_id === params["id"]);
      return found ? HttpResponse.json(found) : HttpResponse.json({ detail: "not found" }, { status: 404 });
    }),
  ];
}

/** The seeded world: pump-02 at T0, one detection, one HIGH alert, one malformed record. */
export const seededHandlers: RequestHandler[] = [
  http.get(`${API}/health`, () => HttpResponse.json({ status: "ok", version: "test" })),
  http.get(`${API}/summary`, () => HttpResponse.json(seed.summary)),
  http.get(`${API}/assets`, () => HttpResponse.json([seed.asset])),
  http.get(`${API}/events`, () => HttpResponse.json(seed.events)),
  http.get(`${API}/detections`, () => HttpResponse.json([seed.detection])),
  http.get(`${API}/alerts/:id/detections`, () => HttpResponse.json([seed.detection])),
  ...alertHandlers([seed.alert]),
  http.get(`${API}/malformed`, () => HttpResponse.json([seed.malformed])),
];

/** The state every fresh `docker compose up` starts in. */
export const emptyHandlers: RequestHandler[] = [
  http.get(`${API}/health`, () => HttpResponse.json({ status: "ok", version: "test" })),
  http.get(`${API}/summary`, () => HttpResponse.json(seed.emptySummary)),
  http.get(`${API}/assets`, () => HttpResponse.json([])),
  http.get(`${API}/events`, () => HttpResponse.json([])),
  http.get(`${API}/detections`, () => HttpResponse.json([])),
  http.get(`${API}/alerts/:id/detections`, () => HttpResponse.json([])),
  ...alertHandlers([]),
  http.get(`${API}/malformed`, () => HttpResponse.json([])),
];

/** Whatever the last run POSTed — the request body a parity test asserts against (C4). */
export const captured: {
  runBody: unknown;
  validateBody: unknown;
  evalBody: unknown;
  previewBodies: unknown[];
  renameBody: unknown;
  deletedRun: string | null;
} = {
  renameBody: null,
  deletedRun: null,
  runBody: null,
  validateBody: null,
  evalBody: null,
  previewBodies: [],
};

/** The id `POST /runs` answers with — what the result tiles link to (ADR-0045). */
export const RUN_ID = seed.savedRun.run_id;

export const pipelineHandlers: RequestHandler[] = [
  http.get(`${PIPELINE}/health`, () => HttpResponse.json({ status: "ok", version: "test" })),
  http.get(`${PIPELINE}/modules`, () => HttpResponse.json(seed.modules)),
  http.get(`${PIPELINE}/data-sources`, () => HttpResponse.json([])),
  http.get(`${PIPELINE}/data-sources/connections`, () => HttpResponse.json([])),
  // Saved jobs (ADR-0050): none unless a test serves some.
  http.get(`${PIPELINE}/jobs`, () => HttpResponse.json([])),
  http.post(`${PIPELINE}/runs/validate`, async ({ request }) => {
    captured.validateBody = await request.json();
    return HttpResponse.json({ ok: true, issues: [] });
  }),
  // Every source reads cleanly unless a test says otherwise: one record per non-blank line.
  http.post(`${PIPELINE}/runs/preview`, async ({ request }) => {
    const body = (await request.json()) as { content: string };
    captured.previewBodies.push(body);
    const events = body.content.split("\n").filter((line) => line.trim()).length;
    return HttpResponse.json({ events, malformed: 0, level: "clean", rejected: [] });
  }),
  // Saved runs (ADR-0046): one saved run, renamable and deletable.
  // A run's progress (ADR-0051): two path segments, so it never reaches `/runs/:id` below.
  http.get(`${PIPELINE}/runs/progress/:id`, () =>
    HttpResponse.json({ phase: "reading", done: 412_000, total: 968_000, elapsed_s: 102 }),
  ),
  http.get(`${PIPELINE}/runs`, () => HttpResponse.json([seed.savedRun])),
  http.get(`${PIPELINE}/runs/:id`, ({ params }) =>
    params["id"] === seed.savedRun.run_id
      ? HttpResponse.json(seed.savedRun)
      : HttpResponse.json({ detail: "run not found" }, { status: 404 }),
  ),
  http.patch(`${PIPELINE}/runs/:id`, async ({ request }) => {
    const body = (await request.json()) as { name: string };
    captured.renameBody = body;
    return HttpResponse.json({ ...seed.savedRun, name: body.name });
  }),
  http.delete(`${PIPELINE}/runs/:id`, ({ params }) => {
    captured.deletedRun = String(params["id"]);
    return new HttpResponse(null, { status: 204 });
  }),
  http.post(`${PIPELINE}/runs`, async ({ request }) => {
    captured.runBody = await request.json();
    return HttpResponse.json({
      run_id: RUN_ID,
      events: 1,
      malformed: 0,
      detections: 1,
      alerts: 1,
      warnings: [],
      destinations: ["postgres"],
      top_alerts: [seed.alert],
    });
  }),
];

/** The `/eval` router, which exists only when IOTSHIELD_EVAL_API is set (ADR-0021 D1). */
export const evalHandlers: RequestHandler[] = [
  http.get(`${PIPELINE}/eval/corpora`, () => HttpResponse.json(seed.corpora)),
  http.get(`${PIPELINE}/eval/detectors`, () => HttpResponse.json(seed.evalDetectors)),
  http.get(`${PIPELINE}/eval/specs`, () => HttpResponse.json(seed.benchSpecs)),
  http.post(`${PIPELINE}/eval/run`, async ({ request }) => {
    captured.evalBody = await request.json();
    return HttpResponse.json(seed.report);
  }),
];

/** No eval plane: the router was never registered, so every `/eval` path is a 404 (D5). */
export const evalAbsentHandlers: RequestHandler[] = [
  http.get(`${PIPELINE}/eval/*`, () => HttpResponse.json({ detail: "Not Found" }, { status: 404 })),
];

/**
 * No login: the `/auth` router was never registered, so `/auth/me` is a 404 (ADR-0039). The
 * default world, because it is what every existing test was written against — a deployment
 * without `IOTSHIELD_AUTH`, where the dashboard behaves exactly as it did before accounts.
 */
export const authAbsentHandlers: RequestHandler[] = [
  http.all(`${PIPELINE}/auth/*`, () => HttpResponse.json({ detail: "Not Found" }, { status: 404 })),
];

/**
 * The Compose wizard's sample library (`/samples/`, static files beside the bundle). A small
 * stand-in for `scripts/make_sample_data.py`'s output: one telemetry sample, one syslog sample,
 * and one scenario over the first.
 */
export const SAMPLES = "http://localhost/samples";
export const SAMPLE_CSV = "2026-08-03T09:00:00+00:00,pump-01,Temperature,91.0\n";
export const SAMPLE_SYSLOG = "<38>1 2026-08-03T09:00:00Z gw sshd 1 - - Failed password for root\n";
export const sampleManifest = {
  samples: [
    {
      id: "pump_telemetry",
      file: "pump_telemetry.csv",
      title: "Overheating pump — telemetry",
      description: "pump-01 climbs above 80 °C.",
      normalizer: "stub",
      source_format: "stub-csv",
    },
    {
      id: "bruteforce_syslog",
      file: "bruteforce_syslog.log",
      title: "SSH brute force — syslog",
      description: "Failed root passwords.",
      normalizer: "syslog",
      source_format: "syslog",
    },
  ],
  scenarios: [
    {
      id: "overheating_pump",
      title: "Overheating pump",
      description: "A telemetry threshold.",
      samples: ["pump_telemetry"],
      detectors: [{ name: "stub_threshold", params: {} }],
      alert_engine: { name: "weighted", params: { bucket_minutes: 5 } },
      expected: { events: 1, malformed: 0, detections: 1, alerts: 1 },
    },
    {
      id: "live_meters",
      title: "Live meters",
      description: "A window of a live database.",
      samples: [],
      pulls: [{ connection: "sensipro", start: "2023-07-01", end: "2023-07-30", params: { utility: "water" } }],
      detectors: [{ name: "stub_threshold", params: {} }],
      alert_engine: { name: "weighted", params: { bucket_minutes: 5 } },
      expected: null,
    },
  ],
};
export const sampleHandlers: RequestHandler[] = [
  http.get(`${SAMPLES}/manifest.json`, () => HttpResponse.json(sampleManifest)),
  http.get(`${SAMPLES}/pump_telemetry.csv`, () => HttpResponse.text(SAMPLE_CSV)),
  http.get(`${SAMPLES}/bruteforce_syslog.log`, () => HttpResponse.text(SAMPLE_SYSLOG)),
];

export const server = setupServer(
  ...seededHandlers,
  ...pipelineHandlers,
  ...evalAbsentHandlers,
  ...authAbsentHandlers,
  ...sampleHandlers,
);
