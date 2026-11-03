/**
 * One run's results, read back as a set (ADR-0045).
 *
 * The Compose page's result tiles open this page in a new tab, each scrolled to its own list.
 * Before the store recorded which run wrote a row, a tile could only have opened the
 * organization's whole store — so "2 alerts" led to a queue of every alert ever raised, and
 * the number on the tile and the rows on the page disagreed. Every list here is filtered by
 * `run_id` on the server, so the counts in the section titles are the tile's counts.
 *
 * Alerts link into Triage (`/?alert=<id>`) rather than re-implementing its inspector: the
 * ranked queue is where an alert is investigated, and this page is only the way in.
 *
 * Above the lists sits the run's own record (ADR-0046): its name (renamable by anyone in the
 * organization), who ran it and when, the chain it ran, **Open in Compose** to run that chain
 * again, and — for admins — delete, which takes every row below with it.
 */
import { useMutation, useQueryClient, type UseQueryResult } from "@tanstack/react-query";
import * as React from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";

import { QueryState } from "@/components/QueryState";
import { Button, TextInput } from "@/components/ui/controls";
import { Card, Notice, PageHeader, Section, Spinner, Truncated } from "@/components/ui/surfaces";
import { DataTable, SeverityBadge, type Column } from "@/components/ui/table";
import { pipelineApi, type Capped } from "@/lib/api/client";
import { ApiError, describeError } from "@/lib/api/http";
import type { DetectionResult, NormalizedEvent, PrioritizedAlert, SavedRunInfo } from "@/lib/api/types";
import { isOrgAdmin, useAuth } from "@/lib/auth";
import { explanationOf } from "@/lib/evidence";
import { confirmDeleteRun, useDeleteRun } from "@/lib/runs";
import { formatNumber, formatUtc } from "@/lib/format";
import {
  keys,
  useRunAlerts,
  useRunDetections,
  useRunEvents,
  useRunMalformed,
  useSavedRun,
  type RunList,
} from "@/lib/queries";
import { malformedColumns } from "@/pages/DataHealthPage";

const PAGE_SIZE = 50;

const muted = <span className="text-muted">—</span>;

const eventColumns: Column<NormalizedEvent>[] = [
  { key: "timestamp", header: "timestamp", render: (e) => formatUtc(e.timestamp) },
  { key: "asset", header: "asset", render: (e) => <span className="font-mono text-xs">{e.asset_id}</span> },
  { key: "lane", header: "lane", render: (e) => e.source_type },
  { key: "metric", header: "metric", render: (e) => e.metric ?? muted },
  { key: "value", header: "value", align: "right", render: (e) => (e.value == null ? muted : formatNumber(e.value, 2)) },
  { key: "severity", header: "severity", render: (e) => e.severity },
  {
    key: "message",
    header: "message",
    render: (e) => e.message ?? <span className="font-mono text-xs break-all">{e.raw}</span>,
    className: "max-w-md",
  },
];

const detectionColumns: Column<DetectionResult>[] = [
  { key: "finding", header: "finding", render: (d) => explanationOf(d) ?? d.method },
  { key: "asset", header: "asset", render: (d) => <span className="font-mono text-xs">{d.asset_id}</span> },
  { key: "lane", header: "lane" },
  { key: "method", header: "method" },
  { key: "score", header: "score", align: "right", render: (d) => formatNumber(d.score, 4) },
  { key: "window_start", header: "window start", render: (d) => formatUtc(d.window_start) },
];

const alertColumns: Column<PrioritizedAlert>[] = [
  { key: "severity", header: "severity", render: (a) => <SeverityBadge severity={a.severity} /> },
  { key: "priority", header: "priority", align: "right", render: (a) => formatNumber(a.priority_score, 1) },
  { key: "asset", header: "asset", render: (a) => <span className="font-mono text-xs">{a.asset_id}</span> },
  { key: "threat_class", header: "threat class", render: (a) => a.threat_class },
  { key: "window_start", header: "window start", render: (a) => formatUtc(a.window_start) },
  {
    key: "open",
    header: "",
    render: (a) => (
      <Link
        to={`/?alert=${encodeURIComponent(a.alert_id ?? "")}`}
        className="text-accent underline-offset-2 hover:underline"
        data-testid={`run-alert-link-${a.alert_id ?? ""}`}
      >
        Inspect in triage
      </Link>
    ),
  },
];

