/**
 * Step 1's sources: `SourceTabs` is the wizard — Source 1, Source 2, … as steps with their
 * status, then + Add source — and `SourceCard` is the one source on screen, with a check of what
 * will be read at the bottom. Sources are never stacked one under another.
 *
 * A card opens on one question — how does this source's data arrive: pull, sample, upload or
 * paste? — and draws only that way's fields. All four used to be on screen at once, each silently
 * replacing the others, so a pasted block could hide behind a pull. Choosing a way drops what
 * the previous way supplied (`withMode`); a pull or sample brings its own reader and shows it
 * rather than offering it.
 *
 * The card edits the same `SourceRow` as before, so what is sent is unchanged. What it adds is a
 * read of the content by the source's own normalizer (`/runs/preview`), so a file in the wrong
 * format is an error here, on the source it belongs to, rather than a malformed pile after the run.
 */
import { Check, Plus } from "lucide-react";
import * as React from "react";

import { ParamFields } from "@/components/ParamFields";
import { Button, Field, Select, Textarea } from "@/components/ui/controls";
import { InfoLines, InfoTip } from "@/components/ui/info";
import { Collapsible, Notice, Spinner } from "@/components/ui/surfaces";
import { describeError } from "@/lib/api/http";
import type { ConnectionInfo, DataSourceInfo, ModuleInfo, PreviewResponse, PullResponse } from "@/lib/api/types";
import { readTextFile } from "@/lib/files";
import { initialValues } from "@/lib/params";
import { fetchSampleText, type Sample } from "@/lib/samples";
import { cn } from "@/lib/utils";
import { ADAPTER_HELP, FIELD_HELP, FORMAT_HELP, paramHelp } from "@/pages/compose/help";
import {
  isFilled,
  MAX_UPLOAD_BYTES,
  sourceFromSample,
  withMode,
  type PulledWindow,
  type SourceMode,
  type SourcePreview,
  type SourceRow,
} from "@/pages/compose/model";
import { PullPanel } from "@/pages/compose/PullPanel";

const NO_SAMPLE = "";
const PREVIEW_LINES = 5;

/** The ways a source's data can arrive, in the order offered, with what each one is. */
const MODES: readonly { readonly mode: SourceMode; readonly label: string; readonly hint: string }[] = [
  { mode: "pull", label: "Pull from a data source", hint: "A date range of a connected database." },
  { mode: "sample", label: "Sample file", hint: "A small file from the built-in library." },
  { mode: "upload", label: "Upload a file", hint: "A text file from your computer." },
  { mode: "paste", label: "Paste records", hint: "Lines typed or pasted here." },
];

/**
 * The first decision on a source: how its data arrives. Only that way's fields are drawn
 * below it, so a card never offers four inputs at once that silently replace each other.
 */
function ModeChoice({
  index,
  mode,
  offered,
  onChoose,
}: {
  readonly index: number;
  readonly mode: SourceMode | null;
  readonly offered: readonly SourceMode[];
  readonly onChoose: (mode: SourceMode) => void;
}) {
  return (
    <div>
      <div className="mb-1 flex items-center gap-1">
        <p className="field-label mb-0" id={`src_${index}_mode_label`}>
          How will this source's data arrive?
        </p>
        <InfoTip label="How will this source's data arrive?">{FIELD_HELP.mode}</InfoTip>
      </div>
      <div
        role="radiogroup"
        aria-labelledby={`src_${index}_mode_label`}
        className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4"
      >
        {MODES.filter((item) => offered.includes(item.mode)).map((item) => {
          const checked = item.mode === mode;
          return (
            <button
              key={item.mode}
              type="button"
              role="radio"
              aria-checked={checked}
              data-testid={`src_${index}_mode_${item.mode}`}
              onClick={() => onChoose(item.mode)}
              className={cn(
                "rounded-md border px-3 py-2 text-left text-sm transition-colors",
                checked ? "border-accent bg-accent-soft text-accent" : "border-line bg-surface text-dim hover:bg-raised",
              )}
            >
              <span className="block font-medium">{item.label}</span>
              <span className="block text-2xs opacity-80">{item.hint}</span>
            </button>
          );
        })}
      </div>
      {mode === null ? <p className="caption mt-2">Choose one to see its fields.</p> : null}
    </div>
  );
}

