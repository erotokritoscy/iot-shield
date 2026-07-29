/**
 * "Run a committed bench spec" — the reproducibility mode (E4).
 *
 * The point of the mode: what runs is the file, not a reconstruction of it, so a number on
 * this page and the same number from the CLI are the same measurement. The specs arrive from
 * `/eval/specs` already parsed, and the page forwards the spec object **untouched** — it
 * never names a path, and it never edits what it was given. Composing a lookalike is what
 * silently dropped the `assets:` block and every source and engine parameter, so
 * `config/bench/stub.yaml` and the page's reconstruction published different priority scores
 * (39.46 vs 35.72) with nothing on either surface disclosing which had run.
 */
import * as React from "react";

import { QueryState } from "@/components/QueryState";
import { MultiSelect } from "@/components/ui/controls";
import { Notice } from "@/components/ui/surfaces";
import type { EvalEntry } from "@/lib/api/client";
import { useEvalSpecs } from "@/lib/queries";

import type { ComposeResult } from "./ComposeEntries";

export function useShippedSpecs(corpusName: string, enabled: boolean): ComposeResult {
  const specs = useEvalSpecs(enabled);
  const [chosen, setChosen] = React.useState<string[] | null>(null);

  // Only specs whose source shape matches the corpus, which is the pairing the server
  // derives (`eval.bench`). Offering the rest would put a guaranteed alignment failure in a
  // dropdown.
  const fitting = (specs.data ?? []).filter((item) => (item.corpora ?? []).includes(corpusName));
  const names = fitting.map((item) => item.name);
  const first = names[0];
  const selected =
    chosen === null
      ? first === undefined
        ? []
        : [first]
      : chosen.filter((name) => names.includes(name));
  const picked = fitting.filter((item) => selected.includes(item.name));

  const entries: EvalEntry[] = picked.map((item) => ({
    label: item.name,
    spec: item.spec,
    // No sweep: an artifact is atomic and a committed spec is a fixed measurement. Sweeping
    // one belongs in the compose mode, where the hyperparameters are on screen (ADR-0022).
    sweep: {},
  }));

  // No blockers of its own: "nothing fits this corpus" is stated once, in the element below,
  // and the run button is already gated on there being entries to run.
  const problems: string[] = [];

  const element = (
    <QueryState query={specs} label="Loading committed bench specs…">
      {() =>
        fitting.length === 0 ? (
          <Notice tone="info">
            No committed bench spec matches <code className="font-mono">{corpusName}</code>&apos;s
            input shape. Compose one instead, or add a spec whose <code>sources</code> match.
          </Notice>
        ) : (
          <div className="space-y-3">
            <MultiSelect
              label="Bench specs"
              options={names}
              value={selected}
              onValueChange={setChosen}
              hint="Each becomes one row of the comparison, exactly as the CLI would run it."
            />
            <ul className="space-y-1">
              {picked.map((item) => (
                <li key={item.name} className="caption">
                  <b className="text-ink">{item.name}</b> — {(item.detectors ?? []).join(", ")}
                </li>
              ))}
            </ul>
          </div>
        )
      }
    </QueryState>
  );

  return { element, entries, problems };
}
