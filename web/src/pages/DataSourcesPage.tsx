/**
 * The organization's data sources, typed IT or OT (ADR-0041) — iotShield's register, carried
 * over: everyone in the organization sees the list, its admins add, edit and remove.
 *
 * A source may say how its data is read (normalizer + format), checked by the server against
 * the same catalogue a run uses; Compose's source card then fills both in from it. Or it may
 * name a **connection** (ADR-0042) — a database the server declares — and then its reader is
 * whatever that connection's connector writes, and Compose can pull a date range from it.
 * Or it may be an **HTTP API** (ADR-0049): the admin enters its URL, authentication and a
 * mapping from its JSON to events, and Compose pulls from it the same way.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { QueryState } from "@/components/QueryState";
import { Button } from "@/components/ui/controls";
import { Notice, PageHeader, Section } from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { dataSourcesApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import type { DataSourceInfo, SourceType } from "@/lib/api/types";
import { isOrgAdmin, useAuth } from "@/lib/auth";
import { keys, useConnections, useDataSources, useModules } from "@/lib/queries";
import { cn } from "@/lib/utils";
import { SourceForm, TYPE_HELP } from "@/pages/datasources/SourceForm";

/** The host of a URL, for a compact label; the URL itself when it does not parse. */
export function hostOf(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return url;
  }
}

export function TypeBadge({ type }: { readonly type: SourceType }) {
  return (
    <span
      title={TYPE_HELP[type]}
      className={cn(
        "inline-flex rounded border px-1.5 py-0.5 text-2xs font-semibold",
        type === "IT" ? "border-info/40 bg-info/10 text-info" : "border-warning/40 bg-warning/10 text-warning",
      )}
    >
      {type}
    </span>
  );
}

export function DataSourcesPage() {
  const auth = useAuth();
  const canEdit = isOrgAdmin(auth.data);
  const client = useQueryClient();
  const sources = useDataSources();
  const modules = useModules(canEdit);
  const connections = useConnections(canEdit);
  const [editing, setEditing] = useState<DataSourceInfo | null>(null);
  const remove = useMutation({
    mutationFn: (source: DataSourceInfo) => dataSourcesApi.remove(source.source_id),
    onSuccess: () => void client.invalidateQueries({ queryKey: keys.dataSources }),
  });

  const columns: Column<DataSourceInfo>[] = [
    {
      key: "type",
      header: "type",
      render: (row) => <TypeBadge type={row.type} />,
    },
    {
      key: "name",
      header: "name",
      render: (row) => <span className="font-medium text-ink">{row.name}</span>,
    },
    {
      key: "reader",
      header: "read by",
      render: (row) =>
        row.normalizer ? (
          <span className="font-mono text-xs">
            {row.normalizer} · {row.source_format}
            {row.connection ? <span className="caption ml-1">(pulled from {row.connection})</span> : null}
            {row.api ? (
              <span className="caption ml-1" title={row.api.url}>
                (pulled from {hostOf(row.api.url)}, {row.api.mapping.lane})
              </span>
            ) : null}
          </span>
        ) : (
          <span className="caption">not set</span>
        ),
    },
    {
      key: "description",
      header: "description",
      render: (row) => row.description ?? "",
    },
  ];
  if (canEdit) {
    columns.push({
      key: "actions",
      header: "",
      render: (row) => (
        <div className="flex justify-end gap-1">
          <Button size="sm" variant="ghost" data-testid={`ds_edit_${row.name}`} onClick={() => setEditing(row)}>
            Edit
          </Button>
          <Button
            size="sm"
            variant="ghost"
            data-testid={`ds_remove_${row.name}`}
            disabled={remove.isPending}
            onClick={() => {
              if (window.confirm(`Remove the data source “${row.name}”? Data already ingested is kept.`)) {
                if (editing?.source_id === row.source_id) setEditing(null);
                remove.mutate(row);
              }
            }}
          >
            Remove
          </Button>
        </div>
      ),
    });
  }

  return (
    <>
      <PageHeader
        title="Data sources"
        description="Where your organization's data comes from. IT sources carry network traffic and logs; OT sources carry the physical process — meters and sensors."
      />
      <div className="space-y-8">
        <Section title="Sources">
          {remove.isError ? <Notice tone="error">{describeRefusal(remove.error)}</Notice> : null}
          <QueryState query={sources} label="Loading data sources…">
            {(rows) => (
              <DataTable
                columns={columns}
                rows={rows}
                rowKey={(row) => row.source_id}
                empty={
                  <p className="caption" data-testid="ds_empty">
                    {canEdit ? "No data sources yet — add the first one below." : "No data sources yet."}
                  </p>
                }
              />
            )}
          </QueryState>
        </Section>
        {canEdit ? (
          <Section title={editing ? `Edit “${editing.name}”` : "Add a data source"}>
            <QueryState query={modules} label="Loading the normalizers…">
              {(catalogue) => (
                <SourceForm
                  key={editing?.source_id ?? "new"}
                  normalizers={catalogue.normalizers}
                  connections={connections.data ?? []}
                  editing={editing}
                  onDone={() => setEditing(null)}
                />
              )}
            </QueryState>
          </Section>
        ) : (
          <Notice tone="info">Only an organization admin can add or change data sources.</Notice>
        )}
      </div>
    </>
  );
}
