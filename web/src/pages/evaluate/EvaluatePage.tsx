import { useQuery } from "@tanstack/react-query";
import * as React from "react";

import { Button, Field, RadioGroup, Select } from "@/components/ui/controls";
import {
  Collapsible,
  CommandBlock,
  JsonBlock,
  Notice,
  PageHeader,
  Spinner,
} from "@/components/ui/surfaces";
import { pipelineApi } from "@/lib/api/client";
import { describeError, describeZodIssue } from "@/lib/api/http";
import { evaluationReportSchema, type EvalReport } from "@/lib/api/schemas";
import { readTextFile } from "@/lib/files";
import { keys, useEvalDetectors, useEvalPlane, useModules } from "@/lib/queries";

import { useComposeEntries } from "./ComposeEntries";
import { useLatch } from "@/lib/latch";
import { PROTOCOL_HELP, runCount, selectionKey } from "./lib";
import { ReportView } from "./Report";
import { useShippedSpecs } from "./ShippedSpecs";

const SHIPPED_SPECS = "Run a committed bench spec";
const COMPOSE_SPECS = "Compose my own";

const GENERATE_COMMAND =
  "uv run python -m iotshield.eval --corpus synthetic --with-timeline " +
  "--spec config/bench/team3.yaml --out reports/evaluation.json";

/**
 * The saved-report path, as an upload (D6).
 *
 * The old page fell back to reading `reports/evaluation.json` from the *server's* disk; a
 * browser cannot. A file picker keeps the CLI → file → dashboard path working with no new
 * endpoint, which is what that fallback was for: the page degrades rather than breaking when
 * the Pipeline API is not there.
 */
function SavedReportFallback() {
  const [report, setReport] = React.useState<EvalReport | null>(null);
  const [problem, setProblem] = React.useState<string | null>(null);

  const load = async (file: File | null) => {
    if (file === null) return;
    try {
      const parsed = evaluationReportSchema.safeParse(JSON.parse(await readTextFile(file)));
      if (!parsed.success) {
        setReport(null);
        // Shared with the wire-response path, so one malformed document does not describe
        // itself two different ways depending on whether it arrived over HTTP or through the
        // file picker.
        const missing = parsed.error.issues.every((item) => item.code === "invalid_type");
        setProblem(
          `${file.name} is not a benchmark report: ${describeZodIssue(parsed.error)}.` +
            (missing
              ? " A report written by an older harness is missing fields this page needs — regenerate it with the command above."
              : ""),
        );
        return;
      }
      setReport(parsed.data);
      setProblem(null);
    } catch (cause) {
      // A corrupt file is an empty state, never a traceback.
      setReport(null);
      setProblem(`${file.name} could not be read as JSON (${describeError(cause)}).`);
    }
  };

  return (
    <div className="space-y-5">
      <Notice tone="warning" title="Pipeline API unreachable">
        <p>
          Start it with <code>IOTSHIELD_EVAL_API=1</code>, or load a report the CLI already wrote:
        </p>
        <CommandBlock>{GENERATE_COMMAND}</CommandBlock>
      </Notice>
      <Field label="Load a saved report JSON" htmlFor="eval_report_upload" className="max-w-md">
        <input
          id="eval_report_upload"
          data-testid="eval_report_upload"
          type="file"
          accept="application/json,.json"
          className="control file:mr-3 file:rounded file:border-0 file:bg-raised file:px-2 file:py-1 file:text-xs file:text-ink"
          onChange={(event) => void load(event.target.files?.[0] ?? null)}
        />
      </Field>
      {problem ? <Notice tone="info">{problem}</Notice> : null}
      {report ? <ReportView report={report} /> : null}
    </div>
  );
}

/**
 * A spinner that counts.
 *
 * A benchmark can run for minutes behind a 600 s proxy timeout, and a motionless "Benchmarking…"
 * gives the operator no way to tell a long run from a dead worker. An elapsed count cannot
 * prove the server is alive, but it turns "is this stuck?" into a number they can judge.
 */
function Benchmarking() {
  const [seconds, setSeconds] = React.useState(0);
  React.useEffect(() => {
    const timer = setInterval(() => setSeconds((value) => value + 1), 1000);
    return () => clearInterval(timer);
  }, []);
  return <Spinner label={`Benchmarking… ${seconds}s elapsed`} />;
}

