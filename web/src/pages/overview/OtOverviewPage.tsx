/**
 * OT overview (ADR-0044): SensiPro's six meter data-quality types, as the iotShield
 * prototype's SensiPro dashboard showed them, counted from iotshield's own results — the
 * `meter_quality` detector (heartbeat, high) and the meter normalizer's tagged rejects
 * (valueError, zero, negative, small), ADR-0043.
 *
 * A single-day range is drawn by hour, as the prototype did; longer ranges by day. The meter
 * selector beside the range narrows every count, chart and record to one meter (`?meter=`).
 */
import { Link, useLocation } from "react-router-dom";

import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Field, Select } from "@/components/ui/controls";
import { InfoTip } from "@/components/ui/info";
import { Notice, PageHeader, Section, Truncated } from "@/components/ui/surfaces";
import { DataTable, StatTile, type Column } from "@/components/ui/table";
import {
  anomaliesOverTime,
  bucketOf,
  bucketsOf,
  daysOf,
  anomaliesPerMeter,
  anomalyTypes,
  QUALITY_COLORS,
  QUALITY_ISSUES,
  QUALITY_MEANING,
  type QualityIssueName,
} from "@/charts/overview";
import { formatUtc } from "@/lib/format";
import type { DayRange } from "@/lib/api/client";
import type { OtOverview, QualityRecordInfo } from "@/lib/api/types";
import { useOtOverview, useOtRecords } from "@/lib/queries";
import { arrived, useReplay } from "@/lib/replay";
import { useUrlChoice } from "@/lib/urlChoice";
import { DayRangePicker, useDayRange } from "@/pages/overview/DayRange";
import { ArrivingFeed, ReplayControls, type FeedItem } from "@/pages/overview/Replay";

const PAGE_SIZE = 50;
const ALL = "";

export const RANGE_INFO =
  "Whole days in UTC, both included. A range of up to a week is drawn hour by hour, a longer one day by day. An anomaly is placed by the reading it is about (a detection by when its window starts).";

/** Where a type is found — the two homes ADR-0043 gave the six. */
const FOUND_BY: Readonly<Record<QualityIssueName, string>> = {
  heartbeat: "meter_quality detector",
  valueError: "rejected at ingest",
  zero: "rejected at ingest",
  negative: "rejected at ingest",
  high: "meter_quality detector",
  small: "rejected at ingest",
};

export function IssueLabel({ issue }: { readonly issue: string }) {
  const known = (QUALITY_ISSUES as readonly string[]).includes(issue) ? (issue as QualityIssueName) : null;
  return (
    <span className="inline-flex items-center gap-1.5">
      <span
        aria-hidden
        className="inline-block h-2.5 w-2.5 rounded-full"
        style={{ backgroundColor: known ? QUALITY_COLORS[known] : "transparent" }}
      />
      <span>{issue}</span>
      {known ? <InfoTip label={issue}>{QUALITY_MEANING[known]}</InfoTip> : null}
    </span>
  );
}

type TypeRow = OtOverview["per_type"][number];

const typeColumns: Column<TypeRow>[] = [
  { key: "issue", header: "type", render: (row) => <IssueLabel issue={row.issue} /> },
  {
    key: "code",
    header: "SensiPro code",
    render: (row) => <span className="tabular text-xs">{`${row.category},${row.subcategory}`}</span>,
  },
  {
    key: "found",
    header: "found by",
    render: (row) => <span className="caption">{FOUND_BY[row.issue as QualityIssueName] ?? ""}</span>,
  },
  { key: "count", header: "anomalies", align: "right", render: (row) => row.count.toLocaleString("en-US") },
];

export function recordColumns(linkMeters: boolean): Column<QualityRecordInfo>[] {
  return [
    { key: "at", header: "reading (UTC)", render: (row) => (row.at ? formatUtc(row.at) : "unknown") },
    {
      key: "asset_id",
      header: "meter",
      render: (row) =>
        row.asset_id === null ? (
          <span className="caption">unknown</span>
        ) : linkMeters ? (
          <MeterLink assetId={row.asset_id} />
        ) : (
          <span className="font-mono text-xs">{row.asset_id}</span>
        ),
    },
    { key: "issue", header: "type", render: (row) => <IssueLabel issue={row.issue} /> },
    {
      key: "detail",
      header: "what was found",
      render: (row) => (
        <span className="text-xs">
          <span className="caption mr-1">{row.source === "detection" ? "detection:" : "rejected:"}</span>
          {row.detail}
        </span>
      ),
    },
  ];
}

