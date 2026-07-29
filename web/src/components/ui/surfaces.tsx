/**
 * Panels, notices and disclosure — the page's furniture.
 *
 * `Notice` is the direct translation of Streamlit's `st.error` / `st.warning` / `st.info` /
 * `st.success`. Keeping one component for all four is what makes the parity audit mechanical:
 * every one of those calls in `dashboard/app.py` becomes a `<Notice tone=…>` here, with its
 * microcopy carried over near-verbatim (brief §7).
 */
import { AlertTriangle, ChevronRight, CircleAlert, CircleCheck, Info, Loader2 } from "lucide-react";
import * as React from "react";

import { Button } from "@/components/ui/controls";
import { cn } from "@/lib/utils";

export function Card({
  className,
  children,
}: {
  readonly className?: string;
  readonly children: React.ReactNode;
}) {
  return (
    <div className={cn("rounded-lg border border-line bg-surface", className)}>{children}</div>
  );
}

export function Section({
  title,
  description,
  actions,
  children,
  className,
}: {
  readonly title?: React.ReactNode;
  readonly description?: React.ReactNode;
  readonly actions?: React.ReactNode;
  readonly children?: React.ReactNode;
  readonly className?: string;
}) {
  return (
    <section className={cn("space-y-3", className)}>
      {title || actions ? (
        <div className="flex items-baseline justify-between gap-4">
          <div className="min-w-0">
            {title ? (
              <h2 className="text-sm font-semibold tracking-tight text-ink">{title}</h2>
            ) : null}
            {description ? <p className="caption mt-0.5">{description}</p> : null}
          </div>
          {actions}
        </div>
      ) : null}
      {children}
    </section>
  );
}

export function PageHeader({
  title,
  description,
}: {
  readonly title: string;
  readonly description: React.ReactNode;
}) {
  return (
    <header className="mb-6">
      <h1 className="text-xl font-semibold tracking-tight text-ink">{title}</h1>
      <p className="caption mt-1 max-w-3xl">{description}</p>
    </header>
  );
}

export type Tone = "error" | "warning" | "info" | "success";

const toneStyles: Record<Tone, { wrap: string; icon: React.ComponentType<{ className?: string }> }> =
  {
    error: { wrap: "border-critical/40 bg-critical/10 text-critical", icon: CircleAlert },
    warning: { wrap: "border-warning/40 bg-warning/10 text-warning", icon: AlertTriangle },
    info: { wrap: "border-info/40 bg-info/10 text-info", icon: Info },
    success: { wrap: "border-success/40 bg-success/10 text-success", icon: CircleCheck },
  };

export function Notice({
  tone,
  title,
  children,
  className,
}: {
  readonly tone: Tone;
  readonly title?: React.ReactNode;
  readonly children?: React.ReactNode;
  readonly className?: string;
}) {
  const { wrap, icon: Icon } = toneStyles[tone];
  return (
    <div
      role={tone === "error" ? "alert" : "status"}
      className={cn("flex gap-2.5 rounded-md border px-3 py-2.5 text-sm", wrap, className)}
    >
      <Icon className="mt-0.5 h-4 w-4 shrink-0" aria-hidden />
      <div className="min-w-0 space-y-1 leading-relaxed [&_code]:font-mono [&_code]:text-xs">
        {title ? <p className="font-medium">{title}</p> : null}
        {children ? <div className="text-ink/90">{children}</div> : null}
      </div>
    </div>
  );
}

/** A shell command the reader is meant to run — monospace, selectable, never truncated. */
export function CommandBlock({ children }: { readonly children: React.ReactNode }) {
  return (
    <pre className="mt-2 overflow-x-auto rounded border border-line bg-raised px-3 py-2 font-mono text-xs leading-relaxed text-ink">
      <code>{children}</code>
    </pre>
  );
}

export function JsonBlock({ value, className }: { readonly value: unknown; readonly className?: string }) {
  return (
    <pre
      className={cn(
        "max-h-96 overflow-auto rounded border border-line bg-raised p-3 font-mono text-xs leading-relaxed text-ink",
        className,
      )}
    >
      {JSON.stringify(value, null, 2)}
    </pre>
  );
}