export function EvaluatePage() {
  const plane = useEvalPlane();
  const [mode, setMode] = React.useState(SHIPPED_SPECS);
  const [protocol, setProtocol] = React.useState("none");
  const [corpusName, setCorpusName] = React.useState<string | null>(null);
  // Held on the shell route (see `latch.tsx`) so navigating away does not discard a finished
  // benchmark whose result is still in the query cache.
  const [latched, setLatched] = useLatch();

  const corpora = plane.query.data;
  const usable = (corpora?.corpora ?? []).filter((item) => !item.error);
  const broken = (corpora?.corpora ?? []).filter((item) => item.error);
  const corpus = usable.find((item) => item.name === corpusName) ?? usable[0];

  // Each mode fetches only what it needs: `/eval/specs` re-walks the corpus root and
  // re-parses every bench YAML, and the compose mode never uses it.
  const composing = mode === COMPOSE_SPECS;
  const detectors = useEvalDetectors(Boolean(corpora) && composing);
  const modules = useModules(Boolean(corpora) && composing);
  const shipped = useShippedSpecs(corpus?.name ?? "", Boolean(corpora) && !composing);
  const composed = useComposeEntries({
    corpus,
    protocol,
    detectors: detectors.data ?? [],
    normalizers: modules.data?.normalizers ?? [],
  });

  const active = composing ? composed : shipped;
  const entries = active.entries;
  const problems = active.problems;
  const selection = corpus ? selectionKey(corpus.name, entries, protocol) : "";
  const latchedKey = latched ? selectionKey(latched.corpus, latched.specs, latched.protocol) : null;

  const runQuery = useQuery({
    queryKey: keys.evalRun(latchedKey ?? ""),
    queryFn: () =>
      pipelineApi.evalRun({
        corpus: latched?.corpus ?? "",
        specs: latched?.specs ?? [],
        fit_protocol: latched?.protocol ?? "none",
        include_timeline: true,
      }),
    enabled: latched !== null,
    // A benchmark is expensive and deterministic for a given request: once run, exploring it
    // must replay from cache rather than re-running on a stray interaction.
    staleTime: Infinity,
    gcTime: Infinity,
  });

  const cataloguesPending = composing && (detectors.isLoading || modules.isLoading);
  const catalogueError = composing ? (detectors.error ?? modules.error) : null;

  const total = entries.reduce((sum, entry) => sum + runCount(entry.sweep), 0);
  const blocked =
    entries.length === 0 || problems.length > 0 || cataloguesPending || catalogueError !== null;

  const header = (
    <PageHeader
      title="Evaluate — detector benchmarks"
      description="How detectors score against labelled corpora. A research instrument: it needs ground truth, which production data by definition never has."
    />
  );

  if (plane.loading) {
    return (
      <>
        {header}
        <Spinner label="Looking for the evaluation plane…" />
      </>
    );
  }
  if (plane.unreachable) {
    return (
      <>
        {header}
        <SavedReportFallback />
      </>
    );
  }
  if (corpora === undefined) {
    return (
      <>
        {header}
        <Notice tone="info">The evaluation plane is not enabled on this deployment.</Notice>
      </>
    );
  }

  if (corpora.corpora.length === 0) {
    return (
      <>
        {header}
        <Notice tone="warning">
          <p>
            No corpora in <code className="font-mono">{corpora.root}</code>. Build the committed one
            with:
          </p>
          <CommandBlock>uv run python -m iotshield.eval generate</CommandBlock>
        </Notice>
        <div className="mt-3 space-y-1">
          {Object.values(corpora.fetchable ?? {}).map((hint) => (
            <p key={hint} className="caption">
              {hint}
            </p>
          ))}
        </div>
      </>
    );
  }

  return (
    <>
      {header}
      <div className="space-y-6">
        {/* A bundle that cannot be loaded is shown as broken rather than hidden — listed with
            its reason (which names the fix) and kept out of the picker, so "my corpus
            vanished" is never the only feedback. */}
        {broken.map((item) => (
          <Notice key={item.name} tone="error">
            <code className="font-mono">{item.name}</code> cannot be loaded: {item.error}
          </Notice>
        ))}

        {corpus === undefined ? null : (
          <>
            <div className="grid gap-4 sm:grid-cols-[2fr_3fr]">
              <Field label="Corpus" htmlFor="eval_corpus">
                <Select
                  id="eval_corpus"
                  data-testid="eval_corpus"
                  options={usable.map((item) => item.name)}
                  value={corpus.name}
                  onChange={(event) => setCorpusName(event.target.value)}
                />
              </Field>
              <Field label="Fit protocol">
                <RadioGroup
                  name="eval_protocol"
                  label="Fit protocol"
                  options={Object.keys(PROTOCOL_HELP).map((value) => ({ value, label: value }))}
                  value={protocol}
                  onValueChange={setProtocol}
                  className="pt-1.5"
                />
              </Field>
            </div>
            <div className="space-y-1">
              <p className="caption">
                {corpus.cases} case(s) · {corpus.description}
              </p>
              {corpus.licence ? (
                <p className="caption">
                  Provenance: {corpus.provenance} · Licence: {corpus.licence}
                </p>
              ) : null}
              <p className="caption">{PROTOCOL_HELP[protocol]}</p>
            </div>

            <Field label="Specs">
              <RadioGroup
                name="eval_spec_mode"
                label="Specs"
                options={[
                  { value: SHIPPED_SPECS, label: SHIPPED_SPECS },
                  { value: COMPOSE_SPECS, label: COMPOSE_SPECS },
                ]}
                value={mode}
                onValueChange={setMode}
                className="pt-1.5"
              />
            </Field>

            {/* "No detector consumes this corpus's lanes" is a claim about the registry, and
                an empty catalogue is also what a pending or failed fetch looks like. Stating
                it from an absence nobody verified told operators their corpus was unusable
                when the truth was that one HTTP call had failed.

                `isLoading`, not `isPending`: in TanStack v5 a *disabled* query is `isPending`
                with `fetchStatus: "idle"`, so the naive check would spin forever in the mode
                that never enables these. */}
            {catalogueError !== null ? (
              <Notice tone="error" title="Could not read the module registry">
                <p>
                  {describeError(catalogueError)} — so this page cannot say which detectors
                  your corpus can feed.
                </p>
              </Notice>
            ) : cataloguesPending ? (
              <Spinner label="Loading the detector catalogue…" />
            ) : (
              active.element
            )}

            {problems.map((problem) => (
              <Notice key={problem} tone="error">
                {problem}
              </Notice>
            ))}

            {/* The exact request, on the page that made it. Neither surface used to display
                the spec it ran, so a dashboard number and a CLI number that disagreed could
                only be reconciled by reading the page's source.

                Two questions, two answers, and they diverge the moment a control is touched:
                "what will Run send" is the live form, "what produced the report below" is the
                latched request. One block labelled in the past tense but fed from the form
                answered the second question with the first one's data — which is the exact
                misattribution this disclosure exists to prevent. */}
            {entries.length > 0 ? (
              <Collapsible label={`Spec the Run button will send (${entries.length} entr(y/ies))`}>
                <JsonBlock value={entries} />
              </Collapsible>
            ) : null}
            {latched !== null && latchedKey !== selection ? (
              <Collapsible label={`Spec that produced the report below (${latched.specs.length} entr(y/ies))`}>
                <JsonBlock value={latched.specs} />
              </Collapsible>
            ) : null}

            {total > entries.length ? (
              <p className="caption">
                This will run <b>{total}</b> benchmark(s) — one per swept value.
              </p>
            ) : null}

            <Button
              variant="primary"
              data-testid="eval_run"
              disabled={blocked || runQuery.isFetching}
              onClick={() => {
                // An unchanged selection leaves the query key where it is, so `setLatched`
                // alone would be a silent no-op — and after a failure, with `retry: false` and
                // `staleTime: Infinity`, a permanently dead button. `refetch` is what makes
                // pressing Run always mean something.
                if (latchedKey === selection) void runQuery.refetch();
                else setLatched({ corpus: corpus.name, specs: entries, protocol });
              }}
            >
              Run benchmark
            </Button>

            {latched === null ? (
              <p className="caption">
                Pick specs and run — results are cached, so exploring them is instant.
              </p>
            ) : (
              <>
                {/* Changing a control after a run leaves the latched report on screen, which
                    reads as "the control did nothing" — the fit protocol especially. Say so
                    rather than re-running unasked: a benchmark is not something to trigger on
                    a stray click. */}
                {latchedKey !== selection ? (
                  <Notice tone="warning">
                    Showing the previous run (corpus <b>{latched.corpus}</b>,{" "}
                    {latched.specs.map((entry) => entry.label).join(", ")}, fit protocol{" "}
                    <b>{latched.protocol}</b>). Press <b>Run benchmark</b> to apply the settings
                    above.
                  </Notice>
                ) : null}
                {/* One chain, because these three are mutually exclusive: a refetch over a
                    cached success must hide the stale report while it runs. */}
                {runQuery.isFetching ? (
                  <Benchmarking />
                ) : runQuery.isError ? (
                  <Notice tone="error">Benchmark failed: {describeError(runQuery.error)}</Notice>
                ) : runQuery.isSuccess ? (
                  <ReportView report={runQuery.data} />
                ) : null}
              </>
            )}
          </>
        )}
      </div>
    </>
  );
}