function MeterLink({ assetId }: { readonly assetId: string }) {
  // The range travels with the link, so the meter opens on the days being looked at.
  const { search } = useLocation();
  return (
    <Link
      to={`/overview/ot/meter/${encodeURIComponent(assetId)}${search}`}
      className="font-mono text-xs text-accent hover:underline"
    >
      {assetId}
    </Link>
  );
}

/** The records table: every anomaly of the range, filterable by type, capped at 500. */
export function AnomalyRecords({
  range,
  assetId,
  total,
}: {
  readonly range: DayRange;
  readonly assetId?: string;
  readonly total?: number | undefined;
}) {
  const [issue, setIssue] = useUrlChoice("type");
  const records = useOtRecords(range, assetId, issue ?? undefined);
  return (
    <Section title="Anomaly records">
      <Field label="Type" htmlFor="records_type" info="Show one of SensiPro's six types, or all of them.">
        <Select
          id="records_type"
          data-testid="records_type"
          className="max-w-xs"
          options={[ALL, ...QUALITY_ISSUES]}
          labelFor={(option) => (option === ALL ? "all types" : option)}
          value={issue ?? ALL}
          onChange={(event) => setIssue(event.target.value)}
        />
      </Field>
      <QueryState query={records} label="Loading anomaly records…">
        {(page) => (
          <>
            <Truncated page={page} noun="anomalies" total={issue ? undefined : total} order="the oldest of the range" />
            <DataTable
              columns={recordColumns(assetId === undefined)}
              rows={page.rows}
              rowKey={(row) => `${row.source}|${row.asset_id}|${row.at}|${row.issue}|${row.detail}`}
              pageSize={PAGE_SIZE}
              empty={<p className="caption">No anomalies in this range.</p>}
            />
          </>
        )}
      </QueryState>
    </Section>
  );
}

/** Whole ranges of up to a week are drawn — and replayed — hour by hour; longer ones by day. */
export const bucketFor = (range: DayRange): "day" | "hour" =>
  daysOf(range.start, range.end).length <= 7 ? "hour" : "day";

/** The newest anomalies that have arrived by the replay's point in time. */
export function anomalyFeed(
  records: readonly QualityRecordInfo[],
  now: string | null,
  bucket: "day" | "hour",
): FeedItem[] {
  if (now === null) return [];
  return records
    .filter((record) => record.at !== null && bucketOf(record.at, bucket) <= now)
    .map((record) => ({
      key: `${record.source}|${record.asset_id}|${record.at}|${record.issue}|${record.detail}`,
      at: record.at ?? "",
      label: <IssueLabel issue={record.issue} />,
      detail: record.asset_id ?? "",
    }));
}

