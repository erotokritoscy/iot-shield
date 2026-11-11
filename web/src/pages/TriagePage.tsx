import * as React from "react";

import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Button, Field, Select } from "@/components/ui/controls";
import { InfoLines, InfoTip } from "@/components/ui/info";
import {
  Collapsible,
  JsonBlock,
  Notice,
  PageHeader,
  Section,
} from "@/components/ui/surfaces";
import { DataTable, DynamicTable, SeverityBadge, StatTile, type Column } from "@/components/ui/table";
import { severityBar } from "@/charts/specs";
import { actionSteps, explanationOf, mitreOf } from "@/lib/evidence";
import { formatNumber, formatUtc } from "@/lib/format";
import type { AlertFacets, AlertSeverity, DetectionResult, PrioritizedAlert, SavedRunInfo } from "@/lib/api/types";
import { SEVERITY_ORDER, SUMMARY_KEYS } from "@/lib/api/types";
import {
  ALERT_PAGE_SIZE,
  useAlert,
  useAlertDetections,
  useAlertFacets,
  useAlertPage,
  useSavedRuns,
  useSummary,
} from "@/lib/queries";
import { isOpen, useTimeRange, type TimeRange } from "@/lib/timeRange";
import { useUrlChoice } from "@/lib/urlChoice";
import { TimeRangeFilter } from "@/components/TimeRangeFilter";
import { bandRange, COUNT_HELP, SEVERITY_GUIDE, TRIAGE_HELP } from "@/pages/triage/help";
import { CHART_SEVERITIES, SeverityChartFilter } from "@/pages/triage/SeverityChartFilter";

/**
 * What each severity means and what to do about it. Collapsed by default: an analyst needs
 * it the first few times, and the queue is what the page is for after that.
 */
