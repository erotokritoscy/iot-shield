/**
 * Which cached fit to judge with: the ids the adapter ships, offered rather than recalled.
 *
 * The same rule every other closed vocabulary follows (ADR-0023 D9). This was the one
 * free-text box left, and the most expensive one to get wrong — an unrecognised id decides
 * whether the detector can judge at all (ADR-0035 D1).
 *
 * Shared by both pages that choose weights (ADR-0036): Compose, where it is the only thing a
 * learning detector can be given because a run never fits, and Evaluate, where it is one of
 * two exclusive modes. Lives in `components/` beside `ParamFields` rather than in the
 * Evaluate feature folder, so the always-present page does not import from the optional one.
 *
 * Falls back to free text only when an adapter declares an artifact param and enumerates
 * nothing, where a dropdown would offer no way forward. Both arms take identical props, so
 * they are written once and spread; the handler is typed structurally because a `<select>` and
 * an `<input>` disagree about their event's element type and agree about `target.value`.
 */
import type * as React from "react";

import { Field, Select, TextInput } from "@/components/ui/controls";
import { artifactIdsOf, type Artifacted } from "@/lib/artifact";

export function ArtifactField({
  summary,
  label,
  id,
  value,
  onChange,
  info,
}: {
  readonly summary: Artifacted;
  /** The artifact param's name, passed narrowed — only a detector that has one gets here. */
  readonly label: string;
  readonly id: string;
  readonly value: string;
  readonly onChange: (artifact: string) => void;
  readonly info?: React.ReactNode;
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
      info={info}
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
