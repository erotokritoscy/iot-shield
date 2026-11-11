/**
 * Monitor's three views against seeded and empty worlds, plus the friendly empty states
 * (the state every fresh `docker compose up` starts in).
 *
 * The React counterpart of the Streamlit suite's `TestTriage` / `TestAssets` /
 * `TestDataHealth` (X4), which went with that app.
 */
import { fireEvent, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http, type JsonBodyType } from "msw";
import { describe, expect, it } from "vitest";

import { TRIAGE_HELP } from "@/pages/triage/help";
import * as seed from "@/test/fixtures";
import { alertHandlers, API, emptyHandlers, PIPELINE, server } from "@/test/msw";
import { renderApp } from "@/test/render";

/** Record the `start`/`end` each request to `path` carried, answering with `body`. */
function recordRange(path: string, body: readonly unknown[]) {
  const asked: { start: string | null; end: string | null }[] = [];
  server.use(
    http.get(`${API}${path}`, ({ request }) => {
      const params = new URL(request.url).searchParams;
      asked.push({ start: params.get("start"), end: params.get("end") });
      return HttpResponse.json(body as JsonBodyType);
    }),
  );
  return asked;
}



/** `count` alerts ranked by descending priority, on `assets` devices in turn. */
function rankedAlerts(count: number, assets = ["pump-02"]) {
  return Array.from({ length: count }, (_, index) => ({
    ...seed.alert,
    alert_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
    asset_id: assets[index % assets.length] as string,
    priority_score: 99 - index * 0.01,
  }));
}

