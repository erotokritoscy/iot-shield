/**
 * The Jobs page (ADR-0050): following and driving saved jobs. What is asserted is the request
 * where the request is the point — a Pause that greys a row but PUTs nothing would come back on
 * reload.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import type { ExecutionInfo, JobInfo, MeResponse } from "@/lib/api/types";
import { PIPELINE, server } from "@/test/msw";
import { renderApp } from "@/test/render";

const JOB_ID = "abababab-abab-4bab-8bab-abababababab";
const RUN = "5e1f0c2a-9b7d-4c3e-8a6f-0d2b4c6e8f10";

const done: ExecutionInfo = {
  execution_id: "e-1",
  job_id: JOB_ID,
  trigger: "schedule",
  requested_by: null,
  status: "succeeded",
  queued_at: "2026-11-09T02:00:00+00:00",
  started_at: "2026-11-09T02:00:05+00:00",
  finished_at: "2026-11-09T02:01:00+00:00",
  run_id: RUN,
  error: null,
};

const JOB: JobInfo = {
  job_id: JOB_ID,
  name: "Nightly plant check",
  created_by: "alice",
  created_at: "2026-11-08T10:00:00+00:00",
  updated_at: "2026-11-08T10:00:00+00:00",
  spec: { sources: [{ normalizer: "passthrough", source_format: "normalized-jsonl", params: {} }] },
  sources: [{ source_id: "s-1", params: {}, window: { kind: "rolling", days: 1, through: "yesterday" } }],
  schedule: { every: "day", hour: 2, minute: 0, weekday: 0 },
  schedule_text: "every day at 02:00 UTC",
  enabled: true,
  next_run_at: "2026-11-10T02:00:00+00:00",
  notify_emails: ["alice@example.com"],
  last: done,
};

const sent: { runs: string[]; puts: unknown[]; deleted: string[] } = { runs: [], puts: [], deleted: [] };

function serve(jobs: JobInfo[], history: ExecutionInfo[] = [done]): void {
  let current = [...jobs];
  server.use(
    http.get(`${PIPELINE}/jobs`, () => HttpResponse.json(current)),
    http.post(`${PIPELINE}/jobs/:id/run`, ({ params }) => {
      sent.runs.push(String(params["id"]));
      const queued: ExecutionInfo = { ...done, execution_id: "e-2", trigger: "manual", status: "queued", run_id: null };
      current = current.map((job) => ({ ...job, last: queued }));
      return HttpResponse.json(queued, { status: 202 });
    }),
    http.put(`${PIPELINE}/jobs/:id`, async ({ request }) => {
      const body = (await request.json()) as { enabled: boolean };
      sent.puts.push(body);
      current = current.map((job) => ({ ...job, enabled: body.enabled, next_run_at: body.enabled ? job.next_run_at : null }));
      return HttpResponse.json(current[0]);
    }),
    http.delete(`${PIPELINE}/jobs/:id`, ({ params }) => {
      sent.deleted.push(String(params["id"]));
      current = [];
      return new HttpResponse(null, { status: 204 });
    }),
    http.get(`${PIPELINE}/jobs/:id/executions`, () => HttpResponse.json(history)),
  );
}

beforeEach(() => {
  sent.runs = [];
  sent.puts = [];
  sent.deleted = [];
});

describe("Jobs page", () => {
  it("lists each job with its schedule, window, next run and last result", async () => {
    serve([JOB]);
    renderApp("/jobs");
    const open = await screen.findByTestId(`job_open_${JOB_ID}`);
    expect(open).toHaveTextContent("Nightly plant check");
    expect(screen.getByText("every day at 02:00 UTC")).toBeInTheDocument();
    expect(screen.getByText("last 1 day to yesterday")).toBeInTheDocument();
    const status = screen.getByTestId(`job_status_${JOB_ID}`);
    expect(status).toHaveTextContent("succeeded");
    expect(within(status).getByRole("link", { name: "open run" })).toHaveAttribute("href", `/runs/${RUN}`);
    expect(screen.getByTestId(`job_edit_${JOB_ID}`)).toHaveAttribute("href", `/compose?job=${JOB_ID}`);
  });

  it("points to Compose when there are no jobs", async () => {
    renderApp("/jobs");
    expect(await screen.findByText(/Save as a job/)).toBeInTheDocument();
  });

  it("queues a run now and shows it running rather than starting a second", async () => {
    serve([JOB]);
    renderApp("/jobs");
    await userEvent.click(await screen.findByTestId(`job_run_${JOB_ID}`));
    await waitFor(() => expect(sent.runs).toEqual([JOB_ID]));
    await waitFor(() => expect(screen.getByTestId(`job_run_${JOB_ID}`)).toBeDisabled());
    expect(screen.getByTestId(`job_run_${JOB_ID}`)).toHaveTextContent("Running…");
  });

  it("pauses a schedule by sending the job back disabled", async () => {
    serve([JOB]);
    renderApp("/jobs");
    await userEvent.click(await screen.findByTestId(`job_toggle_${JOB_ID}`));
    await waitFor(() => expect(sent.puts).toHaveLength(1));
    expect(sent.puts[0]).toMatchObject({ name: JOB.name, sources: JOB.sources, schedule: JOB.schedule, enabled: false });
    expect(await screen.findByTestId(`job_toggle_${JOB_ID}`)).toHaveTextContent("Resume");
  });

  it("opens a job's history, failures with their reason", async () => {
    const failed: ExecutionInfo = { ...done, execution_id: "e-0", status: "failed", run_id: null, error: "Plant API refused the credentials" };
    serve([JOB], [done, failed]);
    renderApp("/jobs");
    await userEvent.click(await screen.findByTestId(`job_open_${JOB_ID}`));
    const history = await screen.findByTestId(`job_history_${JOB_ID}`);
    expect(await within(history).findByText("Plant API refused the credentials")).toBeInTheDocument();
  });

  it("deletes a job only once confirmed", async () => {
    serve([JOB]);
    const confirm = window.confirm;
    try {
      window.confirm = () => false;
      renderApp("/jobs");
      await userEvent.click(await screen.findByTestId(`job_delete_${JOB_ID}`));
      expect(sent.deleted).toEqual([]);
      window.confirm = () => true;
      await userEvent.click(screen.getByTestId(`job_delete_${JOB_ID}`));
      await waitFor(() => expect(sent.deleted).toEqual([JOB_ID]));
      expect(await screen.findByText(/No jobs yet/)).toBeInTheDocument();
    } finally {
      window.confirm = confirm;
    }
  });

  it("offers a member no delete button", async () => {
    const member: MeResponse = {
      user_id: "22222222-2222-4222-8222-222222222222",
      username: "bob",
      role: "member",
      organization: { org_id: "acme", name: "Acme" },
    };
    server.use(http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json(member)));
    serve([JOB]);
    renderApp("/jobs");
    expect(await screen.findByTestId(`job_run_${JOB_ID}`)).toBeInTheDocument();
    expect(screen.queryByTestId(`job_delete_${JOB_ID}`)).not.toBeInTheDocument();
  });

  it("is in the sidebar after Runs", async () => {
    renderApp("/jobs");
    const nav = await screen.findByRole("navigation");
    expect(within(nav).getByRole("link", { name: "Jobs" })).toHaveAttribute("href", "/jobs");
  });
});
