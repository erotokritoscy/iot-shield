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

  it("withholds the verdict while the edited chain is still being checked", async () => {
    // The 350 ms debounce buys the guard fewer requests; it also opens a window in which the
    // verdict on screen describes a spec that no longer exists. Showing a green "Chain is
    // compatible." through it — and letting Run fire — is the page claiming knowledge it does
    // not have about the chain the operator is looking at.
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());

    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "log_burst");
    expect(screen.queryByText("Chain is compatible.")).not.toBeInTheDocument();
    expect(screen.getByTestId("compose_run")).toBeDisabled();
    expect(screen.getByText("Checking the chain…")).toBeInTheDocument();

    // ...and it comes back once the guard has actually seen the new chain.
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
  });

  it("warns that an untrained detector will be refused, which the guard cannot see", async () => {
    // The compatibility guard reasons about a spec WITHOUT constructing adapters, so it cannot
    // know whether one is fitted: `/runs/validate` returns ok and the refusal arrives from
    // `_ensure_fitted` after the operator has pasted their data. `artifact_param` is published
    // for exactly this. A warning, never a gate — naming an artifact makes the chain legal.
    //
    // Shown against the adapter that ships no ids, because that is the only shape where the
    // warning is still live: one that ships an artifact now preselects it (ADR-0036), so the
    // page cannot open on an untrained chain by accident any more.
    await openCompose();
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "unshipped_learner");
    expect(await screen.findByText(/has no fitted artifact/)).toBeInTheDocument();

    // ...and naming one clears it, through the free-text fallback a dropdown cannot offer.
    // One character, not a plausible id: the predicate is `trim() !== ""`, and each keystroke
    // through userEvent costs ~13 ms.
    await userEvent.type(screen.getByTestId("det_0_art"), "x");
    await waitFor(() =>
      expect(screen.queryByText(/has no fitted artifact/)).not.toBeInTheDocument(),
    );

    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
  });

  it("offers a learning detector its artifact and no hyperparameters (ADR-0036)", async () => {
    // A run never fits, so an artifact is the only thing that can configure such a detector:
    // it supplies the hyperparameters its weights were fitted with, and naming one beside it
    // is refused at construction as a half-configured request. Rendering the knobs anyway made
    // each one a trap — moving a single knob turned a working chain into a failed run — and the
    // page only worked because `submittedParams` drops what nobody touched.
    const detector = await openCompose();
    await userEvent.selectOptions(detector, "team3_zscore");

    // The shipped id preselects: mandatory here, unlike an optional vocabulary that must be
    // able to say "not given". A `<select>` has no blank option to render, so opening on ""
    // *displayed* the first id while the value was empty, beside the untrained warning.
    expect(await screen.findByTestId("det_0_art")).toHaveValue("skab_zscore");
    expect(screen.queryByText(/has no fitted artifact/)).not.toBeInTheDocument();

    // No knobs, and no collapsible to hide them in.
    expect(screen.queryByTestId("det_0_window_size")).not.toBeInTheDocument();
    expect(screen.queryByTestId("det_0_percentile")).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /team3_zscore parameters/ }),
    ).not.toBeInTheDocument();
  });

  it("submits a learning detector's artifact and nothing else (ADR-0036)", async () => {
    const detector = await openCompose();
    await userEvent.selectOptions(detector, "team3_zscore");
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    await screen.findByText("Run complete.");
    const body = captured.runBody as RunBody;
    expect(body.spec.detectors).toEqual([
      { name: "team3_zscore", params: { artifact: "skab_zscore" } },
    ]);
  });

  it("lets an uploaded file be taken back", async () => {
    // Attaching a file disables the paste box, and that box's own onChange was the only thing
    // that cleared the filename — so the escape was unreachable and a wrong file could only be
    // undone by reloading the form and rebuilding every other source.
    await openCompose();
    const file = new File([CSV], "readings.csv", { type: "text/csv" });
    await userEvent.upload(screen.getByTestId("src_0_file"), file);

    expect(await screen.findByText("readings.csv")).toBeInTheDocument();
    expect(screen.getByTestId("src_0_text")).toBeDisabled();

    await userEvent.click(screen.getByTestId("src_0_clear"));
    await waitFor(() => expect(screen.getByTestId("src_0_text")).toBeEnabled());
    expect(screen.getByText("source-1")).toBeInTheDocument();
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

  it("takes content from an uploaded file and its origin from the filename (C2)", async () => {
    await openCompose();
    await userEvent.upload(
      screen.getByTestId("src_0_file"),
      new File([CSV], "pump-02.csv", { type: "text/csv" }),
    );
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    await screen.findByText("Run complete.");
    const body = captured.runBody as RunBody;
    // Provenance is the filename, not `source-1`: an operator reading the malformed-record
    // audit later needs to know which file a bad line came from.
    expect(body.inputs).toEqual([{ content: CSV, origin: "pump-02.csv" }]);
  });

  // C4's "an untouched row submits nothing" is asserted at DOM level by C5 below, whose second
  // row keeps the catalogue default, and at unit level by `lib/params.test.ts` (twice, including
  // the `preset`-owns-the-real-default case this used to exercise through the page). A third
  // copy cost 1.0s of a 28.5s suite for no claim of its own; the learning-detector case it
  // originally covered is a page state ADR-0036 deletes rather than moves.
  it("sends a parameter the operator actually moved (C4, the other half)", async () => {
    const detector = await openCompose();
    await userEvent.selectOptions(detector, "log_burst");
    await userEvent.click(screen.getByRole("button", { name: /log_burst parameters/ }));
    const minCount = await screen.findByTestId("det_0_min_count");
    await userEvent.clear(minCount);
    await userEvent.type(minCount, "12");
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    await screen.findByText("Run complete.");
    const body = captured.runBody as RunBody;
    expect(body.spec.detectors).toEqual([{ name: "log_burst", params: { min_count: 12 } }]);
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
