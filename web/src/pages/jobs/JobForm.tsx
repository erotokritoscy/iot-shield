/**
 * The fields of a job (ADR-0050): its name, which days each pull reads, and when it runs.
 * State and rules live in `jobDraft.ts`; this only renders them.
 */
import { Checkbox, Field, NumberInput, Select, TextInput } from "@/components/ui/controls";
import { Notice } from "@/components/ui/surfaces";
import type { DataSourceInfo } from "@/lib/api/types";
import {
  EVERY,
  EVERY_LABEL,
  fixedButScheduled,
  localTime,
  MAX_JOB_DAYS,
  resolveWindow,
  WEEKDAYS,
  type Every,
  type JobDraft,
  type Through,
  type WindowDraft,
  type WindowKind,
} from "@/pages/jobs/jobDraft";

const WINDOW_KINDS: readonly WindowKind[] = ["rolling", "fixed"];
const WINDOW_LABEL: Record<WindowKind, string> = {
  rolling: "The last N days (moves each run)",
  fixed: "These exact days (the same every run)",
};
const THROUGH: readonly Through[] = ["yesterday", "today"];

export function JobForm({
  draft,
  dataSources,
  onChange,
}: {
  readonly draft: JobDraft;
  readonly dataSources: readonly DataSourceInfo[];
  readonly onChange: (next: JobDraft) => void;
}) {
  const set = <K extends keyof JobDraft>(key: K, value: JobDraft[K]) => onChange({ ...draft, [key]: value });
  const setWindow = (index: number, next: WindowDraft) =>
    set(
      "windows",
      draft.windows.map((window, position) => (position === index ? next : window)),
    );
  const sourceName = (id: string) => dataSources.find((source) => source.source_id === id)?.name ?? "a data source";
  const local = draft.every === "day" || draft.every === "week" ? localTime(draft.time) : null;

  return (
    <div className="space-y-4" data-testid="job_form">
      <Field label="Job name" htmlFor="job-name">
        <TextInput
          id="job-name"
          data-testid="job_name"
          maxLength={200}
          placeholder="e.g. Nightly plant check"
          value={draft.name}
          onChange={(event) => set("name", event.target.value)}
        />
      </Field>

      {draft.windows.map((window, index) => {
        const resolved = resolveWindow(window);
        return (
          <div key={index} className="grid gap-4 sm:grid-cols-3" data-testid={`job_window_${index}`}>
            <Field
              label={`Source ${index + 1}: ${sourceName(window.source_id)}`}
              htmlFor={`job-window-kind-${index}`}
              className="sm:col-span-3"
              hint={
                window.kind === "rolling" && Number(window.days) >= 1
                  ? `Run today, it would read ${resolved.start} to ${resolved.end} (UTC).`
                  : undefined
              }
            >
              <Select
                id={`job-window-kind-${index}`}
                data-testid={`job_window_kind_${index}`}
                options={WINDOW_KINDS}
                labelFor={(option) => WINDOW_LABEL[option as WindowKind]}
                value={window.kind}
                onChange={(event) => setWindow(index, { ...window, kind: event.target.value as WindowKind })}
              />
            </Field>
            {window.kind === "rolling" ? (
              <>
                <Field label="Days" htmlFor={`job-days-${index}`}>
                  <NumberInput
                    id={`job-days-${index}`}
                    data-testid={`job_days_${index}`}
                    min={1}
                    max={MAX_JOB_DAYS}
                    value={window.days}
                    onChange={(event) => setWindow(index, { ...window, days: event.target.value })}
                  />
                </Field>
                <Field label="Ending" htmlFor={`job-through-${index}`}>
                  <Select
                    id={`job-through-${index}`}
                    data-testid={`job_through_${index}`}
                    options={THROUGH}
                    labelFor={(option) => (option === "yesterday" ? "Yesterday (whole days)" : "Today (so far)")}
                    value={window.through}
                    onChange={(event) => setWindow(index, { ...window, through: event.target.value as Through })}
                  />
                </Field>
              </>
            ) : (
              <>
                <Field label="From" htmlFor={`job-start-${index}`}>
                  <input
                    id={`job-start-${index}`}
                    type="date"
                    className="control"
                    data-testid={`job_start_${index}`}
                    value={window.start}
                    onChange={(event) => setWindow(index, { ...window, start: event.target.value })}
                  />
                </Field>
                <Field label="To" htmlFor={`job-end-${index}`}>
                  <input
                    id={`job-end-${index}`}
                    type="date"
                    className="control"
                    data-testid={`job_end_${index}`}
                    value={window.end}
                    onChange={(event) => setWindow(index, { ...window, end: event.target.value })}
                  />
                </Field>
              </>
            )}
          </div>
        );
      })}

      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Runs" htmlFor="job-every">
          <Select
            id="job-every"
            data-testid="job_every"
            options={EVERY}
            labelFor={(option) => EVERY_LABEL[option as Every]}
            value={draft.every}
            onChange={(event) => set("every", event.target.value as Every)}
          />
        </Field>
        {draft.every === "hour" ? (
          <Field label="Minutes past the hour" htmlFor="job-minute">
            <NumberInput
              id="job-minute"
              data-testid="job_minute"
              min={0}
              max={59}
              value={draft.minute}
              onChange={(event) => set("minute", event.target.value)}
            />
          </Field>
        ) : null}
        {draft.every === "week" ? (
          <Field label="On" htmlFor="job-weekday">
            <Select
              id="job-weekday"
              data-testid="job_weekday"
              options={WEEKDAYS.map((_, index) => String(index))}
              labelFor={(option) => WEEKDAYS[Number(option)] ?? option}
              value={String(draft.weekday)}
              onChange={(event) => set("weekday", Number(event.target.value))}
            />
          </Field>
        ) : null}
        {draft.every === "day" || draft.every === "week" ? (
          <Field label="At (UTC)" htmlFor="job-time" hint={local ?? undefined}>
            <input
              id="job-time"
              type="time"
              className="control"
              data-testid="job_time"
              value={draft.time}
              onChange={(event) => set("time", event.target.value)}
            />
          </Field>
        ) : null}
      </div>
      {draft.every !== "none" ? (
        <Checkbox
          data-testid="job_enabled"
          label="Enabled — run on this schedule"
          checked={draft.enabled}
          onChange={(event) => set("enabled", event.target.checked)}
        />
      ) : null}
      {fixedButScheduled(draft) ? (
        <Notice tone="warning">
          A scheduled job with fixed days reads the same days on every run. Choose “The last N days” for a job that
          should read new data.
        </Notice>
      ) : null}
    </div>
  );
}
