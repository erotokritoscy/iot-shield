import * as React from "react";

import { ParamFields } from "@/components/ParamFields";
import { Button, Field, Select, Textarea } from "@/components/ui/controls";
import { Collapsible, Notice } from "@/components/ui/surfaces";
import { describeError } from "@/lib/api/http";
import type { ModuleInfo } from "@/lib/api/types";
import { readTextFile } from "@/lib/files";
import { initialValues } from "@/lib/params";
import { fetchSampleText, type Sample } from "@/lib/samples";
import { MAX_UPLOAD_BYTES, sourceFromSample, type SourceRow } from "@/pages/compose/model";

const NO_SAMPLE = "";

/** One input source: which normalizer reads it, and where its content comes from. */
export function SourceCard({
  index,
  row,
  normalizers,
  samples,
  onChange,
  onRemove,
}: {
  readonly index: number;
  readonly row: SourceRow;
  readonly normalizers: readonly ModuleInfo[];
  readonly samples: readonly Sample[];
  readonly onChange: (next: SourceRow) => void;
  readonly onRemove: (() => void) | null;
}) {
  const module = normalizers.find((item) => item.name === row.normalizer);
  const formats = module?.formats ?? [];
  const [fileError, setFileError] = React.useState<string | null>(null);
  const [loadingSample, setLoadingSample] = React.useState(false);
  const chosenSample = samples.find((sample) => sample.file === row.fileName);

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
    <div className="space-y-3 rounded-lg border border-line bg-surface p-4">
      <div className="flex items-center justify-between">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted">Source {index + 1}</p>
        {onRemove ? (
          <Button size="sm" variant="ghost" data-testid={`src_${index}_remove`} onClick={onRemove}>
            Remove source
          </Button>
        ) : null}
      </div>

      {samples.length > 0 ? (
        <Field
          label="Use a sample file"
          htmlFor={`src_${index}_sample`}
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
      {row.fileName !== null ? (
        <p className="caption">
          {row.content.split("\n").filter((line) => line.trim()).length.toLocaleString("en-US")} lines
          loaded from <span className="font-mono">{row.fileName}</span>.
        </p>
      ) : null}
    </div>
  );
}
