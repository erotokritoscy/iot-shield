/**
 * Saved runs on the dashboard (ADR-0046): the Runs list, a run's record on the Run page —
 * rename, delete, Open in Compose — and the chain coming back on the wizard.
 *
 * As on the Compose suite, what is asserted is the request where the request is the point: a
 * rename that renders the new name but PATCHes nothing would look right here and be lost on
 * reload.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { describe, expect, it } from "vitest";

import type { MeResponse } from "@/lib/api/types";
import * as seed from "@/test/fixtures";
import { captured, PIPELINE, RUN_ID, server } from "@/test/msw";
import { renderApp } from "@/test/render";

describe("Runs list", () => {
  it("lists every saved run, each opening its Run page", async () => {
    renderApp("/runs");
    const link = await screen.findByTestId(`runs_open_${RUN_ID}`);
    expect(link).toHaveTextContent("Night shift check");
    expect(link).toHaveAttribute("href", `/runs/${RUN_ID}`);
    expect(screen.getByText("gateway.log")).toBeInTheDocument();
  });

  it("points to Compose when nothing has run yet", async () => {
    server.use(http.get(`${PIPELINE}/runs`, () => HttpResponse.json([])));
    renderApp("/runs");
    expect(await screen.findByText(/every run is saved here/)).toBeInTheDocument();
  });

  it("deletes a run from its row only once confirmed, and drops it from the list", async () => {
    captured.deletedRun = null;
    let deleted = false;
    server.use(
      http.get(`${PIPELINE}/runs`, () => HttpResponse.json(deleted ? [] : [seed.savedRun])),
      http.delete(`${PIPELINE}/runs/:id`, ({ params }) => {
        captured.deletedRun = String(params["id"]);
        deleted = true;
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const confirm = window.confirm;
    try {
      window.confirm = () => false;
      renderApp("/runs");
      await userEvent.click(await screen.findByTestId(`runs_delete_${RUN_ID}`));
      expect(captured.deletedRun).toBeNull();

      window.confirm = () => true;
      await userEvent.click(screen.getByTestId(`runs_delete_${RUN_ID}`));
      await waitFor(() => expect(captured.deletedRun).toBe(RUN_ID));
      // The list is re-read, not edited locally: what it shows is what the server still holds.
      expect(await screen.findByText(/every run is saved here/)).toBeInTheDocument();
    } finally {
      window.confirm = confirm;
    }
  });

  it("offers a member no delete button: only an admin may delete", async () => {
    const member: MeResponse = {
      user_id: "22222222-2222-4222-8222-222222222222",
      username: "bob",
      role: "member",
      organization: { org_id: "acme", name: "Acme" },
    };
    server.use(http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json(member)));
    renderApp("/runs");
    expect(await screen.findByTestId(`runs_open_${RUN_ID}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`runs_delete_${RUN_ID}`)).not.toBeInTheDocument();
  });

  it("is in the sidebar beside Compose", async () => {
    renderApp("/runs");
    const nav = await screen.findByRole("navigation");
    expect(within(nav).getByRole("link", { name: "Runs" })).toHaveAttribute("href", "/runs");
  });
});

describe("A run's record", () => {
  it("shows the name, who ran it and the chain", async () => {
    renderApp(`/runs/${RUN_ID}`);
    expect(await screen.findByTestId("run_name")).toHaveTextContent("Night shift check");
    expect(screen.getByText(/by ana/)).toBeInTheDocument();
    expect(screen.getByText("log_burst")).toBeInTheDocument();
    expect(screen.getByText("postgres, file")).toBeInTheDocument();
  });

  it("renames through the API", async () => {
    renderApp(`/runs/${RUN_ID}`);
    await userEvent.click(await screen.findByTestId("run_rename"));
    const input = screen.getByTestId("run_rename_input");
    await userEvent.clear(input);
    await userEvent.type(input, "Gateway audit");
    await userEvent.click(screen.getByTestId("run_rename_save"));
    await waitFor(() => expect(captured.renameBody).toEqual({ name: "Gateway audit" }));
  });

  it("deletes only once confirmed, then returns to the list", async () => {
    captured.deletedRun = null;
    const confirm = window.confirm;
    try {
      window.confirm = () => false;
      renderApp(`/runs/${RUN_ID}`);
      await userEvent.click(await screen.findByTestId("run_delete"));
      expect(captured.deletedRun).toBeNull();

      window.confirm = () => true;
      await userEvent.click(screen.getByTestId("run_delete"));
      await waitFor(() => expect(captured.deletedRun).toBe(RUN_ID));
      expect(await screen.findByRole("heading", { name: "Runs" })).toBeInTheDocument();
    } finally {
      window.confirm = confirm;
    }
  });

  it("says so for a run that predates saved runs, and still shows its results", async () => {
    const unsaved = "00000000-0000-4000-8000-000000000001";
    renderApp(`/runs/${unsaved}`);
    expect(await screen.findByText(/no saved record/)).toBeInTheDocument();
    expect(screen.getByTestId("run-section-events")).toBeInTheDocument();
  });
});

describe("Open in Compose", () => {
  it("restores a pulled source as a pull, ready to run (ADR-0047)", async () => {
    // A pull is a reference, so unlike an upload the saved run keeps it, and reopening needs
    // no data attached: the run reads the window again.
    const pull = {
      source_id: "7a1c2e3f-0000-4000-8000-00000000abcd",
      start: "2023-07-01",
      end: "2023-07-02",
      params: { utility: "water" },
    };
    server.use(
      http.get(`${PIPELINE}/runs/:id`, () =>
        HttpResponse.json({
          ...seed.savedRun,
          sources: [{ normalizer: "syslog", source_format: "syslog", origin: "Meters 07-01..07-02", pull }],
        }),
      ),
    );
    renderApp(`/compose?from=${RUN_ID}`);
    expect(await screen.findByTestId("compose_reopened")).toHaveTextContent(
      "Sources pulled from a data source are restored",
    );
    expect(screen.getByTestId("src_0_mode_pull")).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("src_0_pulled")).toHaveTextContent(
      "A window of Meters 07-01..07-02, read from the database when the run starts.",
    );
    await userEvent.click(screen.getByTestId("step_4"));
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));
    await screen.findByText("Run complete.");
    const body = captured.runBody as { inputs: unknown[] };
    expect(body.inputs).toEqual([{ pull, origin: "Meters 07-01..07-02" }]);
  });

  it("links the record to the wizard", async () => {
    renderApp(`/runs/${RUN_ID}`);
    expect(await screen.findByTestId("run_reopen")).toHaveAttribute("href", `/compose?from=${RUN_ID}`);
  });

  it("restores the chain as it ran, and asks for the data again", async () => {
    renderApp(`/compose?from=${RUN_ID}`);
    const notice = await screen.findByTestId("compose_reopened");
    expect(notice).toHaveTextContent("Night shift check");
    expect(notice).toHaveTextContent("gateway.log");
    // An uploaded input was not saved, so the way it arrives is asked again — and the saved
    // reader survives the choice rather than being reset to the default.
    expect(screen.getByTestId("src_0_mode_paste")).toHaveAttribute("aria-checked", "false");
    await userEvent.click(screen.getByTestId("src_0_mode_paste"));
    expect(screen.getByTestId("src_0_norm")).toHaveValue("syslog");
    expect(screen.getByTestId("src_0_text")).toHaveValue("");

    // Data first — the wizard will not move on without it — then the restored detector shows.
    await userEvent.type(screen.getByTestId("src_0_text"), "<38>1 2026-08-03T09:00:00Z gw sshd 1 - - x");
    await userEvent.click(screen.getByTestId("step_2"));
    expect(await screen.findByTestId("det_0_name")).toHaveValue("log_burst");

    // And it sends the same request the run sent: the moved param, the restored sinks.
    await userEvent.click(screen.getByTestId("step_4"));
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));
    await screen.findByText("Run complete.");
    const body = captured.runBody as { spec: { detectors: unknown; sinks: { name: string }[] } };
    expect(body.spec.detectors).toEqual([{ name: "log_burst", params: { min_count: 12 } }]);
    expect(body.spec.sinks.map((sink) => sink.name)).toEqual(["postgres", "file"]);
  });

  it("opens a fresh wizard, with the reason, when the run cannot be loaded", async () => {
    renderApp("/compose?from=00000000-0000-4000-8000-000000000001");
    expect(await screen.findByText(/Could not reopen that run/)).toBeInTheDocument();
    expect(await screen.findByTestId("src_0_mode_paste")).toHaveAttribute("aria-checked", "false");
  });

  it("sends the run name it was given, and none when left blank", async () => {
    renderApp(`/compose?from=${RUN_ID}`);
    await userEvent.click(await screen.findByTestId("src_0_mode_paste"));
    await userEvent.type(screen.getByTestId("src_0_text"), "<38>1 2026-08-03T09:00:00Z gw sshd 1 - - x");
    await userEvent.click(screen.getByTestId("step_4"));
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));
    await screen.findByText("Run complete.");
    expect(captured.runBody).not.toHaveProperty("name");

    await userEvent.type(screen.getByTestId("compose_run_name"), "  Gateway audit ");
    await userEvent.click(screen.getByTestId("compose_run"));
    await waitFor(() => expect(captured.runBody).toHaveProperty("name", "Gateway audit"));
  });
});

it("links the receipt to the Runs list once a run is stored", async () => {
  renderApp(`/compose?from=${seed.savedRun.run_id}`);
  await userEvent.click(await screen.findByTestId("src_0_mode_paste"));
  await userEvent.type(screen.getByTestId("src_0_text"), "<38>1 2026-08-03T09:00:00Z gw sshd 1 - - x");
  await userEvent.click(screen.getByTestId("step_4"));
  await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
  await userEvent.click(screen.getByTestId("compose_run"));
  expect(await screen.findByTestId("compose_saved_link")).toHaveAttribute("href", "/runs");
});
