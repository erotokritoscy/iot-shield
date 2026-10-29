/**
 * The shell: a fixed left sidebar whose groups mirror the planes (brief §7).
 *
 * Monitor is the read plane (browse what the pipeline produced), Operate the write plane
 * (compose and trigger a run), Evaluate the third plane that *judges* the pipeline rather
 * than using it — the same grouping `dashboard/app.py: main` explains, kept because it is
 * the shape of the product, not a menu layout.
 */
import { useQueryClient } from "@tanstack/react-query";
import { LogOut, RotateCw } from "lucide-react";
import { NavLink, Outlet } from "react-router-dom";

import { ThemeToggle } from "@/components/ThemeToggle";
import { Button } from "@/components/ui/controls";
import { useAuth, useSignOut } from "@/lib/auth";
import { LatchProvider } from "@/lib/latch";
import { isBenchmarkResult, useEvalPlane } from "@/lib/queries";
import { cn } from "@/lib/utils";

interface NavItem {
  readonly to: string;
  readonly label: string;
}

const MONITOR: NavItem[] = [
  { to: "/", label: "Triage" },
  { to: "/overview/it", label: "IT overview" },
  { to: "/overview/ot", label: "OT overview" },
  { to: "/assets", label: "Assets & telemetry" },
  { to: "/data-health", label: "Data health" },
];

const OPERATE: NavItem[] = [
  { to: "/compose", label: "Compose & run" },
  { to: "/data-sources", label: "Data sources" },
];
const EVALUATE: NavItem[] = [{ to: "/evaluate", label: "Benchmarks" }];
const ACCOUNT: NavItem = { to: "/account", label: "Account" };
const USERS: NavItem = { to: "/users", label: "Users" };
const ORGANIZATIONS: NavItem = { to: "/admin/organizations", label: "Organizations" };

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
  const client = useQueryClient();
  // Only a logged-in deployment has an organization to show; with no login (`disabled`) the
  // sidebar is exactly what it was before ADR-0039.
  const auth = useAuth();
  const me = auth.data?.kind === "user" ? auth.data.me : null;
  // A super admin manages every organization; with no home organization they see no data
  // (ADR-0040), so the three data planes are not offered at all — only the platform group and
  // their own account. With a home they get that organization's pages *and* the platform group.
  const superAdmin = me?.role === "superadmin";
  const homeless = superAdmin && me?.organization === null;
  const manageUsers = me?.role === "admin" || (superAdmin && !homeless);
  const signOut = useSignOut();
  // Everything EXCEPT a benchmark result. Expressed as the one exclusion it really is rather
  // than as a list of what is safe: scoping to the read plane also made the button a silent
  // no-op on Compose and Evaluate, whose catalogues are cheap and safe to re-read. The
  // exclusion is not cosmetic — `invalidateQueries` refetches active queries regardless of
  // `staleTime: Infinity`, so including the eval-run key would silently re-run a benchmark.
  const refresh = () =>
    void client.invalidateQueries({ predicate: (query) => !isBenchmarkResult(query.queryKey) });

  return (
    <div className="flex min-h-full">
      <aside className="fixed inset-y-0 left-0 flex w-56 flex-col border-r border-line bg-surface">
        <div className="flex items-center justify-between px-3 py-4">
          <span className="text-sm font-semibold tracking-tight text-ink">iotshield</span>
          <ThemeToggle />
        </div>
        <nav className="flex-1 overflow-y-auto px-2 py-2">
          {homeless ? (
            <NavGroup title="Platform" items={[ORGANIZATIONS, ACCOUNT]} />
          ) : (
            <>
              <NavGroup title="Monitor" items={MONITOR} />
              <NavGroup title="Operate" items={OPERATE} />
              {/* The probe cannot answer before the first paint, and `available` is false while it
                  is in flight — so branching on `loading` renders nothing either way and the
                  sidebar still grows underneath the cursor when the answer lands. The only fix
                  that fixes it is to reserve the space the answer will need. */}
              {evalPlane.loading ? (
                <div className="mb-6 h-[3.25rem]" aria-hidden />
              ) : evalPlane.available ? (
                <NavGroup title="Evaluate" items={EVALUATE} />
              ) : null}
              {me ? <NavGroup title="Organization" items={manageUsers ? [ACCOUNT, USERS] : [ACCOUNT]} /> : null}
              {superAdmin ? <NavGroup title="Platform" items={[ORGANIZATIONS]} /> : null}
            </>
          )}
        </nav>
        <div className="space-y-2 px-3 py-3">
          {me ? (
            <div className="mb-1 border-b border-line pb-3">
              <p className="truncate text-sm font-medium text-ink" title={me.username}>
                {me.username}
              </p>
              <p className="truncate text-2xs text-muted" title={me.organization?.name ?? "platform"}>
                {[me.organization?.name, me.role === "superadmin" ? "super admin" : me.role]
                  .filter(Boolean)
                  .join(" · ")}
              </p>
              <Button size="sm" variant="ghost" className="mt-2 w-full" onClick={() => void signOut()}>
                <LogOut className="h-3.5 w-3.5" aria-hidden /> Sign out
              </Button>
            </div>
          ) : null}
          {/* Nothing here polls or refetches on focus (H5, and a queue that re-ranks under the
              cursor is worse than a stale one). That makes an explicit refresh the only way to
              ask for current data without reloading the page. */}
          <Button size="sm" variant="ghost" className="w-full" onClick={refresh}>
            <RotateCw className="h-3.5 w-3.5" aria-hidden /> Refresh data
          </Button>
          <p className="text-2xs text-muted">All timestamps UTC</p>
        </div>
      </aside>
      <main className="ml-56 min-w-0 flex-1 px-8 py-7">
        <div className="mx-auto min-w-0 max-w-[1400px]">
          {/* Above the routes, so a finished benchmark survives a look at Triage. */}
          <LatchProvider>
            <Outlet />
          </LatchProvider>
        </div>
      </main>
    </div>
  );
}
