/**
 * The IT and OT overview pages (ADR-0044): what they ask the server for, and what they show.
 * The counting itself is the server's (tests/api/test_overview.py); these pin the requests —
 * the range, the bucket, the meter and the type — and the page around the numbers.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { API, server } from "@/test/msw";
import { renderApp } from "@/test/render";

const asked: { it: string[]; ot: string[]; records: string[]; events: string[] } = {
  it: [],
  ot: [],
  records: [],
  events: [],
};

const PER_TYPE = [
  { issue: "heartbeat", category: 1, subcategory: 0, count: 1 },
  { issue: "valueError", category: 2, subcategory: 0, count: 0 },
  { issue: "zero", category: 2, subcategory: 1, count: 2 },
  { issue: "negative", category: 2, subcategory: 2, count: 0 },
  { issue: "high", category: 2, subcategory: 3, count: 4 },
  { issue: "small", category: 2, subcategory: 4, count: 0 },
];

function serve(): void {
  server.use(
    http.get(`${API}/overview/it`, ({ request }) => {
      asked.it.push(new URL(request.url).search);
      // The flows are on 2023-07-22; a range without that day holds none of them.
      const url = new URL(request.url);
      const holds =
        (url.searchParams.get("start") ?? "") <= "2023-07-22" && (url.searchParams.get("end") ?? "") >= "2023-07-22";
      if (!holds) {
        return HttpResponse.json({
          flows: 0,
          flagged: 0,
          per_day: [],
          per_method: [],
          latest_day: "2023-07-22",
          series: [],
        });
      }
      return HttpResponse.json({
        flows: 40,
        flagged: 10,
        per_day: [{ day: "2023-07-22", count: 2 }],
        per_method: [{ method: "rules:flow", count: 2 }],
        latest_day: "2023-07-22",
        series: [
          { bucket: "2023-07-22T09:00:00+00:00", flows: 30, flagged: 4, detections: 1 },
          { bucket: "2023-07-22T11:00:00+00:00", flows: 10, flagged: 6, detections: 1 },
        ],
      });
    }),
    http.get(`${API}/overview/ot`, ({ request }) => {
      asked.ot.push(new URL(request.url).search);
      return HttpResponse.json({
        total: 7,
        unplaced: 0,
        per_type: PER_TYPE,
        series: [
          { bucket: "2023-07-01T06:00:00+00:00", issue: "high", count: 4 },
          { bucket: "2023-07-03T10:00:00+00:00", issue: "zero", count: 3 },
        ],
        per_meter: [
          { asset_id: "e1", count: 5 },
          { asset_id: "w1", count: 2 },
        ],
        meters: 2,
        latest_day: "2023-07-07",
        meter_series: [
          { bucket: "2023-07-01T06:00:00+00:00", asset_id: "e1", count: 5 },
          { bucket: "2023-07-03T10:00:00+00:00", asset_id: "w1", count: 2 },
        ],
        meter_ids: ["e1", "q1", "w1"],
      });
    }),
    http.get(`${API}/overview/ot/records`, ({ request }) => {
      asked.records.push(new URL(request.url).search);
      return HttpResponse.json([
        {
          issue: "high",
          source: "detection",
          asset_id: "e1",
          at: "2023-07-01T06:00:00Z",
          detail: "meter e1 consumed up to 46 kwh in an hour, above the limit of 35 kwh",
        },
        {
          issue: "zero",
          source: "reject",
          asset_id: "e1",
          at: "2023-07-01T08:00:00Z",
          detail: "[zero] meter e1 at 2023-07-01T08:00:00+00:00: cumulative counter decreased",
        },
      ]);
    }),
    http.get(`${API}/events`, ({ request }) => {
      asked.events.push(new URL(request.url).search);
      return HttpResponse.json([]);
    }),
  );
}

beforeEach(() => {
  asked.it = [];
  asked.ot = [];
  asked.records = [];
  asked.events = [];
  serve();
});

describe("IT overview", () => {
  it("counts flows and detections for the range in the URL", async () => {
    renderApp("/overview/it?start=2023-07-16&end=2023-07-22");
    expect(await screen.findByTestId("stat-flows")).toHaveTextContent("40");
    expect(screen.getByTestId("stat-inside-a-detection")).toHaveTextContent("10 (25%)");
    expect(screen.getByTestId("stat-detections")).toHaveTextContent("2");
    expect(screen.getByTestId("it_methods")).toHaveTextContent("rules:flow 2");
    expect(asked.it).toContain("?start=2023-07-16&end=2023-07-22&bucket=hour");
    // The flows table asks for exactly those days, flow lane only.
    await waitFor(() => expect(asked.events.some((query) => query.includes("lane=flow"))).toBe(true));
    expect(asked.events[0]).toContain("start=2023-07-16T00%3A00%3A00Z");
  });

  it("offers to jump to the latest data when the range holds none of it", async () => {
    renderApp("/overview/it?start=2026-01-01&end=2026-01-07");
    await userEvent.click(await screen.findByTestId("range_latest"));
    await waitFor(() => expect(asked.it).toContain("?start=2023-07-16&end=2023-07-22&bucket=hour"));
    expect(screen.queryByTestId("range_latest")).not.toBeInTheDocument();
  });

  it("does not offer the jump beside a range that has data", async () => {
    renderApp("/overview/it?start=2023-07-01&end=2023-07-22");
    expect(await screen.findByTestId("stat-flows")).toHaveTextContent("40");
    expect(screen.queryByTestId("range_latest")).not.toBeInTheDocument();
  });
});

describe("OT overview", () => {
  it("shows the totals, all six types with SensiPro's codes, and the records", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-07");
    expect(await screen.findByTestId("stat-anomalies")).toHaveTextContent("7");
    expect(screen.getByTestId("stat-meters-affected")).toHaveTextContent("2");
    expect(asked.ot).toContain("?start=2023-07-01&end=2023-07-07&bucket=hour");
    for (const [issue, code] of [
      ["heartbeat", "1,0"],
      ["valueError", "2,0"],
      ["small", "2,4"],
    ] as const) {
      const row = screen.getByRole("button", { name: `About ${issue}` }).closest("tr") as HTMLElement;
      expect(within(row).getByText(code)).toBeInTheDocument();
    }
    expect(await screen.findByText(/above the limit of 35 kwh/)).toBeInTheDocument();
    expect(screen.getAllByText("rejected:").length).toBeGreaterThan(0);
  });

  it("replays the range: counts of what has arrived, a feed, and back to the whole range", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-07");
    expect(await screen.findByTestId("stat-anomalies")).toHaveTextContent("7");
    await userEvent.click(screen.getByTestId("replay_play"));
    // The first step is the range's first hour: nothing has arrived yet.
    expect(screen.getByTestId("replay_now")).toHaveTextContent("Replaying · 2023-07-01 00:00 UTC");
    expect(screen.getByTestId("stat-anomalies")).toHaveTextContent("0");
    expect(screen.getByTestId("replay_feed")).toHaveTextContent("Nothing yet");
    expect(screen.getByTestId("replay_pause")).toBeInTheDocument();
    await userEvent.click(screen.getByTestId("replay_pause"));
    await userEvent.click(screen.getByTestId("replay_reset"));
    expect(screen.getByTestId("stat-anomalies")).toHaveTextContent("7");
    expect(screen.queryByTestId("replay_feed")).not.toBeInTheDocument();
  });

  it("draws a long range by day", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-31");
    expect(await screen.findByText("Anomalies per day")).toBeInTheDocument();
    expect(asked.ot).toContain("?start=2023-07-01&end=2023-07-31&bucket=day");
  });

  it("draws a single day by hour", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-01");
    expect(await screen.findByText("Anomalies per hour")).toBeInTheDocument();
    expect(asked.ot).toContain("?start=2023-07-01&end=2023-07-01&bucket=hour");
  });

  it("filters the records by type", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-07");
    await userEvent.selectOptions(await screen.findByTestId("records_type"), "zero");
    await waitFor(() => expect(asked.records.some((query) => query.includes("issue=zero"))).toBe(true));
  });

  it("offers every meter beside the range and narrows the whole page to the one chosen", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-07");
    const select = await screen.findByTestId("ot_meter");
    // Right after "To", in the range's row.
    const row = screen.getByTestId("day_range");
    expect(within(row).getByTestId("ot_meter")).toBe(select);
    // A quiet meter (q1) is offered too, not only the ones with anomalies.
    await waitFor(() =>
      expect(within(select).getAllByRole("option").map((option) => option.textContent)).toEqual([
        "all sensors",
        "e1",
        "q1",
        "w1",
      ]),
    );
    await userEvent.selectOptions(select, "q1");
    await waitFor(() => expect(asked.ot).toContain("?start=2023-07-01&end=2023-07-07&asset_id=q1&bucket=hour"));
    await waitFor(() => expect(asked.records.some((query) => query.includes("asset_id=q1"))).toBe(true));
    await userEvent.selectOptions(screen.getByTestId("ot_meter"), "");
    expect(screen.getByTestId("ot_meter")).toHaveValue("");
  });

  it("keeps a meter chosen in the URL even when the range has none of its readings", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-07&meter=gone");
    await waitFor(() => expect(asked.ot).toContain("?start=2023-07-01&end=2023-07-07&asset_id=gone&bucket=hour"));
    await waitFor(() => expect(screen.getByTestId("ot_meter")).toHaveValue("gone"));
  });

  it("opens a meter on the same range, asking for that meter alone", async () => {
    renderApp("/overview/ot?start=2023-07-01&end=2023-07-07");
    const meters = await screen.findByTestId("ot_meters");
    await userEvent.click(within(meters).getByRole("link", { name: "w1" }));
    expect(await screen.findByRole("heading", { name: "Meter w1" })).toBeInTheDocument();
    await waitFor(() => expect(asked.ot).toContain("?start=2023-07-01&end=2023-07-07&asset_id=w1&bucket=hour"));
    // A day of its consumption: the range's last day by default.
    await waitFor(() => expect(asked.events.some((query) => query.includes("asset_id=w1"))).toBe(true));
    expect(asked.events.find((query) => query.includes("asset_id=w1"))).toContain("start=2023-07-07T00%3A00%3A00Z");
    expect(await screen.findByText("No readings for this meter on 2023-07-07.")).toBeInTheDocument();
  });
});

it("lists both overviews in the sidebar under Monitor, right after Triage", async () => {
  renderApp("/overview/it?start=2023-07-16&end=2023-07-22");
  expect(await screen.findByRole("link", { name: "IT overview" })).toHaveAttribute("href", "/overview/it");
  expect(screen.getByRole("link", { name: "OT overview" })).toHaveAttribute("href", "/overview/ot");
  const names = screen.getAllByRole("link").map((link) => link.textContent);
  const triage = names.indexOf("Triage");
  expect(names.slice(triage, triage + 3)).toEqual(["Triage", "IT overview", "OT overview"]);
});
