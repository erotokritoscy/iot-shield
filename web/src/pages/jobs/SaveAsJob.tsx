/**
 * "Save as job" on the Compose page's Validate & run step (ADR-0050): the chain on screen, with
 * a window rule per pull and a schedule, saved to run again — or, opened from a job, updating it.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { Link } from "react-router-dom";

import { Button } from "@/components/ui/controls";
import { Collapsible, Notice } from "@/components/ui/surfaces";
import { pipelineApi } from "@/lib/api/client";
import { describeError } from "@/lib/api/http";
import type { DataSourceInfo, JobInfo } from "@/lib/api/types";
import { useAuth } from "@/lib/auth";
import { keys } from "@/lib/queries";
import type { SourceRow } from "@/pages/compose/model";
import { JobForm } from "@/pages/jobs/JobForm";
import {
  defaultRecipients,
  draftFromJob,
  draftFromPulls,
  jobBlocker,
  jobBody,
  jobProblem,
  type JobDraft,
} from "@/pages/jobs/jobDraft";

/** The draft follows the sources: a pull added or changed on step 1 shows up here. */
function aligned(draft: JobDraft, sources: readonly SourceRow[]): JobDraft {
  const fresh = draftFromPulls(sources.flatMap((row) => (row.pull ? [row.pull] : [])));
  return {
    ...draft,
    windows: fresh.windows.map((window, index) => {
      const kept = draft.windows[index];
      return kept && kept.source_id === window.source_id ? { ...kept, params: window.params } : window;
    }),
  };
}

export function SaveAsJob({
  spec,
  sources,
  chainOk,
  job,
  dataSources,
}: {
  readonly spec: unknown;
  readonly sources: readonly SourceRow[];
  /** Every step is complete and the chain passed validation. */
  readonly chainOk: boolean;
  /** The job this page was opened from, when it was (`?job=`). */
  readonly job?: JobInfo | undefined;
  readonly dataSources: readonly DataSourceInfo[];
}) {
  const blocker = jobBlocker(sources);
  const auth = useAuth();
  const me = auth.data?.kind === "user" ? auth.data.me : null;
  const [draft, setDraft] = React.useState<JobDraft>(() =>
    job
      ? draftFromJob(job)
      : draftFromPulls(
          sources.flatMap((row) => (row.pull ? [row.pull] : [])),
          "",
          defaultRecipients(me?.username),
        ),
  );
  const current = aligned(draft, sources);
  const problem = jobProblem(current);
  const queryClient = useQueryClient();
  const save = useMutation({
    mutationFn: (mode: "create" | "update") =>
      mode === "update" && job
        ? pipelineApi.updateJob(job.job_id, jobBody(current, spec))
        : pipelineApi.createJob(jobBody(current, spec)),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: keys.jobs }),
  });
  const disabled = !chainOk || blocker !== null || problem !== null || save.isPending;

  return (
    <Collapsible label={job ? `Update the job “${job.name}”` : "Save as a job — run it again or on a schedule"} defaultOpen={Boolean(job)}>
      <div className="space-y-4" data-testid="job_save">
        {blocker ? (
          <div data-testid="job_blocker">
            <Notice tone="info">{blocker}</Notice>
          </div>
        ) : (
          <>
            <JobForm draft={current} dataSources={dataSources} onChange={setDraft} />
            {problem ? (
              <p className="caption" data-testid="job_problem">
                {problem}
              </p>
            ) : null}
            {!chainOk ? <p className="caption">Complete every step and pass validation first.</p> : null}
            <div className="flex items-center gap-3">
              {job ? (
                <Button variant="primary" data-testid="job_update" disabled={disabled} onClick={() => save.mutate("update")}>
                  Update job
                </Button>
              ) : null}
              <Button
                variant={job ? "secondary" : "primary"}
                data-testid="job_create"
                disabled={disabled}
                onClick={() => save.mutate("create")}
              >
                {job ? "Save as a new job" : "Save job"}
              </Button>
            </div>
          </>
        )}
        {save.isError ? <Notice tone="error">Could not save the job: {describeError(save.error)}</Notice> : null}
        {save.isSuccess ? (
          <div data-testid="job_saved">
            <Notice tone="success">
              Saved “{save.data.name}”
              {save.data.schedule_text ? ` — runs ${save.data.schedule_text}` : ""}. Run it or follow it under{" "}
              <Link to="/jobs" className="underline underline-offset-2">
                Operate → Jobs
              </Link>
              .
            </Notice>
          </div>
        ) : null}
      </div>
    </Collapsible>
  );
}
