/**
 * The three failure families the old suite pinned (M1–M3, X4).
 *
 * A dashboard that shows a traceback — or worse, a blank screen — when the API hiccups is
 * not a product. Each of these must reach the operator as a sentence and, where there is
 * one, the command that fixes it.
 */
import { screen } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "@/test/msw";
import { renderApp } from "@/test/render";

const API = "http://localhost/api";

describe("API failures", () => {
  it("gates Monitor on health and names the start command (M1)", async () => {
    server.use(http.get(`${API}/health`, () => HttpResponse.error()));
    renderApp("/");
    expect(await screen.findByText("Read API unreachable")).toBeInTheDocument();
    expect(
      screen.getByText("IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api"),
    ).toBeInTheDocument();
  });

  it("renders an in-page error when one endpoint fails after health passed (M2)", async () => {
    // The API dying between requests, or a single route 5xx: the page boundary must render
    // a friendly error, not raise.
    server.use(http.get(`${API}/alerts`, () => HttpResponse.error()));
    renderApp("/");
    expect(await screen.findByText("This request failed")).toBeInTheDocument();
    // The rest of the page still works — the KPI row came from a different endpoint.
    expect(screen.getByTestId("stat-events")).toHaveTextContent("3");
  });

  it("renders an in-page error when a response fails contract validation (M3)", async () => {
    // API/UI skew: well-formed JSON that no longer matches the contract. This is the very
    // failure the typed client exists to surface — loud at one boundary rather than
    // rendering wrong (ADR-0008).
    server.use(http.get(`${API}/alerts`, () => HttpResponse.json([{ nonsense: true }])));
    renderApp("/");
    expect(await screen.findByText("This request failed")).toBeInTheDocument();
    expect(screen.getByText(/does not match the contract/)).toBeInTheDocument();
  });

  it("treats an out-of-vocabulary enum as contract drift, not as a blank badge", async () => {
    server.use(
      http.get(`${API}/alerts`, () =>
        HttpResponse.json([
          {
            alert_id: "22222222-2222-2222-2222-222222222222",
            asset_id: "pump-02",
            severity: "apocalyptic",
            priority_score: 62,
            threat_class: "unknown",
            rationale: "x",
            detection_ids: ["a"],
            status: "new",
            window_start: "2026-07-07T12:00:00Z",
            window_end: "2026-07-07T12:00:00Z",
          },
        ]),
      ),
    );
    renderApp("/");
    expect(await screen.findByText("This request failed")).toBeInTheDocument();
  });

  it("names the Pipeline API's start command when the catalogue is unreachable (C1)", async () => {
    server.use(http.get("http://localhost/pipeline/modules", () => HttpResponse.error()));
    renderApp("/compose");
    expect(await screen.findByText("Pipeline API unreachable")).toBeInTheDocument();
    expect(
      screen.getByText("IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api.control"),
    ).toBeInTheDocument();
  });

  it("refuses to render a blank form when the catalogue is empty (C1)", async () => {
    server.use(
      http.get("http://localhost/pipeline/modules", () =>
        HttpResponse.json({ normalizers: [], detectors: [], alert_engines: [], sinks: [] }),
      ),
    );
    renderApp("/compose");
    expect(
      await screen.findByText("The Pipeline API reports no adapters — nothing to compose."),
    ).toBeInTheDocument();
  });
});
