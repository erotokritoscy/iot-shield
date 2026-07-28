/**
 * The one door into both APIs (D4).
 *
 * The old `ApiClient`/`ControlClient` parsed every response through the pydantic contracts,
 * so API/UI drift failed loudly at a single boundary instead of rendering wrong (ADR-0008).
 * TypeScript types are erased at runtime and cannot carry that on their own, so this module
 * restores the property: every response goes through a zod schema here, and *everything*
 * that can go wrong — transport, status, non-JSON body, a body that no longer matches the
 * contract — surfaces as one typed `ApiError` the pages render as a friendly state.
 *
 * All paths are relative. `/api` and `/pipeline` are same-origin prefixes the server routes
 * (nginx in production, the Vite proxy in dev), which is what keeps both FastAPI apps free
 * of CORS — the stance `api/app.py` deliberately takes (D1).
 */
import type { ZodType } from "zod";

export const READ_BASE = "/api";
export const PIPELINE_BASE = "/pipeline";

export type ApiErrorKind =
  /** Nothing answered: the service is down, or DNS/proxy failed. */
  | "transport"
  /** It answered, with a non-2xx status. */
  | "status"
  /** It answered 2xx with something that is not JSON. */
  | "parse"
  /** It answered with JSON that no longer matches the contract — the ADR-0008 boundary. */
  | "contract";

export class ApiError extends Error {
  readonly kind: ApiErrorKind;
  readonly status: number | undefined;
  readonly path: string;

  constructor(kind: ApiErrorKind, path: string, message: string, status?: number) {
    super(message);
    this.name = "ApiError";
    this.kind = kind;
    this.path = path;
    this.status = status;
  }
}

/** Is this the specific "the service isn't there" failure a health gate looks for? */
export function isUnreachable(error: unknown): boolean {
  return error instanceof ApiError && (error.kind === "transport" || error.status === 404);
}

/** A short, human sentence for an error state — never a stack trace (M1–M3). */
export function describeError(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof Error) return error.message;
  return String(error);
}

/**
 * FastAPI reports refusals in `detail`, which may be a string, a list of validation errors,
 * or the structured `{message, issues}` the Pipeline API raises for an incompatible chain.
 * Surfacing the adapter's own carefully worded message is the point — "min_count must be
 * >= 2" reaching nobody is exactly what `api/control.py` fixed on its side.
 */
function detailOf(body: unknown): string | null {
  if (body === null || typeof body !== "object") return null;
  const detail = (body as { detail?: unknown }).detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const parts = detail
      .map((item) => {
        if (item && typeof item === "object" && "msg" in item) {
          const loc = Array.isArray((item as { loc?: unknown }).loc)
            ? ((item as { loc: unknown[] }).loc.join(".") as string)
            : "";
          return loc ? `${loc}: ${String((item as { msg: unknown }).msg)}` : String((item as { msg: unknown }).msg);
        }
        return JSON.stringify(item);
      })
      .filter(Boolean);
    return parts.length > 0 ? parts.join("; ") : null;
  }
  if (detail && typeof detail === "object") {
    const message = (detail as { message?: unknown }).message;
    const issues = (detail as { issues?: unknown }).issues;
    const rendered = Array.isArray(issues)
      ? issues
          .map((issue) =>
            issue && typeof issue === "object" && "message" in issue
              ? `[${String((issue as { stage?: unknown }).stage ?? "")}] ${String((issue as { message: unknown }).message)}`
              : String(issue),
          )
          .join("; ")
      : "";
    return [typeof message === "string" ? message : null, rendered || null]
      .filter(Boolean)
      .join(" — ");
  }
  return null;
}

export interface RequestOptions<T> {
  readonly schema: ZodType<T>;
  readonly method?: "GET" | "POST";
  readonly body?: unknown;
  readonly query?: Record<string, string | number | boolean | undefined>;
  readonly signal?: AbortSignal;
}

function withQuery(path: string, query: RequestOptions<unknown>["query"]): string {
  if (!query) return path;
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered ? `${path}?${rendered}` : path;
}

export async function apiRequest<T>(base: string, path: string, options: RequestOptions<T>): Promise<T> {
  const url = withQuery(`${base}${path}`, options.query);
  let response: Response;
  try {
    response = await fetch(url, {
      method: options.method ?? "GET",
      headers: options.body === undefined ? undefined : { "Content-Type": "application/json" },
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      ...(options.signal ? { signal: options.signal } : {}),
    });
  } catch (cause) {
    throw new ApiError("transport", url, `could not reach ${url} (${describeError(cause)})`);
  }

  let payload: unknown;
  let raw: string;
  try {
    raw = await response.text();
    payload = raw.length === 0 ? null : JSON.parse(raw);
  } catch {
    if (!response.ok) {
      throw new ApiError("status", url, `${url} returned HTTP ${response.status}`, response.status);
    }
    throw new ApiError("parse", url, `${url} returned a body that is not JSON`, response.status);
  }

  if (!response.ok) {
    const detail = detailOf(payload);
    throw new ApiError(
      "status",
      url,
      detail ? `${url} refused the request: ${detail}` : `${url} returned HTTP ${response.status}`,
      response.status,
    );
  }

  const parsed = options.schema.safeParse(payload);
  if (!parsed.success) {
    // Contract drift: the response is well-formed JSON that no longer matches what this
    // client was built against. Loud here beats rendering wrong downstream (ADR-0008).
    const first = parsed.error.issues[0];
    const where = first && first.path.length > 0 ? ` at ${first.path.join(".")}` : "";
    throw new ApiError(
      "contract",
      url,
      `${url} returned data that does not match the contract${where}: ${first?.message ?? "unknown field error"}`,
      response.status,
    );
  }
  return parsed.data;
}
