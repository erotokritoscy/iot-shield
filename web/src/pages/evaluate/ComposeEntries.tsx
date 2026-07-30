/**
 * "Compose my own" — the exploratory mode (E5–E9).
 *
 * The unit is the **entry**: the detectors that run *together* in one pipeline, which is
 * what makes cross-lane corroboration measurable. One detector per entry is the classic
 * head-to-head; several in one entry approximates `config/bench/trimodal_all.yaml`.
 *
 * Exposed as a hook returning its own element so the page can render the shared footer (spec
 * echo, run button, report) beside whichever mode is active, without either mode's state
 * leaking into the other's.
 */
import * as React from "react";

import { ArtifactField } from "@/components/ArtifactField";
import { ParamFields } from "@/components/ParamFields";
import {
  Field,
  MultiSelect,
  RadioGroup,
  RowCount,
  Select,
  TextInput,
} from "@/components/ui/controls";
import { Collapsible, Notice, Section } from "@/components/ui/surfaces";
import type { EvalEntry } from "@/lib/api/client";
import type { CorpusSummary, DetectorSummary, ModuleInfo, ParamInfo } from "@/lib/api/types";
import { initialArtifact, tunablesOf } from "@/lib/artifact";
import { initialValues, submittedParams, sweepable, type ParamValue, type ParamValues } from "@/lib/params";

import {
  composedSpec,
  labelled,
  NO_SWEEP,
  normalizerFor,
  SCRATCH,
  sweepValues,
  TRAINED,
  type SweptValue,
} from "./lib";

const MAX_ENTRIES = 6;

interface PlanState {
  readonly mode: string;
  readonly artifact: string;
  readonly params: ParamValues;
}

interface EntryState {
  readonly selected: string[];
  readonly plans: Record<string, PlanState>;
  readonly sweepKey: string;
  readonly sweepText: string;
  /** Set when a configured sweep was cleared because its key stopped being offered (E7). */
  readonly clearedSweep: string | null;
}

function defaultPlan(summary: DetectorSummary): PlanState {
  return {
    mode: TRAINED,
    // `lib/artifact.ts` owns which id a form opens on and why it preselects where every other
    // closed vocabulary does not; the Compose page reads the same rule (ADR-0036).
    artifact: initialArtifact(summary),
    params: initialValues(tunablesOf(summary)),
  };
}

const emptyEntry = (first: string | undefined): EntryState => ({
  selected: first === undefined ? [] : [first],
  plans: {},
  sweepKey: NO_SWEEP,
  sweepText: "",
  clearedSweep: null,
});

interface Plan {
  readonly detector: { name: string; params: Record<string, unknown> };
  readonly problem: string | null;
  readonly sweepable: ParamInfo[];
}

/**
 * One detector's benchmark configuration, what blocks it, and what it offers to sweep (E6).
 *
 * The ADR-0022 split as the page's central choice. A detector with no cached-fit param (a
 * rule, like `stub_threshold`) shows only its knobs; one that has an artifact shows the two
 * modes, and picking "from scratch" makes the fit protocol load-bearing — which the returned
 * problem says out loud, because an untrained model under protocol `none` scores a row of
 * zeros that reads like a bad detector rather than a skipped one.
 *
 * The third return value is what this detector contributes to its *entry's* sweep menu:
 * empty in trained mode, since an artifact is atomic and sweeping a knob beside it is the
 * request construction refuses.
 */
function planFor(summary: DetectorSummary, state: PlanState, protocol: string): Plan {
  const tunables = tunablesOf(summary);
  if (summary.artifact_param === null || summary.artifact_param === undefined) {
    return {
      detector: { name: summary.name, params: submittedParams(summary.params, state.params) },
      problem: null,
      sweepable: sweepable(tunables),
    };
  }
  if (state.mode === TRAINED) {
    return {
      detector: { name: summary.name, params: { [summary.artifact_param]: state.artifact } },
      problem:
        state.artifact.trim() === ""
          ? `${summary.name} is set to use trained weights but no artifact is named. Enter an artifact id, or switch it to fit from scratch.`
          : null,
      sweepable: [],
    };
  }
  return {
    detector: { name: summary.name, params: submittedParams(tunables, state.params) },
    problem:
      protocol === "none"
        ? `${summary.name} has no trained weights in this mode, and the fit protocol is \`none\` — it would judge nothing. Pick \`prefix\` above, or switch it back to trained weights.`
        : null,
    sweepable: sweepable(tunables),
  };
}

