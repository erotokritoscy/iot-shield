/**
 * The HTTP API half of the data-source form (ADR-0049), as plain state and the rules that read
 * it — no rendering here, like `compose/model.ts`.
 *
 * The credentials are **write-only**: the server never sends them back (`has_credentials` says
 * whether some are stored), so an edit opens with the secret fields blank, and blank means
 * "keep what is stored". The draft therefore never holds a secret it did not get from the
 * keyboard.
 */
import type { ApiEndpoint, ApiMapping, DataSourceRequest } from "@/lib/api/types";

export type AuthKind = ApiEndpoint["auth"];
export type WindowFormat = ApiEndpoint["window_format"];
export type TimestampFormat = ApiMapping["timestamp_format"];
export type MappedLane = ApiMapping["lane"];

export const AUTH_KINDS: readonly AuthKind[] = ["none", "basic", "api_key"];
export const WINDOW_FORMATS: readonly WindowFormat[] = ["iso", "date", "epoch_s", "epoch_ms"];
export const TIMESTAMP_FORMATS: readonly TimestampFormat[] = ["iso", "epoch_s", "epoch_ms"];
export const LANES: readonly MappedLane[] = ["telemetry", "log"];

export const AUTH_LABEL: Readonly<Record<AuthKind, string>> = {
  none: "None",
  basic: "Username and password",
  api_key: "API key / bearer token",
};
export const WINDOW_LABEL: Readonly<Record<WindowFormat, string>> = {
  iso: "ISO 8601 instants (UTC)",
  date: "Dates (YYYY-MM-DD)",
  epoch_s: "Unix seconds",
  epoch_ms: "Unix milliseconds",
};
export const TIMESTAMP_LABEL: Readonly<Record<TimestampFormat, string>> = {
  iso: "ISO 8601 text",
  epoch_s: "Unix seconds",
  epoch_ms: "Unix milliseconds",
};

export interface ApiDraft {
  readonly url: string;
  readonly auth: AuthKind;
  readonly username: string;
  /** Typed this session only; blank keeps the stored one. */
  readonly password: string;
  readonly apiKey: string;
  readonly apiKeyHeader: string;
  readonly apiKeyPrefix: string;
  readonly startParam: string;
  readonly endParam: string;
  readonly windowFormat: WindowFormat;
  readonly nextPath: string;
  readonly lane: MappedLane;
  readonly records: string;
  readonly timestamp: string;
  readonly timestampFormat: TimestampFormat;
  readonly timezone: string;
  readonly assetId: string;
  /** Telemetry: one `name = path` per line. */
  readonly metrics: string;
  readonly message: string;
  readonly severity: string;
}

export const EMPTY_API: ApiDraft = {
  url: "",
  auth: "none",
  username: "",
  password: "",
  apiKey: "",
  apiKeyHeader: "Authorization",
  apiKeyPrefix: "Bearer ",
  startParam: "start",
  endParam: "end",
  windowFormat: "iso",
  nextPath: "",
  lane: "telemetry",
  records: "",
  timestamp: "timestamp",
  timestampFormat: "iso",
  timezone: "",
  assetId: "",
  metrics: "",
  message: "",
  severity: "",
};

export function apiDraftOf(endpoint: ApiEndpoint): ApiDraft {
  const { mapping } = endpoint;
  return {
    ...EMPTY_API,
    url: endpoint.url,
    auth: endpoint.auth,
    username: endpoint.username ?? "",
    apiKeyHeader: endpoint.api_key_header,
    apiKeyPrefix: endpoint.api_key_prefix,
    startParam: endpoint.start_param,
    endParam: endpoint.end_param,
    windowFormat: endpoint.window_format,
    nextPath: endpoint.next_path ?? "",
    lane: mapping.lane,
    records: mapping.records,
    timestamp: mapping.timestamp,
    timestampFormat: mapping.timestamp_format,
    timezone: mapping.timezone ?? "",
    assetId: mapping.asset_id,
    metrics: Object.entries(mapping.metrics ?? {})
      .map(([name, path]) => `${name} = ${path}`)
      .join("\n"),
    message: mapping.message ?? "",
    severity: mapping.severity ?? "",
  };
}

/** `name = path` lines as a mapping, or the first line that is not one. */
export function parseMetrics(text: string): { readonly metrics: Record<string, string> } | { readonly error: string } {
  const metrics: Record<string, string> = {};
  for (const [index, raw] of text.split("\n").entries()) {
    const line = raw.trim();
    if (!line) continue;
    const at = line.indexOf("=");
    const name = at < 0 ? "" : line.slice(0, at).trim();
    const path = at < 0 ? "" : line.slice(at + 1).trim();
    if (!name || !path) return { error: `Line ${index + 1} is not “name = path”: ${line}` };
    if (name in metrics) return { error: `The metric “${name}” is named twice.` };
    metrics[name] = path;
  }
  return { metrics };
}

const orNull = (value: string): string | null => value.trim() || null;

/** What the form can already tell is missing, before the server is asked. */
export function apiProblem(draft: ApiDraft): string | null {
  if (!draft.url.trim()) return "Enter the API's URL.";
  if (!draft.startParam.trim() || !draft.endParam.trim()) return "Name the window's start and end parameters.";
  if (draft.auth === "basic" && !draft.username.trim()) return "Enter the user name.";
  if (!draft.timestamp.trim() || !draft.assetId.trim()) return "Name the timestamp and asset paths.";
  if (draft.lane === "telemetry") {
    const parsed = parseMetrics(draft.metrics);
    if ("error" in parsed) return parsed.error;
    if (Object.keys(parsed.metrics).length === 0) return "Name at least one metric.";
  } else if (!draft.message.trim()) {
    return "Name the path of the log message.";
  }
  return null;
}

/** The request's `api` and `credentials` (call only once `apiProblem` is null). */
export function apiBody(draft: ApiDraft): Pick<DataSourceRequest, "api" | "credentials"> {
  const parsed = parseMetrics(draft.metrics);
  const metrics = "metrics" in parsed ? parsed.metrics : {};
  const telemetry = draft.lane === "telemetry";
  return {
    api: {
      url: draft.url.trim(),
      auth: draft.auth,
      username: draft.auth === "basic" ? orNull(draft.username) : null,
      api_key_header: draft.apiKeyHeader.trim() || "Authorization",
      api_key_prefix: draft.apiKeyPrefix,
      start_param: draft.startParam.trim(),
      end_param: draft.endParam.trim(),
      window_format: draft.windowFormat,
      next_path: orNull(draft.nextPath),
      mapping: {
        lane: draft.lane,
        records: draft.records.trim(),
        timestamp: draft.timestamp.trim(),
        timestamp_format: draft.timestampFormat,
        timezone: orNull(draft.timezone),
        asset_id: draft.assetId.trim(),
        metrics: telemetry ? metrics : {},
        message: telemetry ? null : orNull(draft.message),
        severity: telemetry ? null : orNull(draft.severity),
      },
    },
    // Only the secret this scheme uses, and only when typed: blank keeps the stored one.
    credentials: {
      // Kept exactly as typed: a password may begin or end with a space.
      password: draft.auth === "basic" && draft.password.trim() ? draft.password : null,
      api_key: draft.auth === "api_key" ? orNull(draft.apiKey) : null,
    },
  };
}
