import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterAll, afterEach, beforeAll, vi } from "vitest";

import { server } from "./msw";

/**
 * Vega is stubbed in component tests, deliberately.
 *
 * jsdom has no layout engine, so a rendered Vega view measures nothing — "the chart
 * rendered" would not be evidence that the chart is right. The *specs* carry the dataviz
 * decisions, and they are unit-tested directly in `src/charts/specs.test.ts` (the curve/bars
 * decision table, the zero-span floor, UTC feeding). Page tests assert that the right spec
 * reaches the page, through this stub's `data-spec` attribute.
 */
vi.mock("react-vega", async () => {
  const React = await import("react");
  return {
    VegaLite: ({ spec }: { spec: unknown }) =>
      React.createElement("div", {
        "data-testid": "vega-spec",
        "data-spec": JSON.stringify(spec),
      }),
  };
});

beforeAll(() => server.listen({ onUnhandledRequest: "error" }));
afterEach(() => {
  server.resetHandlers();
  cleanup();
});
afterAll(() => server.close());
