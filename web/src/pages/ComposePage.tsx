import { useMutation } from "@tanstack/react-query";
import * as React from "react";

import { ArtifactField } from "@/components/ArtifactField";
import { ParamFields } from "@/components/ParamFields";
import {
  Button,
  Field,
  MultiSelect,
  RowCount,
  Select,
  Textarea,
} from "@/components/ui/controls";
import {
  Collapsible,
  ErrorState,
  Notice,
  PageHeader,
  Section,
  Spinner,
} from "@/components/ui/surfaces";
import { StatTile } from "@/components/ui/table";
import { pipelineApi, type SourceInput } from "@/lib/api/client";
import { describeError } from "@/lib/api/http";
import { readTextFile } from "@/lib/files";
import type { ModuleInfo, ModulesResponse, SinkInfo } from "@/lib/api/types";
import { judgeOnlyValues, learnsWeights } from "@/lib/artifact";
import { initialValues, submittedParams, type ParamValues } from "@/lib/params";
import { useModules } from "@/lib/queries";

/**
 * The format the reference telemetry normalizer accepts.
 *
 * Duplicated from `iotshield/normalize/stub.py: SOURCE_FORMAT`, which the Streamlit page
 * imported directly. Nothing on the wire says "this is the reference format", and H5 forbids
 * adding an endpoint that would — so the constant travels with the comment naming its
 * source. It is used **only** to preselect, and by declared format rather than by name, so a
 * renamed normalizer cannot silently break the page (C6).
 */
const STUB_CSV_FORMAT = "stub-csv";

const MAX_SOURCES = 10;
const MAX_DETECTORS = 10;

/**
 * Most bytes one uploaded source may contribute.
 *
 * NOT nginx's `client_max_body_size 64m` (`web/nginx.conf.template`) — that bounds the whole
 * request, and this file becomes JSON-escaped text inside a body that also carries the spec
 * and every *other* source. Well under it on purpose: `JSON.stringify` over tens of MB locks
 * the tab long before the proxy would answer, so the browser is the binding constraint, not
 * the proxy. A run this large belongs on the CLI, which streams from disk.
 */
const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

interface SourceRow {
  readonly normalizer: string;
  readonly format: string;
  readonly params: ParamValues;
  readonly content: string;
  /** The uploaded filename, when a file supplied the content; `null` means "pasted". */
  readonly fileName: string | null;
}

interface DetectorRow {
  readonly name: string;
  readonly params: ParamValues;
}

const byName = <T extends { name: string }>(items: readonly T[]): T[] =>
  [...items].sort((a, b) => a.name.localeCompare(b.name));

/**
 * Preselect by the first predicate that matches anything, in order of preference.
 *
 * No picker here may preselect "whichever name sorts first": that was silently wrong for
 * detectors the moment a second lane arrived (ADR-0020), and wrong for sources once `flow`
 * was registered, since it sorts before `stub` — a fresh session would have opened on a flow
 * chain fed telemetry CSV. Falling back to the first entry when nothing matches is
 * deliberate: the guard then reports a mismatched chain, which is more useful than a row
 * contributing no adapter at all.
 */
function preferred<T>(items: readonly T[], ...ranked: ((item: T) => boolean)[]): T | undefined {
  for (const fits of ranked) {
    const match = items.find(fits);
    if (match !== undefined) return match;
  }
  return items[0];
}

/**
 * Whether the configured sources produce everything this detector needs.
 *
 * `every`, not `some`: `consumes` is a conjunction of requirements, so a detector is fed only
 * when all of them are met. The distinction did not exist while a detector consumed exactly
 * one token — its lane — and appeared the moment payload tokens joined them (ADR-0035 D2):
 * `meter_iforest` consumes `lane:telemetry` AND `payload:meter`, and under `some` a plain
 * telemetry source satisfied it on the lane alone.
 *
 * Compared as rendered token strings, which is why this needs no new field: both sides of the
 * comparison already travel on `/modules`, and adding an axis to the vocabulary adds it here.
 */
