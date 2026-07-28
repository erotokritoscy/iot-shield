import * as React from "react";

import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Field, Select } from "@/components/ui/controls";
import { Collapsible, JsonBlock, Notice, PageHeader, Section } from "@/components/ui/surfaces";
import { DataTable, DynamicTable, SeverityBadge, StatTile, type Column } from "@/components/ui/table";
import { severityBar } from "@/charts/specs";
import { formatNumber, formatUtc } from "@/lib/format";
import type { DetectionResult, PrioritizedAlert } from "@/lib/api/types";
import { SUMMARY_KEYS } from "@/lib/api/types";
import { useAlertDetections, useAlerts, useSummary } from "@/lib/queries";

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
];

function AlertInspector({ alerts }: { readonly alerts: readonly PrioritizedAlert[] }) {
  // Preselection lives in initial state only — an operator's explicit choice is never
  // overwritten by a refetch, and a choice that no longer exists falls back to the top of
  // the queue rather than blanking the panel (§4.1, "widget index= honored on first render").
  const [chosenId, setChosenId] = React.useState<string | null>(null);
  const selected = alerts.find((alert) => alert.alert_id === chosenId) ?? alerts[0];
  const detections = useAlertDetections(selected?.alert_id);

  if (selected === undefined) return null;
  const components = selected.score_components ?? {};

  return (
    <Section title="Alert inspector">
      <Field label="Inspect alert">
        <Select
          value={selected.alert_id ?? ""}
          onChange={(event) => setChosenId(event.target.value)}
          options={alerts.map((alert) => alert.alert_id ?? "")}
          labelFor={(id) => {
            const alert = alerts.find((item) => item.alert_id === id);
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
        {selected.recommended_action ? (
          <div>
            <dt className="text-2xs font-semibold uppercase tracking-wide text-muted">
              Recommended action
            </dt>
            <dd className="text-ink">{selected.recommended_action}</dd>
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
                      label={`${row.method} — why it fired`}
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

export function TriagePage() {
  const summary = useSummary();
  const alerts = useAlerts();
  const palette = useChartPalette();

  return (
    <>
      <PageHeader
        title="Alert triage"
        description="The ranked queue: what to look at first, and why. Every timestamp is UTC."
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
        {(rows) =>
          rows.length === 0 ? (
            <Notice tone="info">
              No alerts yet — run the pipeline first (see README: Run the demo pipeline).
            </Notice>
          ) : (
            <div className="space-y-8">
              {(() => {
                const spec = severityBar(rows, palette);
                return spec === null ? null : (
                  <Chart spec={spec} label="Alert count per severity" caption="Alert count per severity." />
                );
              })()}

              <Section title="Ranked queue">
                <DataTable
                  columns={queueColumns}
                  rows={rows}
                  rowKey={(row, index) => row.alert_id ?? String(index)}
                />
              </Section>

              <AlertInspector alerts={rows} />
            </div>
          )
        }
      </QueryState>
    </>
  );
}
