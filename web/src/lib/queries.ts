/**
 * Server state, via TanStack Query (D2).
 *
 * This is the React translation of two Streamlit mechanisms at once. `st.cache_resource` on
 * the clients protected "don't rebuild an HTTP client per interaction" — a module-scope
 * `fetch` wrapper is built once, so nothing is needed. `st.cache_data` protected "don't
 * redo work on every rerun" — React does not re-run the page on every interaction, so what
 * is left is ordinary caching of responses, which is what a query cache is.
 *
 * No polling and no websockets (H5): a query refetches when a page mounts or a mutation
 * invalidates it, exactly as a Streamlit rerun re-read the API.
 */
import { useQuery, type UseQueryResult } from "@tanstack/react-query";

import { pipelineApi, readApi } from "./api/client";
import { isPlaneAbsent, isUnreachable } from "./api/http";
import type { Lane } from "./api/types";

/**
 * The one thing a manual refresh must never touch.
 *
 * `invalidateQueries` refetches *active* queries regardless of `staleTime`, so sweeping the
 * cache would re-run whatever benchmark is on screen — minutes of server work from a button
 * labelled "Refresh data". Everything else in this app is a cheap read and is safe.
 */
export const isBenchmarkResult = (key: readonly unknown[]): boolean =>
  key[0] === "pipeline" && key[1] === "eval" && key[2] === "run";

export const keys = {
  readHealth: ["read", "health"] as const,
  summary: ["read", "summary"] as const,
  assets: ["read", "assets"] as const,
  alerts: ["read", "alerts"] as const,
  events: (assetId: string, lane?: Lane) => ["read", "events", assetId, lane ?? "all"] as const,
  detections: (assetId: string, lane?: Lane) =>
    ["read", "detections", assetId, lane ?? "all"] as const,
  alertDetections: (alertId: string) => ["read", "alert-detections", alertId] as const,
  malformed: ["read", "malformed"] as const,
  modules: ["pipeline", "modules"] as const,
  evalCorpora: ["pipeline", "eval", "corpora"] as const,
  evalDetectors: ["pipeline", "eval", "detectors"] as const,
  evalSpecs: ["pipeline", "eval", "specs"] as const,
  /**
   * A benchmark result, keyed by the exact request that produced it. Listed here with the
   * rest rather than inlined at its call site: it was the one key defined outside this table,
   * so it was also the one key a scope-level invalidation would silently miss.
   */
  evalRun: (selection: string) => ["pipeline", "eval", "run", selection] as const,
};

export const useReadHealth = () => useQuery({ queryKey: keys.readHealth, queryFn: () => readApi.health() });
export const useSummary = () => useQuery({ queryKey: keys.summary, queryFn: () => readApi.summary() });
export const useAssets = () => useQuery({ queryKey: keys.assets, queryFn: () => readApi.assets() });
export const useAlerts = () => useQuery({ queryKey: keys.alerts, queryFn: () => readApi.alerts() });
export const useMalformed = () =>
  useQuery({ queryKey: keys.malformed, queryFn: () => readApi.malformed() });

export const useEvents = (assetId: string | undefined, lane?: Lane) =>
  useQuery({
    queryKey: keys.events(assetId ?? "", lane),
    queryFn: () => readApi.events({ ...(assetId ? { assetId } : {}), ...(lane ? { lane } : {}) }),
    enabled: Boolean(assetId),
  });

export const useDetections = (assetId: string | undefined, lane?: Lane) =>
  useQuery({
    queryKey: keys.detections(assetId ?? "", lane),
    queryFn: () =>
      readApi.detections({ ...(assetId ? { assetId } : {}), ...(lane ? { lane } : {}) }),
    enabled: Boolean(assetId),
  });

export const useAlertDetections = (alertId: string | undefined) =>
  useQuery({
    queryKey: keys.alertDetections(alertId ?? ""),
    queryFn: () => readApi.alertDetections(alertId as string),
    enabled: Boolean(alertId),
  });

export const useModules = (enabled = true) =>
  useQuery({ queryKey: keys.modules, queryFn: () => pipelineApi.modules(), enabled });

export const useEvalDetectors = (enabled = true) =>
  useQuery({ queryKey: keys.evalDetectors, queryFn: () => pipelineApi.evalDetectors(), enabled });

/**
 * The bench-spec list is fetched only by the mode that uses it — the old page made exactly
 * this call lazily, because `/eval/specs` re-walks the corpus root and re-parses every bench
 * YAML on each request (measured ~21 ms) and the other mode never needs it.
 */
export const useEvalSpecs = (enabled: boolean) =>
  useQuery({ queryKey: keys.evalSpecs, queryFn: () => pipelineApi.evalSpecs(), enabled });

export const useCorpora = (enabled = true) =>
  useQuery({ queryKey: keys.evalCorpora, queryFn: () => pipelineApi.corpora(), enabled });

export interface EvalPlane {
  /** Whether the Evaluate mode is offered at all. */
  readonly available: boolean;
  /** Still probing — render neither state yet, so the nav does not flicker. */
  readonly loading: boolean;
  /** The plane is enabled but the Pipeline API did not answer: the upload fallback (D6). */
  readonly unreachable: boolean;
  /**
   * The API answered, and the answer was unusable: a 5xx, an auth redirect, or a response
   * the zod schemas rejected. Distinct from `unreachable` because the remedy is opposite —
   * "wait and retry" versus "these two versions disagree" — and ADR-0031 D4 promises drift
   * fails loudly rather than reading as an outage.
   */
  readonly broken: boolean;
  readonly query: UseQueryResult<Awaited<ReturnType<typeof pipelineApi.corpora>>, Error>;
}

/**
 * The Evaluate gate (D5) — probe, don't flag.
 *
 * A static SPA cannot read `IOTSHIELD_EVAL_UI`, and it never needed to: the real gate was
 * always `IOTSHIELD_EVAL_API`, because without it the `/eval` router is *absent*, not
 * guarded (ADR-0021 D1). So a 404 is the deployment saying "this image has no research
 * plane" — hide the mode, exactly as the unset UI flag did.
 *
 * A *transport* failure is a different fact and is deliberately treated differently: the
 * Pipeline API is down or still starting, which is the situation the old page degraded
 * through rather than disappeared in. The mode stays visible and the page offers the saved
 * report (D6), preserving the CLI → file → dashboard path (E1). See DEVIATIONS.md.
 */
export function useEvalPlane(): EvalPlane {
  const query = useCorpora();
  // Three states, not two. `http.ts` defines `isUnreachable` and `isPlaneAbsent` as
  // deliberately disjoint, "kept beside its sibling so the two cannot drift into
  // overlapping" — and this used NEITHER, taking everything-except-404 as unreachable. So a
  // 500, an auth proxy's 401, and a **zod contract failure** all rendered "Pipeline API
  // unreachable — start it with IOTSHIELD_EVAL_API=1": a fabricated remedy for a flag the
  // operator already set, with the real error (which names the URL and the drifting field)
  // shown nowhere. Contract drift failing loudly at this one boundary is the whole claim of
  // ADR-0031 D4, so it must not be swallowed as a transport blip (ADR-0033).
  const unreachable = query.isError && isUnreachable(query.error);
  const broken = query.isError && !isPlaneAbsent(query.error) && !unreachable;
  return {
    // `broken` keeps the mode VISIBLE so the page can render the real error, rather than
    // hiding the plane (which would read as "this deployment has none") or claiming the API
    // is down (which would read as "wait and retry").
    available: query.isSuccess || unreachable || broken,
    loading: query.isPending,
    unreachable,
    broken,
    query,
  };
}
