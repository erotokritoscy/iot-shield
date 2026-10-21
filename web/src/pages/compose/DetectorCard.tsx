import { ArtifactField } from "@/components/ArtifactField";
import { ParamFields } from "@/components/ParamFields";
import { Button, Field, Select } from "@/components/ui/controls";
import { InfoLines } from "@/components/ui/info";
import { Collapsible } from "@/components/ui/surfaces";
import type { ModuleInfo } from "@/lib/api/types";
import { judgeOnlyValues, learnsWeights } from "@/lib/artifact";
import { ADAPTER_HELP, FIELD_HELP, paramHelp } from "@/pages/compose/help";
import { feeds, missingInputs, type DetectorRow } from "@/pages/compose/model";

/**
 * One detector instance: which adapter, and how it is configured.
 *
 * Params are keyed by row *index*, never by adapter name — that is the whole reason the same
 * adapter can appear twice with different presets (C5).
 *
 * The list is ordered with the detectors your sources can feed first, and each option says
 * whether it can: a detector whose inputs no source produces is offered, not hidden (a
 * deliberately silent run is a real measurement — ADR-0038 D1), but it should never look like
 * an equal choice.
 */
export function DetectorCard({
  index,
  row,
  detectors,
  produced,
  onChange,
  onRemove,
}: {
  readonly index: number;
  readonly row: DetectorRow;
  readonly detectors: readonly ModuleInfo[];
  readonly produced: ReadonlySet<string>;
  readonly onChange: (next: DetectorRow) => void;
  readonly onRemove: (() => void) | null;
}) {
  const module = detectors.find((item) => item.name === row.name);
  // Narrowed once, as a const: `learnsWeights` is the one spelling of "does this adapter judge
  // with a cached fit".
  const artifactParam = module !== undefined && learnsWeights(module) ? module.artifact_param : null;
  const ordered = [
    ...detectors.filter((item) => feeds(item, produced)),
    ...detectors.filter((item) => !feeds(item, produced)),
  ];
  const missing = module ? missingInputs(module, produced) : [];

  return (
    <div className="space-y-3 rounded-lg border border-line bg-surface p-4">
      <div className="flex items-end gap-3">
        <Field
          label={`Detector ${index + 1}`}
          htmlFor={`det_${index}_name`}
          className="flex-1"
          info={<InfoLines lines={[FIELD_HELP.detector, ADAPTER_HELP[`detector:${row.name}`]]} />}
        >
          <Select
            id={`det_${index}_name`}
            data-testid={`det_${index}_name`}
            options={ordered.map((item) => item.name)}
            labelFor={(name) => {
              const item = detectors.find((candidate) => candidate.name === name);
              return item && !feeds(item, produced) ? `${name} — no source feeds it` : name;
            }}
            value={row.name}
            onChange={(event) => {
              const name = event.target.value;
              onChange({ name, params: judgeOnlyValues(detectors.find((item) => item.name === name)) });
            }}
          />
        </Field>
        {onRemove ? (
          <Button size="sm" variant="ghost" data-testid={`det_${index}_remove`} onClick={onRemove}>
            Remove
          </Button>
        ) : null}
      </div>
      {module && missing.length > 0 ? (
        <p className="caption text-warning">
          None of your sources produce {missing.map((token) => <code key={token}>{token}</code>)} — this
          detector will run and find nothing.
        </p>
      ) : module ? (
        <p className="caption">Reads {module.consumes.join(" + ")}, which your sources provide.</p>
      ) : null}
      {artifactParam !== null && module !== undefined ? (
        // A learning adapter gets its artifact and NO hyperparameters (ADR-0036 D1): a run never
        // fits, so naming a knob beside an artifact is refused at construction.
        <div className="space-y-2">
          <ArtifactField
            summary={module}
            label={artifactParam}
            id={`det_${index}_art`}
            info={FIELD_HELP.artifact}
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
            info={(param) => paramHelp("detector", row.name, param)}
            onChange={(name, value) => onChange({ ...row, params: { ...row.params, [name]: value } })}
          />
        </Collapsible>
      )}
    </div>
  );
}
