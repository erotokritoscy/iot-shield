import type { UseQueryResult } from "@tanstack/react-query";

import { ErrorState, Spinner } from "@/components/ui/surfaces";
import { describeError } from "@/lib/api/http";

/**
 * The in-page failure boundary (M2, M3).
 *
 * The health gate covers "the API never started"; this covers a single endpoint erroring,
 * the API dying between requests, or a response that fails contract validation. All three
 * must render a friendly error rather than a blank screen or an unhandled rejection — which
 * is exactly what `dashboard/app.py`'s page-dispatch `try/except _API_ERRORS` did, and what
 * `test_endpoint_failure_mid_page…` and `test_contract_drift_mid_page…` pin.
 */
export function QueryState<T>({
  query,
  label,
  children,
}: {
  readonly query: UseQueryResult<T, Error>;
  readonly label: string;
  readonly children: (data: T) => React.ReactNode;
}) {
  // A *disabled* query is `isPending` in TanStack v5 — status "pending", fetchStatus "idle" —
  // because it has no data and never asked for any. Rendering a spinner for it would hang
  // forever on any query whose `enabled` is conditional. Nothing has been requested, so
  // nothing is the honest render.
  if (query.isPending && query.fetchStatus === "idle") return null;
  if (query.isPending) return <Spinner label={label} />;
  if (query.isError) {
    // Nothing in this app retries on its own (`retry: false`, no polling, no focus refetch),
    // so without a way out a single transient failure is permanent until the operator guesses
    // that a full page reload is the only escape.
    return (
      <ErrorState
        title="This request failed"
        detail={describeError(query.error)}
        onRetry={() => void query.refetch()}
      />
    );
  }
  return <>{children(query.data)}</>;
}