const feeds = (detector: ModuleInfo, produced: ReadonlySet<string>): boolean =>
  detector.consumes.every((token) => produced.has(token));

/**
 * Whether this detector can judge without an artifact being named first.
 *
 * Declared, not name-sniffed: `artifact_param` is published by `/modules` precisely so a
 * caller can tell "needs trained weights" from "ready to run" (ADR-0022). A detector with an
 * artifact param is *untrained* until one is named, and a run refuses an untrained detector
 * outright — which is what stops the page opening on a chain that cannot run, exactly what
 * happened when `meter_iforest` joined the registry and sorted ahead of `stub_threshold`.
 */
const ready = (detector: ModuleInfo): boolean => !learnsWeights(detector);

/**
 * Whether this *configured instance* will have trained state when the run builds it.
 *
 * `ready` asks about the adapter; this asks about the row the operator filled in, and the two
 * belong together because they read the same declaration. It matters because the compatibility
 * guard cannot answer it: the guard's defining property is that it reasons about a spec
 * *without constructing adapters*, and "is this one fitted" is only knowable by constructing
 * it — so `/runs/validate` says ok and the refusal arrives from `_ensure_fitted` after the
 * operator has pasted their data.
 *
 * Reads the *widget* value, not the submitted spec, and that is deliberate: `submittedParams`
 * omits a param still sitting at its catalogue default, so a detector whose `artifact_param`
 * has a non-null default is fitted from that default while the spec carries nothing. Judging
 * the spec would warn about a chain that runs perfectly well.
 */
const fitted = (detector: ModuleInfo, params: ParamValues): boolean =>
  ready(detector) || String(params[detector.artifact_param ?? ""] ?? "").trim() !== "";

function newSource(normalizers: readonly ModuleInfo[]): SourceRow {
  const chosen =
    preferred(normalizers, (item) => item.formats.includes(STUB_CSV_FORMAT)) ?? normalizers[0];
  return {
    normalizer: chosen?.name ?? "",
    format: chosen?.formats[0] ?? "",
    params: initialValues(chosen?.params),
    content: "",
    fileName: null,
  };
}

function newDetector(detectors: readonly ModuleInfo[], produced: ReadonlySet<string>, first: boolean): DetectorRow {
  // Only the first row follows the sources; later rows open on the head of the list, exactly
  // as `index=(… if index == 0 else 0)` did.
  const chosen = first
    ? preferred(
        detectors,
        (item) => feeds(item, produced) && ready(item),
        (item) => feeds(item, produced),
      )
    : detectors[0];
  return { name: chosen?.name ?? "", params: judgeOnlyValues(chosen) };
}

/** Live validation: the assembled spec goes to the guard on change, debounced (C9). */
function useValidation(spec: unknown) {
  const serialized = JSON.stringify(spec);
  const [settled, setSettled] = React.useState(serialized);
  const [result, setResult] = React.useState<
    { ok: boolean; issues: { level: string; stage: string; message: string }[] } | null
  >(null);
  const [error, setError] = React.useState<string | null>(null);
  const [pending, setPending] = React.useState(true);

  React.useEffect(() => {
    if (serialized === settled) return;
    const timer = setTimeout(() => setSettled(serialized), 350);
    return () => clearTimeout(timer);
  }, [serialized, settled]);

  React.useEffect(() => {
    const controller = new AbortController();
    setPending(true);
    pipelineApi
      .validate(JSON.parse(settled), controller.signal)
      .then((response) => {
        setResult(response);
        setError(null);
      })
      .catch((cause: unknown) => {
        if (controller.signal.aborted) return;
        setResult(null);
        setError(describeError(cause));
      })
      .finally(() => {
        if (!controller.signal.aborted) setPending(false);
      });
    return () => controller.abort();
  }, [settled]);

  return { result, error, pending, stale: serialized !== settled };
}

