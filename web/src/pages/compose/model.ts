/**
 * The Compose wizard's state and the rules that read it — no rendering here.
 *
 * Moved out of the page when it became a wizard, unchanged in substance: every predicate below
 * carries the reasoning (and the ADR) it was written with, because each one guards a mistake
 * the single-page form actually made.
 */
import * as React from "react";

import { pipelineApi } from "@/lib/api/client";
import { describeError } from "@/lib/api/http";
import type { ModuleInfo, ModulesResponse } from "@/lib/api/types";
import { judgeOnlyValues, learnsWeights } from "@/lib/artifact";
import { initialValues, submittedParams, type ParamValues } from "@/lib/params";
import type { Sample, Scenario } from "@/lib/samples";

/**
 * The format the reference telemetry normalizer accepts.
 *
 * Duplicated from `iotshield/normalize/stub.py: SOURCE_FORMAT`, which the Streamlit page
 * imported directly. Nothing on the wire says "this is the reference format", and H5 forbids
 * adding an endpoint that would — so the constant travels with the comment naming its
 * source. It is used **only** to preselect, and by declared format rather than by name, so a
 * renamed normalizer cannot silently break the page (C6).
 */
export const STUB_CSV_FORMAT = "stub-csv";

export const MAX_SOURCES = 10;
export const MAX_DETECTORS = 10;

/**
 * Most bytes one uploaded source may contribute.
 *
 * NOT nginx's `client_max_body_size` (`web/nginx.conf.template`) — that bounds the whole
 * request, and this file becomes JSON-escaped text inside a body that also carries the spec
 * and every *other* source. Well under it on purpose: `JSON.stringify` over tens of MB locks
 * the tab long before the proxy would answer, so the browser is the binding constraint, not
 * the proxy. A run this large belongs on the CLI, which streams from disk.
 */
export const MAX_UPLOAD_BYTES = 8 * 1024 * 1024;

export interface SourceRow {
  readonly normalizer: string;
  readonly format: string;
  readonly params: ParamValues;
  readonly content: string;
  /** The uploaded or sample filename, when a file supplied the content; `null` means "pasted". */
  readonly fileName: string | null;
}

export interface DetectorRow {
  readonly name: string;
  readonly params: ParamValues;
}

export const byName = <T extends { name: string }>(items: readonly T[]): T[] =>
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
export function preferred<T>(items: readonly T[], ...ranked: ((item: T) => boolean)[]): T | undefined {
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
 * when all of them are met (ADR-0035 D2): `meter_iforest` consumes `lane:telemetry` AND
 * `payload:meter`, and under `some` a plain telemetry source satisfied it on the lane alone.
 */
export const feeds = (detector: ModuleInfo, produced: ReadonlySet<string>): boolean =>
  detector.consumes.every((token) => produced.has(token));

/** The requirements of `detector` that the configured sources do not produce. */
export const missingInputs = (detector: ModuleInfo, produced: ReadonlySet<string>): string[] =>
  detector.consumes.filter((token) => !produced.has(token));

/**
 * Whether this detector can judge without an artifact being named first (ADR-0022): one with
 * an artifact param is *untrained* until one is named, and a run refuses it outright.
 */
export const ready = (detector: ModuleInfo): boolean => !learnsWeights(detector);

/**
 * Whether this *configured instance* will have trained state when the run builds it.
 *
 * The compatibility guard cannot answer this — it reasons about a spec without constructing
 * adapters — so `/runs/validate` says ok and the refusal would arrive from `_ensure_fitted`
 * after the operator has pasted their data. Reads the *widget* value, not the submitted spec:
 * `submittedParams` omits a param at its catalogue default, so judging the spec would warn
 * about a chain that runs perfectly well.
 */
export const fitted = (detector: ModuleInfo, params: ParamValues): boolean =>
  ready(detector) || String(params[detector.artifact_param ?? ""] ?? "").trim() !== "";

export function newSource(normalizers: readonly ModuleInfo[]): SourceRow {
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

export function newDetector(
  detectors: readonly ModuleInfo[],
  produced: ReadonlySet<string>,
  first: boolean,
): DetectorRow {
  // Only the first row follows the sources; later rows open on the head of the list.
  const chosen = first
    ? preferred(
        detectors,
        (item) => feeds(item, produced) && ready(item),
        (item) => feeds(item, produced),
      )
    : detectors[0];
  return { name: chosen?.name ?? "", params: judgeOnlyValues(chosen) };
}

/** A sample as a source row: its normalizer and format, its text, its filename as origin. */
export function sourceFromSample(
  sample: Sample,
  content: string,
  normalizers: readonly ModuleInfo[],
): SourceRow {
  const module = normalizers.find((item) => item.name === sample.normalizer);
  return {
    normalizer: sample.normalizer,
    format: sample.source_format,
    params: initialValues(module?.params),
    content,
    fileName: sample.file,
  };
}

/**
 * A scenario's detector as a row. The scenario's params are laid over the widget defaults, so
 * the form shows them and `submittedParams` sends exactly the ones that differ from the
 * catalogue — the same request a person clicking the same choices would send.
 */
export function detectorFromScenario(
  entry: Scenario["detectors"][number],
  detectors: readonly ModuleInfo[],
): DetectorRow {
  const module = detectors.find((item) => item.name === entry.name);
  const overrides = Object.fromEntries(
    Object.entries(entry.params).map(([key, value]) => [key, typeof value === "boolean" ? value : String(value)]),
  );
  return { name: entry.name, params: { ...judgeOnlyValues(module), ...overrides } };
}

export interface ComposeState {
  readonly sources: readonly SourceRow[];
  readonly detectors: readonly DetectorRow[];
  readonly engine: string;
  readonly engineParams: ParamValues;
  readonly sinks: readonly string[];
  readonly sinkParams: Readonly<Record<string, ParamValues>>;
}

/** The `PipelineSpec` the state describes — exactly what `/runs` and `/runs/validate` get. */
export function buildSpec(state: ComposeState, catalogue: ModulesResponse) {
  const find = (items: readonly ModuleInfo[], name: string) => items.find((item) => item.name === name)?.params;
  return {
    sources: state.sources.map((source) => ({
      normalizer: source.normalizer,
      source_format: source.format,
      params: submittedParams(find(catalogue.normalizers, source.normalizer), source.params),
    })),
    detectors: state.detectors.map((row) => ({
      name: row.name,
      params: submittedParams(find(catalogue.detectors, row.name), row.params),
    })),
    alert_engine: {
      name: state.engine,
      params: submittedParams(find(catalogue.alert_engines, state.engine), state.engineParams),
    },
    sinks: state.sinks.map((name) => ({
      name,
      params: submittedParams(
        catalogue.sinks.find((item) => item.name === name)?.params,
        state.sinkParams[name] ?? {},
      ),
    })),
  };
}

/** Live validation: the assembled spec goes to the guard on change, debounced (C9). */
export function useValidation(spec: unknown) {
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
