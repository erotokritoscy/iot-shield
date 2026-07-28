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

/** The full-page failure state a health gate renders (M1). */
export function ErrorState({
  title,
  detail,
  command,
}: {
  readonly title: string;
  readonly detail?: React.ReactNode;
  readonly command?: string;
}) {
  return (
    <Notice tone="error" title={title}>
      {detail ? <p>{detail}</p> : null}
      {command ? <CommandBlock>{command}</CommandBlock> : null}
    </Notice>
  );
}