const sourceLabel = (sources: readonly DataSourceInfo[]) => (id: string) => {
  const source = sources.find((item) => item.source_id === id);
  return source ? `${source.type} · ${source.name}` : "— none —";
};

const readerHint = (source: DataSourceInfo) =>
  source.normalizer
    ? `${source.type} · read by ${source.normalizer} (${source.source_format})`
    : `${source.type} · no normalizer set for it.`;

/** The reader a pull or sample set, shown rather than offered: they bring their own. */
function ReaderLine({ index, row }: { readonly index: number; readonly row: SourceRow }) {
  return (
    <p className="caption" data-testid={`src_${index}_reader`}>
      Read by <span className="font-mono">{row.normalizer}</span>
      {row.format ? <> (<span className="font-mono">{row.format}</span>)</> : null}. To read it another way, upload
      or paste it instead.
    </p>
  );
}

/** A chain-check finding about this source (`/runs/validate`, stage `sources[i]`). */
export interface SourceIssue {
  readonly level: string;
  readonly message: string;
}

const nonBlank = (content: string) => content.split("\n").filter((line) => line.trim());
const originOf = (row: SourceRow, index: number) => row.fileName ?? `source-${index + 1}`;

/** A problem with a source, in two words, and whether it stops the wizard or only warns. */
export interface SourceProblem {
  readonly text: string;
  readonly tone: "error" | "warning";
}

/** Nothing this normalizer could read: almost always the wrong normalizer, format or file. */
export const unreadable = (preview: SourcePreview) => preview.result?.level === "unreadable";

/** What is wrong with a source, or null when nothing is. */
export function sourceProblem(
  row: SourceRow,
  issues: readonly SourceIssue[],
  preview: SourcePreview,
): SourceProblem | null {
  if (!isFilled(row)) return { text: "needs content", tone: "warning" };
  if (unreadable(preview)) return { text: "wrong format?", tone: "error" };
  if (issues.some((issue) => issue.level === "error")) return { text: "has an error", tone: "error" };
  if (preview.result?.level === "mostly_rejected") return { text: "mostly unreadable", tone: "warning" };
  if (issues.length > 0) return { text: "has a warning", tone: "warning" };
  return null;
}

/** The sources as steps: one per source with its status, and + Add source at the end. */
export function SourceTabs({
  rows,
  active,
  issuesFor,
  previews,
  canAdd,
  onSelect,
  onAdd,
}: {
  readonly rows: readonly SourceRow[];
  readonly active: number;
  readonly issuesFor: (index: number) => readonly SourceIssue[];
  readonly previews: readonly SourcePreview[];
  readonly canAdd: boolean;
  readonly onSelect: (index: number) => void;
  readonly onAdd: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Sources">
      {rows.map((row, index) => {
        const current = index === active;
        const problem = sourceProblem(row, issuesFor(index), previews[index] ?? { result: null, error: null, pending: false });
        return (
          <button
            key={index}
            type="button"
            data-testid={`src_${index}_tab`}
            aria-current={current ? "step" : undefined}
            title={problem?.text}
            onClick={() => onSelect(index)}
            className={cn(
              "flex min-w-0 max-w-[16rem] items-center gap-2 rounded-md border px-3 py-1.5 text-left text-sm transition-colors",
              current ? "border-accent bg-accent-soft text-accent" : "border-line bg-surface text-dim hover:bg-raised",
            )}
          >
            <span
              className={cn(
                "flex h-5 w-5 shrink-0 items-center justify-center rounded-full border text-2xs font-semibold",
                current
                  ? "border-accent bg-accent text-accent-ink"
                  : problem
                    ? problem.tone === "error"
                      ? "border-critical text-critical"
                      : "border-warning text-warning"
                    : "border-success text-success",
              )}
            >
              {!current && problem === null ? <Check className="h-3 w-3" aria-hidden /> : index + 1}
            </span>
            <span className="min-w-0">
              <span className="block font-medium">Source {index + 1}</span>
              <span className="block truncate font-mono text-2xs opacity-80">
                {problem?.text ?? originOf(row, index)}
              </span>
            </span>
          </button>
        );
      })}
      <Button size="sm" data-testid="compose_src_add" disabled={!canAdd} onClick={onAdd}>
        <Plus className="h-3.5 w-3.5" aria-hidden /> Add source
      </Button>
    </div>
  );
}

