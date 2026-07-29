import { QueryState } from "@/components/QueryState";
import { Notice, PageHeader, Section, Truncated } from "@/components/ui/surfaces";
import { DataTable, DynamicTable, type Column } from "@/components/ui/table";
import type { MalformedRecord } from "@/lib/api/types";
import { useMalformed, useSummary } from "@/lib/queries";

const columns: Column<MalformedRecord>[] = [
  {
    key: "raw",
    header: "raw",
    render: (row) => <span className="font-mono text-xs break-all">{row.raw}</span>,
    className: "max-w-md",
  },
  { key: "error", header: "error" },
  { key: "source_format", header: "source format" },
  { key: "origin", header: "origin" },
];

export function DataHealthPage() {
  const summary = useSummary();
  const malformed = useMalformed();

  return (
    <>
      <PageHeader
        title="Data health"
        description="What the pipeline ingested, and what it could not parse."
      />
      <div className="space-y-8">
        <QueryState query={summary} label="Loading counts…">
          {(data) => <DynamicTable rows={[data.counts]} />}
        </QueryState>

        <Section title="Malformed inputs (captured, never dropped)">
          <QueryState query={malformed} label="Loading malformed records…">
            {(page) =>
              page.rows.length === 0 ? (
                <Notice tone="success">No malformed inputs recorded.</Notice>
              ) : (
                <>
                  {/* This page states a total and a table in the same breath, so an unmarked
                      cap reads as a contradiction the operator has to resolve alone. */}
                  <Truncated
                    page={page}
                    noun="malformed records"
                    total={summary.data?.counts["malformed_records"]}
                    order="the most recent window only"
                  />
                  <DataTable
                    columns={columns}
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
