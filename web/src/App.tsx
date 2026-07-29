import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, Route, Routes } from "react-router-dom";

import { AppShell } from "@/components/AppShell";
import { MonitorGate } from "@/pages/MonitorGate";
import { AssetsPage } from "@/pages/AssetsPage";
import { ComposePage } from "@/pages/ComposePage";
import { DataHealthPage } from "@/pages/DataHealthPage";
import { EvaluatePage } from "@/pages/evaluate/EvaluatePage";
import { TriagePage } from "@/pages/TriagePage";

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
      <Route element={<AppShell />}>
        {/* Monitor's three views sit behind the read-side health gate; Operate and Evaluate
            deliberately do not — they talk to the Pipeline API (or to a local file) and
            handle their own reachability, exactly as `main()` routed around the gate. */}
        <Route element={<MonitorGate />}>
          <Route path="/" element={<TriagePage />} />
          <Route path="/assets" element={<AssetsPage />} />
          <Route path="/data-health" element={<DataHealthPage />} />
        </Route>
        <Route path="/compose" element={<ComposePage />} />
        <Route path="/evaluate" element={<EvaluatePage />} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Route>
    </Routes>
  );
}

export function App({ client }: { readonly client?: QueryClient }) {
  return (
    <QueryClientProvider client={client ?? makeQueryClient()}>
      <AppRoutes />
    </QueryClientProvider>
  );
}