export function OtOverviewPage() {
  const palette = useChartPalette();
  const [range, setRange] = useDayRange();
  const bucket = bucketFor(range);
  const [chosenMeter, setMeter] = useUrlChoice("meter");
  const meter = chosenMeter ?? undefined;
  const overview = useOtOverview(range, meter, bucket);
  const records = useOtRecords(range, meter);
  // A chosen meter stays offered even when the range holds none of its readings.
  const meterIds = overview.data?.meter_ids ?? [];
  const meterOptions = [ALL, ...(meter && !meterIds.includes(meter) ? [meter, ...meterIds] : meterIds)];
  const buckets = bucketsOf(range, bucket);
  const replay = useReplay(buckets);
  const now = replay.now;

  return (
    <>
      <PageHeader
        title="OT overview"
        description="Utility meters and SensiPro's six data-quality anomaly types — the OT side of your data sources."
      />
      <div className="space-y-8">
        <DayRangePicker
          range={range}
          onChange={setRange}
          latestDay={overview.data?.latest_day}
          empty={overview.data?.total === 0}
          info={RANGE_INFO}
        >
          <Field
            label="Sensor"
            htmlFor="ot_meter"
            info="Which sensor (utility meter, by its device id) to show: one, or all of them. Offered: every sensor with a reading in the range."
          >
            <Select
              id="ot_meter"
              data-testid="ot_meter"
              className="max-w-xs"
              options={meterOptions}
              labelFor={(option) => (option === ALL ? "all sensors" : option)}
              value={meter ?? ALL}
              onChange={(event) => setMeter(event.target.value)}
            />
          </Field>
        </DayRangePicker>
        <QueryState query={overview} label="Counting anomalies…">
          {(data) => {
            // During a replay every count is of what has "arrived" by `now`; otherwise the range's.
            const series = data.series.filter((point) => arrived(point.bucket, now));
            const meterSeries = data.meter_series.filter((point) => arrived(point.bucket, now));
            const byIssue = new Map<string, number>();
            for (const point of series) byIssue.set(point.issue, (byIssue.get(point.issue) ?? 0) + point.count);
            const perType = now
              ? data.per_type.map((row) => ({ ...row, count: byIssue.get(row.issue) ?? 0 }))
              : data.per_type;
            const byMeter = new Map<string, number>();
            for (const point of meterSeries)
              byMeter.set(point.asset_id, (byMeter.get(point.asset_id) ?? 0) + point.count);
            const perMeter = now
              ? [...byMeter].map(([asset_id, count]) => ({ asset_id, count })).sort((a, b) => b.count - a.count)
              : data.per_meter;
            const total = now ? series.reduce((sum, point) => sum + point.count, 0) : data.total;
            const meters = now ? byMeter.size : data.meters;

            const line = anomaliesOverTime(data.series, range, bucket, palette, now);
            const pie = anomalyTypes(perType, palette);
            const bars = anomaliesPerMeter(perMeter, palette);
            return (
              <>
                {data.total > 0 ? (
                  <div className="grid gap-4 lg:grid-cols-[1fr_22rem]">
                    <ReplayControls replay={replay} noun="anomalies" />
                    {now ? (
                      <ArrivingFeed title="Arriving now" items={anomalyFeed(records.data?.rows ?? [], now, bucket)} />
                    ) : null}
                  </div>
                ) : null}
                <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
                  <StatTile label="anomalies" value={total.toLocaleString("en-US")} />
                  <StatTile label="meters affected" value={meters.toLocaleString("en-US")} />
                  <StatTile label="without a meter or time" value={now ? "—" : data.unplaced.toLocaleString("en-US")} />
                </div>
                {now ? (
                  <p className="caption">
                    Replaying: counts are of what has arrived so far; meters are counted among the{" "}
                    {data.per_meter.length} with the most anomalies.
                  </p>
                ) : null}
                {data.total === 0 ? (
                  <Notice tone="info">
                    No anomalies in this range.
                    {data.latest_day
                      ? ` The latest meter reading is on ${data.latest_day}.`
                      : " No meter data has been run yet: pull a SensiPro source on Compose and run it with meter_quality."}
                  </Notice>
                ) : null}
                <Section title={bucket === "hour" ? "Anomalies per hour" : "Anomalies per day"}>
                  {line ? (
                    <Chart
                      spec={line}
                      label={bucket === "hour" ? "Anomalies per UTC hour" : "Anomalies per UTC day"}
                      caption={
                        data.unplaced > 0
                          ? `All six types together. ${data.unplaced} rejected line(s) named no meter or time and are counted only by type.`
                          : "All six types together; an empty bucket is a real zero."
                      }
                    />
                  ) : null}
                </Section>
                <div className="grid gap-8 lg:grid-cols-[auto_1fr]">
                  <Section title="By type">
                    {pie ? (
                      <Chart spec={pie} label="Anomalies by SensiPro type" />
                    ) : (
                      <p className="caption">None yet.</p>
                    )}
                  </Section>
                  <Section title="SensiPro's six types">
                    <DataTable columns={typeColumns} rows={perType} rowKey={(row) => row.issue} />
                  </Section>
                </div>
                {data.per_meter.length > 0 ? (
                  <Section
                    title="Meters with the most anomalies"
                    description={`The top ${data.per_meter.length} of ${data.meters.toLocaleString("en-US")}. Open one for its consumption and anomaly lines.`}
                  >
                    {bars ? (
                      <Chart spec={bars} label="Anomalies per meter, most first" />
                    ) : (
                      <p className="caption">None yet.</p>
                    )}
                    <p className="caption mt-2 flex flex-wrap gap-x-3 gap-y-1" data-testid="ot_meters">
                      {data.per_meter.map((row) => (
                        <MeterLink key={row.asset_id} assetId={row.asset_id} />
                      ))}
                    </p>
                  </Section>
                ) : null}
              </>
            );
          }}
        </QueryState>
        <AnomalyRecords range={range} assetId={meter} total={overview.data?.total} />
      </div>
    </>
  );
}
