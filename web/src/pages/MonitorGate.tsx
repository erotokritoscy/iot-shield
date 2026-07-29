import { Outlet } from "react-router-dom";

import { ErrorState, Spinner } from "@/components/ui/surfaces";
import { describeError, isUnreachable } from "@/lib/api/http";
import { useReadHealth } from "@/lib/queries";

/**
 * The read-side health gate (M1).
 *
 * Monitor's three views all read the same API, so one check up front beats three identical
 * failures on the page. Operate and Evaluate deliberately sit *outside* this gate — Operate
 * talks only to the Pipeline API and reports its own reachability, and Evaluate degrades to
 * a saved report — which is how `dashboard/app.py: main` routed them, and what keeps the
 * compose page usable while the read plane is down.
 *
 * Two failures, two remedies. `/health` runs `SELECT 1`, so it proves *database*
 * connectivity and not merely liveness — which means a perfectly healthy API in front of a
 * dead Postgres answers **500**. Printing "nothing answered, start the API" at that moment
 * sends the operator to restart a service that is already running, and the command offered
 * would repoint the dashboard at an empty SQLite file. So the gate reads the *kind* of
 * failure and names only the cause it has evidence for.
 */
export function MonitorGate() {
  const health = useReadHealth();

  if (health.isPending) return <Spinner label="Connecting to the read API…" />;
  if (health.isError) {
    const retry = () => void health.refetch();
    return isUnreachable(health.error) ? (
      <ErrorState
        title="Read API unreachable"
        detail={
          <>
            Nothing answered at <code className="font-mono">/api</code> ({describeError(health.error)}).
            Start it with:
          </>
        }
        command="IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api"
        onRetry={retry}
      />
    ) : (
      <ErrorState
        title="Read API cannot reach its database"
        detail={
          <>
            The API answered, so it is running — but its health check could not query the
            database ({describeError(health.error)}). Check Postgres, not the API:
          </>
        }
        command="docker compose up -d postgres && docker compose logs postgres"
        onRetry={retry}
      />
    );
  }
  return <Outlet />;
}
