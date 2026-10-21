/**
 * IT overview (ADR-0044): the network-flow side, as the iotShield prototype's Biology
 * dashboard showed it — but drawn from what iotshield's own detectors found, not from another
 * classifier's `is_attack` guess. "Inside a detection" means the flow falls in the window of
 * a flow-lane detection on the same device.
 */
import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Notice, PageHeader, Section, Truncated } from "@/components/ui/surfaces";
import { DataTable, StatTile, type Column } from "@/components/ui/table";
import { bucketOf, bucketsOf, detectionsOverTime, flaggedFlows } from "@/charts/overview";
import { formatUtc } from "@/lib/format";
import type { NormalizedEvent } from "@/lib/api/types";
import { useFlows, useItOverview } from "@/lib/queries";
import { arrived, useReplay } from "@/lib/replay";
import { DayRangePicker, useDayRange } from "@/pages/overview/DayRange";
import { bucketFor } from "@/pages/overview/OtOverviewPage";
import { ArrivingFeed, ReplayControls, type FeedItem } from "@/pages/overview/Replay";

const PAGE_SIZE = 50;

const flowColumns: Column<NormalizedEvent>[] = [
  { key: "timestamp", header: "time (UTC)", render: (row) => formatUtc(row.timestamp) },
  { key: "asset_id", header: "device", render: (row) => <span className="font-mono text-xs">{row.asset_id}</span> },
  { key: "message", header: "flow", render: (row) => <span className="font-mono text-xs">{row.message ?? ""}</span> },
  {
    key: "value",
    header: "bytes out",
    align: "right",
    render: (row) => (row.value === null || row.value === undefined ? "-" : row.value.toLocaleString("en-US")),
  },
];

const RANGE_INFO =
  "Whole days in UTC, both included. Up to a week is drawn hour by hour. Flows are counted by when they started; a detection by when its window starts.";

export function ItOverviewPage() {
  const palette = useChartPalette();
  const [range, setRange] = useDayRange();
  const bucket = bucketFor(range);
  const overview = useItOverview(range, bucket);
  const flows = useFlows(range);
  const replay = useReplay(bucketsOf(range, bucket));
  const now = replay.now;

  return (
    <>
      <PageHeader
        title="IT overview"
        description="Network traffic (the flow lane) and what iotshield's detectors found in it — the IT side of your data sources."
      />
      <div className="space-y-8">
        <DayRangePicker
          range={range}
          onChange={setRange}
          latestDay={overview.data?.latest_day}
          empty={overview.data?.flows === 0}
          info={RANGE_INFO}
        />
        <QueryState query={overview} label="Counting flows…">
          {(data) => {
            // During a replay every count is of what has "arrived" by `now`; otherwise the range's.
            const shown = data.series.filter((point) => arrived(point.bucket, now));
            const sum = (pick: (point: (typeof shown)[number]) => number) =>
              shown.reduce((total, point) => total + pick(point), 0);
            const flowCount = now ? sum((point) => point.flows) : data.flows;
            const flagged = now ? sum((point) => point.flagged) : data.flagged;
            const detections = sum((point) => point.detections);
            const share = flowCount > 0 ? Math.round((100 * flagged) / flowCount) : 0;
            const pie = flaggedFlows(flowCount, flagged, palette);
            const bars = detectionsOverTime(data.series, range, bucket, palette, now);
            const feed: FeedItem[] = now
              ? (flows.data?.rows ?? [])
                  .filter((row) => bucketOf(row.timestamp, bucket) <= now)
                  .map((row, index) => ({
                    key: row.event_id ?? `${row.timestamp}|${index}`,
                    at: row.timestamp,
                    label: <span className="font-mono">{row.asset_id}</span>,
                    detail: row.message ?? "",
                  }))
              : [];
            return (
              <>
                {data.flows > 0 ? (
                  <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
                    <ReplayControls replay={replay} noun="flows" />
                    {now ? <ArrivingFeed title="Flows arriving now" items={feed} /> : null}
                  </div>
                ) : null}
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  <StatTile label="flows" value={flowCount.toLocaleString("en-US")} />
                  <StatTile label="inside a detection" value={`${flagged.toLocaleString("en-US")} (${share}%)`} />
                  <StatTile label="detections" value={detections.toLocaleString("en-US")} />
                </div>
                {data.flows === 0 ? (
                  <Notice tone="info">
                    No flows in this range.
                    {data.latest_day ? ` The latest flow is on ${data.latest_day}.` : " No flow data has been run yet."}
                  </Notice>
                ) : (
                  <div className="grid gap-8 lg:grid-cols-[auto_1fr]">
                    <Section title="Flows with and without a detection">
                      {!pie ? <p className="caption">No flows yet.</p> : null}
                      {pie ? (
                        <Chart
                          spec={pie}
                          label="Flows inside a detection versus the rest"
                          caption="Red: the flow falls in the window of a flow-lane detection on the same device."
                        />
                      ) : null}
                    </Section>
                    <Section title={bucket === "hour" ? "Detections per hour" : "Detections per day"}>
                      {bars ? (
                        <Chart
                          spec={bars}
                          label={
                            bucket === "hour" ? "Flow-lane detections per UTC hour" : "Flow-lane detections per UTC day"
                          }
                          caption="Every hour or day of the range is drawn; an empty one is a real zero."
                        />
                      ) : null}
                      {data.per_method.length > 0 ? (
                        <p className="caption mt-2" data-testid="it_methods">
                          By detector:{" "}
                          {data.per_method
                            .map((row) => `${row.method} ${row.count.toLocaleString("en-US")}`)
                            .join(" · ")}
                        </p>
                      ) : null}
                    </Section>
                  </div>
                )}
              </>
            );
          }}
        </QueryState>
        <Section title="Flows">
          <QueryState query={flows} label="Loading flows…">
            {(page) => (
              <>
                <Truncated
                  page={page}
                  noun="flows"
                  total={overview.data?.flows}
                  order="the latest flows of the range"
                />
                <DataTable
                  columns={flowColumns}
                  rows={page.rows}
                  rowKey={(row) => row.event_id ?? `${row.asset_id}|${row.timestamp}|${row.raw}`}
                  pageSize={PAGE_SIZE}
                  empty={<p className="caption">No flows in this range.</p>}
                />
              </>
            )}
          </QueryState>
        </Section>
      </div>
    </>
  );
}
