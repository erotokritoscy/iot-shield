/**
 * Deleting a saved run (ADR-0046), shared by the Runs list and the Run page so both ask the
 * same question and refresh the same caches.
 */
import { useMutation, useQueryClient } from "@tanstack/react-query";

import { pipelineApi } from "./api/client";
import type { SavedRunInfo } from "./api/types";
import { keys } from "./queries";

/** The confirmation names what goes with the run: deleting it deletes every row it stored. */
export function confirmDeleteRun(run: SavedRunInfo): boolean {
  return window.confirm(
    `Delete the run “${run.name}”? Its ${run.events} events, ${run.malformed} malformed records, ` +
      `${run.detections} detections and ${run.alerts} alerts are deleted with it. This cannot be undone.`,
  );
}

/** Admins only on the server; the caller decides whether to offer it (`isOrgAdmin`). */
export function useDeleteRun(onDeleted?: () => void) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (runId: string) => pipelineApi.deleteRun(runId),
    onSuccess: () => {
      // Its rows are gone too, so every read list may have shrunk — not only this run's.
      void client.invalidateQueries({ queryKey: ["read"] });
      void client.invalidateQueries({ queryKey: keys.savedRuns });
      onDeleted?.();
    },
  });
}
