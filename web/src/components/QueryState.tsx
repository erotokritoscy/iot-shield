import type { UseQueryResult } from "@tanstack/react-query";

import { Notice, Spinner } from "@/components/ui/surfaces";
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
  if (query.isPending) return <Spinner label={label} />;
  if (query.isError) {
    return (
      <Notice tone="error" title="This request failed">
        <p>{describeError(query.error)}</p>
      </Notice>
    );
  }
  return <>{children(query.data)}</>;
}