function SourceCard({
  index,
  row,
  normalizers,
  onChange,
}: {
  readonly index: number;
  readonly row: SourceRow;
  readonly normalizers: readonly ModuleInfo[];
  readonly onChange: (next: SourceRow) => void;
}) {
  const module = normalizers.find((item) => item.name === row.normalizer);
  const formats = module?.formats ?? [];
  const [fileError, setFileError] = React.useState<string | null>(null);

  const pickNormalizer = (name: string) => {
    const next = normalizers.find((item) => item.name === name);
    onChange({
      ...row,
      normalizer: name,
      format: next?.formats[0] ?? "",
      params: initialValues(next?.params),
    });
  };

  const onFile = async (file: File | null) => {
    setFileError(null);
    if (file === null) {
      onChange({ ...row, content: "", fileName: null });
      return;
    }
    if (file.size > MAX_UPLOAD_BYTES) {
      setFileError(
        `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB; the limit is ` +
          `${MAX_UPLOAD_BYTES / 1024 / 1024} MB per source. Split it, or run the pipeline from the CLI.`,
      );
      return;
    }
    try {
      onChange({ ...row, content: await readTextFile(file), fileName: file.name });
    } catch (cause) {
      // Swallowing this left the operator believing a file was attached: the origin field
      // still read `source-N`, the textarea was still enabled, and nothing on screen changed.
      setFileError(`Could not read ${file.name}: ${describeError(cause)}`);
    }
  };

  return (
    <div className="space-y-3 rounded-lg border border-line bg-surface p-4">
      <p className="text-xs font-semibold uppercase tracking-wide text-muted">Source {index + 1}</p>
      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Normalizer" htmlFor={`src_${index}_norm`}>
          <Select
            id={`src_${index}_norm`}
            data-testid={`src_${index}_norm`}
            options={normalizers.map((item) => item.name)}
            value={row.normalizer}
            onChange={(event) => pickNormalizer(event.target.value)}
          />
        </Field>
        {/* A format picker only when the normalizer declares more than one dialect —
            otherwise it is a control with a single option, which reads as a decision. */}
        {formats.length > 1 ? (
          <Field label="Input format" htmlFor={`src_${index}_fmt`}>
            <Select
              id={`src_${index}_fmt`}
              data-testid={`src_${index}_fmt`}
              options={formats}
              value={row.format}
              onChange={(event) => onChange({ ...row, format: event.target.value })}
            />
          </Field>
        ) : null}
      </div>

      <Collapsible label={`${row.normalizer} parameters`}>
        <ParamFields
          params={module?.params}
          values={row.params}
          idPrefix={`src_${index}`}
          onChange={(name, value) => onChange({ ...row, params: { ...row.params, [name]: value } })}
        />
      </Collapsible>

      <div className="grid gap-3 sm:grid-cols-2">
        <Field label="Upload a file" htmlFor={`src_${index}_file`}>
          <input
            id={`src_${index}_file`}
            data-testid={`src_${index}_file`}
            type="file"
            className="control file:mr-3 file:rounded file:border-0 file:bg-raised file:px-2 file:py-1 file:text-xs file:text-ink"
            onChange={(event) => void onFile(event.target.files?.[0] ?? null)}
          />
        </Field>
        <Field label="Origin" hint="Provenance recorded with every record from this source.">
          <div className="flex items-center gap-2">
            <p className="control min-w-0 flex-1 truncate bg-raised font-mono text-xs">
              {row.fileName ?? `source-${index + 1}`}
            </p>
            {/* Attaching a file disables the textarea, and the textarea's own onChange was the
                only thing that cleared it — so the escape was unreachable and a wrong file
                could only be undone by reloading the whole form. */}
            {row.fileName !== null ? (
              <Button
                size="sm"
                variant="ghost"
                data-testid={`src_${index}_clear`}
                onClick={() => void onFile(null)}
              >
                Remove file
              </Button>
            ) : null}
          </div>
        </Field>
      </div>

      {fileError ? <Notice tone="error">{fileError}</Notice> : null}

      <Field label="…or paste records" htmlFor={`src_${index}_text`}>
        <Textarea
          id={`src_${index}_text`}
          data-testid={`src_${index}_text`}
          rows={5}
          placeholder="CSV for stub, log lines for syslog."
          value={row.fileName === null ? row.content : ""}
          disabled={row.fileName !== null}
          onChange={(event) => onChange({ ...row, content: event.target.value, fileName: null })}
        />
      </Field>
    </div>
  );
}

