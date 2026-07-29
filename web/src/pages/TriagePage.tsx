import * as React from "react";

import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Field, Select } from "@/components/ui/controls";
import {
  Collapsible,
  JsonBlock,
  Notice,
  PageHeader,
  Section,
  Truncated,
} from "@/components/ui/surfaces";
import { DataTable, DynamicTable, SeverityBadge, StatTile, type Column } from "@/components/ui/table";
import { severityBar } from "@/charts/specs";
import { actionSteps, explanationOf, mitreOf } from "@/lib/evidence";
import { formatNumber, formatUtc } from "@/lib/format";
import type { DetectionResult, PrioritizedAlert } from "@/lib/api/types";
import { SEVERITY_ORDER, SUMMARY_KEYS } from "@/lib/api/types";
import { useAlertDetections, useAlerts, useSummary } from "@/lib/queries";
import { useUrlChoice } from "@/lib/urlChoice";

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
  // finding at ADR-0032 D3, so on the demo_rules camera alert this table rendered four rows
  // reading `rules:flow` that differed only in score. Its absence here was a half-applied
  // fix — Streamlit's evidence table gained the column in the same commit, and ADR-0031 D7
  // says the two dashboards may disagree about nothing that matters (ADR-0033).
  {
    key: "finding",
    header: "finding",
    render: (d) => explanationOf(d) ?? <span className="text-muted">—</span>,
  },
];

