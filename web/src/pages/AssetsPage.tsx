import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Field, Select } from "@/components/ui/controls";
import { Notice, PageHeader, Section, Truncated } from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { telemetryTimeline } from "@/charts/specs";
import { explanationOf } from "@/lib/evidence";
import { formatNumber, formatUtc } from "@/lib/format";
import type { Asset, DetectionResult } from "@/lib/api/types";
import { useAssets, useDetections, useEvents } from "@/lib/queries";
import { useUrlChoice } from "@/lib/urlChoice";

/** Rows per page in the detections table; a 5,000-row render is 25,000 DOM nodes. */
const PAGE_SIZE = 50;

/** `Feed pump 2 [pump-02] (auto-discovered)` — discovery is visible, never implied. */
export function assetLabel(asset: Asset): string {
  const discovered = asset.metadata?.["auto_discovered"] ? " (auto-discovered)" : "";
  return `${asset.name} [${asset.asset_id}]${discovered}`;
}

const detectionColumns: Column<DetectionResult>[] = [
  { key: "method", header: "method" },
  { key: "lane", header: "lane" },
  { key: "score", header: "score", align: "right", render: (d) => formatNumber(d.score, 4) },
  { key: "threshold", header: "threshold", align: "right", render: (d) => formatNumber(d.threshold, 4) },
  { key: "window_start", header: "window start", render: (d) => formatUtc(d.window_start) },
  // Last, so the numeric block stays adjacent and readable, but present: `method` no longer
  // identifies a finding (ADR-0032 D3 — six rules on one lane all report `rules:flow`), and
  // without this the table lists several rows that differ only in score.
  {
    key: "finding",
    header: "finding",
    render: (d) => explanationOf(d) ?? <span className="text-muted">—</span>,
  },
];

function AssetDetail({ asset }: { readonly asset: Asset }) {
  const palette = useChartPalette();
  // Telemetry-only on purpose: the chart plots telemetry *values*, and overlaying a flow or
  // log detection's window on a value series they do not belong to would be misleading.
  const events = useEvents(asset.asset_id, "telemetry");
  const telemetryDetections = useDetections(asset.asset_id, "telemetry");
  // The table lists EVERY lane, so a flow- or log-only asset is not invisible here — its
  // alert sits in Triage while this page used to show it "no telemetry" and nothing else.
  const allDetections = useDetections(asset.asset_id);

  return (
    <div className="space-y-8">
      <p className="caption">
        type: {asset.asset_type} · criticality: {asset.criticality}
        {asset.location ? ` · location: ${asset.location}` : ""}
      </p>

      <QueryState query={events} label="Loading telemetry…">
        {(eventPage) => (
          <QueryState query={telemetryDetections} label="Loading telemetry detections…">
            {(detectionPage) => {
              const spec = telemetryTimeline(eventPage.rows, detectionPage.rows, palette);
              return (
                <>
                  {/* The server orders events `timestamp DESC` and *then* limits, so the cap
                      keeps the NEWEST readings — which means a truncated chart silently
                      begins at an arbitrary instant rather than at the start of the record. */}
                  <Truncated
                    page={eventPage}
                    noun="telemetry readings"
                    order="the most recent window only, so the axis does not start where the data does"
                  />
                  {spec === null ? (
                    <Notice tone="info">No telemetry with numeric values for this asset.</Notice>
                  ) : (
                    <Chart
                      spec={spec}
                      label={`Telemetry values over time for ${asset.asset_id}`}
                      caption="Blue line: telemetry values. Red bands/rules: telemetry detection windows (all lanes are in the table below)."
                    />
                  )}
                </>
              );
            }}
          </QueryState>
        )}
      </QueryState>

      <QueryState query={allDetections} label="Loading detections…">
        {(page) =>
          page.rows.length === 0 ? (
            <Notice tone="info">No detections for this asset.</Notice>
          ) : (
            <Section title="Detections">
              <Truncated page={page} noun="detections" order="the most recent window only" />
              <DataTable
                columns={detectionColumns}
                rows={page.rows}
                rowKey={(row, index) => row.detection_id ?? String(index)}
                pageSize={PAGE_SIZE}
              />
            </Section>
          )
        }
      </QueryState>
    </div>
  );
}

export function AssetsPage() {
  const assets = useAssets();
  const [chosenId, choose] = useUrlChoice("asset");

  return (
    <>
      <PageHeader
        title="Assets & telemetry"
        description="One device's readings on a clock, with what the detectors said about them. UTC throughout."
      />
      <QueryState query={assets} label="Loading assets…">
        {(rows) => {
          if (rows.length === 0) {
            return (
              <Notice tone="info">
                No assets yet — the inventory populates from data (asset discovery).
              </Notice>
            );
          }
          const selected = rows.find((asset) => asset.asset_id === chosenId) ?? rows[0];
          if (selected === undefined) return null;
          return (
            <div className="space-y-6">
              <Field label="Asset" htmlFor="assets_asset" className="max-w-md">
                <Select
                  id="assets_asset"
                  value={selected.asset_id}
                  onChange={(event) => choose(event.target.value)}
                  options={rows.map((asset) => asset.asset_id)}
                  labelFor={(id) => {
                    const asset = rows.find((item) => item.asset_id === id);
                    return asset ? assetLabel(asset) : id;
                  }}
                />
              </Field>
              <AssetDetail key={selected.asset_id} asset={selected} />
            </div>
          );
        }}
      </QueryState>
    </>
  );
}
