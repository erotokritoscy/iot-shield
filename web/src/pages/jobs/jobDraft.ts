/**
 * A job as the form holds it (ADR-0050): its name, each pull's window rule, and the schedule.
 *
 * Kept apart from the form so the rules — which sources can be a job at all, what a rolling
 * window resolves to today, what the request body is — are plain functions with their own tests.
 * Every time here is UTC, like the server's; the form shows the viewer's local time beside it.
 */
import type { JobInfo, JobRequest, JobSchedule, JobWindow } from "@/lib/api/types";
import type { PulledWindow, SourceRow } from "@/pages/compose/model";

export type WindowKind = "fixed" | "rolling";
export type Through = "yesterday" | "today";
export type Every = "none" | "hour" | "day" | "week";

export const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"] as const;
export const EVERY: readonly Every[] = ["none", "hour", "day", "week"];
export const EVERY_LABEL: Record<Every, string> = {
  none: "Only when I run it",
  hour: "Every hour",
  day: "Every day",
  week: "Every week",
};

export interface WindowDraft {
  readonly source_id: string;
  readonly params: Readonly<Record<string, unknown>>;
  readonly kind: WindowKind;
  readonly start: string;
  readonly end: string;
  /** Text, because it is a form field; checked by `jobProblem`. */
  readonly days: string;
  readonly through: Through;
}

export interface JobDraft {
  readonly name: string;
  readonly windows: readonly WindowDraft[];
  readonly every: Every;
  /** "HH:MM", UTC — the time of day for a daily or weekly job. */
  readonly time: string;
  /** Minute past the hour, for an hourly job. */
  readonly minute: string;
  readonly weekday: number;
  readonly enabled: boolean;
}

const DAY_MS = 86_400_000;
const dayNumber = (iso: string) => Date.parse(`${iso}T00:00:00Z`) / DAY_MS;
const isoDay = (day: number) => new Date(day * DAY_MS).toISOString().slice(0, 10);

/** Days in an inclusive window, e.g. 2026-11-01..2026-11-07 is 7. */
export const spanDays = (start: string, end: string): number => dayNumber(end) - dayNumber(start) + 1;

/** Why these sources cannot be saved as a job, or null when they can. */
export function jobBlocker(sources: readonly SourceRow[]): string | null {
  const notPulled = sources.map((row, index) => (row.pull ? null : index + 1)).filter((n) => n !== null);
  if (notPulled.length === 0) return null;
  return (
    `A job reads its data again each time, so every source must be pulled from a data source — ` +
    `source ${notPulled.join(", ")} ${notPulled.length === 1 ? "is" : "are"} an upload or paste, which is not kept.`
  );
}

function windowOf(pull: Omit<PulledWindow, "rows">): WindowDraft {
  return {
    source_id: pull.source_id,
    params: pull.params,
    kind: "fixed",
    start: pull.start,
    end: pull.end,
    days: String(spanDays(pull.start, pull.end)),
    through: "yesterday",
  };
}

/** A new job from the pulls on the Compose page: the same days, run when asked. */
export function draftFromPulls(pulls: readonly Omit<PulledWindow, "rows">[], name = ""): JobDraft {
  return { name, windows: pulls.map(windowOf), every: "none", time: "02:00", minute: "0", weekday: 0, enabled: true };
}

const pad = (n: number) => String(n).padStart(2, "0");

/** A saved job back in the form. The chain is the Compose page's; only these fields are here. */
export function draftFromJob(job: JobInfo): JobDraft {
  const windows = job.sources.map((source): WindowDraft => {
    const window = source.window;
    const base = { source_id: source.source_id, params: source.params ?? {} };
    return window.kind === "fixed"
      ? { ...base, kind: "fixed", start: window.start, end: window.end, days: String(spanDays(window.start, window.end)), through: "yesterday" }
      : { ...base, kind: "rolling", start: "", end: "", days: String(window.days), through: window.through };
  });
  const schedule = job.schedule;
  return {
    name: job.name,
    windows,
    every: schedule?.every ?? "none",
    time: schedule ? `${pad(schedule.hour)}:${pad(schedule.minute)}` : "02:00",
    minute: String(schedule?.minute ?? 0),
    weekday: schedule?.weekday ?? 0,
    enabled: job.enabled,
  };
}