function SeverityGuide() {
  return (
    <Collapsible label="What the severities mean">
      <div className="overflow-x-auto">
        <table className="w-full text-sm" data-testid="severity_guide">
          <thead>
            <tr className="border-b border-line text-left text-2xs uppercase tracking-wide text-muted">
              <th className="py-2 pr-4 font-semibold">Severity</th>
              <th className="py-2 pr-4 font-semibold">Priority</th>
              <th className="py-2 pr-4 font-semibold">What it means</th>
              <th className="py-2 font-semibold">What to do</th>
            </tr>
          </thead>
          <tbody>
            {SEVERITY_GUIDE.map((band, index) => (
              <tr key={band.severity} className="border-b border-line align-top last:border-0">
                <td className="py-2 pr-4">
                  <SeverityBadge severity={band.severity} />
                </td>
                <td className="tabular whitespace-nowrap py-2 pr-4">{bandRange(index)}</td>
                <td className="py-2 pr-4 text-dim">{band.meaning}</td>
                <td className="py-2">{band.response}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="caption mt-2">
        The priority (0–100) is computed by the alert engine from how far the detections went past their
        thresholds, how many lanes agree, how many detections there are and how critical the device is; the
        severity is only its band.
      </p>
    </Collapsible>
  );
}

/** `1 alert`, `11,377 alerts`. */
const alertCount = (count: number) => `${count.toLocaleString("en-US")} alert${count === 1 ? "" : "s"}`;

/** A section title with its (i): one string, or several lines (the first in full ink). */
function HelpHeading({ label, help }: { readonly label: string; readonly help: string | readonly string[] }) {
  return (
    <span className="inline-flex items-center gap-1">
      {label}
      <InfoTip label={label}>{typeof help === "string" ? help : <InfoLines lines={help} />}</InfoTip>
    </span>
  );
}

/** `HIGH 62.0 — pump-02 @ 2026-07-07 12:00`, UTC — the label format the old picker used. */
export function alertLabel(alert: PrioritizedAlert): string {
  return (
    `${alert.severity.toUpperCase()} ${alert.priority_score.toFixed(1)} — ${alert.asset_id}` +
    ` @ ${formatUtc(alert.window_start, "minute")}`
  );
}

const queueColumns: Column<PrioritizedAlert>[] = [
  { key: "severity", header: "severity", render: (a) => <SeverityBadge severity={a.severity} /> },
  { key: "priority", header: "priority", align: "right", render: (a) => formatNumber(a.priority_score, 1) },
  { key: "asset", header: "asset", render: (a) => <span className="font-mono text-xs">{a.asset_id}</span> },
  { key: "window_start", header: "window start", render: (a) => formatUtc(a.window_start) },
  { key: "threat_class", header: "threat class", render: (a) => a.threat_class },
  { key: "status", header: "status", render: (a) => a.status },
];

const evidenceColumns: Column<DetectionResult>[] = [
  { key: "method", header: "method" },
  { key: "lane", header: "lane" },
  { key: "score", header: "score", align: "right", render: (d) => formatNumber(d.score, 4) },
  { key: "threshold", header: "threshold", align: "right", render: (d) => formatNumber(d.threshold, 4) },
  { key: "window_start", header: "window start", render: (d) => formatUtc(d.window_start) },
  { key: "window_end", header: "window end", render: (d) => formatUtc(d.window_end) },
  // The same column AssetsPage carries, and for the same reason: `method` stopped naming a
  // finding at ADR-0032 D3, so on a camera alert raising four rule detections this table rendered four rows
  // reading `rules:flow` that differed only in score. Its absence here was a half-applied
  // fix — Streamlit's evidence table gained the column in the same commit, and ADR-0031 D7
  // says the two dashboards may disagree about nothing that matters (ADR-0033).
  {
    key: "finding",
    header: "finding",
    render: (d) => explanationOf(d) ?? <span className="text-muted">—</span>,
  },
];

/**
 * What one piece of evidence is *called*.
 *
 * `method` used to be a good name by accident: one detector meant one kind of finding, so
 * naming the detector named what happened. The rule detector ends that deliberately — the
 * Detector port gives an adapter one lane and the port suite pins `detection.method` to it,
 * so six rules on the flow lane all report `rules:flow` and the firing rule rides in
 * `Enrichment.origin` instead (ADR-0032 D3). Labelling by method therefore produced three
 * identical "rules:flow — why it fired" headings on one alert, for an inbound SSH session,
 * an SMB conversation and a bulk upload.
 *
 * The fix is not to special-case `rules:*` here — the next detector that fires several ways
 * would need the same patch. It is to label a detection by the sentence *every* detection is
 * required to carry (ADR-0032 D7). `method` stays alongside as the secondary fact it now is:
 * which detector, not which finding.
 */
function EvidenceLabel({ detection }: { readonly detection: DetectionResult }) {
  const explanation = explanationOf(detection);
  return (
    <span className="flex min-w-0 flex-1 items-baseline gap-2">
      <span className="truncate text-ink">{explanation ?? "why it fired"}</span>
      <span className="shrink-0 font-mono text-2xs text-muted">{detection.method}</span>
    </span>
  );
}

function AlertInspector({
  alerts,
  chosenId,
  onChoose,
}: {
  /** The page of the queue on screen. */
  readonly alerts: readonly PrioritizedAlert[];
  readonly chosenId: string | null;
  readonly onChoose: (id: string) => void;
}) {
  // Preselection is a *derivation*, never a write back into state — so an operator's explicit
  // choice is never overwritten by a refetch, and a choice that no longer exists falls back to
  // the top of the page rather than blanking the panel (§4.1, "widget index= honored on first
  // render"). The choice lives in the URL, so it can name an alert on another page of the
  // queue (a shared link, or paging on after choosing): that one is read by id.
  const byId = React.useMemo(
    () => new Map(alerts.map((alert) => [alert.alert_id ?? "", alert])),
    [alerts],
  );
  const onPage = chosenId === null ? undefined : byId.get(chosenId);
  const fetched = useAlert(chosenId !== null && onPage === undefined ? chosenId : null);
  const waiting = chosenId !== null && onPage === undefined && fetched.isPending && fetched.fetchStatus !== "idle";
  const selected = onPage ?? fetched.data ?? (waiting ? undefined : alerts[0]);
  // The chosen alert is offered even when it is not on this page: a Select whose value is not
  // among its options would display the first option while showing another (ADR-0036 D2).
  const options = alerts.map((alert) => alert.alert_id ?? "");
  const selectedId = selected?.alert_id ?? "";
  if (selectedId && !byId.has(selectedId)) options.unshift(selectedId);
  const detections = useAlertDetections(selected?.alert_id);

  if (selected === undefined) return null;
  const components = selected.score_components ?? {};
  const steps = actionSteps(selected.recommended_action);
  // Read off the detections already fetched for the evidence table below, so the techniques
  // and the payload they came from cannot disagree. Empty while that query is in flight,
  // which is the same thing "no detection claims a technique" renders as — correct in both
  // cases, since the block only appears when there is something to show.
  const techniques = mitreOf(detections.data ?? []);

  return (
    <Section title={<HelpHeading label="Alert inspector" help={TRIAGE_HELP.inspector} />}>
      <Field label="Inspect alert" htmlFor="triage_alert" info={TRIAGE_HELP.inspect}>
        <Select
          id="triage_alert"
          value={selectedId}
          onChange={(event) => onChoose(event.target.value)}
          options={options}
          labelFor={(id) => {
            const alert = id === selectedId ? selected : byId.get(id);
            return alert ? alertLabel(alert) : id;
          }}
        />
      </Field>

      <dl className="space-y-2 text-sm">
        <div>
          <dt className="flex items-center gap-1 text-2xs font-semibold uppercase tracking-wide text-muted">
            Rationale <InfoTip label="Rationale">{TRIAGE_HELP.rationale}</InfoTip>
          </dt>
          <dd className="text-ink">{selected.rationale}</dd>
        </div>
        {/* Only when present: an engine that recommends nothing must not render an empty
            heading that reads as "no action needed". */}
        {steps.length > 0 ? (
          <div>
            <dt className="flex items-center gap-1 text-2xs font-semibold uppercase tracking-wide text-muted">
              Recommended action <InfoTip label="Recommended action">{TRIAGE_HELP.actions}</InfoTip>
            </dt>
            {/* A list, because the field IS one: up to six steps unioned from every rule
                that fired (ADR-0032 D5), which an analyst works through one at a time. The
                contract carries them joined into one string, so `actionSteps` recovers what
                `Enrichment.action_text` joined. A single-step fallback renders as a
                one-item list, which is the same sentence with a bullet in front of it. */}
            <dd>
              <ul
                aria-label="Recommended action steps"
                className="list-disc space-y-1 pl-4 text-ink marker:text-muted"
              >
                {steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}
        {techniques.length > 0 ? (
          <div>
            <dt className="flex items-center gap-1 text-2xs font-semibold uppercase tracking-wide text-muted">
              MITRE ATT&CK <InfoTip label="MITRE ATT&CK">{TRIAGE_HELP.mitre}</InfoTip>
            </dt>
            {/* `threat_class` already carries the winning technique, but only that one, and
                without its tactic. An alert whose evidence spans four rules implicates four
                techniques; they existed only inside the raw JSON dump below. */}
            <dd>
              <ul
                aria-label="MITRE ATT&CK techniques"
                className="flex flex-wrap gap-1.5 pt-0.5"
              >
                {techniques.map((technique) => (
                  <li
                    key={technique.technique_id}
                    className="rounded border border-line bg-raised px-1.5 py-0.5 text-2xs text-dim"
                  >
                    {technique.tactic} — {technique.technique}{" "}
                    <span className="font-mono text-muted">({technique.technique_id})</span>
                  </li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}
      </dl>

      <div>
        <p className="mb-2 flex items-center gap-1 text-xs font-medium text-dim">
          Score components <span className="text-muted">(the explainability receipt)</span>
          <InfoTip label="Score components">
            <InfoLines lines={TRIAGE_HELP.components} />
          </InfoTip>
        </p>
        {Object.keys(components).length > 0 ? (
          <DynamicTable rows={[components]} />
        ) : (
          <p className="caption">This engine recorded no score components.</p>
        )}
      </div>

      <div>
        <p className="mb-2 flex items-center gap-1 text-xs font-medium text-dim">
          Evidence — the detections behind this alert
          <InfoTip label="Evidence">
            <InfoLines lines={TRIAGE_HELP.evidence} />
          </InfoTip>
        </p>
        <QueryState query={detections} label="Loading evidence…">
          {(rows) => (
            <>
              <DataTable
                columns={evidenceColumns}
                rows={rows}
                rowKey={(row, index) => row.detection_id ?? String(index)}
                empty={<p className="caption">No detections are linked to this alert.</p>}
              />
              {/* The per-detection evidence payload (top peers, ports, matched messages, …)
                  is what makes a finding actionable — "cam-07 contacted 41 destinations"
                  rather than a bare score. It reached the API but nothing rendered it. */}
              <div className="mt-3 space-y-2">
                {rows
                  .filter((row) => row.evidence && Object.keys(row.evidence).length > 0)
                  .map((row, index) => (
                    <Collapsible
                      key={row.detection_id ?? String(index)}
                      label={<EvidenceLabel detection={row} />}
                    >
                      <JsonBlock value={row.evidence} />
                    </Collapsible>
                  ))}
              </div>
            </>
          )}
        </QueryState>
      </div>
    </Section>
  );
}

/** `Night shift check — 2026-07-30 12:06`, UTC: a run's name alone need not be unique. */
export function runLabel(run: SavedRunInfo): string {
  return `${run.name} — ${formatUtc(run.run_at, "minute")}`;
}

function RankedQueue({
  rows,
  matched,
  facets,
  page,
  onPage,
  severity,
  onSeverity,
  asset,
  onAsset,
  chosenId,
  onChoose,
  runs,
  runId,
  onRun,
}: {
  /** One page of the queue, already filtered and ranked by the server. */
  readonly rows: readonly PrioritizedAlert[];
  /** How many alerts the filters match in all; null if the server did not say. */
  readonly matched: number | null;
  readonly facets: AlertFacets | undefined;
  readonly page: number;
  readonly onPage: (page: number) => void;
  readonly severity: AlertSeverity | "";
  readonly onSeverity: (severity: AlertSeverity | "") => void;
  readonly asset: string;
  readonly onAsset: (asset: string) => void;
  readonly chosenId: string | null;
  readonly onChoose: (id: string) => void;
  readonly runs: readonly SavedRunInfo[];
  readonly runId: string | null;
  readonly onRun: (id: string) => void;
}) {
  // A run id from the URL that the list does not hold (deleted, or older than the list's cap)
  // is still offered, by id: a Select whose value is not among its options would display the
  // first option while filtering by another — the defect ADR-0036 D2 names.
  const runsById = new Map(runs.map((run) => [run.run_id, run]));
  const runOptions = ["", ...runs.map((run) => run.run_id)];
  if (runId && !runsById.has(runId)) runOptions.push(runId);

  // The whole queue's assets, not this page's: a dropdown built from fifty rows would offer
  // only the devices that happen to rank on the page being shown. Same rule for the chosen one.
  const assets = [...(facets?.assets ?? [])];
  if (asset && !assets.includes(asset)) assets.unshift(asset);
  const total = facets?.total ?? null;
  const counts = facets?.by_severity ?? {};

  const pages = matched === null ? null : Math.max(1, Math.ceil(matched / ALERT_PAGE_SIZE));
  const filteredBy = severity !== "" || asset !== "";

  return (
    <Section title={<HelpHeading label="Ranked queue" help={TRIAGE_HELP.queue} />}>
      {/* Every filter is asked of the server, so the queue can be paged through end to end.
          Sorting is deliberately not offered: the order IS the product's output (H3), and a
          client-side re-sort would quietly replace a ranking with a preference. */}
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Severity" htmlFor="triage_severity" info={TRIAGE_HELP.severity}>
          <Select
            id="triage_severity"
            value={severity}
            onChange={(event) => onSeverity(event.target.value as AlertSeverity | "")}
            options={["", ...SEVERITY_ORDER]}
            labelFor={(value) =>
              value === "" ? "any" : `${value} (${(counts[value] ?? 0).toLocaleString("en-US")})`
            }
          />
        </Field>
        <Field label="Asset" htmlFor="triage_asset" info={TRIAGE_HELP.asset}>
          <Select
            id="triage_asset"
            value={asset}
            onChange={(event) => onAsset(event.target.value)}
            options={["", ...assets]}
            labelFor={(value) => (value === "" ? "any" : value)}
          />
        </Field>
        <Field label="Run" htmlFor="triage_run" info={TRIAGE_HELP.run}>
          <Select
            id="triage_run"
            value={runId ?? ""}
            onChange={(event) => onRun(event.target.value)}
            options={runOptions}
            labelFor={(value) => {
              if (value === "") return "any";
              const run = runsById.get(value);
              return run ? runLabel(run) : value;
            }}
          />
        </Field>
        <p className="caption pb-2" data-testid="triage_matched">
          {matched === null
            ? `${rows.length.toLocaleString("en-US")} shown`
            : filteredBy && total !== null
              ? `${matched.toLocaleString("en-US")} of ${alertCount(total)} match`
              : alertCount(matched)}
        </p>
      </div>

      <DataTable
        columns={queueColumns}
        rows={rows}
        rowKey={(row, index) => row.alert_id ?? String(index)}
        selection={{
          key: chosenId,
          onSelect: (row) => onChoose(row.alert_id ?? ""),
          label: (row) => `Inspect ${alertLabel(row)}`,
        }}
        empty={<p className="caption">No alert matches this filter.</p>}
      />
      {pages !== null && pages > 1 ? (
        <div className="flex flex-wrap items-center gap-3" data-testid="triage_pager">
          <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => onPage(0)}>
            First
          </Button>
          <Button size="sm" variant="ghost" disabled={page === 0} onClick={() => onPage(page - 1)}>
            Previous
          </Button>
          <span className="caption tabular">
            Page {(page + 1).toLocaleString("en-US")} of {pages.toLocaleString("en-US")}
          </span>
          <Button
            size="sm"
            variant="ghost"
            disabled={page >= pages - 1}
            onClick={() => onPage(page + 1)}
          >
            Next
          </Button>
          <Button
            size="sm"
            variant="ghost"
            disabled={page >= pages - 1}
            onClick={() => onPage(pages - 1)}
          >
            Last
          </Button>
        </div>
      ) : null}
    </Section>
  );
}

export function TriagePage() {
  const summary = useSummary();
  const [runId, chooseRun] = useUrlChoice("run");
  const [range, setRange] = useTimeRange();
  const ranged = !isOpen(range);
  const [severity, setSeverity] = React.useState<AlertSeverity | "">("");
  const [asset, setAsset] = React.useState("");
  const [page, setPage] = React.useState(0);
  const [chartSeverities, setChartSeverities] = React.useState<AlertSeverity[]>([...CHART_SEVERITIES]);
  const queue = useAlertPage({
    runId,
    range,
    severity: severity === "" ? null : severity,
    assetId: asset === "" ? null : asset,
    page,
  });
  const facets = useAlertFacets(runId, range);
  // Only names the options: if the Pipeline API cannot list runs, the filter offers "any" and
  // the queue still renders.
  const savedRuns = useSavedRuns();
  const palette = useChartPalette();
  const [chosenId, choose] = useUrlChoice("alert");

  // A filter changes which alerts are ranked where, so it goes back to the top of the queue.
  // The asset is cleared with the run and the range, since it may not occur in the new scope.
  const onSeverity = (next: AlertSeverity | "") => {
    setSeverity(next);
    setPage(0);
  };
  const onAsset = (next: string) => {
    setAsset(next);
    setPage(0);
  };
  const onRun = (next: string) => {
    setAsset("");
    setPage(0);
    chooseRun(next);
  };
  const onRange = (next: TimeRange) => {
    setAsset("");
    setPage(0);
    setRange(next);
  };

  // A refresh can shrink the queue under the page being shown; step back to its last page
  // rather than strand the table on one that no longer exists.
  const matched = queue.data?.total ?? null;
  React.useEffect(() => {
    if (matched === null || queue.isPlaceholderData) return;
    const last = Math.max(0, Math.ceil(matched / ALERT_PAGE_SIZE) - 1);
    if (page > last) setPage(last);
  }, [matched, page, queue.isPlaceholderData]);

  const filtered = Boolean(runId) || ranged || severity !== "" || asset !== "";

  return (
    <>
      <PageHeader
        title="Alert triage"
        description={
          <>
            The ranked queue: what to look at first, and why. Every timestamp is UTC.
            {/* Without this, a queue held open for an hour is indistinguishable from a fresh
                one, and the analyst has no way to tell that "no new alerts" means "none
                since I loaded the page". */}
            {queue.dataUpdatedAt > 0 ? (
              <>
                {" "}
                Data as of{" "}
                <span className="tabular">{formatUtc(queue.dataUpdatedAt)}</span> — use
                Refresh data in the sidebar to re-read it.
              </>
            ) : null}
          </>
        }
      />

      <QueryState query={summary} label="Loading counts…">
        {(data) => (
          <div className="mb-8">
            <div className="grid grid-cols-2 gap-3 sm:grid-cols-3 lg:grid-cols-5">
              {/* From the Pipeline API, not `/summary`: runs are saved there (ADR-0046). It
                  lists a capped page, so past the cap the tile says "200+" rather than a
                  number that looks exact; if that API is unreachable it says nothing. */}
              <StatTile
                label="runs"
                value={
                  savedRuns.data
                    ? `${savedRuns.data.rows.length.toLocaleString("en-US")}${savedRuns.data.truncated ? "+" : ""}`
                    : "—"
                }
                info={TRIAGE_HELP.runs}
              />
              {SUMMARY_KEYS.map((key) => (
                <StatTile
                  key={key}
                  label={key.replace(/_/g, " ")}
                  value={data.counts[key] ?? 0}
                  info={COUNT_HELP[key]}
                />
              ))}
            </div>
            {/* `/summary` counts the whole org; under a range, unmarked tiles beside a
                filtered queue would read as the range's totals. */}
            {ranged ? (
              <p className="caption mt-2">Counts are all-time totals; the time range filters the queue below.</p>
            ) : null}
          </div>
        )}
      </QueryState>

      <QueryState query={queue} label="Loading alerts…">
        {({ data: rows }) => {
          // With any filter set, an empty queue is an answer about that filter — keep the
          // filters on screen so they can be changed back, rather than the "nothing has run"
          // guidance.
          if (rows.length === 0 && !filtered) {
            return (
              <Notice tone="info">
                No alerts yet — run the pipeline first (see README: Run the demo pipeline).
              </Notice>
            );
          }
          const spec = facets.data ? severityBar(facets.data.by_severity, palette, chartSeverities) : null;
          // Something to draw before the chart filter, so "nothing drawn" can be told apart
          // from "no alerts" and the filter stays on screen to be changed back.
          const anyAlerts = facets.data ? severityBar(facets.data.by_severity, palette) !== null : false;
          const hidden = CHART_SEVERITIES.filter((s) => !chartSeverities.includes(s));
          return (
            <div className="space-y-8">
              {/* Asked of the server, so it scopes the chart and the queue alike. */}
              <TimeRangeFilter
                range={range}
                onChange={onRange}
                idPrefix="triage_range"
                basis="An alert is in the range when its window STARTS at or after From and before To."
              >
                <SeverityChartFilter
                  value={chartSeverities}
                  onValueChange={setChartSeverities}
                  info={TRIAGE_HELP.chartSeverities}
                />
              </TimeRangeFilter>

              {spec !== null ? (
                <Chart
                  spec={spec}
                  label="Alert count per severity"
                  // Fixed on purpose: a handful of bars, read at a glance — zooming the count
                  // axis would only cut bars off, and the wheel would stop scrolling the page.
                  zoom={false}
                  // Counted by the server over the whole queue in scope (run and range), not
                  // drawn from the page on screen — and not narrowed by the severity and
                  // asset filters, which choose among these bars rather than redraw them.
                  caption={
                    hidden.length === 0
                      ? "Alert count per severity."
                      : `Alert count per severity. Hidden: ${hidden.join(", ")}.`
                  }
                />
              ) : anyAlerts ? (
                <p className="caption" role="status" data-testid="chart_nothing_shown">
                  No alerts in the severities selected for the chart.
                </p>
              ) : null}

              <SeverityGuide />

              <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
                <RankedQueue
                  rows={rows}
                  matched={matched}
                  facets={facets.data}
                  page={page}
                  onPage={setPage}
                  severity={severity}
                  onSeverity={onSeverity}
                  asset={asset}
                  onAsset={onAsset}
                  chosenId={chosenId}
                  onChoose={choose}
                  runs={savedRuns.data?.rows ?? []}
                  runId={runId}
                  onRun={onRun}
                />
                {/* Sticky, and beside the queue rather than below it: an alert is chosen
                    *because* of where it sits relative to its neighbours, so the ranking has
                    to stay on screen while its evidence is read. */}
                <div className="lg:sticky lg:top-6">
                  <AlertInspector alerts={rows} chosenId={chosenId} onChoose={choose} />
                </div>
              </div>
            </div>
          );
        }}
      </QueryState>
    </>
  );
}
