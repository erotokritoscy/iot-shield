/**
 * The three failure families the old suite pinned (M1–M3, X4).
 *
 * A dashboard that shows a traceback — or worse, a blank screen — when the API hiccups is
 * not a product. Each of these must reach the operator as a sentence and, where there is
 * one, the command that fixes it.
 */
import { screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { API, server } from "@/test/msw";
import { renderApp } from "@/test/render";



describe("API failures", () => {
  it("gates Monitor on health and names the start command (M1)", async () => {
    server.use(http.get(`${API}/health`, () => HttpResponse.error()));
    renderApp("/");
    expect(await screen.findByText("Read API unreachable")).toBeInTheDocument();
    expect(
      screen.getByText("IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api"),
    ).toBeInTheDocument();
  });

  it("says the DATABASE is unreachable when the API answers 500, not that the API is down", async () => {
    // `/health` runs SELECT 1, so a healthy API in front of a dead Postgres answers 500. The
    // old gate branched on `isError` alone and told the operator to start a service that was
    // already running — and the command it printed would have repointed the dashboard at an
    // empty SQLite file. Two causes, two remedies.
    server.use(http.get(`${API}/health`, () => HttpResponse.json({ detail: "db down" }, { status: 500 })));
    renderApp("/");
    expect(await screen.findByText("Read API cannot reach its database")).toBeInTheDocument();
    expect(screen.queryByText("Read API unreachable")).not.toBeInTheDocument();
    expect(
      screen.queryByText("IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api"),
    ).not.toBeInTheDocument();
  });

  it("still reads a proxied outage as unreachable, which is how nginx reports it", async () => {
    // Behind nginx a stopped upstream never reaches the browser as a transport error: the
    // proxy is up and answers 502. Classifying that as "it answered" would print the DB
    // remedy for a service that is genuinely not running.
    server.use(http.get(`${API}/health`, () => HttpResponse.text("<html>502</html>", { status: 502 })));
    renderApp("/");
    expect(await screen.findByText("Read API unreachable")).toBeInTheDocument();
  });

  it("offers a way out of a failed request instead of requiring a page reload", async () => {
    // Nothing retries on its own (`retry: false`, no polling, no focus refetch), so without a
    // control here one transient blip is permanent until the operator guesses to hit F5.
    server.use(http.get(`${API}/alerts`, () => HttpResponse.error()));
    renderApp("/");
    expect(await screen.findByText("This request failed")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
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

  it("leaves Operate usable while the read plane is down (M4)", async () => {
    // The health gate is Monitor's, not the app's: Operate talks only to the Pipeline API
    // and reports its own reachability, which is how `main()` routed around it. An operator
    // must be able to fix a broken read plane by running a pipeline into it.
    server.use(http.get(`${API}/health`, () => HttpResponse.error()));
    renderApp("/compose");
    // The wizard renders and the chain can be built and checked to the end.
    await userEvent.type(await screen.findByTestId("src_0_text"), "2026-07-07T12:00:00+00:00,p,T,1");
    await userEvent.click(screen.getByTestId("step_4"));
    expect(await screen.findByText("Chain is compatible.")).toBeInTheDocument();
    expect(screen.queryByText("Read API unreachable")).not.toBeInTheDocument();
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