/** The window's days at `now` (UTC), as the server will work them out. */
export function resolveWindow(window: WindowDraft | JobWindow, now: Date = new Date()): { start: string; end: string } {
  if (window.kind === "fixed") return { start: window.start, end: window.end };
  const today = Math.floor(now.getTime() / DAY_MS);
  const end = window.through === "today" ? today : today - 1;
  const days = typeof window.days === "number" ? window.days : Number(window.days);
  return { start: isoDay(end - days + 1), end: isoDay(end) };
}

const TIME = /^([01]\d|2[0-3]):([0-5]\d)$/;

function scheduleOf(draft: JobDraft): JobSchedule | null {
  if (draft.every === "none") return null;
  if (draft.every === "hour") return { every: "hour", minute: Number(draft.minute), hour: 0, weekday: 0 };
  const [, hour = "0", minute = "0"] = TIME.exec(draft.time) ?? [];
  return { every: draft.every, hour: Number(hour), minute: Number(minute), weekday: draft.every === "week" ? draft.weekday : 0 };
}

/** What stops the draft being saved, or null. The server checks the rest (the chain, the sources). */
export function jobProblem(draft: JobDraft): string | null {
  if (draft.name.trim() === "") return "Name the job.";
  for (const [index, window] of draft.windows.entries()) {
    const label = draft.windows.length > 1 ? `Source ${index + 1}: ` : "";
    if (window.kind === "rolling") {
      const days = Number(window.days);
      if (!Number.isInteger(days) || days < 1) {
        return `${label}a rolling window is a whole number of days, at least 1.`;
      }
    } else if (!window.start || !window.end || window.end < window.start) {
      return `${label}the window's end is before its start.`;
    }
  }
  if (draft.every === "hour") {
    const minute = Number(draft.minute);
    if (!Number.isInteger(minute) || minute < 0 || minute > 59) return "The minute past the hour is 0 to 59.";
  } else if (draft.every !== "none" && !TIME.test(draft.time)) {
    return "The time is HH:MM, in UTC.";
  }
  return null;
}

/** A scheduled job whose windows never move re-reads the same days on every run — worth saying. */
export function fixedButScheduled(draft: JobDraft): boolean {
  return draft.every !== "none" && draft.windows.some((window) => window.kind === "fixed");
}

/** The request body. `spec` is the Compose page's own `buildSpec` result. */
export function jobBody(draft: JobDraft, spec: unknown): JobRequest {
  return {
    name: draft.name.trim(),
    spec: spec as JobRequest["spec"],
    sources: draft.windows.map((window) => ({
      source_id: window.source_id,
      params: { ...window.params },
      window:
        window.kind === "fixed"
          ? { kind: "fixed", start: window.start, end: window.end }
          : { kind: "rolling", days: Number(window.days), through: window.through },
    })),
    schedule: scheduleOf(draft),
    enabled: draft.enabled,
  };
}

/** A saved job's body with one field changed — how the Jobs page enables and disables one. */
export function withEnabled(job: JobInfo, enabled: boolean): JobRequest {
  return {
    name: job.name,
    spec: job.spec as JobRequest["spec"],
    sources: job.sources.map((source) => ({ ...source, params: source.params ?? {} })),
    schedule: job.schedule,
    enabled,
  };
}

/** "HH:MM" UTC as the viewer's local time, e.g. "04:00 your time", or null when it is the same. */
export function localTime(utc: string, now: Date = new Date()): string | null {
  const match = TIME.exec(utc);
  if (!match) return null;
  const at = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(), Number(match[1]), Number(match[2])));
  const local = `${pad(at.getHours())}:${pad(at.getMinutes())}`;
  return local === utc ? null : `${local} your time`;
}

/**
 * The pulls the job's sources read, as the Compose page's rows hold them, at `now` — each
 * labelled the way a pull on that page is (`<source> <start>..<end>`).
 */
export function jobPulls(
  job: JobInfo,
  nameOf: (sourceId: string) => string | undefined = () => undefined,
  now: Date = new Date(),
): { origin: string; pull: Omit<PulledWindow, "rows"> }[] {
  return job.sources.map((source, index) => {
    const { start, end } = resolveWindow(source.window, now);
    return {
      origin: `${nameOf(source.source_id) ?? `source-${index + 1}`} ${start}..${end}`,
      pull: { source_id: source.source_id, params: source.params ?? {}, start, end },
    };
  });
}
