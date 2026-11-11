/**
 * The progress bar a run shows while `POST /runs` is still going (ADR-0051).
 *
 * The run is one synchronous request, so the page names a `progress_id` of its own when it
 * posts it and polls `GET /runs/progress/{id}` once a second until the request answers. A 404
 * is "not started yet" (the server is still checking the chain or counting a pull's rows), not
 * an error, and a failed poll never fails the run: the bar is a view of the run, never a part
 * of it.
 *
 * One bar across the three phases, each given a share of it: reading the sources is most of a
 * pull's time, judging most of the rest, and saving is one commit with no count — so the bar
 * moves through reading and judging by their counts and holds while saving.
 */

import { useQuery } from "@tanstack/react-query";

import { isPlaneAbsent } from "@/lib/api/http";
import { pipelineApi } from "@/lib/api/client";
import type { ProgressInfo } from "@/lib/api/types";

/** Where each phase starts on the bar, and where it ends. */
const SPAN: Readonly<Record<ProgressInfo["phase"], readonly [number, number]>> = {
  starting: [0, 0.02],
  reading: [0.02, 0.6],
  judging: [0.6, 0.95],
  saving: [0.95, 0.99],
  finished: [1, 1],
  failed: [1, 1],
};

/** A fresh id for one run. `crypto.randomUUID` needs a secure context, which plain http over a LAN address is not. */
export function newProgressId(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6]! & 0x0f) | 0x40;
  bytes[8] = (bytes[8]! & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

/** The share of the whole run done, 0–1; null while there is nothing to measure it by. */
export function progressFraction(info: ProgressInfo | null): number | null {
  if (info === null) return null;
  const [from, to] = SPAN[info.phase];
  if (info.phase === "reading" || info.phase === "judging") {
    if (info.total === null || info.total <= 0) return from;
    return from + (to - from) * Math.min(info.done / info.total, 1);
  }
  return from;
}

function elapsed(seconds: number): string {
  const whole = Math.floor(seconds);
  return `${Math.floor(whole / 60)}:${String(whole % 60).padStart(2, "0")}`;
}

const count = (value: number) => value.toLocaleString("en-US");

/** What the bar says: the phase, its count where it has one, and the time so far. */
export function progressText(info: ProgressInfo | null): string {
  if (info === null) return "Starting…";
  const time = ` · ${elapsed(info.elapsed_s)} elapsed`;
  switch (info.phase) {
    case "starting":
      return `Checking the chain and counting the data…${time}`;
    case "reading":
      return info.total === null
        ? `Reading ${count(info.done)} rows…${time}`
        : `Reading ${count(info.done)} / ${count(info.total)} rows${time}`;
    case "judging":
      return `Judging ${count(info.done)} / ${count(info.total ?? info.done)} events${time}`;
    case "saving":
      return `Saving the results…${time}`;
    case "finished":
      return `Finishing…${time}`;
    case "failed":
      return `Stopping…${time}`;
  }
}

export function RunProgress({ progressId }: { readonly progressId: string }) {
  const poll = useQuery({
    queryKey: ["pipeline", "runProgress", progressId],
    queryFn: async () => {
      try {
        return await pipelineApi.runProgress(progressId);
      } catch (error) {
        if (isPlaneAbsent(error)) return null; // not started yet
        throw error;
      }
    },
    refetchInterval: 1000,
    retry: false,
    gcTime: 0,
  });
  // A poll that fails keeps the last state on screen rather than replacing the bar with an error.
  const info = poll.data ?? null;
  const fraction = progressFraction(info);
  const percent = fraction === null ? null : Math.round(fraction * 100);
  return (
    <div className="w-full max-w-xl space-y-1.5" data-testid="compose_progress">
      <div
        className="h-2 w-full overflow-hidden rounded-full bg-raised"
        role="progressbar"
        aria-label="Run progress"
        aria-valuemin={0}
        aria-valuemax={100}
        {...(percent === null ? {} : { "aria-valuenow": percent })}
      >
        <div
          className="h-full rounded-full bg-accent transition-[width] duration-700 ease-out"
          style={{ width: `${percent ?? 2}%` }}
        />
      </div>
      <p className="caption" role="status" data-testid="compose_progress_text">
        {progressText(info)}
        {percent === null ? null : <span className="text-muted"> · {percent}%</span>}
      </p>
    </div>
  );
}
