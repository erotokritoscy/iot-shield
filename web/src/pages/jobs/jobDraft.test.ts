/**
 * The job form's rules (ADR-0050): which chains can be a job, what a rolling window reads, and
 * the body the server is sent.
 */
import { describe, expect, it } from "vitest";

import type { JobInfo } from "@/lib/api/types";
import type { SourceRow } from "@/pages/compose/model";
import {
  draftFromJob,
  draftFromPulls,
  fixedButScheduled,
  jobBlocker,
  jobBody,
  jobProblem,
  jobPulls,
  resolveWindow,
  spanDays,
  withEnabled,
} from "@/pages/jobs/jobDraft";

const PULL = { source_id: "s-1", start: "2026-11-01", end: "2026-11-07", params: { utility: "water" } };
const row = (pull: typeof PULL | null): SourceRow => ({
  normalizer: "meter",
  format: "sensipro-hourly-csv",
  params: {},
  content: "",
  fileName: null,
  pull: pull ? { ...pull, rows: 3 } : null,
});
const NOW = new Date("2026-11-09T01:30:00Z");

const JOB: JobInfo = {
  job_id: "j-1",
  name: "Nightly",
  created_by: "alice",
  created_at: "2026-11-09T00:00:00+00:00",
  updated_at: "2026-11-09T00:00:00+00:00",
  spec: { sources: [] },
  sources: [{ source_id: "s-1", params: {}, window: { kind: "rolling", days: 7, through: "yesterday" } }],
  schedule: { every: "week", hour: 6, minute: 30, weekday: 4 },
  schedule_text: "every Friday at 06:30 UTC",
  enabled: true,
  next_run_at: "2026-11-13T06:30:00+00:00",
  last: null,
};

describe("jobBlocker", () => {
  it("allows a chain whose every source is a pull", () => {
    expect(jobBlocker([row(PULL), row(PULL)])).toBeNull();
  });

  it("names the sources that are uploads, since an upload is not kept", () => {
    expect(jobBlocker([row(PULL), row(null), row(null)])).toMatch(/source 2, 3 are an upload or paste/);
  });
});

describe("windows", () => {
  it("counts an inclusive span", () => {
    expect(spanDays("2026-11-01", "2026-11-07")).toBe(7);
    expect(spanDays("2026-12-31", "2027-01-01")).toBe(2);
  });

  it("resolves a rolling window in UTC, ending yesterday or today", () => {
    expect(resolveWindow({ kind: "rolling", days: 7, through: "yesterday" }, NOW)).toEqual({
      start: "2026-11-02",
      end: "2026-11-08",
    });
    expect(resolveWindow({ kind: "rolling", days: 1, through: "today" }, NOW)).toEqual({
      start: "2026-11-09",
      end: "2026-11-09",
    });
  });

  it("restores a job's pulls with each window as it reads now, labelled by its source", () => {
    expect(jobPulls(JOB, (id) => (id === "s-1" ? "Meters" : undefined), NOW)).toEqual([
      {
        origin: "Meters 2026-11-02..2026-11-08",
        pull: { source_id: "s-1", params: {}, start: "2026-11-02", end: "2026-11-08" },
      },
    ]);
  });
});

describe("the request body", () => {
  it("starts from the composed pulls: the same days, run when asked", () => {
    const draft = draftFromPulls([PULL], "Weekly water");
    expect(jobProblem(draft)).toBeNull();
    expect(jobBody(draft, { sources: [] })).toEqual({
      name: "Weekly water",
      spec: { sources: [] },
      sources: [
        {
          source_id: "s-1",
          params: { utility: "water" },
          window: { kind: "fixed", start: "2026-11-01", end: "2026-11-07" },
        },
      ],
      schedule: null,
      enabled: true,
    });
  });

  it("sends a rolling window and a daily schedule in UTC", () => {
    const base = draftFromPulls([PULL], "Nightly");
    const draft = {
      ...base,
      windows: base.windows.map((window) => ({ ...window, kind: "rolling" as const, days: "1" })),
      every: "day" as const,
      time: "02:15",
    };
    const body = jobBody(draft, {});
    expect(body.sources[0]?.window).toEqual({ kind: "rolling", days: 1, through: "yesterday" });
    expect(body.schedule).toEqual({ every: "day", hour: 2, minute: 15, weekday: 0 });
    expect(fixedButScheduled(draft)).toBe(false);
  });

  it("warns about a schedule that would re-read the same fixed days", () => {
    expect(fixedButScheduled({ ...draftFromPulls([PULL], "x"), every: "hour" })).toBe(true);
  });

  it.each([
    [{ name: "  " }, "Name the job."],
    [{ every: "day" as const, time: "25:00" }, "The time is HH:MM, in UTC."],
    [{ every: "hour" as const, minute: "60" }, "The minute past the hour is 0 to 59."],
  ])("refuses %o", (change, problem) => {
    expect(jobProblem({ ...draftFromPulls([PULL], "ok"), ...change })).toBe(problem);
  });

  it("refuses a rolling window of no days or a fraction of one", () => {
    const base = draftFromPulls([PULL], "ok");
    for (const days of ["0", "2.5"]) {
      const draft = { ...base, windows: base.windows.map((w) => ({ ...w, kind: "rolling" as const, days })) };
      expect(jobProblem(draft)).toMatch(/whole number of days, at least 1/);
    }
  });

  it("accepts a rolling window of years (ADR-0052)", () => {
    const base = draftFromPulls([PULL], "ok");
    const draft = { ...base, windows: base.windows.map((w) => ({ ...w, kind: "rolling" as const, days: "1220" })) };
    expect(jobProblem(draft)).toBeNull();
  });

  it("round-trips a saved job through the form", () => {
    const draft = draftFromJob(JOB);
    expect(draft).toMatchObject({ every: "week", time: "06:30", weekday: 4, enabled: true });
    expect(jobBody(draft, JOB.spec)).toEqual({
      name: "Nightly",
      spec: JOB.spec,
      sources: JOB.sources,
      schedule: JOB.schedule,
      enabled: true,
    });
  });

  it("pauses a job by sending it back unchanged but disabled", () => {
    expect(withEnabled(JOB, false)).toMatchObject({ name: "Nightly", schedule: JOB.schedule, enabled: false });
  });
});