/** The selected source, all of it on one card, and on to the next source or a new one. */
export function SourceCard({
  index,
  row,
  normalizers,
  samples,
  dataSources = [],
  connections = [],
  issues,
  checking,
  preview,
  onChange,
  onNextSource,
  onAddSource,
  onRemove,
}: {
  readonly index: number;
  readonly row: SourceRow;
  readonly normalizers: readonly ModuleInfo[];
  readonly samples: readonly Sample[];
  /** The organization's data sources; picking one fills in how its data is read. */
  readonly dataSources?: readonly DataSourceInfo[];
  /** The source databases this organization may pull from (ADR-0042). */
  readonly connections?: readonly ConnectionInfo[];
  readonly issues: readonly SourceIssue[];
  /** The chain check is still settling, so `issues` may describe an older edit. */
  readonly checking: boolean;
  /** This source read by its normalizer: how much of it is readable. */
  readonly preview: SourcePreview;
  readonly onChange: (next: SourceRow) => void;
  /** Move to the following source; null on the last one. */
  readonly onNextSource: (() => void) | null;
  /** Open a new source after this one; null when the limit is reached. */
  readonly onAddSource: (() => void) | null;
  readonly onRemove: (() => void) | null;
}) {
  const module = normalizers.find((item) => item.name === row.normalizer);
  const formats = module?.formats ?? [];
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [loadingSample, setLoadingSample] = React.useState(false);
  const chosenSample = samples.find((sample) => sample.file === row.fileName);
  const lines = nonBlank(row.content);

  const pickNormalizer = (name: string) => {
    const next = normalizers.find((item) => item.name === name);
    onChange({
      ...row,
      normalizer: name,
      format: next?.formats[0] ?? "",
      params: initialValues(next?.params),
      // Reading it some other way means it is no longer read as that data source says.
      dataSource: null,
      pull: null,
    });
  };

  const chosenSource = dataSources.find((source) => source.source_id === row.dataSource);
  const connection = connections.find((item) => item.name === chosenSource?.connection);
  // Only a source whose connection this organization may use can be pulled from.
  const pullable = dataSources.filter((source) =>
    connections.some((item) => item.name === source.connection),
  );
  // A way that cannot apply here is not offered — except the one already chosen.
  const offered = MODES.map((item) => item.mode).filter(
    (mode) =>
      mode === row.mode ||
      (mode === "pull" ? pullable.length > 0 : mode === "sample" ? samples.length > 0 : true),
  );
  const pickDataSource = (sourceId: string) => {
    const source = dataSources.find((item) => item.source_id === sourceId);
    if (source === undefined) {
      onChange({ ...row, dataSource: null, pull: null });
      return;
    }
    const reader = normalizers.find((item) => item.name === source.normalizer);
    onChange(
      reader && source.source_format
        ? {
            ...row,
            dataSource: source.source_id,
            normalizer: reader.name,
            format: source.source_format,
            params: initialValues(reader.params),
            pull: null,
          }
        : { ...row, dataSource: source.source_id, pull: null },
    );
  };

  // A pull sets the source to the format its connector writes and remembers the window: the
  // run reads the rows itself (ADR-0047). `content` holds the sample, which is what the
  // preview checks the format against — the same normalizer, on real connector output.
  const onPulled = (pulled: PullResponse, window: Omit<PulledWindow, "source_id" | "rows">) => {
    setFileError(null);
    const reader = normalizers.find((item) => item.name === pulled.normalizer);
    onChange({
      ...row,
      normalizer: pulled.normalizer,
      format: pulled.source_format,
      params: row.normalizer === pulled.normalizer ? row.params : initialValues(reader?.params),
      content: pulled.sample,
      fileName: pulled.origin,
      pull: { ...window, source_id: row.dataSource ?? "", rows: pulled.rows },
    });
  };

  const onFile = async (file: File | null) => {
    setFileError(null);
    if (file === null) {
      onChange({ ...row, content: "", fileName: null, pull: null });
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
      onChange({ ...row, content: await readTextFile(file), fileName: file.name, pull: null });
    } catch (cause) {
      // Swallowing this left the operator believing a file was attached.
      setFileError(`Could not read ${file.name}: ${describeError(cause)}`);
    }
  };

  // A sample brings its own normalizer and format: a Zeek log handed to the CSV normalizer
  // would be 100% rejected, so choosing one sets all three together.
  const onSample = async (sampleId: string) => {
    setFileError(null);
    const sample = samples.find((item) => item.id === sampleId);
    if (sample === undefined) {
      onChange({ ...row, content: "", fileName: null, pull: null });
      return;
    }
    setLoadingSample(true);
    try {
      onChange(sourceFromSample(sample, await fetchSampleText(sample), normalizers));
    } catch (cause) {
      setFileError(describeError(cause));
    } finally {
      setLoadingSample(false);
    }
  };

  return (
    <div data-testid={`src_${index}_card`} className="space-y-4 rounded-lg border border-accent/50 bg-surface p-4">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted">Source {index + 1}</p>
        {onRemove ? (
          <Button size="sm" variant="ghost" data-testid={`src_${index}_remove`} onClick={onRemove}>
            Remove source
          </Button>
        ) : null}
      </div>

      <ModeChoice
        index={index}
        mode={row.mode ?? null}
        offered={offered}
        onChoose={(mode) => {
          setFileError(null);
          onChange(withMode(row, mode));
        }}
      />

      {row.mode === "pull" ? (
        <>
          <Field
            label="Data source"
            htmlFor={`src_${index}_source`}
            info={FIELD_HELP.dataSource}
            hint={chosenSource ? readerHint(chosenSource) : "Which of your connected data sources to pull from."}
          >
            <Select
              id={`src_${index}_source`}
              data-testid={`src_${index}_source`}
              options={[NO_SAMPLE, ...pullable.map((source) => source.source_id)]}
              labelFor={sourceLabel(pullable)}
              value={chosenSource?.source_id ?? NO_SAMPLE}
              onChange={(event) => pickDataSource(event.target.value)}
            />
          </Field>
          {chosenSource && connection ? (
            <PullPanel
              key={chosenSource.source_id}
              index={index}
              source={chosenSource}
              connection={connection}
              onPulled={onPulled}
            />
          ) : null}
          {chosenSource ? <ReaderLine index={index} row={row} /> : null}
        </>
      ) : null}

      {row.mode === "sample" ? (
        <>
          <Field
            label="Sample file"
            htmlFor={`src_${index}_sample`}
            info={FIELD_HELP.sample}
            hint={chosenSample ? chosenSample.description : "A small file from the built-in library."}
          >
            <Select
              id={`src_${index}_sample`}
              data-testid={`src_${index}_sample`}
              options={[NO_SAMPLE, ...samples.map((sample) => sample.id)]}
              labelFor={(id) =>
                id === NO_SAMPLE ? "— choose one —" : (samples.find((sample) => sample.id === id)?.title ?? id)
              }
              value={chosenSample?.id ?? NO_SAMPLE}
              disabled={loadingSample}
              onChange={(event) => void onSample(event.target.value)}
            />
          </Field>
          {/* A sample brings its own reader; overriding it is what Upload / Paste are for. */}
          {chosenSample ? <ReaderLine index={index} row={row} /> : null}
        </>
      ) : null}

      {row.mode === "upload" || row.mode === "paste" ? (
        <>
          {dataSources.length > 0 ? (
            <Field
              label="Data source"
              htmlFor={`src_${index}_source`}
              info={FIELD_HELP.dataSource}
              hint={
                chosenSource
                  ? chosenSource.normalizer
                    ? readerHint(chosenSource)
                    : `${chosenSource.type} · no normalizer set for it — choose one below.`
                  : "Optional: which of your data sources this is; it fills in the reader."
              }
            >
              <Select
                id={`src_${index}_source`}
                data-testid={`src_${index}_source`}
                options={[NO_SAMPLE, ...dataSources.map((source) => source.source_id)]}
                labelFor={sourceLabel(dataSources)}
                value={chosenSource?.source_id ?? NO_SAMPLE}
                onChange={(event) => pickDataSource(event.target.value)}
              />
            </Field>
          ) : null}

          <div className="grid gap-3 sm:grid-cols-2">
            <Field
              label="Normalizer"
              htmlFor={`src_${index}_norm`}
              info={
                <InfoLines
                  lines={[
                    FIELD_HELP.normalizer,
                    ADAPTER_HELP[`normalizer:${row.normalizer}`],
                    ...formats.map((format) => FORMAT_HELP[format]),
                  ]}
                />
              }
            >
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
              <Field
                label="Input format"
                htmlFor={`src_${index}_fmt`}
                info={<InfoLines lines={[FIELD_HELP.format, FORMAT_HELP[row.format]]} />}
              >
                <Select
                  id={`src_${index}_fmt`}
                  data-testid={`src_${index}_fmt`}
                  options={formats}
                  value={row.format}
                  onChange={(event) => onChange({ ...row, format: event.target.value, pull: null })}
                />
              </Field>
            ) : null}
          </div>
        </>
      ) : null}

      {row.mode === "upload" ? (
        <div className="grid gap-3 sm:grid-cols-2">
          <Field label="Upload a file" htmlFor={`src_${index}_file`} info={FIELD_HELP.upload}>
            <input
              id={`src_${index}_file`}
              data-testid={`src_${index}_file`}
              type="file"
              className="control file:mr-3 file:rounded file:border-0 file:bg-raised file:px-2 file:py-1 file:text-xs file:text-ink"
              onChange={(event) => void onFile(event.target.files?.[0] ?? null)}
            />
          </Field>
          <Field label="Origin" info={FIELD_HELP.origin} hint="Provenance recorded with every record from this source.">
            <div className="flex items-center gap-2">
              <p className="control min-w-0 flex-1 truncate bg-raised font-mono text-xs">{originOf(row, index)}</p>
              {row.fileName !== null ? (
                <Button size="sm" variant="ghost" data-testid={`src_${index}_clear`} onClick={() => void onFile(null)}>
                  Remove file
                </Button>
              ) : null}
            </div>
          </Field>
        </div>
      ) : null}

      {row.mode === "paste" ? (
        <Field
          label="Paste records"
          htmlFor={`src_${index}_text`}
          info={<InfoLines lines={[FIELD_HELP.paste, FORMAT_HELP[row.format]]} />}
          hint={FORMAT_HELP[row.format] ? `Expected: ${FORMAT_HELP[row.format]}` : undefined}
        >
          <Textarea
            id={`src_${index}_text`}
            data-testid={`src_${index}_text`}
            rows={5}
            placeholder="CSV for stub, log lines for syslog."
            value={row.content}
            onChange={(event) => onChange({ ...row, content: event.target.value, fileName: null, pull: null })}
          />
        </Field>
      ) : null}

      {fileError ? <Notice tone="error">{fileError}</Notice> : null}

      {row.mode ? (
        <Collapsible label={`${row.normalizer} parameters`}>
          <ParamFields
            params={module?.params}
            values={row.params}
            idPrefix={`src_${index}`}
            info={(param) => paramHelp("normalizer", row.normalizer, param)}
            onChange={(name, value) => onChange({ ...row, params: { ...row.params, [name]: value } })}
          />
        </Collapsible>
      ) : null}

      {row.pull ? (
        <p className="caption" data-testid={`src_${index}_pulled`}>
          {row.pull.rows === null ? "A window" : `${row.pull.rows.toLocaleString("en-US")} rows`} of{" "}
          <span className="font-mono">{row.fileName}</span>, read from the database when the run starts.
        </p>
      ) : row.fileName !== null && row.mode === "upload" ? (
        <p className="caption">
          {lines.length.toLocaleString("en-US")} lines loaded from <span className="font-mono">{row.fileName}</span>.
        </p>
      ) : null}

      <SourceCheck index={index} row={row} lines={lines} issues={issues} checking={checking} preview={preview} />

      {onAddSource || onNextSource ? (
        <div className="flex items-center justify-end gap-2 border-t border-line pt-3">
          {onAddSource ? (
            <Button size="sm" data-testid={`src_${index}_add_next`} onClick={onAddSource}>
              <Plus className="h-3.5 w-3.5" aria-hidden /> Add another source
            </Button>
          ) : null}
          {onNextSource ? (
            <Button size="sm" variant="primary" data-testid={`src_${index}_next_source`} onClick={onNextSource}>
              Next: Source {index + 2} →
            </Button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

/** The first rejected records, by line, as the normalizer explained them. */
function RejectedLines({ index, result }: { readonly index: number; readonly result: PreviewResponse }) {
  if (result.rejected.length === 0) return null;
  return (
    <ul data-testid={`src_${index}_rejected`} className="mt-1.5 space-y-1">
      {result.rejected.map((line, position) => (
        <li key={position} className="text-xs">
          <span className="font-medium">{line.line !== null ? `Line ${line.line}` : "A record"}:</span> {line.error}
          <code className="mt-0.5 block truncate text-2xs opacity-80">{line.raw}</code>
        </li>
      ))}
      {result.malformed > result.rejected.length ? (
        <li className="text-xs opacity-80">…and {(result.malformed - result.rejected.length).toLocaleString("en-US")} more.</li>
      ) : null}
    </ul>
  );
}

/** What the normalizer made of this source, as one notice — or a spinner while it reads. */
function ReadCheck({
  index,
  row,
  preview,
}: {
  readonly index: number;
  readonly row: SourceRow;
  readonly preview: SourcePreview;
}) {
  const reader = row.format ? `${row.normalizer} (${row.format})` : row.normalizer;
  if (preview.pending) return <Spinner label={`Reading this source with ${reader}…`} />;
  if (preview.error !== null) {
    return <Notice tone="warning">Could not check what {reader} can read: {preview.error}</Notice>;
  }
  const result = preview.result;
  if (result === null) return null;
  const total = result.events + result.malformed;
  const count = (n: number) => n.toLocaleString("en-US");
  switch (result.level) {
    case "unreadable":
      return (
        <div data-testid={`src_${index}_read_error`}>
          <Notice tone="error" title="Wrong format?">
            {reader} could not read any of the {count(total)} records in this source. Check that the normalizer and input
            format match the data, or pick another file.
            <RejectedLines index={index} result={result} />
          </Notice>
        </div>
      );
    case "mostly_rejected":
      return (
        <div data-testid={`src_${index}_read_warning`}>
          <Notice tone="warning" title="Mostly unreadable">
            {reader} could read only {count(result.events)} of {count(total)} records; the input may not match the chosen
            normalizer or format. The rest will be kept on Monitor → Data health.
            <RejectedLines index={index} result={result} />
          </Notice>
        </div>
      );
    case "some_rejected":
      return (
        <div data-testid={`src_${index}_read_note`}>
          <Notice tone="info">
            {count(result.events)} of {count(total)} records read; {count(result.malformed)} cannot be read and will be kept
            on Monitor → Data health.
            <RejectedLines index={index} result={result} />
          </Notice>
        </div>
      );
    case "empty":
      return <Notice tone="warning">{reader} found no records in this input.</Notice>;
    case "clean":
      return null;
  }
}

/** What will be read — the first lines of it — and what the checks say of this source. */
function SourceCheck({
  index,
  row,
  lines,
  issues,
  checking,
  preview,
}: {
  readonly index: number;
  readonly row: SourceRow;
  readonly lines: readonly string[];
  readonly issues: readonly SourceIssue[];
  readonly checking: boolean;
  readonly preview: SourcePreview;
}) {
  const readable = preview.result !== null && (preview.result.level === "clean" || preview.result.level === "some_rejected");
  return (
    <div className="space-y-3" data-testid={`src_${index}_check`}>
      {lines.length > 0 ? (
        <div>
          <p className="field-label">
            First {Math.min(PREVIEW_LINES, lines.length)} of {lines.length.toLocaleString("en-US")} lines
            {row.pull ? " of the sample" : ""}
          </p>
          <pre
            data-testid={`src_${index}_preview`}
            className="max-h-40 overflow-auto rounded border border-line bg-raised p-2 font-mono text-2xs leading-relaxed"
          >
            {lines.slice(0, PREVIEW_LINES).join("\n")}
          </pre>
        </div>
      ) : null}

      {lines.length > 0 ? <ReadCheck index={index} row={row} preview={preview} /> : null}

      {checking ? (
        <Spinner label="Checking this source against the chain…" />
      ) : (
        issues.map((issue, position) => (
          <Notice key={position} tone={issue.level === "error" ? "error" : "warning"}>
            {issue.message}
          </Notice>
        ))
      )}
      {!checking && issues.length === 0 && readable && preview.result ? (
        <Notice tone="success">
          Ready: {row.normalizer} reads {preview.result.events.toLocaleString("en-US")} of{" "}
          {(preview.result.events + preview.result.malformed).toLocaleString("en-US")} records
          {row.pull ? " in the sample" : ""}.
        </Notice>
      ) : null}
    </div>
  );
}
