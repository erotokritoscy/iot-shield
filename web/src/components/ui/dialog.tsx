/**
 * A modal dialog: a panel over a dimmed page, for a task that must not lose the page behind it
 * (Compose's *Create new data source*, which would otherwise drop the chain composed so far).
 *
 * Built on a portal rather than the native `<dialog>`, whose `showModal()` jsdom does not
 * implement, so the tests would exercise a different element than the browser. It does what a
 * modal owes a keyboard user: focus moves into the panel on open and back to where it was on
 * close, Tab stays inside the panel, and Escape, the close button or a click on the backdrop
 * closes it.
 */
import { X } from "lucide-react";
import * as React from "react";
import { createPortal } from "react-dom";

import { cn } from "@/lib/utils";

const FIELDS = "input:not([disabled]), select:not([disabled]), textarea:not([disabled])";
const FOCUSABLE =
  'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export function Dialog({
  open,
  title,
  description,
  onClose,
  children,
  className,
  "data-testid": testId,
}: {
  readonly open: boolean;
  readonly title: React.ReactNode;
  readonly description?: React.ReactNode;
  readonly onClose: () => void;
  readonly children: React.ReactNode;
  readonly className?: string;
  readonly "data-testid"?: string;
}) {
  const panel = React.useRef<HTMLDivElement>(null);
  const body = React.useRef<HTMLDivElement>(null);
  const titleId = React.useId();
  const descriptionId = React.useId();
  const close = React.useRef(onClose);
  close.current = onClose;

  React.useEffect(() => {
    if (!open) return undefined;
    const before = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    // The first field of the content — not the close button before it, nor a field's (i).
    const first =
      body.current?.querySelector<HTMLElement>(FIELDS) ?? body.current?.querySelector<HTMLElement>(FOCUSABLE);
    (first ?? panel.current)?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.stopPropagation();
        close.current();
        return;
      }
      if (event.key !== "Tab" || !panel.current) return;
      const items = [...panel.current.querySelectorAll<HTMLElement>(FOCUSABLE)];
      if (items.length === 0) return;
      const [head, tail] = [items[0], items[items.length - 1]];
      if (event.shiftKey && document.activeElement === head) {
        event.preventDefault();
        tail?.focus();
      } else if (!event.shiftKey && document.activeElement === tail) {
        event.preventDefault();
        head?.focus();
      }
    };
    document.addEventListener("keydown", onKey);
    const overflow = document.body.style.overflow;
    document.body.style.overflow = "hidden"; // the page behind does not scroll under the panel
    return () => {
      document.removeEventListener("keydown", onKey);
      document.body.style.overflow = overflow;
      before?.focus();
    };
  }, [open]);

  if (!open) return null;
  return createPortal(
    <div
      className="fixed inset-0 z-40 flex items-start justify-center overflow-y-auto bg-ink/40 px-4 py-10"
      data-testid={testId ? `${testId}_backdrop` : undefined}
      onMouseDown={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descriptionId : undefined}
        tabIndex={-1}
        data-testid={testId}
        className={cn(
          "w-full max-w-2xl rounded-lg border border-line bg-surface p-5 shadow-lg focus:outline-none",
          className,
        )}
      >
        <div className="mb-4 flex items-start justify-between gap-4">
          <div className="min-w-0">
            <h2 id={titleId} className="text-sm font-semibold tracking-tight text-ink">
              {title}
            </h2>
            {description ? (
              <p id={descriptionId} className="caption mt-1">
                {description}
              </p>
            ) : null}
          </div>
          <button
            type="button"
            aria-label="Close"
            data-testid={testId ? `${testId}_close` : undefined}
            onClick={onClose}
            className="rounded p-1 text-muted hover:bg-raised hover:text-ink"
          >
            <X className="h-4 w-4" aria-hidden />
          </button>
        </div>
        <div ref={body}>{children}</div>
      </div>
    </div>,
    document.body,
  );
}