function RunSection<T>({
  list,
  title,
  query,
  columns,
  rowKey,
  order,
}: {
  readonly list: RunList;
  readonly title: string;
  readonly query: UseQueryResult<Capped<T>, Error>;
  readonly columns: readonly Column<T>[];
  readonly rowKey: (row: T, index: number) => string;
  readonly order: string;
}) {
  return (
    // The anchor a tile links to (`/runs/<id>#alerts`); scroll-margin keeps the heading clear
    // of the viewport's top edge when the browser jumps to it.
    <div id={list} className="scroll-mt-6" data-testid={`run-section-${list}`}>
      <Section title={query.data ? `${title} (${query.data.rows.length.toLocaleString("en-US")})` : title}>
        <QueryState query={query} label={`Loading ${list}…`}>
          {(page) =>
            page.rows.length === 0 ? (
              <p className="caption">This run produced no {list}.</p>
            ) : (
              <>
                <Truncated page={page} noun={list} order={order} />
                <DataTable columns={columns} rows={page.rows} rowKey={rowKey} pageSize={PAGE_SIZE} />
              </>
            )
          }
        </QueryState>
      </Section>
    </div>
  );
}

/** The chain a run ran, in one line per stage — read from its saved spec, loosely. */
function chainOf(spec: Record<string, unknown>): { label: string; value: string }[] {
  const names = (value: unknown, key: string): string =>
    Array.isArray(value)
      ? value
          .map((item) => (typeof item === "object" && item !== null ? String((item as Record<string, unknown>)[key] ?? "") : ""))
          .filter(Boolean)
          .join(", ")
      : "";
  const engine = spec["alert_engine"];
  return [
    { label: "Normalizers", value: names(spec["sources"], "normalizer") },
    { label: "Detectors", value: names(spec["detectors"], "name") },
    {
      label: "Alert engine",
      value: typeof engine === "object" && engine !== null ? String((engine as Record<string, unknown>)["name"] ?? "") : "",
    },
    { label: "Sinks", value: names(spec["sinks"], "name") },
  ].filter((row) => row.value !== "");
}

function RunRecord({ run }: { readonly run: SavedRunInfo }) {
  const client = useQueryClient();
  const navigate = useNavigate();
  const auth = useAuth();
  const [draft, setDraft] = React.useState<string | null>(null);
  const refresh = () => void client.invalidateQueries({ queryKey: keys.savedRuns });
  const rename = useMutation({
    mutationFn: (name: string) => pipelineApi.renameRun(run.run_id, name),
    onSuccess: () => {
      setDraft(null);
      refresh();
    },
  });
  const remove = useDeleteRun(() => navigate("/runs"));

  return (
    <Card className="space-y-4 p-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        {draft === null ? (
          <div className="min-w-0">
            <h2 className="text-base font-semibold text-ink" data-testid="run_name">
              {run.name}
            </h2>
            <p className="caption mt-0.5">
              Ran {formatUtc(run.run_at, "minute")} by {run.created_by ?? "the CLI"} · read{" "}
              <span className="font-mono text-xs">{run.sources.map((source) => source.origin).join(", ")}</span>
            </p>
          </div>
        ) : (
          <form
            className="flex min-w-0 flex-1 items-center gap-2"
            onSubmit={(event) => {
              event.preventDefault();
              if (draft.trim()) rename.mutate(draft.trim());
            }}
          >
            <TextInput
              aria-label="Run name"
              data-testid="run_rename_input"
              value={draft}
              maxLength={200}
              autoFocus
              onChange={(event) => setDraft(event.target.value)}
              className="min-w-0 flex-1"
            />
            <Button type="submit" variant="primary" size="sm" disabled={!draft.trim() || rename.isPending} data-testid="run_rename_save">
              Save
            </Button>
            <Button type="button" variant="ghost" size="sm" onClick={() => setDraft(null)}>
              Cancel
            </Button>
          </form>
        )}
        <div className="flex flex-wrap items-center gap-2">
          {draft === null ? (
            <Button size="sm" data-testid="run_rename" onClick={() => setDraft(run.name)}>
              Rename
            </Button>
          ) : null}
          <Link
            to={`/compose?from=${encodeURIComponent(run.run_id)}`}
            data-testid="run_reopen"
            className="inline-flex h-7 items-center rounded-md border border-accent bg-accent px-2.5 text-xs font-medium text-accent-ink hover:bg-accent/90"
          >
            Open in Compose
          </Link>
          {isOrgAdmin(auth.data) ? (
            <Button
              size="sm"
              variant="ghost"
              data-testid="run_delete"
              disabled={remove.isPending}
              onClick={() => {
                if (confirmDeleteRun(run)) remove.mutate(run.run_id);
              }}
            >
              Delete
            </Button>
          ) : null}
        </div>
      </div>
      {rename.isError ? <Notice tone="error">Could not rename: {describeError(rename.error)}</Notice> : null}
      {remove.isError ? <Notice tone="error">Could not delete: {describeError(remove.error)}</Notice> : null}
      <dl className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        {chainOf(run.spec).map((row) => (
          <div key={row.label}>
            <dt className="field-label">{row.label}</dt>
            <dd className="text-sm">{row.value}</dd>
          </div>
        ))}
      </dl>
      {run.warnings.map((warning, index) => (
        <Notice key={index} tone="warning">
          {warning}
        </Notice>
      ))}
    </Card>
  );
}

