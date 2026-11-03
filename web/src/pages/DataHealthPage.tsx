import { QueryState } from "@/components/QueryState";
import { Notice, PageHeader, Section, Truncated } from "@/components/ui/surfaces";
import { DataTable, DynamicTable, type Column } from "@/components/ui/table";
import type { MalformedRecord } from "@/lib/api/types";
import { formatUtc } from "@/lib/format";
import { useMalformed, useSummary } from "@/lib/queries";
import { isOpen, useTimeRange } from "@/lib/timeRange";
import { TimeRangeFilter } from "@/components/TimeRangeFilter";

/** Shared with the Run page, which shows one run's slice of this same audit. */
export const malformedColumns: Column<MalformedRecord>[] = [
  {
    key: "raw",
    header: "raw",
    render: (row) => <span className="font-mono text-xs break-all">{row.raw}</span>,
    className: "max-w-md",
  },
  { key: "error", header: "error" },
  { key: "source_format", header: "source format" },
  { key: "origin", header: "origin" },
  // The clock the time range filters on, so the filter's effect is visible in the table.
  { key: "created_at", header: "ingested (UTC)", render: (row) => formatUtc(row.created_at) },
];

export function DataHealthPage() {
  const summary = useSummary();
  const [range, setRange] = useTimeRange();
  const ranged = !isOpen(range);
  const malformed = useMalformed(range);

  return (
    <>
      <PageHeader
        title="Data health"
        description="What the pipeline ingested, and what it could not parse."
      />
      <div className="space-y-8">
        <QueryState query={summary} label="Loading counts…">
          {(data) => (
            <>
              <DynamicTable rows={[data.counts]} />
              {ranged ? (
                <p className="caption mt-2">
                  Counts are all-time totals; the time range filters the malformed inputs below.
                </p>
              ) : null}
            </>
          )}
        </QueryState>

        <Section title="Malformed inputs (captured, never dropped)">
          {/* "Ingested", not "occurred": a malformed line has no timestamp of its own — the
              time is exactly what failed to parse — so when it was stored is its only clock. */}
          <div className="mb-4">
            <TimeRangeFilter
              range={range}
              onChange={setRange}
              idPrefix="health_range"
              basis="Ingest time: a record is in the range when it was STORED at or after From and before To. A malformed line has no timestamp of its own — that is what failed to parse."
            />
          </div>
          <QueryState query={malformed} label="Loading malformed records…">
            {(page) =>
              page.rows.length === 0 ? (
                <Notice tone="success">
                  {ranged ? "No malformed inputs ingested in this time range." : "No malformed inputs recorded."}
                </Notice>
              ) : (
                <>
                  {/* This page states a total and a table in the same breath, so an unmarked
                      cap reads as a contradiction the operator has to resolve alone. */}
                  <Truncated
                    page={page}
                    noun="malformed records"
                    // `/summary` counts the whole org, so it is no total for a range's slice.
                    total={ranged ? undefined : summary.data?.counts["malformed_records"]}
                    order="the most recent window only"
                  />
                  <DataTable
                    columns={malformedColumns}
                    rows={page.rows}
                    rowKey={(row, index) => row.record_id ?? String(index)}
                    pageSize={50}
                  />
                </>
              )
            }
          </QueryState>
        </Section>
      </div>
    </>
  );
}