const PAGE_SIZE = 50;

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
  readonly alerts: readonly PrioritizedAlert[];
  readonly chosenId: string | null;
  readonly onChoose: (id: string) => void;
}) {
  // Preselection is a *derivation*, never a write back into state — so an operator's explicit
  // choice is never overwritten by a refetch, and a choice that no longer exists falls back to
  // the top of the queue rather than blanking the panel (§4.1, "widget index= honored on first
  // render"). Moving the backing store from useState to the URL does not disturb either
  // property; it only stops the choice evaporating when the page unmounts.
  // Indexed, not scanned. Selection now re-renders the whole route (the choice lives in the
  // URL), and a linear `find` inside `labelFor` made that a 1,000 × 1,000 pass on every row
  // click — measured at 7.5 ms, versus 1.7 ms indexed.
  const byId = React.useMemo(
    () => new Map(alerts.map((alert) => [alert.alert_id ?? "", alert])),
    [alerts],
  );
  const selected = (chosenId === null ? undefined : byId.get(chosenId)) ?? alerts[0];
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
    <Section title="Alert inspector">
      <Field label="Inspect alert" htmlFor="triage_alert">
        <Select
          id="triage_alert"
          value={selected.alert_id ?? ""}
          onChange={(event) => onChoose(event.target.value)}
          options={alerts.map((alert) => alert.alert_id ?? "")}
          labelFor={(id) => {
            const alert = byId.get(id);
            return alert ? alertLabel(alert) : id;
          }}
        />
      </Field>

      <dl className="space-y-2 text-sm">
        <div>
          <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">Rationale</dt>
          <dd className="text-ink">{selected.rationale}</dd>
        </div>
        {/* Only when present: an engine that recommends nothing must not render an empty
            heading that reads as "no action needed". */}
        {steps.length > 0 ? (
          <div>
            <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
              Recommended action
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
            <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
              MITRE ATT&CK
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
        <p className="mb-2 text-xs font-medium text-dim">
          Score components <span className="text-muted">(the explainability receipt)</span>
        </p>
        {Object.keys(components).length > 0 ? (
          <DynamicTable rows={[components]} />
        ) : (
          <p className="caption">This engine recorded no score components.</p>
        )}
      </div>

      <div>
        <p className="mb-2 text-xs font-medium text-dim">
          Evidence — the detections behind this alert
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

function RankedQueue({
  rows,
  chosenId,
  onChoose,
}: {
  readonly rows: readonly PrioritizedAlert[];
  readonly chosenId: string | null;
  readonly onChoose: (id: string) => void;
}) {
  const [severity, setSeverity] = React.useState("");
  const [asset, setAsset] = React.useState("");

  // Unmemoised on purpose: measured at 19 µs for 1,000 alerts across 40 assets, which is 0.1%
  // of a frame. A `useMemo` here would cost more in dependency bookkeeping and reading
  // difficulty than it saves.
  const assets = [...new Set(rows.map((row) => row.asset_id))].sort();
  const filtered = rows.filter(
    (row) =>
      (severity === "" || row.severity === severity) && (asset === "" || row.asset_id === asset),
  );

  return (
    <Section title="Ranked queue">
      {/* Filtering is a view over rows the server already ranked. Sorting is deliberately not
          offered: the order IS the product's output (H3), and a client-side re-sort would
          quietly replace a ranking with a preference. */}
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Severity" htmlFor="triage_severity">
          <Select
            id="triage_severity"
            value={severity}
            onChange={(event) => setSeverity(event.target.value)}
            options={["", ...SEVERITY_ORDER]}
            labelFor={(value) => (value === "" ? "any" : value)}
          />
        </Field>
        <Field label="Asset" htmlFor="triage_asset">
          <Select
            id="triage_asset"
            value={asset}
            onChange={(event) => setAsset(event.target.value)}
            options={["", ...assets]}
            labelFor={(value) => (value === "" ? "any" : value)}
          />
        </Field>
        <p className="caption pb-2">
          {filtered.length.toLocaleString("en-US")} of {rows.length.toLocaleString("en-US")} shown
        </p>
      </div>

      <DataTable
        columns={queueColumns}
        rows={filtered}
        rowKey={(row, index) => row.alert_id ?? String(index)}
        pageSize={PAGE_SIZE}
        selection={{
          key: chosenId,
          onSelect: (row) => onChoose(row.alert_id ?? ""),
          label: (row) => `Inspect ${alertLabel(row)}`,
        }}
        empty={<p className="caption">No alert matches this filter.</p>}
      />
    </Section>
  );
}

export function TriagePage() {
  const summary = useSummary();
  const alerts = useAlerts();
  const palette = useChartPalette();
  const [chosenId, choose] = useUrlChoice("alert");

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
            {alerts.dataUpdatedAt > 0 ? (
              <>
                {" "}
                Data as of{" "}
                <span className="tabular">{formatUtc(alerts.dataUpdatedAt)}</span> — use
                Refresh data in the sidebar to re-read it.
              </>
            ) : null}
          </>
        }
      />

      <QueryState query={summary} label="Loading counts…">
        {(data) => (
          <div className="mb-8 grid grid-cols-2 gap-3 lg:grid-cols-4">
            {SUMMARY_KEYS.map((key) => (
              <StatTile key={key} label={key.replace(/_/g, " ")} value={data.counts[key] ?? 0} />
            ))}
          </div>
        )}
      </QueryState>

      <QueryState query={alerts} label="Loading alerts…">
        {(page) => {
          const { rows, truncated, limit } = page;
          if (rows.length === 0) {
            return (
              <Notice tone="info">
                No alerts yet — run the pipeline first (see README: Run the demo pipeline).
              </Notice>
            );
          }
          const spec = severityBar(rows, palette);
          return (
            <div className="space-y-8">
              <Truncated
                page={page}
                noun="alerts"
                total={summary.data?.counts["alerts"]}
                order="the highest-priority slice of the queue, not all of it"
              />

              {spec === null ? null : (
                <Chart
                  spec={spec}
                  label="Alert count per severity"
                  caption={
                    // The chart is drawn from whatever the cap returned. Naming that is the
                    // difference between "count per severity" and "count per severity among
                    // the highest-priority 1,000" — two different claims, and only one of them
                    // is true here.
                    truncated
                      ? `Alert count per severity, among the ${limit.toLocaleString("en-US")} highest-priority alerts.`
                      : "Alert count per severity."
                  }
                />
              )}

              <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_22rem] lg:items-start">
                <RankedQueue rows={rows} chosenId={chosenId} onChoose={choose} />
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
