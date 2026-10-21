/**
 * One meter (ADR-0044), as the iotShield prototype's SensiPro device page showed it: a day of
 * its hourly consumption, and each of SensiPro's anomaly types over the range as its own line,
 * with the records underneath.
 */
import { Link, useParams } from "react-router-dom";

import { Chart, useChartPalette } from "@/components/Chart";
import { QueryState } from "@/components/QueryState";
import { Field, TextInput } from "@/components/ui/controls";
import { PageHeader, Section } from "@/components/ui/surfaces";
import { bucketsOf, hourlyConsumption, typesOverTime } from "@/charts/overview";
import { useMeterDay, useOtOverview, useOtRecords } from "@/lib/queries";
import { useReplay } from "@/lib/replay";
import { useUrlChoice } from "@/lib/urlChoice";
import { DayRangePicker, useDayRange } from "@/pages/overview/DayRange";
import { AnomalyRecords, anomalyFeed, bucketFor, RANGE_INFO } from "@/pages/overview/OtOverviewPage";
import { ArrivingFeed, ReplayControls } from "@/pages/overview/Replay";

const PATTERN = /^\d{4}-\d{2}-\d{2}$/;

export function MeterPage() {
  const { assetId = "" } = useParams();
  const palette = useChartPalette();
  const [range, setRange] = useDayRange();
  const bucket = bucketFor(range);
  const overview = useOtOverview(range, assetId, bucket);
  const records = useOtRecords(range, assetId);
  const replay = useReplay(bucketsOf(range, bucket));
  const [chosenDay, setDay] = useUrlChoice("day");
  const day = chosenDay && PATTERN.test(chosenDay) ? chosenDay : range.end;
  const readings = useMeterDay(assetId, day);

  return (
    <>
      <PageHeader
        title={`Meter ${assetId}`}
        description={
          <>
            Its hourly consumption and SensiPro&apos;s anomaly types over time.{" "}
            <Link
              to={{ pathname: "/overview/ot", search: `?start=${range.start}&end=${range.end}` }}
              className="text-accent hover:underline"
            >
              ← OT overview
            </Link>
          </>
        }
      />
      <div className="space-y-8">
        <DayRangePicker
          range={range}
          onChange={setRange}
          latestDay={overview.data?.latest_day}
          empty={overview.data?.total === 0}
          info={RANGE_INFO}
        />
        <Section title="Consumption on one day">
          <Field
            label="Day (UTC)"
            htmlFor="meter_day"
            info="The hourly consumption of this meter on one UTC day. Each bar is the hour ending at its time."
          >
            <TextInput
              id="meter_day"
              data-testid="meter_day"
              type="date"
              className="max-w-xs"
              value={day}
              onChange={(event) => PATTERN.test(event.target.value) && setDay(event.target.value)}
            />
          </Field>
          <QueryState query={readings} label="Loading readings…">
            {(events) => {
              const spec = hourlyConsumption(events, palette);
              return spec ? (
                <Chart spec={spec} label={`Hourly consumption of meter ${assetId} on ${day}`} />
              ) : (
                <p className="caption">No readings for this meter on {day}.</p>
              );
            }}
          </QueryState>
        </Section>
        <Section title="Anomalies by type">
          {overview.data && overview.data.total > 0 ? (
            <div className="mb-4 grid gap-4 lg:grid-cols-[1fr_22rem]">
              <ReplayControls replay={replay} noun="anomalies" />
              {replay.now ? (
                <ArrivingFeed title="Arriving now" items={anomalyFeed(records.data?.rows ?? [], replay.now, bucket)} />
              ) : null}
            </div>
          ) : null}
          <QueryState query={overview} label="Counting anomalies…">
            {(data) => {
              const spec = typesOverTime(data.series, range, bucket, palette, replay.now);
              return spec ? (
                <Chart
                  spec={spec}
                  label={`Anomalies of meter ${assetId} by type`}
                  caption="One line per type that occurred; an empty bucket is a real zero."
                />
              ) : (
                <p className="caption" data-testid="meter_no_anomalies">
                  No anomalies for this meter in this range.
                </p>
              );
            }}
          </QueryState>
        </Section>
        <AnomalyRecords range={range} assetId={assetId} total={overview.data?.total} />
      </div>
    </>
  );
}
