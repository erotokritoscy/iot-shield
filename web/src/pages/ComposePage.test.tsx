/**
 * The orchestration page's load-bearing form semantics (C4, C5, C6, C9, C10).
 *
 * These are the assertions that need the *outgoing request body*, not the rendered DOM: what
 * makes a run succeed or fail is which parameters the page declared, and that is invisible
 * on screen by construction.
 */
import { screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it } from "vitest";

import { captured } from "@/test/msw";
import { renderApp } from "@/test/render";

const CSV = "2026-07-07T12:00:00+00:00,pump-02,Pressure,96.0";

interface RunBody {
  spec: {
    sources: { normalizer: string; source_format: string; params: Record<string, unknown> }[];
    detectors: { name: string; params: Record<string, unknown> }[];
    alert_engine: { name: string; params: Record<string, unknown> };
    sinks: { name: string; params: Record<string, unknown> }[];
  };
  inputs: { content: string; origin: string }[];
}

async function openCompose(): Promise<HTMLSelectElement> {
  renderApp("/compose");
  return (await screen.findByTestId("det_0_name")) as HTMLSelectElement;
}

describe("Compose & run", () => {
  beforeEach(() => {
    captured.runBody = null;
    captured.validateBody = null;
  });

  it("preselects a detector the sources feed AND that is ready to run (C6)", async () => {
    // `meter_iforest` sorts first and declares an `artifact_param`, so it is *untrained*
    // until one is named and a run refuses it outright. Alphabetical preselection would open
    // the page on a chain that cannot run — which is exactly what happened when it joined
    // the registry.
    const detector = await openCompose();
    expect(detector.value).toBe("stub_threshold");

    // The normalizer is chosen by the format it declares, not by its name: `flow` sorts
    // before `stub`, so a name-ordered default would open on a flow chain fed telemetry CSV.
    expect((screen.getByTestId("src_0_norm") as HTMLSelectElement).value).toBe("stub");
  });

  it("shows a format picker only when the normalizer declares more than one dialect (C2)", async () => {
    await openCompose();
    expect(screen.queryByTestId("src_0_fmt")).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByTestId("src_0_norm"), "flow");
    expect(await screen.findByTestId("src_0_fmt")).toBeInTheDocument();
  });

  it("reports a compatible chain, then runs it and renders the receipt (C9, C10)", async () => {
    await openCompose();
    expect(await screen.findByText("Chain is compatible.")).toBeInTheDocument();

    // The run button is gated on validation AND on every source having content.
    expect(screen.getByTestId("compose_run")).toBeDisabled();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    expect(await screen.findByText("Run complete.")).toBeInTheDocument();
    expect(screen.getByTestId("stat-events")).toHaveTextContent("1");
    expect(screen.getByTestId("stat-malformed")).toHaveTextContent("0");
    expect(screen.getByTestId("stat-detections")).toHaveTextContent("1");
    expect(screen.getByTestId("stat-alerts")).toHaveTextContent("1");
    expect(screen.getByText("Written to: postgres")).toBeInTheDocument();
    // postgres was written and alerts exist, so the hand-off to the browse surface appears.
    expect(screen.getByText(/see them ranked and explained under/)).toBeInTheDocument();

    const body = captured.runBody as RunBody;
    expect(body.inputs).toEqual([{ content: CSV, origin: "source-1" }]);
    expect(body.spec.sources[0]).toEqual({
      normalizer: "stub",
      source_format: "stub-csv",
      params: {},
    });
  });

  it("omits every untouched parameter from the submitted spec (C4)", async () => {
    // `describe_params` back-fills a learning adapter's real defaults, so every widget
    // renders populated. Submitting all of them made an untouched `team3_zscore` row an
    // explicit request for its whole hyperparameter block *plus* its artifact — the
    // half-configured request ADR-0022 D4 refuses.
    const detector = await openCompose();
    await userEvent.selectOptions(detector, "team3_zscore");
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    await screen.findByText("Run complete.");
    const body = captured.runBody as RunBody;
    expect(body.spec.detectors).toEqual([{ name: "team3_zscore", params: {} }]);
  });

  it("sends a parameter the operator actually moved (C4, the other half)", async () => {
    const detector = await openCompose();
    await userEvent.selectOptions(detector, "team3_zscore");
    await userEvent.click(screen.getByRole("button", { name: /team3_zscore parameters/ }));
    const windowSize = await screen.findByTestId("det_0_window_size");
    await userEvent.clear(windowSize);
    await userEvent.type(windowSize, "20");
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    await screen.findByText("Run complete.");
    const body = captured.runBody as RunBody;
    expect(body.spec.detectors).toEqual([{ name: "team3_zscore", params: { window_size: 20 } }]);
  });

  it("lets the same adapter appear twice with different params (C5)", async () => {
    // The point of rendering detectors as a list: the old multiselect was over *names*, so
    // one adapter meant one instance, hence one preset per run. Two rows of the same adapter
    // with different presets is exactly how two presets run together.
    await openCompose();
    await userEvent.click(screen.getByTestId("compose_det_n_add"));

    await userEvent.selectOptions(await screen.findByTestId("det_0_name"), "log_burst");
    await userEvent.selectOptions(await screen.findByTestId("det_1_name"), "log_burst");
    // Both rows now offer the same adapter's params — open the first row's panel only.
    const panels = screen.getAllByRole("button", { name: /^log_burst parameters/ });
    await userEvent.click(panels[0] as HTMLElement);
    await userEvent.selectOptions(await screen.findByTestId("det_0_preset"), "auth_failure");

    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    await screen.findByText("Run complete.");
    const body = captured.runBody as RunBody;
    expect(body.spec.detectors).toEqual([
      { name: "log_burst", params: { preset: "auth_failure" } },
      // The second row kept the catalogue default, so it says nothing — two instances of one
      // adapter, independently configured, which the name-keyed version could not express.
      { name: "log_burst", params: {} },
    ]);
  });

  it("defaults the sink to postgres and captions a run that skips it (C8, C10)", async () => {
    await openCompose();
    const postgres = screen.getByRole("checkbox", { name: "postgres" });
    expect(postgres).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "file" })).not.toBeChecked();

    await userEvent.click(postgres);
    expect(
      await screen.findByText("Pick at least one sink so results have somewhere to go."),
    ).toBeInTheDocument();
  });
});
