/**
 * The shell: a fixed left sidebar whose groups mirror the planes (brief §7).
 *
 * Monitor is the read plane (browse what the pipeline produced), Operate the write plane
 * (compose and trigger a run), Evaluate the third plane that *judges* the pipeline rather
 * than using it — the same grouping `dashboard/app.py: main` explains, kept because it is
 * the shape of the product, not a menu layout.
 */
import { NavLink, Outlet } from "react-router-dom";

import { ThemeToggle } from "@/components/ThemeToggle";
import { useEvalPlane } from "@/lib/queries";
import { cn } from "@/lib/utils";

interface NavItem {
  readonly to: string;
  readonly label: string;
}

const MONITOR: NavItem[] = [
  { to: "/", label: "Triage" },
  { to: "/assets", label: "Assets & telemetry" },
  { to: "/data-health", label: "Data health" },
];

const OPERATE: NavItem[] = [{ to: "/compose", label: "Compose & run" }];
const EVALUATE: NavItem[] = [{ to: "/evaluate", label: "Benchmarks" }];

function NavGroup({ title, items }: { readonly title: string; readonly items: NavItem[] }) {
  return (
    <div className="mb-6">
      <p className="mb-1.5 px-3 text-2xs font-semibold uppercase tracking-widest text-muted">
        {title}
      </p>
      <ul className="space-y-0.5">
        {items.map((item) => (
          <li key={item.to}>
            <NavLink
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "block rounded px-3 py-1.5 text-sm transition-colors",
                  isActive
                    ? "bg-accent-soft font-medium text-accent"
                    : "text-dim hover:bg-raised hover:text-ink",
                )
              }
            >
              {item.label}
            </NavLink>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function AppShell() {
  // D5 — a static bundle cannot read the server's env, so the Evaluate mode is gated by
  // *probing* the plane instead of by a second flag. A 404 means the router was never
  // registered (ADR-0021 D1: absent, not guarded), which is the deployment saying no.
  const evalPlane = useEvalPlane();

  return (
    <div className="flex min-h-full">
      <aside className="fixed inset-y-0 left-0 flex w-56 flex-col border-r border-line bg-surface">
        <div className="flex items-center justify-between px-3 py-4">
          <span className="text-sm font-semibold tracking-tight text-ink">iotshield</span>
          <ThemeToggle />
        </div>
        <nav className="flex-1 overflow-y-auto px-2 py-2">
          <NavGroup title="Monitor" items={MONITOR} />
          <NavGroup title="Operate" items={OPERATE} />
          {evalPlane.available ? <NavGroup title="Evaluate" items={EVALUATE} /> : null}
        </nav>
        <p className="px-3 py-3 text-2xs text-muted">All timestamps UTC</p>
      </aside>
      <main className="ml-56 min-w-0 flex-1 px-8 py-7">
        <div className="mx-auto min-w-0 max-w-[1400px]">
          <Outlet />
        </div>
      </main>
    </div>
  );
}