/** The record, if the run was saved — runs from before ADR-0046 have rows but no record. */
function RunHeader({ runId }: { readonly runId: string }) {
  const saved = useSavedRun(runId);
  if (saved.isPending) return <Spinner label="Loading the run…" />;
  if (saved.isError) {
    return saved.error instanceof ApiError && saved.error.status === 404 ? (
      <p className="caption">This run has no saved record — it ran before runs were saved.</p>
    ) : (
      <Notice tone="error">Could not load the run: {describeError(saved.error)}</Notice>
    );
  }
  return <RunRecord run={saved.data} />;
}

export function RunPage() {
  const { runId = "" } = useParams();
  const { hash } = useLocation();
  const events = useRunEvents(runId);
  const malformed = useRunMalformed(runId);
  const detections = useRunDetections(runId);
  const alerts = useRunAlerts(runId);

  // The browser's own jump to `#alerts` happens on load, before any list has arrived, so the
  // anchor it was aiming at sits at the top of an empty page. Re-aim once everything above
  // the target has rendered — the sections above it are what decide where it lands.
  const settled = [events, malformed, detections, alerts].every((query) => !query.isPending);
  React.useEffect(() => {
    if (!settled || !hash) return;
    document.getElementById(decodeURIComponent(hash.slice(1)))?.scrollIntoView({ block: "start" });
  }, [settled, hash]);

  return (
    <>
      <PageHeader
        title="Run results"
        description={
          <>
            Everything one pipeline run stored, and nothing else. Run{" "}
            <span className="font-mono text-xs">{runId}</span>. Every timestamp is UTC.
          </>
        }
      />
      <div className="space-y-8">
        <RunHeader runId={runId} />
        {settled &&
        [events, malformed, detections, alerts].every((query) => query.data?.rows.length === 0) ? (
          <Notice tone="info">
            Nothing is stored under this run in your organization — it may belong to another
            organization, or have been written before runs were recorded.
          </Notice>
        ) : null}
        <RunSection
          list="events"
          title="Events"
          query={events}
          columns={eventColumns}
          rowKey={(row, index) => row.event_id ?? String(index)}
          order="this run's most recent events only"
        />
        <RunSection
          list="malformed"
          title="Malformed inputs"
          query={malformed}
          columns={malformedColumns}
          rowKey={(row, index) => row.record_id ?? String(index)}
          order="this run's most recent malformed records only"
        />
        <RunSection
          list="detections"
          title="Detections"
          query={detections}
          columns={detectionColumns}
          rowKey={(row, index) => row.detection_id ?? String(index)}
          order="this run's most recent detections only"
        />
        <RunSection
          list="alerts"
          title="Alerts"
          query={alerts}
          columns={alertColumns}
          rowKey={(row, index) => row.alert_id ?? String(index)}
          order="this run's highest-priority alerts only"
        />
      </div>
    </>
  );
}
