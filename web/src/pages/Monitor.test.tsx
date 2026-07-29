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

import * as seed from "@/test/fixtures";
import { API, emptyHandlers, server } from "@/test/msw";
import { renderApp } from "@/test/render";



describe("truncation", () => {
  it("says so when the queue is only the top slice, and scopes the chart's claim to it", async () => {
    // Every list endpoint takes a `limit`, so every one of them can return a prefix of the
    // truth that looks exactly like the whole of it. `/alerts` is priority-descending, so the
    // rows dropped are the low-priority tail — which skews the severity chart drawn from them
    // while the KPI tile beside it reports the true total from an uncapped `/summary`.
    // The client asks for limit+1 and renders limit, which is the only test that generalises
    // to the per-asset queries (`counts.events` is global; `/events` is filtered).
    const many = Array.from({ length: 1001 }, (_, index) => ({
      ...seed.alert,
      alert_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    }));
    server.use(
      http.get(`${API}/alerts`, () => HttpResponse.json(many)),
      http.get(`${API}/summary`, () =>
        HttpResponse.json({ ...seed.summary, counts: { ...seed.summary.counts, alerts: 4312 } }),
      ),
    );
    renderApp("/");

    expect(await screen.findByText(/Showing 1,000 of 4,312 alerts/)).toBeInTheDocument();
    expect(
      screen.getByText(/among the 1,000 highest-priority alerts/),
    ).toBeInTheDocument();
  });

  it("stays quiet when the list is complete", async () => {
    renderApp("/");
    expect(await screen.findByTestId("stat-alerts")).toHaveTextContent("1");
    expect(screen.queryByText(/Showing .* of .* alerts/)).not.toBeInTheDocument();
    expect(screen.getByText("Alert count per severity.")).toBeInTheDocument();
  });
});

describe("Triage", () => {
  it("selects an alert from the queue itself, and puts the choice in the URL", async () => {
    // The queue and the inspector used to be disconnected widgets: the only selector was a
    // native <select> below the whole table, whose labels start with the severity word, so
    // type-ahead cycles severities rather than finding an alert. Row activation is a real
    // <button> inside the first cell — NOT role="button" on the <tr>, which would replace the
    // row's own role and break row/cell relationships for assistive technology.
    const second = {
      ...seed.alert,
      alert_id: "00000000-0000-4000-8000-000000000002",
      asset_id: "cam-07",
      severity: "critical",
      priority_score: 88.4,
      rationale: "the second alert, reachable only by choosing its row",
    };
    server.use(http.get(`${API}/alerts`, () => HttpResponse.json([seed.alert, second])));
    renderApp("/");

    // The first alert is the fallback selection, so the second one proves row activation did
    // something rather than the panel merely defaulting.
    expect(await screen.findByText("seeded incident on Feed pump 2")).toBeInTheDocument();
    await userEvent.click(
      await screen.findByRole("button", { name: /Inspect CRITICAL 88.4 — cam-07/ }),
    );
    expect(
      await screen.findByText("the second alert, reachable only by choosing its row"),
    ).toBeInTheDocument();
    // The picker follows the queue: one selection, two surfaces.
    await waitFor(() =>
      expect(screen.getByLabelText("Inspect alert")).toHaveValue(second.alert_id),
    );
  });

  it("filters the queue without ever re-sorting it", async () => {
    // Filtering is a view over rows the server already ranked. Sorting is deliberately absent:
    // the order IS the product's output (H3), and a client-side re-sort would quietly replace
    // a ranking with a preference.
    renderApp("/");
    const severity = await screen.findByLabelText("Severity");
    expect(await screen.findByText("1 of 1 shown")).toBeInTheDocument();

    await userEvent.selectOptions(severity, "low");
    expect(await screen.findByText("No alert matches this filter.")).toBeInTheDocument();
    expect(screen.getByText("0 of 1 shown")).toBeInTheDocument();

    await userEvent.selectOptions(severity, "high");
    expect(await screen.findByText("1 of 1 shown")).toBeInTheDocument();
  });
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
