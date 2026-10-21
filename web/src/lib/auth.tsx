/**
 * Who is logged in, as server state (ADR-0039).
 *
 * The login is a query like any other: `/auth/me` answers with the user, a 401 (nobody), or
 * a 404 — the account routes are absent because the deployment set no `IOTSHIELD_AUTH`, so
 * there is nothing to log into and every page is open, exactly as before organizations
 * existed. That is the same probe-for-absence the Evaluate mode uses (ADR-0031 D5), so one
 * server flag decides login end to end and the bundle needs no flag of its own.
 *
 * The session cookie is `HttpOnly`, so this code never sees a token: it only ever learns
 * *whether* the browser's cookie is still good, by asking.
 */
import { useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { Navigate, Outlet, useLocation, useNavigate } from "react-router-dom";

import { ErrorState, Spinner } from "@/components/ui/surfaces";
import { authApi } from "@/lib/api/client";
import { ApiError, describeError, isPlaneAbsent, isUnreachable, setUnauthorizedHandler } from "@/lib/api/http";
import type { MeResponse } from "@/lib/api/types";

export type AuthState =
  /** The deployment has no login (the `/auth` routes are absent). */
  | { readonly kind: "disabled" }
  /** Login is required and this browser has no valid session. */
  | { readonly kind: "anonymous" }
  | { readonly kind: "user"; readonly me: MeResponse };

/**
 * Whether this caller administers their organization: everyone does with no login (the one
 * default organization's admin), otherwise its admins and a super admin at home in it.
 */
export function isOrgAdmin(state: AuthState | undefined): boolean {
  if (state?.kind === "disabled") return true;
  if (state?.kind !== "user") return false;
  const { me } = state;
  return me.organization !== null && (me.role === "admin" || me.role === "superadmin");
}

export const authKey = ["auth", "me"] as const;

const ANONYMOUS: AuthState = { kind: "anonymous" };

async function fetchAuthState(): Promise<AuthState> {
  try {
    return { kind: "user", me: await authApi.me() };
  } catch (error) {
    if (isPlaneAbsent(error)) return { kind: "disabled" };
    if (error instanceof ApiError && error.status === 401) return ANONYMOUS;
    throw error;
  }
}

/** The login state. Fetched once and then trusted until a 401 or a sign-in changes it. */
export const useAuth = () =>
  useQuery({ queryKey: authKey, queryFn: fetchAuthState, staleTime: Infinity });

/**
 * Forget everything the previous user could see, then become `state`.
 *
 * Clearing the whole cache is the point, not a side effect: queries are keyed by endpoint,
 * not by tenant, so without it the next user of this browser would be shown the previous
 * organization's alerts from cache until each query happened to refetch.
 */
function becomeAuthState(client: QueryClient, state: AuthState): void {
  const others = { predicate: (query: { queryKey: readonly unknown[] }) => !isAuthKey(query.queryKey) };
  // Everything but the login state itself. `client.clear()` would also drop the login query
  // while a page is still observing it, and the observer immediately refetches — racing, and
  // possibly overwriting, the state set on the next line.
  void client.cancelQueries(others);
  client.removeQueries(others);
  client.setQueryData(authKey, state);
}

const isAuthKey = (key: readonly unknown[]): boolean =>
  key.length === authKey.length && key.every((part, index) => part === authKey[index]);

export function useSignedIn(): (me: MeResponse) => void {
  const client = useQueryClient();
  return (me) => becomeAuthState(client, { kind: "user", me });
}

export function useSignOut(): () => Promise<void> {
  const client = useQueryClient();
  const navigate = useNavigate();
  return async () => {
    try {
      await authApi.logout();
    } catch {
      // The cookie may already be gone or the API down; the local state is cleared either way.
    }
    becomeAuthState(client, ANONYMOUS);
    navigate("/login", { replace: true });
  };
}

/**
 * Turns any 401 from the APIs into "you are logged out".
 *
 * Mounted once inside the query client. A session can end while a page is open — it
 * expires, or an admin removes the user — and the first request to notice is whichever
 * runs next, so `http.ts` reports it here rather than every page handling it.
 */
export function SessionExpiryBridge() {
  const client = useQueryClient();
  useEffect(() => {
    setUnauthorizedHandler(() => {
      if (client.getQueryData<AuthState>(authKey)?.kind !== "anonymous") {
        becomeAuthState(client, ANONYMOUS);
      }
    });
    return () => setUnauthorizedHandler(null);
  }, [client]);
  return null;
}

/** Where the login page sends the user once they are in: back where they were headed. */
export interface LoginRedirect {
  readonly from?: string;
}

/** Only an in-app path may be a redirect target — never another origin (`//evil.example`). */
export function safeRedirect(from: string | undefined): string {
  return from && from.startsWith("/") && !from.startsWith("//") && from !== "/login" ? from : "/";
}

/**
 * The guard around the whole app shell.
 *
 * An unreachable Pipeline API is let *through* rather than blocking every page: the auth
 * routes live there, but Monitor reads the read API, which enforces login on its own — so if
 * login is required the first Monitor request answers 401 and the bridge above redirects.
 * Blocking here instead would make a down write-plane take the read plane with it, which is
 * the coupling ADR-0015 split the two APIs to avoid.
 */
export function RequireAuth() {
  const auth = useAuth();
  const location = useLocation();

  if (auth.isPending) return <Spinner label="Checking your login…" />;
  if (auth.isError && !isUnreachable(auth.error)) {
    return (
      <div className="mx-auto max-w-xl px-6 py-10">
        <ErrorState
          title="Could not check your login"
          detail={describeError(auth.error)}
          onRetry={() => void auth.refetch()}
        />
      </div>
    );
  }
  if (auth.data?.kind === "anonymous") {
    const state: LoginRedirect = { from: `${location.pathname}${location.search}` };
    return <Navigate to="/login" replace state={state} />;
  }
  return <Outlet />;
}

/** The signed-in super admin, or null (logged out, an org user, or no login at all). */
export function useIsSuperAdmin(): boolean {
  const auth = useAuth();
  return auth.data?.kind === "user" && auth.data.me.role === "superadmin";
}

export const ADMIN_HOME = "/admin/organizations";

/**
 * A super admin with no home organization: they manage organizations and see no data at all.
 * One *with* a home (the seeded admin@admin.com, at home in UCY) sees that organization's data
 * like its admin would, so it is not this.
 */
export function useIsHomelessSuperAdmin(): boolean {
  const auth = useAuth();
  return auth.data?.kind === "user" && auth.data.me.role === "superadmin" && auth.data.me.organization === null;
}

/**
 * Around every page that shows an organization's data (Monitor, Operate, Evaluate, Users).
 *
 * A super admin without a home organization sees no organization's data (ADR-0040). The APIs
 * already refuse them with 403; this keeps the dashboard from rendering a page of refusals
 * and sends them to the one place they work.
 */
export function TenantOnly() {
  return useIsHomelessSuperAdmin() ? <Navigate to={ADMIN_HOME} replace /> : <Outlet />;
}

/** Around the super admin's pages. Anyone else is sent home rather than shown a 403. */
export function SuperAdminOnly() {
  return useIsSuperAdmin() ? <Outlet /> : <Navigate to="/" replace />;
}

/**
 * Whether the login page should offer registration. Public (no session needed), and read
 * once: `IOTSHIELD_OPEN_REGISTRATION` is a deployment setting, not something that changes
 * under an open page. While unknown it is `undefined`, so the pages offer nothing they might
 * have to take back.
 */
export function useOpenRegistration(): boolean | undefined {
  const options = useQuery({
    queryKey: ["auth", "options"] as const,
    queryFn: () => authApi.options(),
    staleTime: Infinity,
  });
  return options.data?.open_registration;
}