/**
 * One detector instance: which adapter, and how it is configured.
 *
 * The mirror of `SourceCard`, extracted for the same reason — a whole-row `onChange` means the
 * three "replace the row at this index" closures collapse into one, and a hoisted
 * `artifactParam` const keeps its narrowing where the property path `module.artifact_param`
 * lost it inside a callback (which is what needed an `as string` cast).
 *
 * Params are keyed by row *index*, never by adapter name — that is the whole reason the same
 * adapter can appear twice with different presets (C5).
 */
function DetectorCard({
  index,
  row,
  detectors,
  onChange,
}: {
  readonly index: number;
  readonly row: DetectorRow;
  readonly detectors: readonly ModuleInfo[];
  readonly onChange: (next: DetectorRow) => void;
}) {
  const module = detectors.find((item) => item.name === row.name);
  // Narrowed once, as a const: `learnsWeights` is the one spelling of "does this adapter judge
  // with a cached fit", and reading the property again inside the handler below would need a
  // cast to re-assert what this line already proved.
  const artifactParam = module !== undefined && learnsWeights(module) ? module.artifact_param : null;

  return (
    <div className="space-y-3 rounded-lg border border-line bg-surface p-4">
      <Field label={`Detector ${index + 1}`} htmlFor={`det_${index}_name`}>
        <Select
          id={`det_${index}_name`}
          data-testid={`det_${index}_name`}
          options={detectors.map((item) => item.name)}
          value={row.name}
          onChange={(event) => {
            const name = event.target.value;
            onChange({ name, params: judgeOnlyValues(detectors.find((item) => item.name === name)) });
          }}
        />
      </Field>
      {artifactParam !== null && module !== undefined ? (
        // A learning adapter gets its artifact and NO hyperparameters (ADR-0036 D1): a run never
        // fits, so naming a knob beside an artifact is refused at construction. Inline rather
        // than inside the collapsible, because it is the one param without which the run is
        // refused and so must not need looking for.
        <div className="space-y-2">
          <ArtifactField
            summary={module}
            label={artifactParam}
            id={`det_${index}_art`}
            value={String(row.params[artifactParam] ?? "")}
            onChange={(artifact) => onChange({ ...row, params: { [artifactParam]: artifact } })}
          />
          <p className="caption">
            A run never fits: to choose hyperparameters and learn a baseline from data, use the
            Evaluate page&apos;s fit protocol.
          </p>
        </div>
      ) : (
        <Collapsible label={`${row.name} parameters`}>
          <ParamFields
            params={module?.params}
            values={row.params}
            idPrefix={`det_${index}`}
            onChange={(name, value) => onChange({ ...row, params: { ...row.params, [name]: value } })}
          />
        </Collapsible>
      )}
    </div>
  );
}

