/**
 * The HTTP API fields of the data-source form (ADR-0049): where the API is, how to sign in,
 * how a window is asked for, and how its JSON becomes events. State lives in `apiDraft.ts`.
 */
import { Field, Select, Textarea, TextInput } from "@/components/ui/controls";
import {
  AUTH_KINDS,
  AUTH_LABEL,
  LANES,
  TIMESTAMP_FORMATS,
  TIMESTAMP_LABEL,
  WINDOW_FORMATS,
  WINDOW_LABEL,
  type ApiDraft,
  type AuthKind,
  type MappedLane,
  type TimestampFormat,
  type WindowFormat,
} from "@/pages/datasources/apiDraft";

export const API_HELP = {
  url: "The https:// address that returns this source's records as JSON. It must be reachable from the server and public; an internal host has to be allowed by the server's operator (IOTSHIELD_API_ALLOWED_HOSTS).",
  auth: "How the server signs in to the API. Credentials are encrypted on the server and never shown again.",
  username: "The user name for HTTP basic authentication.",
  password: "Sent with the user name as HTTP basic authentication.",
  apiKey: "The token or key the API issued. Sent in the header below, after the prefix.",
  apiKeyHeader: "The request header the key goes in: Authorization for a bearer token, or e.g. X-API-Key.",
  apiKeyPrefix: "Text sent before the key — “Bearer ” for a bearer token, empty for a bare key.",
  window:
    "A pull asks for a date range. Name the query parameters the API reads it from, and the form it expects; dates are inclusive, instants run to midnight UTC after the last day.",
  nextPath:
    "Optional: where each response says the next page is, as a dotted path (e.g. links.next). Leave blank for an API that answers in one page. A next page must be on the same host.",
  lane: "Readings (telemetry: one number per metric) or log lines (a message and a level).",
  records: "Dotted path to the list of records in the response, e.g. data.items. Blank when the response is the list itself.",
  timestamp: "Path of each record's time, inside the record.",
  timestampFormat: "How that time is written.",
  timezone:
    "Only for ISO times written without an offset: the IANA zone they are in, e.g. Asia/Nicosia. Left blank, such times are refused rather than guessed.",
  assetId: "Path of the device's id inside each record — the asset its events are filed under.",
  metrics:
    "One per line, “name = path”, e.g. “Temperature = readings.temp”. Each becomes its own event, so a record with three readings yields three.",
  message: "Path of the log line's text.",
  severity: "Optional: path of the level (debug, info, warning, error, critical; warn, err and fatal are understood too).",
} as const;

