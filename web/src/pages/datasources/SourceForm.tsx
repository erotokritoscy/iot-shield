/**
 * The form that adds a data source, or edits one (ADR-0041): name, type, description, where it
 * can be pulled from (a declared connection or an HTTP API, ADR-0049) and the reader for it.
 *
 * Shared by the Data sources page and Compose's *Create new data source* dialog, so a source
 * added from either is the same request with the same checks.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";

import { Button, Field, Select, Textarea, TextInput } from "@/components/ui/controls";
import { Notice } from "@/components/ui/surfaces";
import { dataSourcesApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import type { ConnectionInfo, DataSourceInfo, ModuleInfo, SourceType } from "@/lib/api/types";
import { keys } from "@/lib/queries";
import { ApiFields } from "@/pages/datasources/ApiFields";
import { apiBody, apiDraftOf, apiProblem, EMPTY_API, type ApiDraft } from "@/pages/datasources/apiDraft";

const TYPES: readonly SourceType[] = ["IT", "OT"];
const NO_READER = "";
const NO_CONNECTION = "";
/** The "Pull from" choice that means an HTTP API rather than a declared connection. */
const VIA_API = "(http-api)";

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
    "Optional: where Compose can pull a date range of this source from. Either a database the server is set up to read (config/connections.yaml) — then the reader is whatever its connector writes — or an HTTP API you describe here, whose JSON records you map to events.",
} as const;

interface Draft {
  readonly name: string;
  readonly type: SourceType;
  readonly description: string;
  readonly normalizer: string;
  readonly format: string;
  /** A declared connection's name, `VIA_API`, or `NO_CONNECTION`. */
  readonly connection: string;
  readonly api: ApiDraft;
}

const EMPTY: Draft = {
  name: "",
  type: "IT",
  description: "",
  normalizer: NO_READER,
  format: "",
  connection: NO_CONNECTION,
  api: EMPTY_API,
};

const draftOf = (source: DataSourceInfo): Draft => ({
  name: source.name,
  type: source.type,
  description: source.description ?? "",
  normalizer: source.normalizer ?? NO_READER,
  format: source.source_format ?? "",
  connection: source.api ? VIA_API : (source.connection ?? NO_CONNECTION),
  api: source.api ? apiDraftOf(source.api) : EMPTY_API,
});

/** Add a source, or edit one when `editing` is set. */
export function SourceForm({
  normalizers,
  connections,
  editing,
  onDone,
  onSaved,
  onCancel,
}: {
  readonly normalizers: readonly ModuleInfo[];
  readonly connections: readonly ConnectionInfo[];
  readonly editing: DataSourceInfo | null;
  readonly onDone: () => void;
  /** Told the source as the server stored it, once saved — before `onDone`. */
  readonly onSaved?: (source: DataSourceInfo) => void;
  /** Shows a Cancel button on a new source too (a dialog's); an edit always has one. */
  readonly onCancel?: () => void;
}) {
  const client = useQueryClient();
  const [draft, setDraft] = useState<Draft>(editing ? draftOf(editing) : EMPTY);
  const formats = normalizers.find((item) => item.name === draft.normalizer)?.formats ?? [];
  const connection = connections.find((item) => item.name === draft.connection);
  // A connection the server no longer declares stays visible, so saving does not drop it silently.
  const viaApi = draft.connection === VIA_API;
  const connectionOptions = [
    NO_CONNECTION,
    ...connections.map((item) => item.name),
    ...(draft.connection && !connection && !viaApi ? [draft.connection] : []),
    VIA_API,
  ];
  const problem = viaApi ? apiProblem(draft.api) : null;

  const save = useMutation({
    mutationFn: () => {
      const body = {
        name: draft.name.trim(),
        type: draft.type,
        description: draft.description.trim() || null,
        // A connected or API source is read the way it writes; the server fills that in.
        normalizer: draft.connection ? null : draft.normalizer || null,
        source_format: draft.connection ? null : draft.normalizer ? draft.format || null : null,
        connection: draft.connection && !viaApi ? draft.connection : null,
        // Omitted rather than null for any other source: leaving it out removes an API.
        ...(viaApi ? apiBody(draft.api) : {}),
      };
      return editing ? dataSourcesApi.update(editing.source_id, body) : dataSourcesApi.add(body);
    },
    onSuccess: (saved) => {
      void client.invalidateQueries({ queryKey: keys.dataSources });
      setDraft(EMPTY);
      onSaved?.(saved);
      onDone();
    },
  });

  const submit = (event: FormEvent) => {
    event.preventDefault();
    if (draft.name.trim() && problem === null) save.mutate();
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
      <Field
        label="Pull from"
        htmlFor="ds-connection"
        info={HELP.connection}
        hint={
          viaApi
            ? "An HTTP API, read by passthrough (normalized-jsonl) after your mapping."
            : connection
              ? `${connection.description} Read by ${connection.normalizer} (${connection.source_format}).`
              : draft.connection
                ? "This connection is no longer declared on the server."
                : "Nothing: upload or paste this source's data on Compose."
        }
      >
        <Select
          id="ds-connection"
          data-testid="ds_connection"
          options={connectionOptions}
          labelFor={(option) =>
            option === NO_CONNECTION ? "— nothing —" : option === VIA_API ? "An HTTP API…" : option
          }
          value={draft.connection}
          onChange={(event) => setDraft({ ...draft, connection: event.target.value })}
        />
      </Field>
      {viaApi ? (
        <ApiFields
          draft={draft.api}
          hasCredentials={Boolean(editing?.api) && Boolean(editing?.has_credentials)}
          onChange={(api) => setDraft({ ...draft, api })}
        />
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
      {problem && draft.api.url.trim() ? (
        <p className="caption" data-testid="ds_api_problem">
          {problem}
        </p>
      ) : null}
      {save.isError ? <Notice tone="error">{describeRefusal(save.error)}</Notice> : null}
      <div className="flex gap-2">
        <Button
          type="submit"
          variant="primary"
          data-testid="ds_save"
          disabled={save.isPending || !draft.name.trim() || problem !== null}
        >
          {save.isPending ? "Saving…" : editing ? "Save changes" : "Add data source"}
        </Button>
        {editing || onCancel ? (
          <Button type="button" variant="ghost" data-testid="ds_cancel" onClick={onCancel ?? onDone}>
            Cancel
          </Button>
        ) : null}
      </div>
    </form>
  );
}
