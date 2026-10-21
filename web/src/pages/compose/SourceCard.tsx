/**
 * Step 1's sources: `SourceTabs` is the wizard — Source 1, Source 2, … as steps with their
 * status, then + Add source — and `SourceCard` is the one source on screen, every field of it at
 * once (data, format, settings), with a check of what will be read at the bottom. Sources are
 * never stacked one under another.
 *
 * The card edits the same `SourceRow` as before, so what is sent is unchanged. What it adds is a
 * read of the content by the source's own normalizer (`/runs/preview`), so a file in the wrong
 * format is an error here, on the source it belongs to, rather than a malformed pile after the run.
 */
import { Check, Plus } from "lucide-react";
import * as React from "react";

import { ParamFields } from "@/components/ParamFields";
import { Button, Field, Select, Textarea } from "@/components/ui/controls";
import { InfoLines } from "@/components/ui/info";
import { Collapsible, Notice, Spinner } from "@/components/ui/surfaces";
import { describeError } from "@/lib/api/http";
import type { ConnectionInfo, DataSourceInfo, ModuleInfo, PreviewResponse, PullResponse } from "@/lib/api/types";
import { readTextFile } from "@/lib/files";
import { initialValues } from "@/lib/params";
import { fetchSampleText, type Sample } from "@/lib/samples";
import { cn } from "@/lib/utils";
import { ADAPTER_HELP, FIELD_HELP, FORMAT_HELP, paramHelp } from "@/pages/compose/help";
import { MAX_UPLOAD_BYTES, sourceFromSample, type SourcePreview, type SourceRow } from "@/pages/compose/model";
import { PullPanel } from "@/pages/compose/PullPanel";

const NO_SAMPLE = "";
const PREVIEW_LINES = 5;

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
  if (nonBlank(row.content).length === 0) return { text: "needs content", tone: "warning" };
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
    });
  };

  const chosenSource = dataSources.find((source) => source.source_id === row.dataSource);
  const connection = connections.find((item) => item.name === chosenSource?.connection);
  const pickDataSource = (sourceId: string) => {
    const source = dataSources.find((item) => item.source_id === sourceId);
    if (source === undefined) {
      onChange({ ...row, dataSource: null });
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
          }
        : { ...row, dataSource: source.source_id },
    );
  };

  // A pull fills the source as an uploaded file would, in the format its connector writes.
  const onPulled = (pulled: PullResponse) => {
    setFileError(null);
    const reader = normalizers.find((item) => item.name === pulled.normalizer);
    onChange({
      ...row,
      normalizer: pulled.normalizer,
      format: pulled.source_format,
      params: row.normalizer === pulled.normalizer ? row.params : initialValues(reader?.params),
      content: pulled.content,
      fileName: pulled.origin,
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
      onChange({ ...row, content: "", fileName: null });
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

      {dataSources.length > 0 ? (
        <Field
          label="Data source"
          htmlFor={`src_${index}_source`}
          info={FIELD_HELP.dataSource}
          hint={
            chosenSource
              ? chosenSource.normalizer
                ? `${chosenSource.type} · read by ${chosenSource.normalizer} (${chosenSource.source_format})` +
                  (chosenSource.connection ? " · pull it below, or upload / paste." : ".")
                : `${chosenSource.type} · no normalizer set for it — choose one below.`
              : "Optional: which of your data sources this is."
          }
        >
          <Select
            id={`src_${index}_source`}
            data-testid={`src_${index}_source`}
            options={[NO_SAMPLE, ...dataSources.map((source) => source.source_id)]}
            labelFor={(id) => {
              const source = dataSources.find((item) => item.source_id === id);
              return source ? `${source.type} · ${source.name}` : "— none —";
            }}
            value={chosenSource?.source_id ?? NO_SAMPLE}
            onChange={(event) => pickDataSource(event.target.value)}
          />
        </Field>
      ) : null}

      {chosenSource && connection ? (
        <PullPanel
          key={chosenSource.source_id}
          index={index}
          source={chosenSource}
          connection={connection}
          onPulled={onPulled}
        />
      ) : null}

      {samples.length > 0 ? (
        <Field
          label="Use a sample file"
          htmlFor={`src_${index}_sample`}
          info={FIELD_HELP.sample}
          hint={chosenSample ? chosenSample.description : "Or upload / paste your own below."}
        >
          <Select
            id={`src_${index}_sample`}
            data-testid={`src_${index}_sample`}
            options={[NO_SAMPLE, ...samples.map((sample) => sample.id)]}
            labelFor={(id) =>
              id === NO_SAMPLE ? "— none —" : (samples.find((sample) => sample.id === id)?.title ?? id)
            }
            value={chosenSample?.id ?? NO_SAMPLE}
            disabled={loadingSample}
            onChange={(event) => void onSample(event.target.value)}
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
          info={(param) => paramHelp("normalizer", row.normalizer, param)}
          onChange={(name, value) => onChange({ ...row, params: { ...row.params, [name]: value } })}
        />
      </Collapsible>

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

      {fileError ? <Notice tone="error">{fileError}</Notice> : null}

      <Field
        label="…or paste records"
        htmlFor={`src_${index}_text`}
        info={<InfoLines lines={[FIELD_HELP.paste, FORMAT_HELP[row.format]]} />}
        hint={FORMAT_HELP[row.format] ? `Expected: ${FORMAT_HELP[row.format]}` : undefined}
      >
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
      {row.fileName !== null ? (
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
          {(preview.result.events + preview.result.malformed).toLocaleString("en-US")} records.
        </Notice>
      ) : null}
    </div>
  );
}
