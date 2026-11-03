/**
 * The Run page (ADR-0045): one run's rows, and only them.
 *
 * What matters is the request, not the rendering — a page that drew the seeded world without
 * asking for `run_id` would look identical here and show the whole store in production, which
 * is the bug the page exists to fix. So every list's outgoing query is asserted.
 */
import { screen, waitFor, within } from "@testing-library/react";
import { HttpResponse, http } from "msw";
import { describe, expect, it, vi } from "vitest";

import * as seed from "@/test/fixtures";
import { API, emptyHandlers, RUN_ID, server } from "@/test/msw";
import { renderApp } from "@/test/render";

const LISTS = ["events", "malformed", "detections", "alerts"] as const;

describe("Run page", () => {
  it("asks every list for this run's rows alone", async () => {
    const asked: Record<string, string | null> = {};
    const respond = (list: (typeof LISTS)[number], rows: unknown[]) =>
      http.get(`${API}/${list}`, ({ request }) => {
        asked[list] = new URL(request.url).searchParams.get("run_id");
        return HttpResponse.json(rows);
      });
    server.use(
      respond("events", seed.events),
      respond("malformed", [seed.malformed]),
      respond("detections", [seed.detection]),
      respond("alerts", [seed.alert]),
    );
    renderApp(`/runs/${RUN_ID}#alerts`);

    const alerts = await screen.findByTestId("run-section-alerts");
    expect(await within(alerts).findByText("Alerts (1)")).toBeInTheDocument();
    expect(asked).toEqual(Object.fromEntries(LISTS.map((list) => [list, RUN_ID])));
    expect(within(screen.getByTestId("run-section-events")).getByText(`Events (${seed.events.length})`)).toBeInTheDocument();
  });

  it("lands on the list the tile named, once the lists above it have rendered", async () => {
    const scrolled: Element[] = [];
    const spy = vi.spyOn(Element.prototype, "scrollIntoView").mockImplementation(function (this: Element) {
      scrolled.push(this);
    });
    renderApp(`/runs/${RUN_ID}#detections`);
    await waitFor(() => expect(scrolled).toContain(screen.getByTestId("run-section-detections")));
    spy.mockRestore();
  });

  it("sends an alert to triage, where it is investigated", async () => {
    renderApp(`/runs/${RUN_ID}`);
    const link = await screen.findByTestId(`run-alert-link-${seed.alert.alert_id}`);
    expect(link).toHaveAttribute("href", `/?alert=${seed.alert.alert_id}`);
  });

  it("says so when nothing is stored under the run, rather than showing four empty lists", async () => {
    server.use(...emptyHandlers);
    renderApp(`/runs/${RUN_ID}`);
    expect(await screen.findByText(/Nothing is stored under this run/)).toBeInTheDocument();
  });
});
