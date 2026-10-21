/**
 * Pull a date range out of a data source's own database (ADR-0042) — the other way, beside
 * a sample, an upload and a paste, for a source's content to arrive. The server queries the
 * database the source's connection names and answers with text its normalizer reads, which
 * then fills the source exactly as an uploaded file would: the wrong-format check, the chain
 * check and the run see no difference.
 */
import { useMutation, useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import * as React from "react";

import { ParamFields } from "@/components/ParamFields";
import { Button, Field, TextInput } from "@/components/ui/controls";
import { Notice } from "@/components/ui/surfaces";
import { dataSourcesApi } from "@/lib/api/client";
import { describeRefusal } from "@/lib/api/http";
import type { ConnectionInfo, DataSourceInfo, PullResponse } from "@/lib/api/types";
import { initialValues, submittedParams, type ParamValues } from "@/lib/params";
import { keys } from "@/lib/queries";
import { FIELD_HELP, paramHelp } from "@/pages/compose/help";

const DEVICES = "devices";
const DEVICES_SHOWN = 200;

export function PullPanel({
  index,
  source,
  connection,
  onPulled,
}: {
  readonly index: number;
  readonly source: DataSourceInfo;
  readonly connection: ConnectionInfo;
  readonly onPulled: (pulled: PullResponse) => void;
}) {
  // The device list is its own control (searchable, many), so the generic form gets the rest.
  const otherParams = connection.params.filter((param) => param.name !== DEVICES);
  const [start, setStart] = React.useState("");
  const [end, setEnd] = React.useState("");
  const [values, setValues] = React.useState<ParamValues>(() => initialValues(otherParams));
  const [chosen, setChosen] = React.useState<readonly string[]>([]);
  const [filter, setFilter] = React.useState("");

  const sent = submittedParams(otherParams, values);
  // The device list depends on the other params (water meters vs electricity meters), so
  // they ride along as its query — and changing them forgets devices that no longer apply.
  const filters = Object.fromEntries(Object.entries(sent).map(([name, value]) => [name, String(value)]));
  const devices = useQuery({
    queryKey: keys.devices(source.source_id, filters),
    queryFn: () => dataSourcesApi.devices(source.source_id, filters),
    enabled: connection.has_devices,
    staleTime: 60_000,
  });
  const known = new Set((devices.data ?? []).map((device) => device.id));
  const selected = devices.data ? chosen.filter((id) => known.has(id)) : chosen;
  const needle = filter.trim().toLowerCase();
  const matching = (devices.data ?? []).filter((device) => !needle || device.label.toLowerCase().includes(needle));

  const pull = useMutation({
    mutationFn: () =>
      dataSourcesApi.pull(source.source_id, {
        start,
        end,
        params: selected.length > 0 ? { ...sent, [DEVICES]: selected } : sent,
      }),
    onSuccess: onPulled,
  });
  const ready = start !== "" && end !== "" && !pull.isPending;

  return (
    <div data-testid={`src_${index}_pull`} className="space-y-3 rounded-md border border-line bg-raised/40 p-3">
      <p className="text-sm font-medium text-ink">
        Pull from <span className="font-mono">{connection.name}</span>
        <span className="caption ml-2">{connection.description}</span>
      </p>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="From" htmlFor={`src_${index}_pull_start`} info={FIELD_HELP.pullFrom}>
          <TextInput
            id={`src_${index}_pull_start`}
            data-testid={`src_${index}_pull_start`}
            type="date"
            value={start}
            onChange={(event) => setStart(event.target.value)}
          />
        </Field>
        <Field label="To" htmlFor={`src_${index}_pull_end`} info={FIELD_HELP.pullTo}>
          <TextInput
            id={`src_${index}_pull_end`}
            data-testid={`src_${index}_pull_end`}
            type="date"
            value={end}
            min={start || undefined}
            onChange={(event) => setEnd(event.target.value)}
          />
        </Field>
      </div>
      {otherParams.length > 0 ? (
        <ParamFields
          params={otherParams}
          values={values}
          idPrefix={`src_${index}_pull`}
          info={(param) => paramHelp("connector", connection.connector, param)}
          onChange={(name, value) => setValues((current) => ({ ...current, [name]: value }))}
        />
      ) : null}
      {connection.has_devices ? (
        <Field
          label="Devices"
          htmlFor={`src_${index}_pull_devices`}
          info={FIELD_HELP.pullDevices}
          hint={
            selected.length > 0
              ? `${selected.length} selected.`
              : `None selected: every one${devices.data ? ` of the ${devices.data.length.toLocaleString("en-US")}` : ""} is pulled.`
          }
        >
          <TextInput
            data-testid={`src_${index}_pull_filter`}
            aria-label="Filter devices"
            placeholder="Filter…"
            value={filter}
            onChange={(event) => setFilter(event.target.value)}
            className="mb-1"
          />
          <select
            id={`src_${index}_pull_devices`}
            data-testid={`src_${index}_pull_devices`}
            multiple
            size={6}
            className="control font-mono text-xs"
            value={[...selected]}
            onChange={(event) => {
              const picked = new Set([...event.target.selectedOptions].map((option) => option.value));
              // Keep picks the filter hides; replace only what is on screen.
              const shown = new Set(matching.map((device) => device.id));
              setChosen([...selected.filter((id) => !shown.has(id)), ...picked]);
            }}
          >
            {matching.slice(0, DEVICES_SHOWN).map((device) => (
              <option key={device.id} value={device.id}>
                {device.label}
              </option>
            ))}
          </select>
          {matching.length > DEVICES_SHOWN ? (
            <p className="caption mt-1">
              Showing {DEVICES_SHOWN} of {matching.length.toLocaleString("en-US")}; filter to find others.
            </p>
          ) : null}
          {devices.isError ? <Notice tone="warning">{describeRefusal(devices.error)}</Notice> : null}
        </Field>
      ) : null}
      {pull.isError ? <Notice tone="error">{describeRefusal(pull.error)}</Notice> : null}
      {pull.isSuccess ? (
        <Notice tone="success">Pulled {pull.data.rows.toLocaleString("en-US")} rows into this source.</Notice>
      ) : null}
      <Button
        size="sm"
        variant="primary"
        data-testid={`src_${index}_pull_go`}
        disabled={!ready}
        onClick={() => pull.mutate()}
      >
        <Download className="h-3.5 w-3.5" aria-hidden /> {pull.isPending ? "Pulling…" : "Pull"}
      </Button>
    </div>
  );
}