export function Collapsible({
  label,
  children,
  defaultOpen = false,
  className,
}: {
  readonly label: React.ReactNode;
  readonly children: React.ReactNode;
  readonly defaultOpen?: boolean;
  readonly className?: string;
}) {
  const [open, setOpen] = React.useState(defaultOpen);
  return (
    <div className={cn("rounded border border-line bg-surface", className)}>
      <button
        type="button"
        aria-expanded={open}
        onClick={() => setOpen((value) => !value)}
        className="flex w-full items-center gap-1.5 px-3 py-2 text-left text-xs font-medium text-dim hover:text-ink"
      >
        <ChevronRight
          className={cn("h-3.5 w-3.5 shrink-0 transition-transform", open && "rotate-90")}
          aria-hidden
        />
        {label}
      </button>
      {open ? <div className="border-t border-line px-3 py-3">{children}</div> : null}
    </div>
  );
}

export function Spinner({ label }: { readonly label: string }) {
  return (
    <div className="flex items-center gap-2 text-sm text-dim" role="status">
      <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
      {label}
    </div>
  );
}

/**
 * "You are not looking at all of it."
 *
 * Takes the whole `Capped` page rather than loose props, and returns null itself when the cap
 * did not bite — the visibility rule is the reason this component exists, so leaving it to a
 * ternary at each call site is how a fifth capped list ships without a disclaimer. Taking the
 * page object also makes it impossible to report one endpoint's `limit` beside another's
 * `truncated`.
 *
 * A permanent disclaimer would be noise that gets tuned out, leaving the one case that matters
 * indistinguishable from the 99 that do not.
 */
export function Truncated({
  page,
  noun,
  total,
  order,
}: {
  readonly page: { readonly truncated: boolean; readonly limit: number };
  readonly noun: string;
  /** An uncapped count, where one exists. `/summary` has them for the unfiltered lists only. */
  readonly total?: number | undefined;
  /** How the server chose which ones survived — the part that decides if the view is biased. */
  readonly order: string;
}) {
  if (!page.truncated) return null;
  const shown = page.limit.toLocaleString("en-US");
  return (
    <Notice
      tone="warning"
      title={
        total === undefined
          ? `Showing the first ${shown} ${noun}`
          : `Showing ${shown} of ${total.toLocaleString("en-US")} ${noun}`
      }
    >
      <p>
        The API caps this list, so this page is {order}.
        {/* Only where a page actually shows counts. On Assets there are none, and claiming
            otherwise would send the reader looking for a number that is not there. */}
        {total === undefined
          ? " Anything drawn from the table below describes this slice only."
          : " Counts above come from the database and are complete; anything drawn from the table below describes this slice only."}
      </p>
    </Notice>
  );
}

/**
 * The retry control every failure state carries.
 *
 * One definition because the label and the variant are a decision, and two copies of a
 * decision drift. `errors.test.tsx` asserts on the label.
 */
export function RetryButton({ onRetry }: { readonly onRetry: () => void }) {
  return (
    <Button size="sm" variant="ghost" onClick={onRetry}>
      Try again
    </Button>
  );
}

/** The full-page failure state a health gate renders (M1). */
export function ErrorState({
  title,
  detail,
  command,
  onRetry,
}: {
  readonly title: string;
  readonly detail?: React.ReactNode;
  readonly command?: string;
  /**
   * A way out. Typed as the callback rather than as a `ReactNode` slot on purpose: a slot
   * accepts anything and therefore asks for nothing, which is how one of this app's three
   * API-failure states shipped without any recovery at all.
   */
  readonly onRetry?: () => void;
}) {
  return (
    <Notice tone="error" title={title}>
      {detail ? <p>{detail}</p> : null}
      {command ? <CommandBlock>{command}</CommandBlock> : null}
      {onRetry ? <RetryButton onRetry={onRetry} /> : null}
    </Notice>
  );
}
