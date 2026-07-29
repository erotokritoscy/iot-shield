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

const tunablesOf = (summary: DetectorSummary): ParamInfo[] =>
  (summary.params ?? []).filter((param) => param.name !== summary.artifact_param);

/** The param that names a cached fit, when this detector has one (ADR-0022). */
const artifactParamOf = (summary: DetectorSummary): ParamInfo | undefined =>
  (summary.params ?? []).find((param) => param.name === summary.artifact_param);

/** The artifact ids the adapter ships, published as that param's closed vocabulary. */
const artifactIdsOf = (summary: DetectorSummary): string[] =>
  artifactParamOf(summary)?.choices ?? [];

/**
 * Which cached fit to judge with: the ids the adapter ships, offered rather than recalled.
 *
 * The same rule every other closed vocabulary on this page already follows (ADR-0023 D9).
 * This was the one free-text box left, and the most expensive one to get wrong — an
 * unrecognised id decides whether the detector can judge at all (ADR-0035 D1).
 *
 * Falls back to free text only when an adapter declares an artifact param and enumerates
 * nothing, where a dropdown would offer no way forward. Both arms take identical props, so
 * they are written once and spread; the handler is typed structurally because a `<select>`
 * and an `<input>` disagree about their event's element type and agree about `target.value`.
 */
function ArtifactField({
  summary,
  label,
  id,
  value,
  onChange,
}: {
  readonly summary: DetectorSummary;
  /** The artifact param's name, passed narrowed — only a detector that has one gets here. */
  readonly label: string;
  readonly id: string;
  readonly value: string;
  readonly onChange: (artifact: string) => void;
}) {
  const ids = artifactIdsOf(summary);
  const shared = {
    id,
    "data-testid": id,
    value,
    onChange: (event: { target: { value: string } }) => onChange(event.target.value),
  };
  return (
    <Field
      label={label}
      htmlFor={id}
      hint="A committed artifact id. It supplies both the weights and the hyperparameters they were fitted with — the two travel together."
      className="max-w-sm"
    >
      {ids.length > 0 ? (
        <Select {...shared} options={ids} />
      ) : (
        <TextInput {...shared} placeholder="artifact id" />
      )}
    </Field>
  );
}

function defaultPlan(summary: DetectorSummary): PlanState {
  const artifactParam = artifactParamOf(summary);
  const shipped =
    artifactParam?.default === null || artifactParam?.default === undefined
      ? ""
      : String(artifactParam.default);
  return {
    mode: TRAINED,
    // The factory default if it has one, else the first id the adapter actually ships —
    // read from the catalogue, where a hardcoded table used to name one of the two shipped
    // artifacts from memory (ADR-0035 D1).
    //
    // Preselecting here is right where `params.initialValue` deliberately refuses to: an
    // optional param must be able to say "not given", but in TRAINED mode an artifact is
    // *mandatory* — a blank is a reported blocker, not an absent value — and it is always
    // submitted explicitly rather than being omitted as untouched. So the only thing a blank
    // default buys is a page that opens broken.
    artifact: shipped || (artifactIdsOf(summary)[0] ?? ""),
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
  // Which detectors this corpus can actually feed, from the shape the API says it yields.
  // Both sides of this intersection arrive decoded from /eval, so the page never parses a
  // `lane:`/`payload:` prefix (H6).
  //
  // TWO axes, and the second is why this list used to lie (ADR-0035 D2). A lane is satisfied
  // by any source of that modality; a payload is satisfied only by a source that carries the
  // typed vocabulary. `meter_iforest` needs both, so on every `stub-csv` corpus it matched on
  // lane, sorted alphabetically first, and became entry 1's default — a detector that could
  // not read a single event of the corpus it was offered for.
  //
  // The lane test is `some` and the payload test is `every`, which is not an inconsistency:
  // a detector fires if ANY of its lanes is present, but it needs ALL of the vocabularies it
  // reads through.
  const corpusLanes = React.useMemo(() => new Set(corpus?.lanes ?? []), [corpus]);
  const corpusPayloads = React.useMemo(() => new Set(corpus?.payloads ?? []), [corpus]);
  const eligible = React.useMemo(
    () =>
      detectors
        .filter(
          (item) =>
            item.lanes.some((lane) => corpusLanes.has(lane)) &&
            item.payloads.every((payload) => corpusPayloads.has(payload)),
        )
        .map((item) => item.name),
    [detectors, corpusLanes, corpusPayloads],
  );
  const eligibleKey = eligible.join(" ");

  const [engine, setEngine] = React.useState("weighted");
  const [count, setCount] = React.useState<number | null>(null);
  const [entries, setEntries] = React.useState<EntryState[]>([]);

  // Reconcile the entry list with the corpus and the count. Selections are **pruned, not
  // reset**, when the corpus changes: a flow detector picked on `fleet_scan` is not an option
  // on `synthetic`, but whatever else the entry held is still what the operator wanted.
  React.useEffect(() => {
    // Nothing to default *to* until the detector catalogue has arrived: creating entries
    // against an empty eligible list would fix the count at 1 and leave entry 1 blank, which
    // is the opposite of "one detector per entry, walking down the list".
    if (eligible.length === 0) return;
    const wanted = count ?? Math.min(2, Math.max(1, eligible.length));
    if (count === null) setCount(wanted);
    setEntries((previous) => {
      const next: EntryState[] = [];
      for (let index = 0; index < wanted; index += 1) {
        const existing = previous[index];
        // Default to one detector per entry, walking down the eligible list, so a fresh page
        // is the head-to-head everyone expects and multi-detector entries are opt-in.
        if (existing === undefined) {
          next.push(emptyEntry(eligible[index]));
          continue;
        }
        const pruned = existing.selected.filter((name) => eligible.includes(name));
        next.push(pruned.length === existing.selected.length ? existing : { ...existing, selected: pruned });
      }
      const same =
        next.length === previous.length && next.every((entry, index) => entry === previous[index]);
      return same ? previous : next;
    });
  }, [count, eligibleKey, eligible]);

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

  const element =
    eligible.length === 0 ? (
      <Notice tone="info">
        No registered detector can read this corpus. It feeds lane(s){" "}
        <b>{(corpus?.lanes ?? []).join(", ") || "none"}</b> carrying payload(s){" "}
        <b>{(corpus?.payloads ?? []).join(", ") || "none"}</b>, and a detector needs one of
        those lanes and all of the payloads it reads through.
      </Notice>
    ) : (
      <div className="space-y-6">
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
                  options={eligible}
                  value={item.entry.selected}
                  onValueChange={(selected) => updateEntry(index, { selected })}
                  hint="Two or more run together in one pipeline, so their alerts can corroborate."
                />

                {item.plans.map(({ name, summary, state, plan: detectorPlan }) => (
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
                              Trained weights are atomic: the artifact supplies <b>{name}</b>&apos;s
                              hyperparameters too, so there is nothing here to edit or sweep. Switch
                              to <i>{SCRATCH}</i> to choose them yourself.
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
                    {detectorPlan.problem ? null : null}
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

  return { element, entries: eligible.length === 0 ? [] : built, problems: eligible.length === 0 ? [] : problems };
}
