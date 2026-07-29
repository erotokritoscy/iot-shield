/**
 * Dense tables and the KPI tile — the pieces that carry most of this product's information.
 *
 * Numerics are right-aligned and tabular (brief §7): a column of priority scores is read by
 * comparing digits in place, which proportional figures make impossible.
 */
import * as React from "react";

import { Button } from "@/components/ui/controls";
import { formatCell, isFlatRecord } from "@/lib/format";
import type { AlertSeverity } from "@/lib/api/types";
import { cn } from "@/lib/utils";

export interface Column<T> {
  readonly key: string;
  readonly header: string;
  readonly align?: "left" | "right";
  readonly render?: (row: T) => React.ReactNode;
  readonly className?: string;
}

/**
 * Makes rows activate something.
 *
 * One object rather than three optional props, so the combinations that mean nothing —
 * a highlight with no click, a label with no handler — are unrepresentable.
 *
 * `key` is compared against whatever `rowKey` returns, so a caller's selected value must come
 * from the same function.
 */
export interface Selection<T> {
  readonly key: string | null;
  readonly onSelect: (row: T) => void;
  /** The activator's accessible name; the visible cell content is usually not enough. */
  readonly label: (row: T) => string;
}

/**
 * The row's real control.
 *
 * A `<button>` inside the first cell, NOT `role="button"` on the `<tr>` — that would replace
 * the row's own role, make it an invalid child of `rowgroup`, and sever the row↔cell
 * relationships assistive technology navigates by. A table whose rows do something is still a
 * table. The row's `onClick` is a pointer affordance layered on top of this.
 */
function RowActivator({
  selected,
  label,
  onActivate,
  children,
}: {
  readonly selected: boolean;
  readonly label: string;
  readonly onActivate: () => void;
  readonly children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-current={selected ? "true" : undefined}
      aria-label={label}
      onClick={(event) => {
        event.stopPropagation();
        onActivate();
      }}
      className="rounded text-left focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
    >
      {children}
    </button>
  );
}

export function DataTable<T>({
  columns,
  rows,
  rowKey,
  caption,
  empty,
  className,
  selection,
  pageSize,
}: {
  readonly columns: readonly Column<T>[];
  readonly rows: readonly T[];
  readonly rowKey: (row: T, index: number) => string;
  readonly caption?: React.ReactNode;
  readonly empty?: React.ReactNode;
  readonly className?: string;
  readonly selection?: Selection<T>;
  /**
   * Page the rows rather than rendering all of them.
   *
   * Here rather than at a call site because every large table in this app has the same
   * problem — the read plane caps lists at 1,000–5,000 rows, and 5,000 rows × 5 cells is
   * 25,000 DOM nodes — and because the clamp below is the sort of arithmetic that gets
   * copied slightly wrong.
   */
  readonly pageSize?: number;
}) {
  const [page, setPage] = React.useState(0);
  if (rows.length === 0 && empty) return <>{empty}</>;

  const pages = pageSize ? Math.max(1, Math.ceil(rows.length / pageSize)) : 1;
  // Clamped, not just reset on filter change: `rows` can also shrink under a refetch, which
  // would otherwise strand the view on a page that no longer exists.
  const current = Math.min(page, pages - 1);
  const visible = pageSize ? rows.slice(current * pageSize, (current + 1) * pageSize) : rows;

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
            {visible.map((row, index) => {
              const key = rowKey(row, index);
              const selected = selection !== undefined && key === selection.key;
              return (
                <tr
                  key={key}
                  onClick={selection ? () => selection.onSelect(row) : undefined}
                  className={cn(
                    "border-b border-line/70 last:border-0",
                    selected ? "bg-accent/10" : "hover:bg-raised/60",
                    selection && "cursor-pointer",
                  )}
                >
                  {columns.map((column, columnIndex) => {
                    const content = column.render
                      ? column.render(row)
                      : formatCell((row as Record<string, unknown>)[column.key]);
                    return (
                      <td
                        key={column.key}
                        className={cn(
                          "px-3 py-1.5 align-top text-ink",
                          column.align === "right" ? "text-right tabular" : "text-left",
                          column.className,
                        )}
                      >
                        {selection && columnIndex === 0 ? (
                          <RowActivator
                            selected={selected}
                            label={selection.label(row)}
                            onActivate={() => selection.onSelect(row)}
                          >
                            {content}
                          </RowActivator>
                        ) : (
                          content
                        )}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {pages > 1 ? (
        <div className="mt-2 flex items-center gap-3">
          <Button size="sm" variant="ghost" disabled={current === 0} onClick={() => setPage(current - 1)}>
            Previous
          </Button>
          <span className="caption">
            Page {current + 1} of {pages} · {rows.length.toLocaleString("en-US")} rows
          </span>
          <Button
            size="sm"
            variant="ghost"
            disabled={current === pages - 1}
            onClick={() => setPage(current + 1)}
          >
            Next
          </Button>
        </div>
      ) : null}
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
      if (keys.includes(key) || exclude.includes(key)) continue;
      // Matrix-valued fields are dropped by SHAPE, not by name. A named exclusion list is
      // exactly what this component promises not to have: the next nested metric the harness
      // adds would land here unlisted, fall through `formatCell` to `JSON.stringify`, and put
      // several hundred characters in one cell — with no code change, which is the property
      // the passthrough schemas exist to give and this list would quietly take away.
      const value = row[key];
      if (value !== null && value !== undefined && typeof value === "object" && !isFlatRecord(value)) {
        continue;
      }
      keys.push(key);
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
