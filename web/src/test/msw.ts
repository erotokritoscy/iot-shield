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

const API = "http://localhost/api";
const PIPELINE = "http://localhost/pipeline";

/** The seeded world: pump-02 at T0, one detection, one HIGH alert, one malformed record. */
export const seededHandlers: RequestHandler[] = [
  http.get(`${API}/health`, () => HttpResponse.json({ status: "ok", version: "test" })),
  http.get(`${API}/summary`, () => HttpResponse.json(seed.summary)),
  http.get(`${API}/assets`, () => HttpResponse.json([seed.asset])),
  http.get(`${API}/events`, () => HttpResponse.json(seed.events)),
  http.get(`${API}/detections`, () => HttpResponse.json([seed.detection])),
  http.get(`${API}/alerts`, () => HttpResponse.json([seed.alert])),
  http.get(`${API}/alerts/:id/detections`, () => HttpResponse.json([seed.detection])),
  http.get(`${API}/malformed`, () => HttpResponse.json([seed.malformed])),
];

/** The state every fresh `docker compose up` starts in. */
export const emptyHandlers: RequestHandler[] = [
  http.get(`${API}/health`, () => HttpResponse.json({ status: "ok", version: "test" })),
  http.get(`${API}/summary`, () => HttpResponse.json(seed.emptySummary)),
  http.get(`${API}/assets`, () => HttpResponse.json([])),
  http.get(`${API}/events`, () => HttpResponse.json([])),
  http.get(`${API}/detections`, () => HttpResponse.json([])),
  http.get(`${API}/alerts`, () => HttpResponse.json([])),
  http.get(`${API}/alerts/:id/detections`, () => HttpResponse.json([])),
  http.get(`${API}/malformed`, () => HttpResponse.json([])),
];

/** Whatever the last run POSTed — the request body a parity test asserts against (C4). */
export const captured: { runBody: unknown; validateBody: unknown; evalBody: unknown } = {
  runBody: null,
  validateBody: null,
  evalBody: null,
};

export const pipelineHandlers: RequestHandler[] = [
  http.get(`${PIPELINE}/health`, () => HttpResponse.json({ status: "ok", version: "test" })),
  http.get(`${PIPELINE}/modules`, () => HttpResponse.json(seed.modules)),
  http.post(`${PIPELINE}/runs/validate`, async ({ request }) => {
    captured.validateBody = await request.json();
    return HttpResponse.json({ ok: true, issues: [] });
  }),
  http.post(`${PIPELINE}/runs`, async ({ request }) => {
    captured.runBody = await request.json();
    return HttpResponse.json({
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

export const server = setupServer(...seededHandlers, ...pipelineHandlers, ...evalAbsentHandlers);
