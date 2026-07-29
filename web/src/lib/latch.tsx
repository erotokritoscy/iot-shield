import * as React from "react";

import type { EvalEntry } from "@/lib/api/client";

/** The request a displayed report belongs to — corpus, entries and fit protocol together. */
export interface Latched {
  readonly corpus: string;
  readonly specs: readonly EvalEntry[];
  readonly protocol: string;
}

type Latch = readonly [Latched | null, (next: Latched) => void];

const LatchContext = React.createContext<Latch>([null, () => undefined]);

/**
 * Holds the latched benchmark request above the route that renders it.
 *
 * A benchmark runs for minutes and its result is then cached for the tab's lifetime
 * (`gcTime: Infinity`). Keeping the *key* to that result in the page's own state meant a
 * glance at Triage discarded a finished report still sitting in memory. Mounted on the shell
 * route, this outlives the Evaluate page without outliving the app — which a module-scoped
 * variable would have done, leaking one render's run into the next.
 */
export function LatchProvider({ children }: { readonly children: React.ReactNode }) {
  const [latched, setLatched] = React.useState<Latched | null>(null);
  const value = React.useMemo<Latch>(() => [latched, setLatched], [latched]);
  return <LatchContext.Provider value={value}>{children}</LatchContext.Provider>;
}

export const useLatch = (): Latch => React.useContext(LatchContext);