export function ApiFields({
  draft,
  hasCredentials,
  onChange,
}: {
  readonly draft: ApiDraft;
  /** The server holds credentials for this source already, so blank keeps them. */
  readonly hasCredentials: boolean;
  readonly onChange: (next: ApiDraft) => void;
}) {
  const set = <K extends keyof ApiDraft>(key: K, value: ApiDraft[K]) => onChange({ ...draft, [key]: value });
  const keep = hasCredentials ? "Leave blank to keep the stored one." : undefined;

  return (
    <div className="space-y-4 rounded-md border border-line bg-raised/40 p-3" data-testid="ds_api">
      <Field label="API URL" htmlFor="ds-api-url" info={API_HELP.url}>
        <TextInput
          id="ds-api-url"
          data-testid="ds_api_url"
          placeholder="https://api.example.com/v1/readings"
          autoComplete="off"
          value={draft.url}
          onChange={(event) => set("url", event.target.value)}
        />
      </Field>

      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Authentication" htmlFor="ds-api-auth" info={API_HELP.auth}>
          <Select
            id="ds-api-auth"
            data-testid="ds_api_auth"
            options={AUTH_KINDS}
            labelFor={(option) => AUTH_LABEL[option as AuthKind]}
            value={draft.auth}
            onChange={(event) => set("auth", event.target.value as AuthKind)}
          />
        </Field>
        {draft.auth === "basic" ? (
          <>
            <Field label="Username" htmlFor="ds-api-username" info={API_HELP.username}>
              <TextInput
                id="ds-api-username"
                data-testid="ds_api_username"
                autoComplete="off"
                value={draft.username}
                onChange={(event) => set("username", event.target.value)}
              />
            </Field>
            <Field label="Password" htmlFor="ds-api-password" info={API_HELP.password} hint={keep}>
              <TextInput
                id="ds-api-password"
                data-testid="ds_api_password"
                type="password"
                autoComplete="new-password"
                placeholder={hasCredentials ? "•••••••• (stored)" : undefined}
                value={draft.password}
                onChange={(event) => set("password", event.target.value)}
              />
            </Field>
          </>
        ) : null}
        {draft.auth === "api_key" ? (
          <Field label="API key" htmlFor="ds-api-key" info={API_HELP.apiKey} hint={keep} className="sm:col-span-2">
            <TextInput
              id="ds-api-key"
              data-testid="ds_api_key"
              type="password"
              autoComplete="new-password"
              placeholder={hasCredentials ? "•••••••• (stored)" : undefined}
              value={draft.apiKey}
              onChange={(event) => set("apiKey", event.target.value)}
            />
          </Field>
        ) : null}
      </div>
      {draft.auth === "api_key" ? (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Header" htmlFor="ds-api-header" info={API_HELP.apiKeyHeader}>
            <TextInput
              id="ds-api-header"
              data-testid="ds_api_header"
              value={draft.apiKeyHeader}
              onChange={(event) => set("apiKeyHeader", event.target.value)}
            />
          </Field>
          <Field label="Prefix" htmlFor="ds-api-prefix" info={API_HELP.apiKeyPrefix}>
            <TextInput
              id="ds-api-prefix"
              data-testid="ds_api_prefix"
              value={draft.apiKeyPrefix}
              onChange={(event) => set("apiKeyPrefix", event.target.value)}
            />
          </Field>
        </div>
      ) : null}

      <div className="grid gap-4 sm:grid-cols-4">
        <Field label="Start parameter" htmlFor="ds-api-start" info={API_HELP.window}>
          <TextInput
            id="ds-api-start"
            data-testid="ds_api_start"
            value={draft.startParam}
            onChange={(event) => set("startParam", event.target.value)}
          />
        </Field>
        <Field label="End parameter" htmlFor="ds-api-end" info={API_HELP.window}>
          <TextInput
            id="ds-api-end"
            data-testid="ds_api_end"
            value={draft.endParam}
            onChange={(event) => set("endParam", event.target.value)}
          />
        </Field>
        <Field label="Window format" htmlFor="ds-api-window" info={API_HELP.window}>
          <Select
            id="ds-api-window"
            data-testid="ds_api_window"
            options={WINDOW_FORMATS}
            labelFor={(option) => WINDOW_LABEL[option as WindowFormat]}
            value={draft.windowFormat}
            onChange={(event) => set("windowFormat", event.target.value as WindowFormat)}
          />
        </Field>
        <Field label="Next page" htmlFor="ds-api-next" info={API_HELP.nextPath}>
          <TextInput
            id="ds-api-next"
            data-testid="ds_api_next"
            placeholder="e.g. links.next"
            value={draft.nextPath}
            onChange={(event) => set("nextPath", event.target.value)}
          />
        </Field>
      </div>

      <p className="text-sm font-medium text-ink">Mapping the response to events</p>
      <div className="grid gap-4 sm:grid-cols-3">
        <Field label="Events are" htmlFor="ds-api-lane" info={API_HELP.lane}>
          <Select
            id="ds-api-lane"
            data-testid="ds_api_lane"
            options={LANES}
            labelFor={(option) => (option === "telemetry" ? "Readings (telemetry)" : "Log lines")}
            value={draft.lane}
            onChange={(event) => set("lane", event.target.value as MappedLane)}
          />
        </Field>
        <Field label="Records path" htmlFor="ds-api-records" info={API_HELP.records} className="sm:col-span-2">
          <TextInput
            id="ds-api-records"
            data-testid="ds_api_records"
            placeholder="blank = the response is the list"
            value={draft.records}
            onChange={(event) => set("records", event.target.value)}
          />
        </Field>
        <Field label="Timestamp path" htmlFor="ds-api-ts" info={API_HELP.timestamp}>
          <TextInput
            id="ds-api-ts"
            data-testid="ds_api_ts"
            value={draft.timestamp}
            onChange={(event) => set("timestamp", event.target.value)}
          />
        </Field>
        <Field label="Timestamp format" htmlFor="ds-api-ts-format" info={API_HELP.timestampFormat}>
          <Select
            id="ds-api-ts-format"
            data-testid="ds_api_ts_format"
            options={TIMESTAMP_FORMATS}
            labelFor={(option) => TIMESTAMP_LABEL[option as TimestampFormat]}
            value={draft.timestampFormat}
            onChange={(event) => set("timestampFormat", event.target.value as TimestampFormat)}
          />
        </Field>
        {draft.timestampFormat === "iso" ? (
          <Field label="Time zone" htmlFor="ds-api-tz" info={API_HELP.timezone}>
            <TextInput
              id="ds-api-tz"
              data-testid="ds_api_tz"
              placeholder="only for times without an offset"
              value={draft.timezone}
              onChange={(event) => set("timezone", event.target.value)}
            />
          </Field>
        ) : null}
        <Field label="Asset id path" htmlFor="ds-api-asset" info={API_HELP.assetId}>
          <TextInput
            id="ds-api-asset"
            data-testid="ds_api_asset"
            value={draft.assetId}
            onChange={(event) => set("assetId", event.target.value)}
          />
        </Field>
      </div>
      {draft.lane === "telemetry" ? (
        <Field label="Metrics" htmlFor="ds-api-metrics" info={API_HELP.metrics}>
          <Textarea
            id="ds-api-metrics"
            data-testid="ds_api_metrics"
            rows={3}
            placeholder={"Temperature = readings.temp\nPressure = readings.pressure"}
            value={draft.metrics}
            onChange={(event) => set("metrics", event.target.value)}
          />
        </Field>
      ) : (
        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Message path" htmlFor="ds-api-message" info={API_HELP.message}>
            <TextInput
              id="ds-api-message"
              data-testid="ds_api_message"
              value={draft.message}
              onChange={(event) => set("message", event.target.value)}
            />
          </Field>
          <Field label="Level path" htmlFor="ds-api-severity" info={API_HELP.severity}>
            <TextInput
              id="ds-api-severity"
              data-testid="ds_api_severity"
              value={draft.severity}
              onChange={(event) => set("severity", event.target.value)}
            />
          </Field>
        </div>
      )}
    </div>
  );
}
