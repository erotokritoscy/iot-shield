/**
 * Dense tables and the KPI tile — the pieces that carry most of this product's information.
 *
 * Numerics are right-aligned and tabular (brief §7): a column of priority scores is read by
 * comparing digits in place, which proportional figures make impossible.
 */
import * as React from "react";

import { formatCell } from "@/lib/format";
import type { AlertSeverity } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface Column<T> {
  readonly key: string;
  readonly header: string;
  readonly align?: "left" | "right";
  readonly render?: (row: T) => React.ReactNode;
  readonly className?: string;
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  caption,
  empty,
  className,
}: {
  readonly columns: readonly Column<T>[];
  readonly rows: readonly T[];
  readonly rowKey: (row: T, index: number) => string;
  readonly caption?: React.ReactNode;
  readonly empty?: React.ReactNode;
  readonly className?: string;
}) {
  if (rows.length === 0 && empty) return <>{empty}</>;
  return (
    <div className={cn("min-w-0", className)}>
      <div className="overflow-x-auto rounded-lg border border-line">
        <table className="w-full border-collapse text-sm">
          <thead>
            <tr className="bg-raised">
              {columns.map((column) => (
                <th
                  key={column.key}
                  scope="col"
                  className={cn(
                    "whitespace-nowrap border-b border-line px-3 py-2 text-2xs font-semibold uppercase tracking-wide text-muted",
                    column.align === "right" ? "text-right" : "text-left",
                  )}
                >
                  {column.header}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, index) => (
              <tr key={rowKey(row, index)} className="border-b border-line/70 last:border-0 hover:bg-raised/60">
                {columns.map((column) => (
                  <td
                    key={column.key}
                    className={cn(
                      "px-3 py-1.5 align-top text-ink",
                      column.align === "right" ? "text-right tabular" : "text-left",
                      column.className,
                    )}
                  >
                    {column.render
                      ? column.render(row)
                      : formatCell((row as Record<string, unknown>)[column.key])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {caption ? <p className="caption mt-2">{caption}</p> : null}
    </div>
  );
}

/**
 * A table whose columns are whatever the rows carry, in the order the API serialized them.
 *
 * The evaluation report's head-to-head and per-case tables were `pd.DataFrame(records)` —
 * i.e. every key the report holds. Fixing a column list here would silently hide a metric
 * the harness later adds, which is the one thing those two tables must not do.
 */
export function DynamicTable({
  rows,
  exclude = [],
  leading = [],
  caption,
  className,
}: {
  readonly rows: readonly Record<string, unknown>[];
  readonly exclude?: readonly string[];
  readonly leading?: readonly string[];
  readonly caption?: React.ReactNode;
  readonly className?: string;
}) {
  const keys: string[] = [];
  for (const row of rows) {
    for (const key of Object.keys(row)) {
      if (!keys.includes(key) && !exclude.includes(key)) keys.push(key);
    }
  }
  const ordered = [...leading.filter((key) => keys.includes(key)), ...keys.filter((key) => !leading.includes(key))];
  const columns: Column<Record<string, unknown>>[] = ordered.map((key) => ({
    key,
    header: key,
    align: rows.some((row) => typeof row[key] === "number") ? "right" : "left",
    render: (row) => {
      const value = row[key];
      // An empty cell means *undefined*, not zero — a clean recording has no faults to
      // recall (ADR-0021 D2). Rendering `0` here would be a different claim.
      if (value === null || value === undefined) return <span className="text-muted">—</span>;
      return formatCell(value);
    },
  }));
  return (
    <DataTable
      columns={columns}
      rows={rows}
      rowKey={(_row, index) => String(index)}
      caption={caption}
      className={className}
    />
  );
}

export function StatTile({ label, value }: { readonly label: string; readonly value: React.ReactNode }) {
  return (
    <div
      data-testid={`stat-${label.replace(/\s+/g, "-")}`}
      className="rounded-lg border border-line bg-surface px-4 py-3"
    >
      <p className="text-2xs uppercase tracking-wide text-muted">{label}</p>
      <p className="tabular mt-1 text-2xl font-semibold leading-none text-ink">{value}</p>
    </div>
  );
}

const severityClass: Record<string, string> = {
  low: "border-sev-low/40 bg-sev-low/10 text-sev-low",
  medium: "border-sev-medium/40 bg-sev-medium/15 text-sev-medium",
  high: "border-sev-high/40 bg-sev-high/15 text-sev-high",
  critical: "border-sev-critical/45 bg-sev-critical/15 text-sev-critical",
  emergency: "border-sev-emergency/50 bg-sev-emergency/20 text-sev-emergency",
};

/**
 * Severity as a labelled badge. The **text is always present**: colour supplements identity,
 * it never replaces it — the same CVD rule the charts follow (brief §7, charts.py).
 */
export function SeverityBadge({ severity }: { readonly severity: AlertSeverity | string }) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded border px-1.5 py-0.5 text-2xs font-semibold uppercase tracking-wide",
        severityClass[severity] ?? "border-line bg-raised text-dim",
      )}
    >
      {severity}
    </span>
  );
}
