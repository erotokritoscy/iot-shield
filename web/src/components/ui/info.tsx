/**
 * The (i) beside a form label: what this field expects, one tap away.
 *
 * Opens on hover and on keyboard focus, and a click *pins* it open so a phone (no hover) and
 * a reader who wants to keep it on screen both get it; Escape or a click elsewhere closes a
 * pinned one. The button sits beside the `<label>`, never inside it, so the control's
 * accessible name stays the label alone and the explanation is its *description*.
 */
import { Info } from "lucide-react";
import * as React from "react";

import { cn } from "@/lib/utils";

export function InfoTip({
  label,
  children,
  className,
}: {
  /** The field it explains — names the button ("About Normalizer") for assistive tech. */
  readonly label: string;
  readonly children: React.ReactNode;
  readonly className?: string;
}) {
  const id = React.useId();
  const wrapper = React.useRef<HTMLSpanElement>(null);
  const [hovered, setHovered] = React.useState(false);
  const [focused, setFocused] = React.useState(false);
  const [pinned, setPinned] = React.useState(false);
  const open = hovered || focused || pinned;

  React.useEffect(() => {
    if (!pinned) return;
    const onPointer = (event: PointerEvent) => {
      if (wrapper.current?.contains(event.target as Node)) return;
      // A click elsewhere closes it outright, whatever opened it.
      setPinned(false);
      setFocused(false);
      setHovered(false);
    };
    document.addEventListener("pointerdown", onPointer);
    return () => document.removeEventListener("pointerdown", onPointer);
  }, [pinned]);

  return (
    <span
      ref={wrapper}
      className={cn("relative inline-flex", className)}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
    >
      <button
        type="button"
        aria-label={`About ${label}`}
        aria-expanded={pinned}
        aria-describedby={open ? id : undefined}
        className="inline-flex h-4 w-4 items-center justify-center rounded-full text-muted transition-colors hover:text-accent focus-visible:text-accent focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-accent"
        onClick={() => setPinned((value) => !value)}
        onFocus={() => setFocused(true)}
        onBlur={() => setFocused(false)}
        onKeyDown={(event) => {
          if (event.key === "Escape") {
            setPinned(false);
            setFocused(false);
            setHovered(false);
          }
        }}
      >
        <Info className="h-3.5 w-3.5" aria-hidden />
      </button>
      {open ? (
        <span
          id={id}
          role="tooltip"
          className="absolute left-0 top-full z-30 mt-1 w-72 max-w-[calc(100vw-2rem)] rounded-md border border-line bg-raised p-2.5 text-xs font-normal normal-case leading-relaxed tracking-normal text-ink shadow-lg"
        >
          {children}
        </span>
      ) : null}
    </span>
  );
}

/** Several short paragraphs inside one InfoTip: the first in full ink, the rest dimmed. */
export function InfoLines({ lines }: { readonly lines: readonly (string | null | undefined)[] }) {
  const shown = lines.filter((line): line is string => typeof line === "string" && line.length > 0);
  return (
    <>
      {shown.map((line, index) => (
        <span key={index} className={cn("block", index > 0 && "mt-1.5 text-dim")}>
          {line}
        </span>
      ))}
    </>
  );
}
