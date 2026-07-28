import { QueryClient } from "@tanstack/react-query";
import { render, type RenderResult } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import { App } from "@/App";

/** The whole app at a route, with a fresh cache so no test inherits another's answers. */
export function renderApp(route = "/"): RenderResult {
  const client = new QueryClient({
    defaultOptions: {
      queries: { retry: false, gcTime: 0, staleTime: 0 },
      mutations: { retry: false },
    },
  });
  return render(
    <MemoryRouter initialEntries={[route]}>
      <App client={client} />
    </MemoryRouter>,
  );
}