describe("paging through the whole queue", () => {
  it("pages through every alert from the server, not a capped prefix of them", async () => {
    // The page used to load the top 1,000 and say so; past that the queue was unreachable.
    // Now each page is its own request, so the 120th alert is as reachable as the first.
    const offsets: (string | null)[] = [];
    const many = rankedAlerts(120);
    server.use(
      http.get(`${API}/alerts`, ({ request }) => {
        const params = new URL(request.url).searchParams;
        offsets.push(params.get("offset"));
        const offset = Number(params.get("offset"));
        const limit = Number(params.get("limit"));
        return HttpResponse.json(many.slice(offset, offset + limit), {
          headers: { "X-Total-Count": "120" },
        });
      }),
    );
    renderApp("/");

    expect(await screen.findByTestId("triage_matched")).toHaveTextContent("120 alerts");
    expect(screen.getByText("Page 1 of 3")).toBeInTheDocument();
    expect(screen.queryByText(/Showing .* of .* alerts/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Last" }));
    expect(await screen.findByText("Page 3 of 3")).toBeInTheDocument();
    expect(offsets).toEqual(["0", "100"]);
    // The last page holds the 20 lowest-ranked alerts, the 120th among them.
    const queue = (await screen.findAllByRole("table"))[0] as HTMLElement;
    await waitFor(() => expect(within(queue).getAllByRole("row")).toHaveLength(21));
    expect(within(queue).getAllByText("97.8").length).toBeGreaterThan(0);
  });

  it("draws the severity chart from the server's counts of the whole queue", async () => {
    server.use(
      http.get(`${API}/alerts/facets`, () =>
        HttpResponse.json({ total: 11_377, by_severity: { high: 11_000, critical: 377 }, assets: ["pump-02"] }),
      ),
    );
    renderApp("/");
    expect(await screen.findByText("Alert count per severity.")).toBeInTheDocument();
    expect(screen.queryByText(/highest-priority/)).not.toBeInTheDocument();
    // The one chart left unzoomable, as it was.
    expect(screen.queryByTestId("chart_zoom_hint")).not.toBeInTheDocument();
    // The severity filter names how many each choice holds, from the same counts.
    expect(await screen.findByRole("option", { name: "high (11,000)" })).toBeInTheDocument();
  });

  it("hides chart severities beside the range, without touching the queue's filter", async () => {
    server.use(
      http.get(`${API}/alerts/facets`, () =>
        HttpResponse.json({ total: 11_377, by_severity: { high: 11_000, critical: 377 }, assets: ["pump-02"] }),
      ),
    );
    renderApp("/");
    const filter = await screen.findByRole("group", { name: "Chart severities" });
    // In the range row, right after "To (UTC)".
    expect(within(screen.getByTestId("time_range")).getByRole("group", { name: "Chart severities" })).toBe(filter);

    await userEvent.click(within(filter).getByRole("checkbox", { name: "high" }));
    expect(await screen.findByText("Alert count per severity. Hidden: high.")).toBeInTheDocument();
    expect(screen.getByLabelText("Severity")).toHaveValue("");

    await userEvent.click(within(filter).getByRole("checkbox", { name: "critical" }));
    expect(await screen.findByTestId("chart_nothing_shown")).toBeInTheDocument();
    // The filter stays to be changed back.
    await userEvent.click(within(filter).getByRole("checkbox", { name: "high" }));
    await userEvent.click(within(filter).getByRole("checkbox", { name: "critical" }));
    expect(await screen.findByText("Alert count per severity.")).toBeInTheDocument();
  });

  it("explains every severity in a table, with its priority band", async () => {
    renderApp("/");
    const toggle = await screen.findByRole("button", { name: /What the severities mean/ });
    expect(screen.queryByTestId("severity_guide")).not.toBeInTheDocument();
    await userEvent.click(toggle);
    const rows = within(screen.getByTestId("severity_guide")).getAllByRole("row").slice(1);
    expect(rows.map((row) => within(row).getAllByRole("cell").slice(0, 2).map((cell) => cell.textContent))).toEqual([
      ["emergency", "90–100"],
      ["critical", "70–89.9"],
      ["high", "50–69.9"],
      ["medium", "30–49.9"],
      ["low", "0–29.9"],
    ]);
    expect(rows[1]).toHaveTextContent("Investigate immediately");
  });

  it("offers every asset with alerts, not only the ones ranked on this page", async () => {
    // The page holds pump-02's alerts only; gw-01 and cam-07 rank further down the queue.
    server.use(
      http.get(`${API}/alerts/facets`, () =>
        HttpResponse.json({ total: 60, by_severity: { high: 60 }, assets: ["cam-07", "gw-01", "pump-02"] }),
      ),
    );
    renderApp("/");
    const asset = await screen.findByLabelText("Asset");
    await waitFor(() =>
      expect(within(asset).getAllByRole("option").map((option) => option.textContent)).toEqual([
        "any",
        "cam-07",
        "gw-01",
        "pump-02",
      ]),
    );
  });

  it("asks the server for the severity and the asset, and goes back to page one", async () => {
    const asked: string[] = [];
    server.use(...alertHandlers(rankedAlerts(120, ["pump-02", "gw-01"])));
    server.use(
      http.get(`${API}/alerts`, ({ request }) => {
        const params = new URL(request.url).searchParams;
        asked.push(`${params.get("severity")}/${params.get("asset_id")}/${params.get("offset")}`);
        // Unfiltered, a queue; filtered, nothing — so the empty state is the filter's answer.
        const unfiltered = params.get("severity") === null && params.get("asset_id") === null;
        return HttpResponse.json(unfiltered ? [seed.alert] : [], {
          headers: { "X-Total-Count": unfiltered ? "1" : "0" },
        });
      }),
    );
    renderApp("/");
    await screen.findByTestId("triage_matched");
    await userEvent.selectOptions(screen.getByLabelText("Severity"), "critical");
    await userEvent.selectOptions(screen.getByLabelText("Asset"), "gw-01");
    await waitFor(() => expect(asked.at(-1)).toBe("critical/gw-01/0"));
    expect(await screen.findByText("No alert matches this filter.")).toBeInTheDocument();
  });

  it("inspects an alert named in the URL even when it is not on the page shown", async () => {
    const elsewhere = {
      ...seed.alert,
      alert_id: "00000000-0000-4000-8000-00000000beef",
      asset_id: "cam-07",
      rationale: "an alert from page 40 of the queue",
    };
    server.use(...alertHandlers([seed.alert]));
    server.use(http.get(`${API}/alerts/:id`, () => HttpResponse.json(elsewhere)));
    renderApp(`/?alert=${elsewhere.alert_id}`);
    expect(await screen.findByText("an alert from page 40 of the queue")).toBeInTheDocument();
    expect(screen.getByLabelText("Inspect alert")).toHaveValue(elsewhere.alert_id);
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
    expect(await screen.findByText("1 alert")).toBeInTheDocument();

    await userEvent.selectOptions(severity, "low");
    expect(await screen.findByText("No alert matches this filter.")).toBeInTheDocument();
    expect(screen.getByText("0 of 1 alert match")).toBeInTheDocument();

    await userEvent.selectOptions(severity, "high");
    expect(await screen.findByText("1 of 1 alert match")).toBeInTheDocument();
  });

  it("filters by run on the server, naming each run, and keeps the filter on an empty run", async () => {
    // An alert carries no run_id, so the run filter cannot be a view over the page: it must
    // be a request (ADR-0045). Asserting the query string is the point — a client-side filter
    // would render the same here and miss every alert of the run below the 1,000 cap.
    const asked: (string | null)[] = [];
    server.use(
      http.get(`${API}/alerts`, ({ request }) => {
        const runId = new URL(request.url).searchParams.get("run_id");
        asked.push(runId);
        return HttpResponse.json(runId === null ? [seed.alert] : []);
      }),
    );
    renderApp("/");
    const run = await screen.findByLabelText("Run");
    expect(await within(run).findByRole("option", { name: /^Night shift check — / })).toBeInTheDocument();
    expect(asked).toEqual([null]);

    await userEvent.selectOptions(run, seed.savedRun.run_id);
    await waitFor(() => expect(asked).toContain(seed.savedRun.run_id));
    // The run has no alerts: that is an answer about the run, not "nothing has run yet".
    expect(await screen.findByText("No alert matches this filter.")).toBeInTheDocument();
    expect(screen.getByLabelText("Run")).toHaveValue(seed.savedRun.run_id);
    expect(screen.queryByText(/run the pipeline first/)).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByLabelText("Run"), "");
    expect(await screen.findByText("1 shown")).toBeInTheDocument();
  });

  it("filters by time range on the server, as UTC, and keeps the filter on an empty range", async () => {
    // Asked of the server, not applied to the page: the queue is capped at 1,000, so a
    // client-side filter would miss every in-range alert ranked below the cap.
    const asked = recordRange("/alerts", []);
    renderApp("/?from=2026-07-07T12:00&to=2026-07-08T00:00");
    expect(await screen.findByLabelText("From (UTC)")).toHaveValue("2026-07-07T12:00");
    await waitFor(() =>
      expect(asked).toContainEqual({ start: "2026-07-07T12:00:00Z", end: "2026-07-08T00:00:00Z" }),
    );
    // An empty range is an answer about the range, so the filter stays to widen it again.
    expect(await screen.findByText("No alert matches this filter.")).toBeInTheDocument();
    expect(screen.queryByText(/run the pipeline first/)).not.toBeInTheDocument();
    // `/summary` is org-wide, so its tiles are named all-time beside a filtered queue.
    expect(screen.getByText(/Counts are all-time totals/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Clear range" }));
    await waitFor(() => expect(asked.at(-1)).toEqual({ start: null, end: null }));
    // Cleared, an empty queue is the whole org's answer again, so the guidance returns.
    expect(await screen.findByText(/run the pipeline first/)).toBeInTheDocument();
  });

  it("asks with one open side when only one bound is set", async () => {
    const asked = recordRange("/alerts", [seed.alert]);
    renderApp("/");
    fireEvent.change(await screen.findByLabelText("To (UTC)"), {
      target: { value: "2026-07-07T13:30" },
    });
    await waitFor(() => expect(asked.at(-1)).toEqual({ start: null, end: "2026-07-07T13:30:00Z" }));
    expect(screen.getByText(/Counts are all-time totals/)).toBeInTheDocument();
  });

  it("warns when From is after To", async () => {
    recordRange("/alerts", []);
    renderApp("/?from=2026-07-09T00:00&to=2026-07-08T00:00");
    expect(await screen.findByText(/“From” is after “To”/)).toBeInTheDocument();
  });

  it("opens on the run named in the URL, even one the run list does not hold", async () => {
    const asked: (string | null)[] = [];
    server.use(
      http.get(`${API}/alerts`, ({ request }) => {
        asked.push(new URL(request.url).searchParams.get("run_id"));
        return HttpResponse.json([seed.alert]);
      }),
    );
    renderApp("/?run=gone-run");
    expect(await screen.findByLabelText("Run")).toHaveValue("gone-run");
    expect(asked).toEqual(["gone-run"]);
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
    // The disclosure is named by the sentence the detection carries, not by its method —
    // which now appears twice, once as the table's cell and once beside that heading.
    expect(await screen.findAllByText("stub_threshold")).toHaveLength(2);
    const disclosure = await screen.findByRole("button", {
      name: /Pressure 96 is above the configured threshold of 80/,
    });
    await userEvent.click(disclosure);
    expect(await screen.findByText(/"peak": 96/)).toBeInTheDocument();
  });

  it("names each piece of evidence by what it found, not by which detector found it", async () => {
    // The regression ADR-0032 D3 opened: six rules share one `method`, so labelling the
    // disclosures by method rendered several identical "rules:flow — why it fired" headings
    // for genuinely different findings — the walkthrough's camera raises two on the flow lane.
    server.use(
      http.get(`${API}/alerts/:id/detections`, () => HttpResponse.json(seed.ruleDetections)),
    );
    renderApp("/");

    for (const finding of [
      "The device originated a connection to port 23 (telnet).",
      "Outbound bytes on one flow exceed 10000.",
    ]) {
      // A function matcher, not a RegExp: these sentences contain parentheses and periods,
      // which a `new RegExp(finding)` would silently reinterpret as a group and a wildcard.
      expect(
        await screen.findByRole("button", { name: (name) => name.startsWith(finding) }),
      ).toBeInTheDocument();
    }
    // The method is still shown — it is now the secondary fact (which detector), which is
    // exactly why it appears twice while the two headings differ.
    expect(await screen.findAllByText("rules:flow")).toHaveLength(4); // 2 table cells + 2 labels
  });

  it("renders the unioned next steps as a checklist and lists every technique cited", async () => {
    // `recommended_action` is one string holding up to six imperatives unioned from every
    // rule that fired (ADR-0032 D5); as a paragraph it is a wall nobody works through. The
    // MITRE tactics existed only inside the raw JSON dump.
    const alert = {
      ...seed.alert,
      threat_class: "Remote Services (T1021)",
      recommended_action: "Isolate the device. Block outbound telnet. Review the destination.",
    };
    server.use(
      http.get(`${API}/alerts`, () => HttpResponse.json([alert])),
      http.get(`${API}/alerts/:id/detections`, () => HttpResponse.json(seed.ruleDetections)),
    );
    renderApp("/");

    const actions = await screen.findByRole("list", { name: "Recommended action steps" });
    expect(within(actions).getAllByRole("listitem").map((step) => step.textContent)).toEqual([
      "Isolate the device.",
      "Block outbound telnet.",
      "Review the destination.",
    ]);

    // Every technique the alert's evidence cites, tactic included — `threat_class` carries
    // only the winning one, and the rest reached the UI as raw JSON or not at all.
    const techniques = await screen.findByRole("list", { name: "MITRE ATT&CK techniques" });
    expect(within(techniques).getAllByRole("listitem").map((row) => row.textContent)).toEqual([
      "Lateral Movement — Remote Services (T1021)",
      "Exfiltration — Exfiltration Over C2 Channel (T1041)",
    ]);
  });

  it("explains the counts, the filters and the inspector behind an (i)", async () => {
    server.use(
      http.get(`${API}/alerts`, () =>
        HttpResponse.json([{ ...seed.alert, recommended_action: "Isolate the device." }]),
      ),
      http.get(`${API}/alerts/:id/detections`, () => HttpResponse.json(seed.ruleDetections)),
    );
    renderApp("/");
    await screen.findByRole("list", { name: "MITRE ATT&CK techniques" });
    const topics = [
      ...["runs", "events", "detections", "alerts", "malformed records"],
      ...["Ranked queue", "Severity", "Asset", "Run"],
      ...["Alert inspector", "Inspect alert", "Rationale", "Recommended action", "MITRE ATT&CK"],
      ...["Score components", "Evidence"],
    ];
    for (const topic of topics) {
      expect(screen.getByRole("button", { name: `About ${topic}` }), topic).toBeInTheDocument();
    }
    await userEvent.click(screen.getByRole("button", { name: "About Severity" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent(TRIAGE_HELP.severity);
  });

  it("counts the saved runs first, before the events", async () => {
    renderApp("/");
    expect(await screen.findByTestId("stat-runs")).toHaveTextContent("1");
    const tiles = screen.getAllByTestId(/^stat-/).map((tile) => tile.dataset["testid"]);
    expect(tiles.slice(0, 2)).toEqual(["stat-runs", "stat-events"]);
  });

  it("marks a capped run list as a lower bound rather than an exact count", async () => {
    server.use(
      http.get(`${PIPELINE}/runs`, () =>
        HttpResponse.json(
          Array.from({ length: 201 }, (_, index) => ({
            ...seed.savedRun,
            run_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
          })),
        ),
      ),
    );
    renderApp("/");
    await waitFor(() => expect(screen.getByTestId("stat-runs")).toHaveTextContent("200+"));
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
    expect(await screen.findByTestId("assets_asset")).toHaveTextContent("Feed pump 2 [pump-02]");
    expect(await screen.findByText(/criticality: high/)).toBeInTheDocument();
    expect(
      await screen.findByText(/Red bands\/rules: telemetry detection windows/),
    ).toBeInTheDocument();
    expect(await screen.findByRole("heading", { name: "Detections" })).toBeInTheDocument();
    // UTC in the table beside the chart (A4).
    expect(await screen.findByText("2026-07-07 12:00:30")).toBeInTheDocument();
  });

  it("searches the asset list from inside the dropdown and opens the asset picked", async () => {
    const gateway = { ...seed.discoveredAsset, asset_id: "gw-11", name: "Edge gateway" };
    server.use(
      http.get(`${API}/assets`, () => HttpResponse.json([seed.asset, seed.discoveredAsset, gateway])),
    );
    renderApp("/assets");
    await userEvent.click(await screen.findByTestId("assets_asset"));
    expect(screen.getAllByRole("option")).toHaveLength(3);
    await userEvent.type(screen.getByTestId("assets_asset_search"), "gw");
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Edge gateway [gw-11] (auto-discovered)",
    ]);
    await userEvent.click(screen.getByRole("option", { name: /Edge gateway/ }));
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(screen.getByTestId("assets_asset")).toHaveTextContent("Edge gateway [gw-11]");
    expect(await screen.findByText(/criticality: medium/)).toBeInTheDocument();
  });

  it("narrows the readings and both detection lists to the time range", async () => {
    const events = recordRange("/events", []);
    const detections = recordRange("/detections", []);
    renderApp("/assets?from=2026-07-07T12:00&to=2026-07-07T12:10");
    const sent = { start: "2026-07-07T12:00:00Z", end: "2026-07-07T12:10:00Z" };
    await waitFor(() => expect(events).toContainEqual(sent));
    // Telemetry detections for the chart, all-lane detections for the table: both narrowed.
    await waitFor(() => expect(detections.filter((d) => d.start === sent.start)).toHaveLength(2));
    expect(
      await screen.findByText("No detections for this asset in this time range."),
    ).toBeInTheDocument();
  });

  it("reads every reading, a server page at a time, and says nothing was cut", async () => {
    // The chart used to plot the newest 5,000 and warn that the axis started mid-record.
    const total = 12_345;
    const readings = Array.from({ length: total }, (_, index) => ({
      ...seed.events[0],
      event_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
      timestamp: new Date(Date.UTC(2026, 6, 7, 12) + index * 1000).toISOString(),
    }));
    const offsets: (string | null)[] = [];
    server.use(
      http.get(`${API}/events`, ({ request }) => {
        const params = new URL(request.url).searchParams;
        offsets.push(params.get("offset"));
        const offset = Number(params.get("offset"));
        const limit = Number(params.get("limit"));
        return HttpResponse.json(readings.slice(offset, offset + limit) as JsonBodyType, {
          headers: { "X-Total-Count": String(total) },
        });
      }),
    );
    renderApp("/assets");
    expect(await screen.findByText(/Red bands\/rules: telemetry detection windows/)).toBeInTheDocument();
    expect(offsets).toEqual(["0", "10000"]);
    expect(screen.getByTestId("chart_zoom_hint")).toBeInTheDocument();
    expect(screen.queryByText(/Showing the first/)).not.toBeInTheDocument();
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

  it("filters malformed inputs by when they were ingested, and says so", async () => {
    const asked = recordRange("/malformed", []);
    renderApp("/data-health");
    fireEvent.change(await screen.findByLabelText("From (UTC)"), {
      target: { value: "2026-07-07T12:00" },
    });
    await waitFor(() => expect(asked.at(-1)).toEqual({ start: "2026-07-07T12:00:00Z", end: null }));
    expect(
      await screen.findByText("No malformed inputs ingested in this time range."),
    ).toBeInTheDocument();
    expect(screen.getByText(/Counts are all-time totals/)).toBeInTheDocument();
  });

  it("shows each malformed record's ingest time, the clock the range filters on", async () => {
    renderApp("/data-health");
    expect(await screen.findByRole("columnheader", { name: "ingested (UTC)" })).toBeInTheDocument();
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
