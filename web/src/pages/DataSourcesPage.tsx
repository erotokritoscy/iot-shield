/**
 * The organization's data sources, typed IT or OT (ADR-0041) — iotShield's register, carried
 * over: everyone in the organization sees the list, its admins add, edit and remove.
 *
 * A source may say how its data is read (normalizer + format), checked by the server against
 * the same catalogue a run uses; Compose's source card then fills both in from it. Or it may
 * name a **connection** (ADR-0042) — a database the server declares — and then its reader is
 * whatever that connection's connector writes, and Compose can pull a date range from it.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { QueryState } from "@/components/QueryState";
import { Button, Field, Select, Textarea, TextInput } from "@/components/ui/controls";
import { Notice, PageHeader, Section } from "@/components/ui/surfaces";
import { DataTable, type Column } from "@/components/ui/table";
import { dataSourcesApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import type { ConnectionInfo, DataSourceInfo, ModuleInfo, SourceType } from "@/lib/api/types";
import { isOrgAdmin, useAuth } from "@/lib/auth";
import { keys, useConnections, useDataSources, useModules } from "@/lib/queries";
import { cn } from "@/lib/utils";

const TYPES: readonly SourceType[] = ["IT", "OT"];
const NO_READER = "";
const NO_CONNECTION = "";

export const TYPE_HELP: Readonly<Record<SourceType, string>> = {
  IT: "IT — information technology: network traffic (flows, packet captures) and system logs.",
  OT: "OT — operational technology: the physical process — meters, sensors, controllers.",
};

const HELP = {
  name: "What your team calls this source, e.g. “Camera network” or “SensiPro meters”. Unique within the organization.",
  type: `${TYPE_HELP.IT} ${TYPE_HELP.OT}`,
  description:
    "Optional: anything a colleague should know — where it is captured, who owns it, how often it is exported.",
  normalizer:
    "Optional: the reader for this source's data. When set, picking this source on Compose fills in the normalizer and format for you.",
  format: "The exact dialect this normalizer reads for this source.",
  connection:
    "Optional: a database the server is set up to read (config/connections.yaml). With one, Compose can pull a date range of this source straight from it, and the reader is whatever that database's connector writes.",
} as const;

interface Draft {
  readonly name: string;
  readonly type: SourceType;
  readonly description: string;
  readonly normalizer: string;
  readonly format: string;
  readonly connection: string;
}

const EMPTY: Draft = {
  name: "",
  type: "IT",
  description: "",
  normalizer: NO_READER,
  format: "",
  connection: NO_CONNECTION,
};

const draftOf = (source: DataSourceInfo): Draft => ({
  name: source.name,
  type: source.type,
  description: source.description ?? "",
  normalizer: source.normalizer ?? NO_READER,
  format: source.source_format ?? "",
  connection: source.connection ?? NO_CONNECTION,
});

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

/** Add a source, or edit one when `editing` is set. */
function SourceForm({
  normalizers,
  connections,
  editing,
  onDone,
}: {
  readonly normalizers: readonly ModuleInfo[];
  readonly connections: readonly ConnectionInfo[];
  readonly editing: DataSourceInfo | null;
  readonly onDone: () => void;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>(editing ? draftOf(editing) : EMPTY);
  const formats = normalizers.find((item) => item.name === draft.normalizer)?.formats ?? [];
  const connection = connections.find((item) => item.name === draft.connection);
  // A connection the server no longer declares stays visible, so saving does not drop it silently.
  const connectionOptions = [
    NO_CONNECTION,
    ...connections.map((item) => item.name),
    ...(draft.connection && !connection ? [draft.connection] : []),
  ];

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: draft.name.trim(),
        type: draft.type,
        description: draft.description.trim() || null,
        // A connected source is read the way its connector writes; the server fills that in.
        normalizer: draft.connection ? null : draft.normalizer || null,
        source_format: draft.connection ? null : draft.normalizer ? draft.format || null : null,
        connection: draft.connection || null,
      };
      return editing ? dataSourcesApi.update(editing.source_id, body) : dataSourcesApi.add(body);
    },
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: keys.dataSources });
      setDraft(EMPTY);
      onDone();
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.name.trim()) save.mutate();
  };

  return (
    <form className="space-y-4" onSubmit={submit} noValidate data-testid="source_form">
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Name" htmlFor="ds-name" info={HELP.name} className="sm:col-span-2">
          <TextInput
            id="ds-name"
            data-testid="ds_name"
            autoComplete="off"
            maxLength={100}
            value={draft.name}
            onChange={(event) => setDraft({ ...draft, name: event.target.value })}
          />
        </Field>
        <Field label="Type" htmlFor="ds-type" info={HELP.type}>
          <Select
            id="ds-type"
            data-testid="ds_type"
            options={TYPES}
            labelFor={(option) => (option === "IT" ? "IT — network, logs" : "OT — meters, sensors")}
            value={draft.type}
            onChange={(event) => setDraft({ ...draft, type: event.target.value as SourceType })}
          />
        </Field>
      </div>
      <Field label="Description" htmlFor="ds-description" info={HELP.description}>
        <Textarea
          id="ds-description"
          data-testid="ds_description"
          rows={2}
          maxLength={500}
          className="font-sans text-sm"
          value={draft.description}
          onChange={(event) => setDraft({ ...draft, description: event.target.value })}
        />
      </Field>
      {connectionOptions.length > 1 ? (
        <Field
          label="Connection"
          htmlFor="ds-connection"
          info={HELP.connection}
          hint={
            connection
              ? `${connection.description} Read by ${connection.normalizer} (${connection.source_format}).`
              : draft.connection
                ? "This connection is no longer declared on the server."
                : "None: upload or paste this source's data on Compose."
          }
        >
          <Select
            id="ds-connection"
            data-testid="ds_connection"
            options={connectionOptions}
            labelFor={(option) => (option === NO_CONNECTION ? "— none —" : option)}
            value={draft.connection}
            onChange={(event) => setDraft({ ...draft, connection: event.target.value })}
          />
        </Field>
      ) : null}
      {draft.connection ? null : (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Normalizer" htmlFor="ds-normalizer" info={HELP.normalizer}>
            <Select
              id="ds-normalizer"
              data-testid="ds_normalizer"
              options={[NO_READER, ...normalizers.map((item) => item.name)]}
              labelFor={(option) => (option === NO_READER ? "— not set —" : option)}
              value={draft.normalizer}
              onChange={(event) => {
                const next = normalizers.find((item) => item.name === event.target.value);
                setDraft({
                  ...draft,
                  normalizer: event.target.value,
                  format: next?.formats[0] ?? "",
                });
              }}
            />
          </Field>
          {formats.length > 1 ? (
            <Field label="Input format" htmlFor="ds-format" info={HELP.format}>
              <Select
                id="ds-format"
                data-testid="ds_format"
                options={formats}
                value={draft.format}
                onChange={(event) => setDraft({ ...draft, format: event.target.value })}
              />
            </Field>
          ) : null}
        </div>
      )}
      {save.isError ? <Notice tone="error">{describeRefusal(save.error)}</Notice> : null}
      <div className="flex gap-2">
        <Button type="submit" variant="primary" data-testid="ds_save" disabled={save.isPending || !draft.name.trim()}>
          {save.isPending ? "Saving…" : editing ? "Save changes" : "Add data source"}
        </Button>
        {editing ? (
          <Button type="button" variant="ghost" onClick={onDone}>
            Cancel
          </Button>
        ) : null}
      </div>
    </form>
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
