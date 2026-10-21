/**
 * The replay's controls and its "arriving now" feed (ADR-0044 amendment). The feed is the
 * newest few items that have "arrived" by the replay's point in time, newest on top, each
 * sliding in as it appears.
 */
import { Pause, Play, RotateCcw } from "lucide-react";

import { Button, Select } from "@/components/ui/controls";
import { InfoTip } from "@/components/ui/info";
import { formatUtc } from "@/lib/format";
import { SPEEDS, type Replay, type Speed } from "@/lib/replay";

const FEED_SIZE = 6;

function nowLabel(now: string): string {
  return now.length === 10 ? now : formatUtc(now, "minute");
}

export function ReplayControls({ replay, noun }: { readonly replay: Replay; readonly noun: string }) {
  const progress = replay.cursor === null ? 0 : ((replay.cursor + 1) / Math.max(1, replay.steps)) * 100;
  return (
    <div className="space-y-2" data-testid="replay">
      <div className="flex flex-wrap items-center gap-2">
        {replay.playing ? (
          <Button size="sm" data-testid="replay_pause" onClick={replay.pause}>
            <Pause className="h-3.5 w-3.5" aria-hidden /> Pause
          </Button>
        ) : (
          <Button
            size="sm"
            variant="primary"
            data-testid="replay_play"
            disabled={replay.steps === 0}
            onClick={replay.play}
          >
            <Play className="h-3.5 w-3.5" aria-hidden /> {replay.cursor === null ? "Replay" : "Resume"}
          </Button>
        )}
        <Button
          size="sm"
          variant="ghost"
          data-testid="replay_reset"
          disabled={replay.cursor === null}
          onClick={replay.reset}
        >
          <RotateCcw className="h-3.5 w-3.5" aria-hidden /> Reset
        </Button>
        <Select
          aria-label="Replay speed"
          data-testid="replay_speed"
          className="w-auto"
          options={SPEEDS.map(String)}
          labelFor={(option) => `${option}×`}
          value={String(replay.speed)}
          onChange={(event) => replay.setSpeed(Number(event.target.value) as Speed)}
        />
        <InfoTip label="Replay">
          Plays the chosen range forward in time, one {replay.steps > 0 ? "bucket" : "step"} at a time, as if the {noun}{" "}
          were arriving now. It replays stored results — nothing new is fetched — and when it ends the page shows the
          whole range again.
        </InfoTip>
        <span className="caption" aria-live="polite" data-testid="replay_now">
          {replay.now ? `Replaying · ${nowLabel(replay.now)} UTC` : ""}
        </span>
      </div>
      {replay.cursor !== null ? (
        <div className="h-1 w-full overflow-hidden rounded bg-raised" aria-hidden>
          <div className="h-full bg-accent transition-[width] duration-200" style={{ width: `${progress}%` }} />
        </div>
      ) : null}
    </div>
  );
}

export interface FeedItem {
  readonly key: string;
  readonly at: string;
  readonly label: React.ReactNode;
  readonly detail: React.ReactNode;
}

/** The newest items that have arrived by now, newest first. Shown only while replaying. */
export function ArrivingFeed({ items, title }: { readonly items: readonly FeedItem[]; readonly title: string }) {
  const newest = items.slice(-FEED_SIZE).reverse();
  return (
    <div className="rounded-lg border border-line bg-surface p-3" data-testid="replay_feed">
      <p className="field-label">{title}</p>
      {newest.length === 0 ? (
        <p className="caption">Nothing yet…</p>
      ) : (
        <ul className="space-y-1.5">
          {newest.map((item) => (
            <li key={item.key} className="feed-in flex min-w-0 items-baseline gap-2 text-xs">
              <span className="tabular shrink-0 text-muted">{formatUtc(item.at, "minute")}</span>
              <span className="shrink-0">{item.label}</span>
              <span className="min-w-0 truncate text-dim">{item.detail}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