/**
 * The entry's sweepable knobs, keyed by the text the server will parse (E7).
 *
 * Qualified with the detector name as soon as an entry holds more than one, which is what
 * makes every knob reachable. This menu used to *hide* any name two of the entry's detectors
 * shared — `threshold` on `stub_threshold + flow_burst` — mirroring a server refusal, so the
 * dropdown shrank from four entries to one with no explanation. Naming the detector removes
 * the ambiguity instead of routing around it. A single-detector entry keeps bare names, so
 * `stub_threshold[threshold=40]` reads exactly as it always has and no published label moves.
 */
export function offeredKnobs(byDetector: readonly (readonly [string, ParamInfo[]])[]): Record<string, ParamInfo> {
  const qualify = byDetector.length > 1;
  const offered: Record<string, ParamInfo> = {};
  for (const [name, params] of byDetector) {
    for (const param of params) {
      offered[qualify ? `${name}.${param.name}` : param.name] = param;
    }
  }
  return offered;
}

export interface ComposeResult {
  readonly element: React.ReactNode;
  readonly entries: EvalEntry[];
  readonly problems: string[];
}

export function useComposeEntries({
  corpus,
  protocol,
  detectors,
  normalizers,
}: {
  readonly corpus: CorpusSummary | undefined;
  readonly protocol: string;
  readonly detectors: readonly DetectorSummary[];
  readonly normalizers: readonly ModuleInfo[];
}): ComposeResult {
  // Ineligible detectors are **marked, not removed** (ADR-0035 D2 amendment): `offeredNames` is
  // every registered detector, and eligibility now decides only what may be *defaulted* to.
  //
  // Filtering was the wrong half of D2's fix. `meter_iforest` requires a meter payload and no
  // corpus in this product carries one, so the filter did not hide a bad *combination* — it
  // removed that detector from every corpus at once, which is a deletion. The page then reported
  // five detectors where the registry has six, and the reason (there is no meter corpus) was
  // hidden along with it. The bug D2 actually fixed was that `meter_iforest` sorted first and
  // became entry 1's *default*, so a meaningless benchmark ran without anyone choosing it;
  // defaulting from the eligible subset alone is the narrow fix for that.
  //
  // Running one anyway is a measurement rather than a mistake, which is why nothing here refuses
  // it: `evaluate` carries both the guard's declaration-level warning and the detector's own
  // `why_silent` into the case notes, so the row of zeros arrives with two explanations
  // ("reported, not refused" — ADR-0026 amendment, ADR-0027 D8).
  //
  // The REASON is the primitive and eligibility is derived from it, rather than the rule being
  // written twice — once positively to filter and once negated to annotate. `can_judge`'s own
  // docstring is about exactly that hazard ("a rule written twice is a rule that can differ
  // twice"), and two spellings inside one component could mark a detector while still
  // preselecting it.
  const notes = React.useMemo(() => {
    // Both sides of this comparison arrive decoded from /eval, so the page never parses a
    // `lane:`/`payload:` prefix (H6). TWO axes, and the second is why this list used to lie
    // (ADR-0035 D2): a lane is satisfied by any source of that modality, a payload only by a
    // source carrying the typed vocabulary. The lane test is `some` and the payload test is
    // `every`, which is not an inconsistency — a detector fires if ANY of its lanes is present,
    // but it needs ALL of the vocabularies it reads through.
    const lanes = new Set(corpus?.lanes ?? []);
    const payloads = new Set(corpus?.payloads ?? []);
    const feeds = [...lanes].join(", ") || "none";
    const carries = [...payloads].join(", ") || "none";
    const reasons: Record<string, string> = {};
    for (const item of detectors) {
      // Lane first, then payload: first-unmet-wins, the rule the guard already applies. A
      // detector on a foreign lane is *also* missing that lane's payload, and naming both
      // faults would ask someone to fix two things to repair one.
      if (!item.lanes.some((lane) => lanes.has(lane))) {
        reasons[item.name] = `reads lane ${item.lanes.join("/")}, this corpus feeds ${feeds}`;
        continue;
      }
      const missing = item.payloads.filter((payload) => !payloads.has(payload));
      if (missing.length > 0) {
        reasons[item.name] = `needs payload ${missing.join(", ")}, this corpus carries ${carries}`;
      }
    }
    return reasons;
  }, [detectors, corpus]);

  const offeredNames = React.useMemo(() => detectors.map((item) => item.name), [detectors]);
  const eligible = React.useMemo(
    () => offeredNames.filter((name) => notes[name] === undefined),
    [offeredNames, notes],
  );
  const eligibleKey = eligible.join(" ");

  const [engine, setEngine] = React.useState("weighted");
  const [count, setCount] = React.useState<number | null>(null);
  const [entries, setEntries] = React.useState<EntryState[]>([]);

  // Reconcile the entry list with the catalogue and the count. Selections are **pruned, not
  // reset**: a detector the registry no longer offers cannot be built, so it cannot stay.
  //
  // Pruned against what is *constructible*, not against what is eligible. Eligibility is a
  // statement about this corpus, and an ineligible pick is now a legal, explained choice — so
  // pruning on it would let the list say "selectable" while this said no. It also fired on every
  // re-run of this effect, not only on a corpus switch: bumping the entry count silently dropped
  // a deliberate pick, with none of the "here is what I cleared" courtesy the sweep path gives.
  React.useEffect(() => {
    // Nothing to default *to* until the detector catalogue has arrived: creating entries against
    // an empty list would fix the count at 1 and leave entry 1 blank, which is the opposite of
    // "one detector per entry, walking down the list". The test is the catalogue rather than
    // eligibility, so a corpus no detector can feed still gets its entry rows and therefore
    // still shows the marked list, instead of the page going blank.
    if (detectors.length === 0) return;
    const wanted = count ?? Math.min(2, Math.max(1, eligible.length));
    if (count === null) setCount(wanted);
    setEntries((previous) => {
      const next: EntryState[] = [];
      for (let index = 0; index < wanted; index += 1) {
        const existing = previous[index];
        // Default to one detector per entry, walking down the ELIGIBLE list, so a fresh page is
        // the head-to-head everyone expects and multi-detector entries are opt-in. This is the
        // line that keeps ADR-0035 D2's bug fixed.
        if (existing === undefined) {
          next.push(emptyEntry(eligible[index]));
          continue;
        }
        const pruned = existing.selected.filter((name) => offeredNames.includes(name));
        next.push(
          pruned.length === existing.selected.length
            ? existing
            : { ...existing, selected: pruned },
        );
      }
      const same =
        next.length === previous.length && next.every((entry, index) => entry === previous[index]);
      return same ? previous : next;
    });
  }, [count, eligibleKey, eligible, detectors, offeredNames]);

  const summaries = React.useMemo(
    () => new Map(detectors.map((item) => [item.name, item])),
    [detectors],
  );

  const updateEntry = (index: number, patch: Partial<EntryState>) =>
    setEntries((previous) =>
      previous.map((entry, position) => (position === index ? { ...entry, ...patch } : entry)),
    );

  const updatePlan = (index: number, name: string, patch: Partial<PlanState>) =>
    setEntries((previous) =>
      previous.map((entry, position) => {
        if (position !== index) return entry;
        const summary = summaries.get(name);
        const base = entry.plans[name] ?? (summary ? defaultPlan(summary) : undefined);
        if (base === undefined) return entry;
        return { ...entry, plans: { ...entry.plans, [name]: { ...base, ...patch } } };
      }),
    );

  // --- derive everything the page needs from the state above -------------------
  const derived = entries.map((entry) => {
    const plans: { name: string; summary: DetectorSummary; state: PlanState; plan: Plan }[] = [];
    for (const name of entry.selected) {
      const summary = summaries.get(name);
      if (summary === undefined) continue;
      const state = entry.plans[name] ?? defaultPlan(summary);
      plans.push({ name, summary, state, plan: planFor(summary, state, protocol) });
    }
    const offered = offeredKnobs(plans.map((item) => [item.name, item.plan.sweepable] as const));
    const options = [NO_SWEEP, ...Object.keys(offered)];
    const parsed = sweepValues(entry.sweepText);
    const sweep: Record<string, SweptValue[]> =
      entry.sweepKey !== NO_SWEEP && options.includes(entry.sweepKey) && parsed.length > 0
        ? { [entry.sweepKey]: parsed }
        : {};
    return {
      entry,
      plans,
      offered,
      options,
      sweep,
      parsed,
      label: entry.selected.join(" + "),
      problems: plans.map((item) => item.plan.problem).filter((problem): problem is string => problem !== null),
    };
  });

  // Editing an entry changes which keys exist. A configured sweep that silently vanished
  // would let the run measure an *unswept* spec — the user gets a number they did not ask
  // for. So it is cleared deliberately, **with a message naming what was cleared**.
  const staleKey = derived
    .map((item) => (item.entry.sweepKey !== NO_SWEEP && !item.options.includes(item.entry.sweepKey) ? item.entry.sweepKey : ""))
    .join(" ");
  React.useEffect(() => {
    if (staleKey.replace(/ /g, "") === "") return;
    setEntries((previous) =>
      previous.map((entry, index) => {
        const stale = staleKey.split(" ")[index];
        return stale ? { ...entry, sweepKey: NO_SWEEP, clearedSweep: stale } : entry;
      }),
    );
  }, [staleKey]);

  const plan = labelled(
    derived.map((item) => ({
      label: item.label,
      detectors: item.plans.map((detector) => detector.plan.detector),
      sweep: item.sweep,
    })),
  );

  const built: EvalEntry[] = plan.map((item) => ({
    label: item.label,
    spec: composedSpec(item.detectors, engine, corpus?.source_formats ?? [], normalizers),
    sweep: item.sweep,
  }));

  const problems = derived.flatMap((item) => item.problems);

  // A corpus format no registered normalizer accepts produced `{"normalizer": ""}` in the
  // spec, with Run still enabled: the server then refused a normalizer with no name, which
  // reads like a bug in the request rather than a gap in the registry.
  const unreadable = (corpus?.source_formats ?? []).filter(
    (format) => normalizerFor(format, normalizers) === "",
  );
  if (normalizers.length > 0 && unreadable.length > 0) {
    problems.push(
      `No registered normalizer reads ${unreadable.join(", ")}, which this corpus is stored in.`,
    );
  }

  const fittable = new Set(detectors.filter((item) => item.fittable).map((item) => item.name));
  const picked = new Set(plan.flatMap((item) => item.detectors.map((detector) => detector.name)));
  const noneFittable =
    protocol !== "none" && picked.size > 0 && [...picked].every((name) => !fittable.has(name));

  const element = (
    <div className="space-y-6">
      {/* A banner above a populated list, not a replacement for it. Nothing is chosen for
          the operator here, but every detector stays visible and says why it will not fire
          — which is also what would have made "no corpus carries a meter payload" legible
          instead of leaving the page looking like the registry is smaller than it is. */}
      {eligible.length === 0 ? (
        <Notice tone="info">
          No registered detector can read this corpus. It feeds lane(s){" "}
          <b>{(corpus?.lanes ?? []).join(", ") || "none"}</b> carrying payload(s){" "}
          <b>{(corpus?.payloads ?? []).join(", ") || "none"}</b>, and a detector needs one of
          those lanes and all of the payloads it reads through.
        </Notice>
      ) : null}

      <Field label="Alert engine" htmlFor="eval_engine" className="max-w-xs">
        <Select
          id="eval_engine"
          data-testid="eval_engine"
          options={["weighted", "stub"]}
          value={engine}
          onChange={(event) => setEngine(event.target.value)}
        />
      </Field>

      <Section
        title="Entries"
        description="One entry is one row of the comparison. Put a single detector in each to compare them head to head; put several in one to run them together in a single pipeline, which is how alerts come to cite more than one lane."
      >
        <RowCount
          label="entry"
          count={count ?? 1}
          max={MAX_ENTRIES}
          testId="eval_entry_n"
          onChange={setCount}
        />

        <div className="space-y-4">
          {derived.map((item, index) => (
            <div
              key={index}
              data-testid={`eval_entry_${index}`}
              className="space-y-3 rounded-lg border border-line bg-surface p-4"
            >
              <p className="text-xs font-semibold uppercase tracking-wide text-muted">
                Entry {index + 1}
              </p>
              <MultiSelect
                label={`Detectors in entry ${index + 1}`}
                options={offeredNames}
                notes={notes}
                value={item.entry.selected}
                onValueChange={(selected) => updateEntry(index, { selected })}
                hint="Two or more run together in one pipeline, so their alerts can corroborate. An annotated detector can still be run; it will score zeros and the report will say why."
              />

              {item.plans.map(({ name, summary, state }) => (
                <Collapsible key={name} label={`${name} — hyperparameters`}>
                  {summary.artifact_param ? (
                    <div className="space-y-3">
                      <RadioGroup
                        name={`eval_e${index}_${name}_mode`}
                        label={`${name}: trained artifact or from scratch`}
                        options={[
                          { value: TRAINED, label: TRAINED },
                          { value: SCRATCH, label: SCRATCH },
                        ]}
                        value={state.mode}
                        onValueChange={(mode) => updatePlan(index, name, { mode })}
                      />
                      {state.mode === TRAINED ? (
                        <>
                          <ArtifactField
                            summary={summary}
                            label={summary.artifact_param}
                            id={`eval_e${index}_${name}_art`}
                            value={state.artifact}
                            onChange={(artifact) => updatePlan(index, name, { artifact })}
                          />
                          {/* Why this detector contributes nothing to the entry's sweep
                              menu. Without it the menu silently gains options when this
                              radio moves, and the radio lives inside a collapsed panel. */}
                          <p className="caption">
                            So there is nothing here to edit or sweep. Switch to <i>{SCRATCH}</i>{" "}
                            to choose <b>{name}</b>&apos;s hyperparameters yourself.
                          </p>
                        </>
                      ) : (
                        <ParamFields
                          params={tunablesOf(summary)}
                          values={state.params}
                          idPrefix={`eval_e${index}_${name}`}
                          onChange={(param, value: ParamValue) =>
                            updatePlan(index, name, { params: { ...state.params, [param]: value } })
                          }
                        />
                      )}
                    </div>
                  ) : (
                    <ParamFields
                      params={summary.params}
                      values={state.params}
                      idPrefix={`eval_e${index}_${name}`}
                      onChange={(param, value: ParamValue) =>
                        updatePlan(index, name, { params: { ...state.params, [param]: value } })
                      }
                    />
                  )}
                </Collapsible>
              ))}

              {item.plans.length > 0 ? (
                <div className="space-y-2">
                  {item.entry.clearedSweep ? (
                    <Notice tone="warning">
                      The <b>{item.entry.clearedSweep}</b> sweep no longer applies to this
                      entry&apos;s detectors and has been cleared. Pick one below if you still want
                      a sweep.
                    </Notice>
                  ) : null}
                  {Object.keys(item.offered).length > 0 ? (
                    <div className="grid gap-3 sm:grid-cols-2">
                      <Field label="Sweep a hyperparameter" htmlFor={`eval_e${index}_sw`}>
                        <Select
                          id={`eval_e${index}_sw`}
                          data-testid={`eval_e${index}_sw`}
                          options={item.options}
                          value={item.entry.sweepKey}
                          onChange={(event) => {
                            const sweepKey = event.target.value;
                            const knob = item.offered[sweepKey];
                            updateEntry(index, {
                              sweepKey,
                              clearedSweep: null,
                              sweepText:
                                sweepKey === NO_SWEEP
                                  ? ""
                                  : knob?.default === null || knob?.default === undefined
                                    ? ""
                                    : String(knob.default),
                            });
                          }}
                        />
                      </Field>
                      {item.entry.sweepKey !== NO_SWEEP ? (
                        <Field
                          label={`${item.entry.sweepKey} values (comma-separated)`}
                          htmlFor={`eval_e${index}_swv`}
                        >
                          <TextInput
                            id={`eval_e${index}_swv`}
                            data-testid={`eval_e${index}_swv`}
                            value={item.entry.sweepText}
                            onChange={(event) => updateEntry(index, { sweepText: event.target.value })}
                          />
                        </Field>
                      ) : null}
                    </div>
                  ) : null}
                  {item.entry.sweepKey !== NO_SWEEP ? (
                    <p className="caption">
                      {item.parsed.length === 0
                        ? `Enter comma-separated values to sweep ${item.entry.sweepKey}.`
                        : `-> ${item.parsed.length} run(s), one per value, compared head to head`}
                    </p>
                  ) : null}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      </Section>

      {noneFittable ? (
        <Notice tone="info">
          None of the selected detectors can be trained; they will be evaluated as configured
          and the report will say so.
        </Notice>
      ) : null}
    </div>
  );

  // Gated on the catalogue rather than on eligibility: a deliberately selected detector this
  // corpus cannot feed is a legal run that reports why it found nothing, so withholding its
  // spec here would re-impose the refusal this component just stopped making.
  return {
    element,
    entries: built,
    // Only `problems` needs the gate: with no catalogue there are no entries, so `built` is
    // already empty, but the "no normalizer reads this format" message would fire on a corpus
    // whose formats simply have not loaded yet.
    problems: detectors.length === 0 ? [] : problems,
  };
}
