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
import { ApiError } from "./api/http";
import type { Lane } from "./api/types";

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
  const error = query.error;
  const planeOff = error instanceof ApiError && error.kind === "status" && error.status === 404;
  const unreachable = query.isError && !planeOff;
  return {
    available: query.isSuccess || unreachable,
    loading: query.isPending,
    unreachable,
    query,
  };
}
