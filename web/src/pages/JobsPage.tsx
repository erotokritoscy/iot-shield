/**
 * The organization's saved jobs (ADR-0050): what each runs, when, and how its last run went.
 *
 * A job is made on the Compose page ("Save as a job", on Validate & run) and edited there too —
 * "Edit" reopens it with `?job=` — so this page is for following and driving jobs: run one now,
 * pause its schedule, read its history, open the runs it made. Runs happen in the `worker`
 * service, so the page polls while anything is queued or running.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import * as React from "react";
import { Link } from "react-router-dom";

import { QueryState } from "@/components/QueryState";
import { Button } from "@/components/ui/controls";
import { Notice, PageHeader, Spinner } from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { pipelineApi } from "@/lib/api/client";
import { describeError } from "@/lib/api/http";
import type { ExecutionInfo, JobInfo } from "@/lib/api/types";
import { isOrgAdmin, useAuth } from "@/lib/auth";
import { formatUtc } from "@/lib/format";
import { keys, useJobExecutions, useJobs } from "@/lib/queries";
import { withEnabled } from "@/pages/jobs/jobDraft";

const STATUS_TONE: Record<string, string> = {
  queued: "text-dim",
  running: "text-accent",
  succeeded: "text-success",
  failed: "text-critical",
};

function Status({ execution }: { readonly execution: ExecutionInfo | null }) {
  if (!execution) return <span className="text-muted">never run</span>;
  return (
    <span className={STATUS_TONE[execution.status] ?? ""} data-testid={`job_status_${execution.job_id}`}>
      {execution.status}
      {execution.run_id ? (
        <>
          {" — "}
          <Link to={`/runs/${encodeURIComponent(execution.run_id)}`} className="underline underline-offset-2">
            open run
          </Link>
        </>
      ) : null}
    </span>
  );
}

function windowText(job: JobInfo): string {
  return job.sources
    .map((source) =>
      source.window.kind === "rolling"
        ? `last ${source.window.days} day${source.window.days === 1 ? "" : "s"} to ${source.window.through}`
        : `${source.window.start}..${source.window.end}`,
    )
    .join("; ");
}

function History({ job }: { readonly job: JobInfo }) {
  const executions = useJobExecutions(job.job_id);
  const columns: Column<ExecutionInfo>[] = [
    { key: "queued", header: "queued", render: (run) => formatUtc(run.queued_at, "minute") },
    { key: "trigger", header: "by", render: (run) => (run.trigger === "schedule" ? "schedule" : (run.requested_by ?? "—")) },
    { key: "status", header: "result", render: (run) => <Status execution={run} /> },
    {
      key: "error",
      header: "",
      render: (run) => (run.error ? <span className="text-xs text-critical">{run.error}</span> : null),
      className: "max-w-md",
    },
  ];
  return (
    <div className="space-y-2" data-testid={`job_history_${job.job_id}`}>
      <p className="field-label">History of “{job.name}”</p>
      {executions.isPending ? <Spinner label="Loading its runs…" /> : null}
      {executions.isError ? <Notice tone="error">{describeError(executions.error)}</Notice> : null}
      {executions.data ? (
        executions.data.length === 0 ? (
          <p className="caption">It has not run yet.</p>
        ) : (
          <DataTable columns={columns} rows={executions.data} rowKey={(run) => run.execution_id} pageSize={20} />
        )
      ) : null}
    </div>
  );
}

export function JobsPage() {
  const jobs = useJobs();
  const auth = useAuth();
  const admin = isOrgAdmin(auth.data);
  const queryClient = useQueryClient();
  const [shown, setShown] = React.useState<string | null>(null);
  const refresh = (jobId: string) => {
    void queryClient.invalidateQueries({ queryKey: keys.jobs });
    void queryClient.invalidateQueries({ queryKey: keys.jobExecutions(jobId) });
  };
  const runNow = useMutation({ mutationFn: (jobId: string) => pipelineApi.runJob(jobId), onSuccess: (_, id) => refresh(id) });
  const toggle = useMutation({
    mutationFn: (job: JobInfo) => pipelineApi.updateJob(job.job_id, withEnabled(job, !job.enabled)),
    onSuccess: (job) => refresh(job.job_id),
  });
  const remove = useMutation({
    mutationFn: (jobId: string) => pipelineApi.deleteJob(jobId),
    onSuccess: (_, id) => {
      if (shown === id) setShown(null);
      void queryClient.invalidateQueries({ queryKey: keys.jobs });
    },
  });

  const columns: Column<JobInfo>[] = [
    {
      key: "name",
      header: "name",
      render: (job) => (
        <button
          type="button"
          className="text-left font-medium text-accent underline-offset-2 hover:underline"
          data-testid={`job_open_${job.job_id}`}
          onClick={() => setShown(shown === job.job_id ? null : job.job_id)}
        >
          {job.name}
        </button>
      ),
      className: "max-w-xs",
    },
    {
      key: "schedule",
      header: "runs",
      render: (job) =>
        job.schedule_text ? (
          <span className={job.enabled ? "" : "text-muted line-through"}>{job.schedule_text}</span>
        ) : (
          <span className="text-muted">when asked</span>
        ),
    },
    { key: "reads", header: "reads", render: (job) => <span className="font-mono text-xs">{windowText(job)}</span> },
    {
      key: "next",
      header: "next run",
      render: (job) => (job.next_run_at ? formatUtc(job.next_run_at, "minute") : <span className="text-muted">—</span>),
    },
    { key: "last", header: "last run", render: (job) => <Status execution={job.last} /> },
    {
      key: "actions",
      header: "",
      align: "right",
      render: (job) => {
        const busy = job.last?.status === "queued" || job.last?.status === "running";
        return (
          <div className="flex justify-end gap-1">
            <Button
              size="sm"
              variant="primary"
              data-testid={`job_run_${job.job_id}`}
              disabled={busy || (runNow.isPending && runNow.variables === job.job_id)}
              onClick={() => runNow.mutate(job.job_id)}
            >
              {busy ? "Running…" : "Run now"}
            </Button>
            {job.schedule ? (
              <Button
                size="sm"
                variant="ghost"
                data-testid={`job_toggle_${job.job_id}`}
                disabled={toggle.isPending && toggle.variables?.job_id === job.job_id}
                onClick={() => toggle.mutate(job)}
              >
                {job.enabled ? "Pause" : "Resume"}
              </Button>
            ) : null}
            <Link
              to={`/compose?job=${encodeURIComponent(job.job_id)}`}
              className="inline-flex h-7 items-center rounded-md px-2.5 text-xs text-dim hover:bg-raised hover:text-ink"
              data-testid={`job_edit_${job.job_id}`}
            >
              Edit
            </Link>
            {admin ? (
              <Button
                size="sm"
                variant="ghost"
                data-testid={`job_delete_${job.job_id}`}
                aria-label={`Delete ${job.name}`}
                disabled={remove.isPending && remove.variables === job.job_id}
                onClick={() => {
                  if (window.confirm(`Delete the job “${job.name}” and its history? The runs it made are kept.`)) {
                    remove.mutate(job.job_id);
                  }
                }}
              >
                Delete
              </Button>
            ) : null}
          </div>
        );
      },
    },
  ];

  const failed = runNow.error ?? toggle.error ?? remove.error;
  return (
    <>
      <PageHeader
        title="Jobs"
        description="Saved chains that read their data sources again — when you run them, or on a schedule. Every time is UTC; each run is saved under Runs."
      />
      <QueryState query={jobs} label="Loading jobs…">
        {(list) =>
          list.length === 0 ? (
            <Notice tone="info">
              No jobs yet. <Link to="/compose" className="underline underline-offset-2">Compose</Link> a chain that pulls
              from a data source, then choose <strong>Save as a job</strong> on its last step.
            </Notice>
          ) : (
            <div className="space-y-4">
              {failed ? <Notice tone="error">{describeError(failed)}</Notice> : null}
              <DataTable columns={columns} rows={list} rowKey={(job) => job.job_id} pageSize={50} />
              {list
                .filter((job) => job.job_id === shown)
                .map((job) => (
                  <History key={job.job_id} job={job} />
                ))}
            </div>
          )
        }
      </QueryState>
    </>
  );
}
