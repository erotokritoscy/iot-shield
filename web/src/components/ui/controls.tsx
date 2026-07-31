/**
 * Form controls, in the shadcn spirit — the source lives here and we own it (D2).
 *
 * Native `<select>`, `<input>` and `<textarea>` under the token styling rather than a
 * headless listbox library: this is a dense operator console whose forms are the product
 * surface, and the platform controls bring keyboard, mobile and assistive behaviour for
 * free — shadcn/ui without Radix (ADR-0031).
 */
import { cva, type VariantProps } from "class-variance-authority";
import { Minus, Plus } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

const buttonStyles = cva(
  "inline-flex items-center justify-center gap-2 whitespace-nowrap rounded border text-sm font-medium transition-colors disabled:pointer-events-none disabled:opacity-45",
  {
    variants: {
      variant: {
        primary: "border-accent bg-accent text-accent-ink hover:bg-accent/90",
        secondary: "border-line bg-surface text-ink hover:bg-raised",
        ghost: "border-transparent bg-transparent text-dim hover:bg-raised hover:text-ink",
      },
      size: {
        sm: "h-7 px-2.5 text-xs",
        md: "h-9 px-3.5",
      },
    },
    defaultVariants: { variant: "secondary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonStyles> {}

export function Button({ className, variant, size, ...props }: ButtonProps) {
  return <button className={cn(buttonStyles({ variant, size }), className)} {...props} />;
}

interface FieldProps {
  readonly label: string;
  readonly htmlFor?: string;
  readonly hint?: React.ReactNode;
  readonly children: React.ReactNode;
  readonly className?: string;
}

export function Field({ label, htmlFor, hint, children, className }: FieldProps) {
  return (
    <div className={cn("min-w-0", className)}>
      <label className="field-label" htmlFor={htmlFor}>
        {label}
      </label>
      {children}
      {hint ? <p className="caption mt-1">{hint}</p> : null}
    </div>
  );
}

export interface SelectProps extends React.SelectHTMLAttributes<HTMLSelectElement> {
  readonly options: readonly string[];
  readonly labelFor?: (option: string) => string;
}

export function Select({ options, labelFor, className, ...props }: SelectProps) {
  return (
    <select className={cn("control", className)} {...props}>
      {options.map((option) => (
        <option key={option} value={option}>
          {labelFor ? labelFor(option) : option}
        </option>
      ))}
    </select>
  );
}

export function TextInput({
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input type="text" className={cn("control", className)} {...props} />;
}

export function NumberInput({
  className,
  ...props
}: React.InputHTMLAttributes<HTMLInputElement>) {
  return <input type="number" className={cn("control tabular", className)} {...props} />;
}

export function Textarea({
  className,
  ...props
}: React.TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={cn("control font-mono text-xs leading-relaxed", className)} {...props} />;
}

interface CheckboxProps extends Omit<React.InputHTMLAttributes<HTMLInputElement>, "type"> {
  /** A node rather than a string so a caller can append a reason beside the name, which the
   * wrapping `<label>` then folds into the accessible name — the reason is read out with the
   * option instead of being decoration a screen reader never reaches. */
  readonly label: React.ReactNode;
}

export function Checkbox({ label, className, id, ...props }: CheckboxProps) {
  const generated = React.useId();
  const inputId = id ?? generated;
  return (
    <div className="flex items-center gap-2">
      <input
        id={inputId}
        type="checkbox"
        className={cn(
          "h-4 w-4 rounded border-line text-accent accent-[rgb(var(--c-accent))]",
          className,
        )}
        {...props}
      />
      <label htmlFor={inputId} className="text-sm text-ink">
        {label}
      </label>
    </div>
  );
}

interface RadioGroupProps {
  readonly name: string;
  /** The group's accessible name. Defaults to `name`, which is an identifier, not prose. */
  readonly label?: string;
  readonly options: readonly { value: string; label: string }[];
  readonly value: string;
  readonly onValueChange: (value: string) => void;
  readonly className?: string;
}

export function RadioGroup({
  name,
  label,
  options,
  value,
  onValueChange,
  className,
}: RadioGroupProps) {
  return (
    // `name` is a form field name (`eval_protocol`), not prose. Announcing it as the group's
    // accessible name reads out an identifier; a radiogroup is a div, so the visible `Field`
    // label beside it cannot be associated with `htmlFor` and has to be passed in.
    <div
      role="radiogroup"
      aria-label={label ?? name}
      className={cn("flex flex-wrap gap-x-5 gap-y-2", className)}
    >
      {options.map((option) => (
        <label key={option.value} className="flex cursor-pointer items-center gap-2 text-sm">
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={value === option.value}
            onChange={() => onValueChange(option.value)}
            className="h-3.5 w-3.5 accent-[rgb(var(--c-accent))]"
          />
          <span className={value === option.value ? "text-ink" : "text-dim"}>{option.label}</span>
        </label>
      ))}
    </div>
  );
}

/**
 * How many rows a repeatable section has (sources, detector instances, benchmark entries).
 *
 * Streamlit expressed this as a number input ("How many detectors?"); a React form adds and
 * removes a row directly, which states the same 1–N bound as an action rather than as a
 * count to retype into a clamped field (ADR-0031).
 */
export function RowCount({
  label,
  count,
  max,
  testId,
  onChange,
}: {
  readonly label: string;
  readonly count: number;
  readonly max: number;
  readonly testId: string;
  readonly onChange: (count: number) => void;
}) {
  return (
    <div className="flex items-center gap-2">
      <Button
        size="sm"
        data-testid={`${testId}_add`}
        disabled={count >= max}
        onClick={() => onChange(count + 1)}
      >
        <Plus className="h-3.5 w-3.5" aria-hidden /> Add {label}
      </Button>
      <Button
        size="sm"
        variant="ghost"
        data-testid={`${testId}_remove`}
        disabled={count <= 1}
        onClick={() => onChange(count - 1)}
      >
        <Minus className="h-3.5 w-3.5" aria-hidden /> Remove last
      </Button>
      <span className="caption">
        {count} of {max}
      </span>
    </div>
  );
}

interface MultiSelectProps {
  readonly label: string;
  readonly options: readonly string[];
  readonly value: readonly string[];
  readonly onValueChange: (value: string[]) => void;
  readonly hint?: React.ReactNode;
  /** Why an option will not do what its name suggests, keyed by option.
   *
   * An annotated option stays **selectable**. A disabled checkbox with a tooltip is the same
   * refusal as omitting it, and the case this exists for is a choice that is futile rather
   * than invalid: the run is legal, produces a real result, and explains itself.
   */
  readonly notes?: Readonly<Record<string, string>>;
}

/**
 * A checkbox list rather than a `<select multiple>`: multi-select via ctrl-click is a
 * discoverability trap, and these lists (detectors in an entry, sinks) are short.
 */
export function MultiSelect({
  label,
  options,
  value,
  onValueChange,
  hint,
  notes,
}: MultiSelectProps) {
  const toggle = (option: string) => {
    // Preserve the *offered* order rather than click order, so a spec's label reads the
    // same however it was assembled.
    const next = value.includes(option)
      ? value.filter((item) => item !== option)
      : options.filter((item) => item === option || value.includes(item));
    onValueChange([...next]);
  };
  return (
    <fieldset className="min-w-0">
      <legend className="field-label">{label}</legend>
      <div className="flex flex-wrap gap-x-4 gap-y-1.5 rounded border border-line bg-surface px-3 py-2">
        {options.length === 0 ? (
          <span className="caption">Nothing to choose from.</span>
        ) : (
          options.map((option) => {
            const note = notes?.[option];
            return (
              <Checkbox
                key={option}
                label={
                  note === undefined ? (
                    option
                  ) : (
                    <>
                      {option} <span className="text-dim">— {note}</span>
                    </>
                  )
                }
                checked={value.includes(option)}
                onChange={() => toggle(option)}
              />
            );
          })
        )}
      </div>
      {hint ? <p className="caption mt-1">{hint}</p> : null}
    </fieldset>
  );
}