function ComposeForm({ catalogue }: { readonly catalogue: ModulesResponse }) {
  const normalizers = React.useMemo(() => byName(catalogue.normalizers), [catalogue]);
  const detectorCatalogue = React.useMemo(() => byName(catalogue.detectors), [catalogue]);
  const engines = React.useMemo(() => byName(catalogue.alert_engines), [catalogue]);
  const sinks = React.useMemo(() => byName(catalogue.sinks), [catalogue]);

  // Preselection happens exactly once, when this component mounts with the catalogue — the
  // React translation of "Streamlit honours `index=` only on a widget's first render".
  // Switching a source afterwards leaves the operator's detector choice alone (and the guard
  // speaks up if it no longer fits).
  const [sources, setSources] = React.useState<SourceRow[]>(() => [newSource(normalizers)]);
  const [detectors, setDetectors] = React.useState<DetectorRow[]>(() => {
    const produced = new Set(
      normalizers.find((item) => item.name === newSource(normalizers).normalizer)?.produces ?? [],
    );
    return [newDetector(detectorCatalogue, produced, true)];
  });
  const [engine, setEngine] = React.useState<string>(() => engines[0]?.name ?? "");
  const [engineParams, setEngineParams] = React.useState<ParamValues>(() =>
    initialValues(engines[0]?.params),
  );
  const [selectedSinks, setSelectedSinks] = React.useState<string[]>(() =>
    sinks.some((item) => item.name === "postgres")
      ? ["postgres"]
      : sinks.slice(0, 1).map((item) => item.name),
  );
  const [sinkParams, setSinkParams] = React.useState<Record<string, ParamValues>>(() =>
    Object.fromEntries(sinks.map((item) => [item.name, initialValues(item.params)])),
  );

  const producedLanes = React.useMemo(
    () =>
      new Set(
        sources.flatMap(
          (source) => normalizers.find((item) => item.name === source.normalizer)?.produces ?? [],
        ),
      ),
    [sources, normalizers],
  );

  const setSourceCount = (count: number) =>
    setSources((rows) => {
      if (count <= rows.length) return rows.slice(0, count);
      return [...rows, ...Array.from({ length: count - rows.length }, () => newSource(normalizers))];
    });

  const setDetectorCount = (count: number) =>
    setDetectors((rows) => {
      if (count <= rows.length) return rows.slice(0, count);
      return [
        ...rows,
        ...Array.from({ length: count - rows.length }, () =>
          newDetector(detectorCatalogue, producedLanes, false),
        ),
      ];
    });

  const spec = React.useMemo(() => {
    const sinkSpecs = selectedSinks.map((name) => ({
      name,
      params: submittedParams(
        sinks.find((item) => item.name === name)?.params,
        sinkParams[name] ?? {},
      ),
    }));
    return {
      sources: sources.map((source) => ({
        normalizer: source.normalizer,
        source_format: source.format,
        params: submittedParams(
          normalizers.find((item) => item.name === source.normalizer)?.params,
          source.params,
        ),
      })),
      detectors: detectors.map((row) => ({
        name: row.name,
        params: submittedParams(
          detectorCatalogue.find((item) => item.name === row.name)?.params,
          row.params,
        ),
      })),
      alert_engine: {
        name: engine,
        params: submittedParams(engines.find((item) => item.name === engine)?.params, engineParams),
      },
      sinks: sinkSpecs,
    };
  }, [
    sources,
    detectors,
    engine,
    engineParams,
    selectedSinks,
    sinkParams,
    normalizers,
    detectorCatalogue,
    engines,
    sinks,
  ]);

  const inputs: SourceInput[] = sources.map((source, index) => ({
    content: source.content,
    origin: source.fileName ?? `source-${index + 1}`,
  }));

  const gated = detectors.length === 0 || selectedSinks.length === 0;
  const validation = useValidation(spec);
  const run = useMutation({ mutationFn: () => pipelineApi.run(spec, inputs) });

  // The debounce buys the guard fewer requests; it also creates a window in which the verdict
  // on screen describes a spec that no longer exists. `settling` is that window. Showing a
  // green "Chain is compatible." through it — and letting Run fire — is the page claiming
  // knowledge it does not have about the chain the operator is actually looking at.
  const settling = validation.pending || validation.stale;
  const validationOk = validation.result?.ok ?? false;
  const contentReady = inputs.every((input) => input.content.trim().length > 0);
  const canRun = !gated && validationOk && !settling && contentReady && !run.isPending;
  const wrotePostgres = selectedSinks.includes("postgres");

  // Detectors the operator chose that cannot run as configured — see `fitted` above for why
  // only the page can know this. H3 permits reading a published declaration; this is not
  // re-deriving server logic.
  // Carries the row INDEX, not just the name: detector rows are identified by position
  // (ADR-0023 D9 makes one adapter twice a first-class case, and this file's own state is
  // "keyed by index, never by adapter name"). Keying the notices below by name gave two
  // untrained rows of one adapter the same React key — "the behavior is unsupported and
  // could change in a future version" (ADR-0033).
  const untrained = detectors
    .map((row, index) => ({ row, index }))
    .filter(({ row }) => {
      const module = detectorCatalogue.find((item) => item.name === row.name);
      return module !== undefined && !fitted(module, row.params);
    });

  return (
    <div className="space-y-8">
      <Section
        title="1 · Input sources"
        description="Add one source per file/format; each is routed to its normalizer and pooled."
      >
        <RowCount
          label="source"
          count={sources.length}
          max={MAX_SOURCES}
          testId="compose_n"
          onChange={setSourceCount}
        />
        <div className="space-y-4">
          {sources.map((source, index) => (
            <SourceCard
              key={index}
              index={index}
              row={source}
              normalizers={normalizers}
              onChange={(next) =>
                setSources((rows) => rows.map((row, position) => (position === index ? next : row)))
              }
            />
          ))}
        </div>
      </Section>

      <Section
        title="2 · Detectors"
        description="Add one row per detector instance. The same adapter may appear more than once — that is how you run two presets (e.g. a fan-out and an exfiltration rule) at once."
      >
        <RowCount
          label="detector"
          count={detectors.length}
          max={MAX_DETECTORS}
          testId="compose_det_n"
          onChange={setDetectorCount}
        />
        <div className="space-y-3">
          {detectors.map((row, index) => (
            <DetectorCard
              key={index}
              index={index}
              row={row}
              detectors={detectorCatalogue}
              onChange={(next) =>
                setDetectors((rows) => rows.map((old, position) => (position === index ? next : old)))
              }
            />
          ))}
        </div>
      </Section>

      <Section title="3 · Alert engine">
        <Field label="Alert engine" htmlFor="compose_engine" className="max-w-md">
          <Select
            id="compose_engine"
            data-testid="compose_engine"
            options={engines.map((item) => item.name)}
            value={engine}
            onChange={(event) => {
              const name = event.target.value;
              setEngine(name);
              setEngineParams(initialValues(engines.find((item) => item.name === name)?.params));
            }}
          />
        </Field>
        <Collapsible label={`${engine} parameters`}>
          <ParamFields
            params={engines.find((item) => item.name === engine)?.params}
            values={engineParams}
            idPrefix={`eng_${engine}`}
            onChange={(name, value) => setEngineParams((values) => ({ ...values, [name]: value }))}
          />
        </Collapsible>
      </Section>

      <Section title="4 · Result sinks">
        <MultiSelect
          label="Sinks"
          options={sinks.map((item) => item.name)}
          value={selectedSinks}
          onValueChange={setSelectedSinks}
          hint="Where results go. The 'postgres' store is what the Triage view reads."
        />
        <div className="space-y-2">
          {selectedSinks.map((name) => (
            <Collapsible key={name} label={`${name} parameters`}>
              <ParamFields
                params={sinks.find((item: SinkInfo) => item.name === name)?.params}
                values={sinkParams[name] ?? {}}
                idPrefix={`sink_${name}`}
                onChange={(param, value) =>
                  setSinkParams((values) => ({
                    ...values,
                    [name]: { ...(values[name] ?? {}), [param]: value },
                  }))
                }
              />
            </Collapsible>
          ))}
        </div>
      </Section>

      {/* Notices, not errors: an empty selection is a transient state on the way to a run,
          not a malformed request. */}
      {detectors.length === 0 ? (
        <Notice tone="info">Pick at least one detector to continue.</Notice>
      ) : selectedSinks.length === 0 ? (
        <Notice tone="info">Pick at least one sink so results have somewhere to go.</Notice>
      ) : (
        <Section title="5 · Validate & run">
          {/* While settling, the previous verdict is withheld rather than dimmed: a dimmed
              green badge is still a green badge to anyone scanning the page. */}
          {settling ? (
            <Spinner label="Checking the chain…" />
          ) : (
            <>
              {validation.error !== null ? (
                <Notice tone="error">Validation request failed: {validation.error}</Notice>
              ) : null}
              {validation.result?.issues.map((issue, index) => (
                <Notice key={index} tone={issue.level === "error" ? "error" : "warning"}>
                  [{issue.stage}] {issue.message}
                </Notice>
              ))}
              {validation.result !== null &&
              validation.result.ok &&
              validation.result.issues.length === 0 ? (
                <Notice tone="success">Chain is compatible.</Notice>
              ) : null}
            </>
          )}
          {untrained.map(({ row, index }) => (
            <Notice key={index} tone="warning">
              <b>{row.name}</b> has no fitted artifact, so the run will be refused before it
              judges anything. Name one in its parameters, or choose a detector that is ready to
              run. The chain check above cannot see this — it never builds the detector.
            </Notice>
          ))}
          {!contentReady ? (
            <p className="caption">
              Every source needs content — upload a file or paste records above.
            </p>
          ) : null}

          <div className="flex items-center gap-3">
            <Button
              variant="primary"
              data-testid="compose_run"
              disabled={!canRun}
              onClick={() => run.mutate()}
            >
              Run pipeline
            </Button>
            {run.isPending ? <Spinner label="Running…" /> : null}
          </div>

          {run.isError ? <Notice tone="error">Run failed: {describeError(run.error)}</Notice> : null}

          {run.isSuccess ? (
            <div className="space-y-4 pt-2">
              <Notice tone="success">Run complete.</Notice>
              <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
                <StatTile label="events" value={run.data.events} />
                <StatTile label="malformed" value={run.data.malformed} />
                <StatTile label="detections" value={run.data.detections} />
                <StatTile label="alerts" value={run.data.alerts} />
              </div>
              {run.data.warnings.map((warning, index) => (
                <Notice key={index} tone="warning">
                  {warning}
                </Notice>
              ))}
              <p className="caption">
                Written to: {run.data.destinations.join(", ") || "nothing"}
              </p>
              {/* A receipt, not a results view: the ranked, explained alerts live on the read
                  plane. Triage reads the postgres store only, so the hand-off is shown only
                  when that sink was written. */}
              {run.data.alerts > 0 && wrotePostgres ? (
                <Notice tone="info">
                  {run.data.alerts} alert(s) written — see them ranked and explained under{" "}
                  <strong>Monitor → Triage</strong>.
                </Notice>
              ) : null}
              {!wrotePostgres ? (
                <p className="caption">
                  No postgres sink selected — results are in the chosen sink(s), not Triage.
                </p>
              ) : null}
            </div>
          ) : null}
        </Section>
      )}
    </div>
  );
}

export function ComposePage() {
  const modules = useModules();

  return (
    <>
      <PageHeader
        title="Compose & run a pipeline"
        description="Pick input sources and modules, validate the chain, then trigger a run. Results appear under Monitor → Triage."
      />
      {modules.isPending ? <Spinner label="Loading the adapter catalogue…" /> : null}
      {modules.isError ? (
        <ErrorState
          title="Pipeline API unreachable"
          detail={<>{describeError(modules.error)}. Start it with:</>}
          command="IOTSHIELD_DATABASE_URL=sqlite:///demo.db python -m iotshield.api.control"
          onRetry={() => void modules.refetch()}
        />
      ) : null}
      {modules.isSuccess ? (
        modules.data.normalizers.length === 0 ||
        modules.data.detectors.length === 0 ||
        modules.data.alert_engines.length === 0 ||
        modules.data.sinks.length === 0 ? (
          <Notice tone="error">The Pipeline API reports no adapters — nothing to compose.</Notice>
        ) : (
          <ComposeForm catalogue={modules.data} />
        )
      ) : null}
    </>
  );
}
