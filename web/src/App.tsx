import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/AppShell";
import { RequireAuth, SessionExpiryBridge, SuperAdminOnly, TenantOnly } from "@/lib/auth";
import { AccountPage } from "@/pages/AccountPage";
import { OrganizationPage } from "@/pages/admin/OrganizationPage";
import { OrganizationsPage } from "@/pages/admin/OrganizationsPage";
import { MonitorGate } from "@/pages/MonitorGate";
import { AssetsPage } from "@/pages/AssetsPage";
import { LoginPage } from "@/pages/auth/LoginPage";
import { RegisterPage } from "@/pages/auth/RegisterPage";
import { ComposePage } from "@/pages/ComposePage";
import { DataHealthPage } from "@/pages/DataHealthPage";
import { DataSourcesPage } from "@/pages/DataSourcesPage";
import { EvaluatePage } from "@/pages/evaluate/EvaluatePage";
import { ItOverviewPage } from "@/pages/overview/ItOverviewPage";
import { MeterPage } from "@/pages/overview/MeterPage";
import { OtOverviewPage } from "@/pages/overview/OtOverviewPage";
import { RunPage } from "@/pages/RunPage";
import { RunsPage } from "@/pages/RunsPage";
import { TriagePage } from "@/pages/TriagePage";
import { UsersPage } from "@/pages/UsersPage";

/**
 * One retry-free client: a dead API should reach the page as a friendly error immediately,
 * not after three silent retries (M1–M3). Nothing polls (H5).
 */
export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false, gcTime: 5 * 60_000 },
      mutations: { retry: false },
    },
  });
}

export function AppRoutes() {
  return (
    <Routes>
      {/* Reachable logged-out; both bounce to the app when there is nothing to log into. */}
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />
      {/* Everything else needs a session when the deployment requires one (ADR-0039). */}
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          {/* Everything that shows an organization's data. The super admin sees none of it
              and is sent to Organizations instead (ADR-0040). */}
          <Route element={<TenantOnly />}>
            {/* Monitor's three views sit behind the read-side health gate; Operate and
                Evaluate deliberately do not — they talk to the Pipeline API (or to a local
                file) and handle their own reachability, exactly as `main()` routed around
                the gate. */}
            <Route element={<MonitorGate />}>
              <Route path="/" element={<TriagePage />} />
              <Route path="/assets" element={<AssetsPage />} />
              <Route path="/data-health" element={<DataHealthPage />} />
              <Route path="/overview/it" element={<ItOverviewPage />} />
              <Route path="/overview/ot" element={<OtOverviewPage />} />
              <Route path="/overview/ot/meter/:assetId" element={<MeterPage />} />
              <Route path="/runs/:runId" element={<RunPage />} />
            </Route>
            <Route path="/compose" element={<ComposePage />} />
            <Route path="/runs" element={<RunsPage />} />
            <Route path="/data-sources" element={<DataSourcesPage />} />
            <Route path="/evaluate" element={<EvaluatePage />} />
            <Route path="/users" element={<UsersPage />} />
          </Route>
          <Route element={<SuperAdminOnly />}>
            <Route path="/admin/organizations" element={<OrganizationsPage />} />
            <Route path="/admin/organizations/:orgId" element={<OrganizationPage />} />
          </Route>
          <Route path="/account" element={<AccountPage />} />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Route>
    </Routes>
  );
}

export function App({ client }: { readonly client?: QueryClient }) {
  return (
    <QueryClientProvider client={client ?? makeQueryClient()}>
      <SessionExpiryBridge />
      <AppRoutes />
    </QueryClientProvider>
  );
}
