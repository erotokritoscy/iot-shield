import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Field, SearchSelect } from "@/components/ui/controls";
import {
  Notice,
  PageHeader,
  Section,
} from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { telemetryTimeline } from "@/charts/specs";
import { explanationOf } from "@/lib/evidence";
import { formatNumber, formatUtc } from "@/lib/format";
import type { Asset, DetectionResult } from "@/lib/api/types";
import { useAssets, useDetections, useEvents } from "@/lib/queries";
import { isOpen, type TimeRange, useTimeRange } from "@/lib/timeRange";
import { useUrlChoice } from "@/lib/urlChoice";
import { TimeRangeFilter } from "@/components/TimeRangeFilter";

/** Rows per page in the detections table; a 5,000-row render is 25,000 DOM nodes. */
const PAGE_SIZE = 50;

/** `Feed pump 2 [pump-02] (auto-discovered)` — discovery is visible, never implied. */
export function assetLabel(asset: Asset): string {
  const discovered = asset.metadata?.["auto_discovered"]
    ? " (auto-discovered)"
    : "";
  return `${asset.name} [${asset.asset_id}]${discovered}`;
}

const detectionColumns: Column<DetectionResult>[] = [
  { key: "method", header: "method" },
  { key: "lane", header: "lane" },
  {
    key: "score",
    header: "score",
    align: "right",
    render: (d) => formatNumber(d.score, 4),
  },
  {
    key: "threshold",
    header: "threshold",
    align: "right",
    render: (d) => formatNumber(d.threshold, 4),
  },
  {
    key: "window_start",
    header: "window start",
    render: (d) => formatUtc(d.window_start),
  },
  // Last, so the numeric block stays adjacent and readable, but present: `method` no longer
  // identifies a finding (ADR-0032 D3 — six rules on one lane all report `rules:flow`), and
  // without this the table lists several rows that differ only in score.
  {
    key: "finding",
    header: "finding",
    render: (d) => explanationOf(d) ?? <span className="text-muted">—</span>,
  },
];

function AssetDetail({
  asset,
  range,
}: {
  readonly asset: Asset;
  readonly range: TimeRange;
}) {
  const palette = useChartPalette();
  // Telemetry-only on purpose: the chart plots telemetry *values*, and overlaying a flow or
  // log detection's window on a value series they do not belong to would be misleading.
  const events = useEvents(asset.asset_id, "telemetry", range);
  const telemetryDetections = useDetections(asset.asset_id, "telemetry", range);
  // The table lists EVERY lane, so a flow- or log-only asset is not invisible here — its
  // alert sits in Triage while this page used to show it "no telemetry" and nothing else.
  const allDetections = useDetections(asset.asset_id, undefined, range);

  return (
    <div className="space-y-8">
      <p className="caption">
        type: {asset.asset_type} · criticality: {asset.criticality}
        {asset.location ? ` · location: ${asset.location}` : ""}
      </p>

      <QueryState query={events} label="Loading telemetry…">
        {(readings) => (
          <QueryState
            query={telemetryDetections}
            label="Loading telemetry detections…"
          >
            {(telemetryFindings) => {
              const spec = telemetryTimeline(readings, telemetryFindings, palette);
              return (
                <>
                  {/* Every reading in the range, not a capped slice (`readApi.allEvents`):
                      a cap kept the newest rows, so the axis began at an arbitrary instant
                      rather than where the record does. */}
                  {spec === null ? (
                    <Notice tone="info">
                      No telemetry with numeric values for this asset
                      {isOpen(range) ? "" : " in this time range"}.
                    </Notice>
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
        {(detections) =>
          detections.length === 0 ? (
            <Notice tone="info">
              No detections for this asset
              {isOpen(range) ? "" : " in this time range"}.
            </Notice>
          ) : (
            <Section title="Detections">
              <DataTable
                columns={detectionColumns}
                rows={detections}
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
  const [range, setRange] = useTimeRange();

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
                No assets yet — the inventory populates from data (asset
                discovery).
              </Notice>
            );
          }
          const selected =
            rows.find((asset) => asset.asset_id === chosenId) ?? rows[0];
          if (selected === undefined) return null;
          return (
            <div className="space-y-6">
              <div className="flex flex-wrap items-end gap-3">
                <Field
                  label="Asset"
                  htmlFor="assets_asset"
                  className="w-full max-w-md"
                >
                  <SearchSelect
                    id="assets_asset"
                    data-testid="assets_asset"
                    placeholder="Search by name or id…"
                    value={selected.asset_id}
                    onChange={choose}
                    options={rows.map((asset) => asset.asset_id)}
                    labelFor={(id) => {
                      const asset = rows.find((item) => item.asset_id === id);
                      return asset ? assetLabel(asset) : id;
                    }}
                  />
                </Field>
                <TimeRangeFilter
                  range={range}
                  onChange={setRange}
                  idPrefix="assets_range"
                  basis="Readings are in the range when their timestamp is between From and To, both included; detections when their window starts at or after From and before To."
                />
              </div>
              <AssetDetail
                key={selected.asset_id}
                asset={selected}
                range={range}
              />
            </div>
          );
        }}
      </QueryState>
    </>
  );
}
