/**
 * One input per adapter param, typed off the `/modules` (or `/eval/detectors`) schema (C3).
 *
 * Values are still validated by the guard and the contracts, so free-text entry is safe
 * here. Which of them are *submitted* is `lib/params.ts`'s job — see C4.
 */
import { Checkbox, Field, NumberInput, Select, TextInput } from "@/components/ui/controls";
import type { ParamInfo } from "@/lib/api/types";
import { isNumeric, type ParamValue, type ParamValues } from "@/lib/params";

export function ParamFields({
  params,
  values,
  onChange,
  idPrefix,
}: {
  readonly params: readonly ParamInfo[] | null | undefined;
  readonly values: ParamValues;
  readonly onChange: (name: string, value: ParamValue) => void;
  readonly idPrefix: string;
}) {
  const list = params ?? [];
  if (list.length === 0) {
    return <p className="caption">This adapter has no parameters.</p>;
  }
  return (
    <div className="grid gap-3 sm:grid-cols-2">
      {list.map((param) => {
        const id = `${idPrefix}_${param.name}`;
        const value = values[param.name] ?? "";
        if (param.choices && param.choices.length > 0) {
          // A closed vocabulary the factory declared (a preset, a lane): offer it rather
          // than making the operator type it from memory into a free-text box, where a typo
          // surfaces only as a guard error after the fact (ADR-0023 D9).
          return (
            <Field key={param.name} label={param.name} htmlFor={id}>
              <Select
                id={id}
                data-testid={id}
                options={param.choices}
                value={String(value)}
                onChange={(event) => onChange(param.name, event.target.value)}
              />
            </Field>
          );
        }
        if (isNumeric(param)) {
          return (
            <Field
              key={param.name}
              label={param.name}
              htmlFor={id}
              hint={
                param.default === null || param.default === undefined
                  ? "optional — leave empty to let the adapter decide"
                  : undefined
              }
            >
              <NumberInput
                id={id}
                data-testid={id}
                step={param.type === "int" ? 1 : "any"}
                value={String(value)}
                onChange={(event) => onChange(param.name, event.target.value)}
              />
            </Field>
          );
        }
        if (param.type === "bool") {
          return (
            <div key={param.name} className="flex items-end pb-1.5">
              <Checkbox
                id={id}
                data-testid={id}
                label={param.name}
                checked={value === true}
                onChange={(event) => onChange(param.name, event.target.checked)}
              />
            </div>
          );
        }
        return (
          <Field key={param.name} label={param.name} htmlFor={id}>
            <TextInput
              id={id}
              data-testid={id}
              value={String(value)}
              onChange={(event) => onChange(param.name, event.target.value)}
            />
          </Field>
        );
      })}
    </div>
  );
}
