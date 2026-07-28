import { Outlet } from "react-router-dom";

import { ErrorState, Spinner } from "@/components/ui/surfaces";
import { describeError } from "@/lib/api/http";
import { useReadHealth } from "@/lib/queries";

/**
 * The read-side health gate (M1).
 *
 * Monitor's three views all read the same API, so one check up front beats three identical
 * failures on the page. Operate and Evaluate deliberately sit *outside* this gate — Operate
 * talks only to the Pipeline API and reports its own reachability, and Evaluate degrades to
 * a saved report — which is how `dashboard/app.py: main` routed them, and what keeps the
 * compose page usable while the read plane is down.
 */
export function MonitorGate() {
  const health = useReadHealth();

  if (health.isPending) return <Spinner label="Connecting to the read API…" />;
  if (health.isError) {
    return (
      <ErrorState
        title="Read API unreachable"
        detail={
          <>
            Nothing answered at <code className="font-mono">/api</code> ({describeError(health.error)}).
            Start it with:
          </>
        }
        command="IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api"
      />
    );
  }
  return <Outlet />;
}
