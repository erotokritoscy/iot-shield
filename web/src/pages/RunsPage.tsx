/**
 * Every pipeline run this organization has made, newest first (ADR-0046).
 *
 * Before this page a run could be found again only from the result tiles of the tab that ran
 * it; close the tab and the run was reachable only by remembering its id. Each row opens the
 * Run page — the run's record above its four lists — from which the chain can be reopened on
 * Compose. Runs from the CLI and the seed are listed too: they are saved by the same sink.
 */
import { Link } from "react-router-dom";

import { QueryState } from "@/components/QueryState";
import { Button } from "@/components/ui/controls";
import { Notice, PageHeader, Truncated } from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { describeError } from "@/lib/api/http";
import type { SavedRunInfo } from "@/lib/api/types";
import { isOrgAdmin, useAuth } from "@/lib/auth";
import { formatUtc } from "@/lib/format";
import { useSavedRuns } from "@/lib/queries";
import { confirmDeleteRun, useDeleteRun } from "@/lib/runs";

const count = (value: number) => <span className="tabular">{value.toLocaleString("en-US")}</span>;

const columns: Column<SavedRunInfo>[] = [
  {
    key: "name",
    header: "name",
    render: (run) => (
      <Link
        to={`/runs/${encodeURIComponent(run.run_id)}`}
        className="font-medium text-accent underline-offset-2 hover:underline"
        data-testid={`runs_open_${run.run_id}`}
      >
        {run.name}
      </Link>
    ),
    className: "max-w-sm",
  },
  { key: "run_at", header: "ran at", render: (run) => formatUtc(run.run_at, "minute") },
  { key: "by", header: "by", render: (run) => run.created_by ?? <span className="text-muted">CLI</span> },
  {
    key: "sources",
    header: "sources",
    render: (run) => (
      <span className="font-mono text-xs">{run.sources.map((source) => source.origin).join(", ")}</span>
    ),
    className: "max-w-xs",
  },
  { key: "events", header: "events", align: "right", render: (run) => count(run.events) },
  { key: "malformed", header: "malformed", align: "right", render: (run) => count(run.malformed) },
  { key: "detections", header: "detections", align: "right", render: (run) => count(run.detections) },
  { key: "alerts", header: "alerts", align: "right", render: (run) => count(run.alerts) },
];

export function RunsPage() {
  const runs = useSavedRuns();
  const auth = useAuth();
  const remove = useDeleteRun();
  // Members may rename but not delete (ADR-0046), so the column exists only for an admin: a
  // button that always answers 403 is a trap, not a feature.
  const shown: Column<SavedRunInfo>[] = isOrgAdmin(auth.data)
    ? [
        ...columns,
        {
          key: "actions",
          header: "",
          align: "right",
          render: (run) => (
            <Button
              size="sm"
              variant="ghost"
              data-testid={`runs_delete_${run.run_id}`}
              aria-label={`Delete ${run.name}`}
              // Only the row being deleted waits; the others stay usable.
              disabled={remove.isPending && remove.variables === run.run_id}
              onClick={() => {
                if (confirmDeleteRun(run)) remove.mutate(run.run_id);
              }}
            >
              Delete
            </Button>
          ),
        },
      ]
    : columns;
  return (
    <>
      <PageHeader
        title="Runs"
        description="Every pipeline run in this organization, newest first. Open one to see its results or run its chain again. Every timestamp is UTC."
      />
      <QueryState query={runs} label="Loading runs…">
        {(page) =>
          page.rows.length === 0 ? (
            <Notice tone="info">
              No runs yet. <Link to="/compose" className="underline underline-offset-2">Compose and run</Link> a
              pipeline — every run is saved here.
            </Notice>
          ) : (
            <div className="space-y-4">
              <Truncated page={page} noun="runs" order="the most recent runs only" />
              {remove.isError ? (
                <Notice tone="error">Could not delete: {describeError(remove.error)}</Notice>
              ) : null}
              <DataTable columns={shown} rows={page.rows} rowKey={(run) => run.run_id} pageSize={50} />
            </div>
          )
        }
      </QueryState>
    </>
  );
}
