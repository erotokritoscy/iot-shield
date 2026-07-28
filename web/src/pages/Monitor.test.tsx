/**
 * Monitor's three views against seeded and empty worlds, plus the friendly empty states
 * (the state every fresh `docker compose up` starts in).
 *
 * The React counterpart of `TestTriage` / `TestAssets` / `TestDataHealth` in
 * `tests/dashboard/test_app.py` (X4).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import { emptyHandlers, server } from "@/test/msw";
import { renderApp } from "@/test/render";

describe("Triage", () => {
  it("shows the KPI row, the ranked queue and the drill-down", async () => {
    renderApp("/");

    // Streamlit's four metrics, in the same order: events / detections / alerts / malformed.
    expect(await screen.findByTestId("stat-events")).toHaveTextContent("3");
    expect(screen.getByTestId("stat-detections")).toHaveTextContent("1");
    expect(screen.getByTestId("stat-alerts")).toHaveTextContent("1");
    expect(screen.getByTestId("stat-malformed-records")).toHaveTextContent("1");

    // The queue: severity badge, priority to one decimal, asset, UTC window start.
    const queue = (await screen.findAllByRole("table"))[0] as HTMLElement;
    expect(within(queue).getByText("high")).toBeInTheDocument();
    expect(within(queue).getByText("62.0")).toBeInTheDocument();
    expect(within(queue).getByText("pump-02")).toBeInTheDocument();
    expect(within(queue).getByText("2026-07-07 12:00:30")).toBeInTheDocument();

    // The inspector: the option label format, the rationale, the recommended action, and
    // the score components — the explainability receipt.
    expect(
      await screen.findByRole("option", { name: "HIGH 62.0 — pump-02 @ 2026-07-07 12:00" }),
    ).toBeInTheDocument();
    expect(screen.getByText("seeded incident on Feed pump 2")).toBeInTheDocument();
    expect(screen.getByText("Inspect pump-02.")).toBeInTheDocument();
    expect(await screen.findByText("asset_criticality")).toBeInTheDocument();

    // The evidence table, and the per-detection payload that makes a finding actionable.
    expect(await screen.findByText("stub_threshold")).toBeInTheDocument();
    const disclosure = await screen.findByRole("button", {
      name: /stub_threshold — why it fired/,
    });
    await userEvent.click(disclosure);
    expect(await screen.findByText(/"peak": 96/)).toBeInTheDocument();
  });

  it("draws the severity chart with its caption", async () => {
    renderApp("/");
    expect(await screen.findByText("Alert count per severity.")).toBeInTheDocument();
    expect(screen.getByTestId("vega-spec")).toBeInTheDocument();
  });

  it("renders a KPI the summary does not carry as 0, not as blank (T8)", async () => {
    // `summary.counts` is an open mapping, so a key the server stops sending must read as
    // zero — a blank tile would look like a rendering fault rather than a count.
    server.use(
      http.get("http://localhost/api/summary", () =>
        HttpResponse.json({ counts: { events: 7 }, top_alert: null }),
      ),
    );
    renderApp("/");
    expect(await screen.findByTestId("stat-events")).toHaveTextContent("7");
    expect(screen.getByTestId("stat-detections")).toHaveTextContent("0");
    expect(screen.getByTestId("stat-malformed-records")).toHaveTextContent("0");
  });

  it("shows guidance rather than an empty table when nothing has run", async () => {
    server.use(...emptyHandlers);
    renderApp("/");
    expect(
      await screen.findByText(/No alerts yet — run the pipeline first/),
    ).toBeInTheDocument();
    // ...and the chart is hidden entirely, not drawn as an empty axis.
    expect(screen.queryByTestId("vega-spec")).not.toBeInTheDocument();
  });
});

describe("Assets & telemetry", () => {
  it("renders the picker caption, the timeline and the all-lane detections table", async () => {
    renderApp("/assets");
    expect(
      await screen.findByRole("option", { name: "Feed pump 2 [pump-02]" }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/criticality: high/)).toBeInTheDocument();
    expect(
      await screen.findByText(/Red bands\/rules: telemetry detection windows/),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Detections" })).toBeInTheDocument();
    // UTC in the table beside the chart (A4).
    expect(await screen.findByText("2026-07-07 12:00:30")).toBeInTheDocument();
  });

  it("shows the discovery empty state when the inventory is empty", async () => {
    server.use(...emptyHandlers);
    renderApp("/assets");
    expect(await screen.findByText(/No assets yet/)).toBeInTheDocument();
  });
});

describe("Data health", () => {
  it("shows the counts and the malformed audit", async () => {
    renderApp("/data-health");
    expect(
      await screen.findByRole("heading", { name: /Malformed inputs \(captured, never dropped\)/ }),
    ).toBeInTheDocument();
    expect(await screen.findByText("garbage-line")).toBeInTheDocument();
    expect(screen.getByText("unparseable")).toBeInTheDocument();
    expect(screen.getByText("test://seed")).toBeInTheDocument();
  });

  it("takes the counts table's columns from whatever /summary carries (DH4)", async () => {
    // `counts` is an open mapping; a fixed column list here would hide a count the API
    // starts reporting, which is the one thing an audit view must not do.
    server.use(
      http.get("http://localhost/api/summary", () =>
        HttpResponse.json({ counts: { events: 3, quarantined_rows: 9 }, top_alert: null }),
      ),
    );
    renderApp("/data-health");
    const counts = (await screen.findAllByRole("table"))[0] as HTMLElement;
    expect(within(counts).getByText("quarantined_rows")).toBeInTheDocument();
    expect(within(counts).getByText("9")).toBeInTheDocument();
  });

  it("shows a success state when nothing was rejected", async () => {
    server.use(...emptyHandlers);
    renderApp("/data-health");
    expect(await screen.findByText("No malformed inputs recorded.")).toBeInTheDocument();
  });
});

describe("the sidebar", () => {
  it("groups the nav by plane and states the timezone", async () => {
    renderApp("/");
    await waitFor(() => expect(screen.getByText("Monitor")).toBeInTheDocument());
    expect(screen.getByText("Operate")).toBeInTheDocument();
    expect(screen.getByText("All timestamps UTC")).toBeInTheDocument();
  });
});
