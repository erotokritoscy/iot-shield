/**
 * The Evaluate mode: its gate, its two spec modes, and the semantics that stop a benchmark
 * quietly measuring something the operator did not ask for (E1–E10).
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { HttpResponse, http } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import * as seed from "@/test/fixtures";
import { PIPELINE, captured, evalHandlers, server } from "@/test/msw";
import { renderApp } from "@/test/render";

const COMPOSE = "Compose my own";
const SCRATCH = "Fit from scratch with my hyperparameters";

const withEvalPlane = () => server.use(...evalHandlers);

async function openEvaluate(): Promise<void> {
  withEvalPlane();
  renderApp("/evaluate");
  await screen.findByTestId("eval_corpus");
}

async function openCompose(): Promise<void> {
  await openEvaluate();
  await userEvent.click(screen.getByRole("radio", { name: COMPOSE }));
  await screen.findByTestId("eval_entry_n_add");
}



describe("claims the page is entitled to make", () => {
  it("does not turn a failed catalogue fetch into a fact about the corpus", async () => {
    // An empty detector list is also what a pending or failed fetch looks like. Stating "no
    // registered detector consumes this corpus's lanes" from an absence nobody verified told
    // operators their corpus was unusable when one HTTP call had failed.
    withEvalPlane();
    server.use(
      http.get(`${PIPELINE}/eval/detectors`, () =>
        HttpResponse.json({ detail: "boom" }, { status: 500 }),
      ),
    );
    renderApp("/evaluate");
    await screen.findByTestId("eval_corpus");
    await userEvent.click(screen.getByRole("radio", { name: COMPOSE }));

    expect(await screen.findByText("Could not read the module registry")).toBeInTheDocument();
    expect(screen.queryByText(/No registered detector consumes/)).not.toBeInTheDocument();
    expect(screen.getByTestId("eval_run")).toBeDisabled();
  });

  it("distinguishes the spec it will send from the spec that produced the report", async () => {
    // Two questions with different answers the moment a control is touched. One block labelled
    // in the past tense but fed from the live form answered "what produced this number?" with
    // the form's current contents — the exact misattribution the disclosure exists to prevent.
    await openEvaluate();
    await userEvent.click(screen.getByTestId("eval_run"));
    await screen.findByRole("heading", { name: "Per case" });

    expect(
      screen.getByRole("button", { name: /Spec the Run button will send/ }),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /Spec that produced the report below/ }),
    ).not.toBeInTheDocument();

    // Drift the controls: now the two differ, and both are named for what they are.
    await userEvent.click(screen.getByRole("radio", { name: /prefix/ }));
    expect(
      await screen.findByRole("button", { name: /Spec that produced the report below/ }),
    ).toBeInTheDocument();
  });

  it("re-runs an unchanged selection instead of doing nothing", async () => {
    // `staleTime: Infinity` on a key that only moves when the selection moves meant pressing
    // Run again was a silent no-op — and after a failure, with `retry: false`, a permanently
    // dead button. Correct caching, wrong affordance: the cache is an optimisation the
    // operator must be able to override.
    let calls = 0;
    withEvalPlane();
    server.use(
      http.post(`${PIPELINE}/eval/run`, async ({ request }) => {
        calls += 1;
        captured.evalBody = await request.json();
        return HttpResponse.json(seed.report);
      }),
    );
    renderApp("/evaluate");
    await screen.findByTestId("eval_corpus");

    await userEvent.click(screen.getByTestId("eval_run"));
    await screen.findByRole("heading", { name: "Per case" });
    expect(calls).toBe(1);

    await userEvent.click(screen.getByTestId("eval_run"));
    await waitFor(() => expect(calls).toBe(2));
  });
});

describe("the Evaluate gate (D5/E1)", () => {
  it("hides the mode when the eval router was never registered", async () => {
    // A 404 is the deployment saying "this image has no research plane" — the router is
    // absent, not guarded (ADR-0021 D1), which is what IOTSHIELD_EVAL_UI used to express.
    renderApp("/");
    await screen.findByText("Monitor");
    await waitFor(() => expect(screen.queryByText("Evaluate")).not.toBeInTheDocument());
  });

  it("shows the mode when the plane answers", async () => {
    withEvalPlane();
    renderApp("/");
    expect(await screen.findByText("Evaluate")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Benchmarks" })).toBeInTheDocument();
  });

  it("fetches only the endpoints the active mode uses (E19)", async () => {
    // /eval/specs re-walks the corpus root and re-parses every bench YAML, and the compose
    // mode never needs it; the committed-spec mode never needs the detector catalogue. The
    // old page fetched each lazily for exactly this reason.
    let detectorCalls = 0;
    withEvalPlane();
    // After the plane handlers, so this counting one wins (`server.use` prepends).
    server.use(
      http.get("http://localhost/pipeline/eval/detectors", () => {
        detectorCalls += 1;
        return HttpResponse.json([]);
      }),
    );
    renderApp("/evaluate");
    await screen.findByRole("checkbox", { name: "stub" });
    expect(detectorCalls).toBe(0);

    await userEvent.click(screen.getByRole("radio", { name: COMPOSE }));
    await waitFor(() => expect(detectorCalls).toBe(1));
  });

  it.each([
    ["{not json", /could not be read as JSON/],
    ['{"hello": 1}', /is not a benchmark report/],
  ])("treats an unreadable saved report as an empty state, not a traceback (E18)", async (
    body,
    message,
  ) => {
    server.use(http.get("http://localhost/pipeline/eval/corpora", () => HttpResponse.error()));
    renderApp("/evaluate");
    await userEvent.upload(
      await screen.findByTestId("eval_report_upload"),
      new File([body as string], "evaluation.json", { type: "application/json" }),
    );
    expect(await screen.findByText(message as RegExp)).toBeInTheDocument();
  });

  it("offers the saved-report upload when the Pipeline API is unreachable (D6)", async () => {
    // A transport failure is a *different fact* from a 404: the plane is enabled but the
    // Pipeline API is down or still starting, which is the situation the old page degraded
    // through rather than disappeared in.
    server.use(http.get("http://localhost/pipeline/eval/corpora", () => HttpResponse.error()));
    renderApp("/evaluate");
    expect(await screen.findByText("Pipeline API unreachable")).toBeInTheDocument();
    expect(screen.getByTestId("eval_report_upload")).toBeInTheDocument();
    expect(screen.getByText(/uv run python -m iotshield.eval/)).toBeInTheDocument();
  });
});

describe("corpora (E2, E3)", () => {
  it("lists a broken bundle with its reason and keeps it out of the picker", async () => {
    await openEvaluate();
    expect(await screen.findByText(/labels schema v4 is stale/)).toBeInTheDocument();
    const picker = screen.getByTestId("eval_corpus");
    expect(within(picker).queryByRole("option", { name: "stale_bundle" })).not.toBeInTheDocument();
    expect(within(picker).getByRole("option", { name: "synthetic" })).toBeInTheDocument();
  });

  it("names the root, the generate command and the fetchable hints when there are none (E2)", async () => {
    // "There is nothing here" must arrive with the command that fixes it, and with the
    // datasets this deployment could convert locally but does not ship.
    withEvalPlane();
    server.use(
      http.get("http://localhost/pipeline/eval/corpora", () =>
        HttpResponse.json({
          root: "/app/data/eval",
          corpora: [],
          fetchable: { skab: "SKAB is GPLv3: run `uv run python -m iotshield.eval fetch skab`" },
        }),
      ),
    );
    renderApp("/evaluate");
    expect(await screen.findByText("/app/data/eval")).toBeInTheDocument();
    expect(screen.getByText("uv run python -m iotshield.eval generate")).toBeInTheDocument();
    expect(screen.getByText(/SKAB is GPLv3/)).toBeInTheDocument();
  });

  it("captions the corpus, offers both fit protocols and no holdout", async () => {
    await openEvaluate();
    expect(await screen.findByText(/11 case\(s\) ·/)).toBeInTheDocument();
    const protocols = screen.getAllByRole("radio").filter((input) => ["none", "prefix"].includes((input as HTMLInputElement).value));
    expect(protocols.map((input) => (input as HTMLInputElement).value)).toEqual(["none", "prefix"]);
    expect(screen.getByText(/Judge with each detector exactly as configured/)).toBeInTheDocument();
  });

  it("shows provenance and licence only for a licenced corpus", async () => {
    await openEvaluate();
    expect(screen.queryByText(/Licence:/)).not.toBeInTheDocument();
    await userEvent.selectOptions(screen.getByTestId("eval_corpus"), "skab_valve1");
    expect(await screen.findByText(/Licence: GPL-3.0/)).toBeInTheDocument();
  });
});

describe("committed bench specs (E4, E9)", () => {
  it("offers only the specs whose source shape fits the corpus", async () => {
    await openEvaluate();
    expect(await screen.findByRole("checkbox", { name: "stub" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "flow" })).not.toBeInTheDocument();

    // ...and the pairing follows the corpus rather than being a fixed list.
    await userEvent.selectOptions(screen.getByTestId("eval_corpus"), "fleet_scan");
    expect(await screen.findByRole("checkbox", { name: "flow" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "stub" })).not.toBeInTheDocument();
  });

  it("explains itself when no committed spec fits", async () => {
    await openEvaluate();
    await userEvent.selectOptions(screen.getByTestId("eval_corpus"), "device_trimodal");
    expect(await screen.findByText(/No committed bench spec matches/)).toBeInTheDocument();
  });

  it("sends the committed spec VERBATIM and shows what it sent", async () => {
    captured.evalBody = null;
    await openEvaluate();
    await screen.findByRole("checkbox", { name: "stub" });
    await userEvent.click(screen.getByRole("button", { name: /Spec the Run button will send/ }));
    // The exact request, on the page that made it — the reconciliation tool for
    // "dashboard number ≠ CLI number".
    expect(await screen.findByText(/"assets"/)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("eval_run"));
    await screen.findByRole("heading", { name: "Head to head" });
    const body = captured.evalBody as { specs: { label: string; spec: Record<string, unknown> }[] };
    expect(body.specs[0]?.label).toBe("stub");
    // The `assets:` block and every source/engine param survive — composing a lookalike is
    // what let the page and the CLI publish different numbers for the same benchmark.
    expect(body.specs[0]?.spec).toHaveProperty("assets");
  });
});

describe("composing entries (E5, E6, E7)", () => {
  beforeEach(() => {
    captured.evalBody = null;
  });

  it("offers only detectors the corpus's lanes can feed, and prunes on a corpus switch", async () => {
    await openCompose();
    // synthetic is telemetry-only. Scoped to entry 0, since every entry offers the same list.
    const entry = () => within(screen.getByTestId("eval_entry_0"));
    expect(entry().getByRole("checkbox", { name: "stub_threshold" })).toBeChecked();
    expect(entry().queryByRole("checkbox", { name: "flow_burst" })).not.toBeInTheDocument();

    await userEvent.selectOptions(screen.getByTestId("eval_corpus"), "fleet_scan");
    await waitFor(() =>
      expect(entry().getByRole("checkbox", { name: "flow_burst" })).toBeInTheDocument(),
    );
    expect(entry().queryByRole("checkbox", { name: "stub_threshold" })).not.toBeInTheDocument();

    // Switching back must prune the stale pick rather than raising.
    await userEvent.selectOptions(screen.getByTestId("eval_corpus"), "synthetic");
    await waitFor(() =>
      expect(entry().getByRole("checkbox", { name: "stub_threshold" })).toBeInTheDocument(),
    );
  });

  it("blocks the run when a trained detector names no artifact", async () => {
    // An empty artifact box reaches the factory as "no artifact", which ADR-0022 means as
    // *untrained* — so clearing it must be refused, not quietly reinterpreted.
    await openCompose();
    await userEvent.click(await screen.findByRole("button", { name: /team3_zscore — hyperparameters/ }));
    const artifact = await screen.findByTestId("eval_e1_team3_zscore_art");
    expect(artifact).toHaveValue("skab_zscore");
    await userEvent.clear(artifact);
    expect(await screen.findByText(/no artifact is named/)).toBeInTheDocument();
    expect(screen.getByTestId("eval_run")).toBeDisabled();
  });

  it("blocks fitting from scratch under protocol `none`, and says why", async () => {
    // An untrained model under `none` judges nothing; a row of zeros would read as "this
    // detector is bad" when the truth is that it never ran.
    await openCompose();
    await userEvent.click(await screen.findByRole("button", { name: /team3_zscore — hyperparameters/ }));
    await userEvent.click(await screen.findByRole("radio", { name: SCRATCH }));
    expect(await screen.findByText(/Pick `prefix` above/)).toBeInTheDocument();
    expect(screen.getByTestId("eval_run")).toBeDisabled();
  });

  it("says why trained mode offers no knobs, and re-offers them in scratch mode", async () => {
    await openCompose();
    await userEvent.click(await screen.findByRole("button", { name: /team3_zscore — hyperparameters/ }));
    expect(await screen.findByText(/Trained weights are atomic/)).toBeInTheDocument();
    expect(screen.queryByTestId("eval_e1_team3_zscore_window_size")).not.toBeInTheDocument();
    await userEvent.click(await screen.findByRole("radio", { name: SCRATCH }));
    // 60, not 0: the factory param defaults to None, so the real default has to come from
    // the adapter's hyperparameter model via /eval/detectors.
    expect(await screen.findByTestId("eval_e1_team3_zscore_window_size")).toHaveValue(60);
  });

  it("says so when a fit protocol is asked of detectors that cannot train (E17)", async () => {
    // Not a blocker: they are evaluated as configured and the report says so. But silence
    // here would let someone read `prefix` numbers as fitted ones.
    await openCompose();
    const entry1 = within(screen.getByTestId("eval_entry_1"));
    await userEvent.click(entry1.getByRole("checkbox", { name: "team3_zscore" }));
    await userEvent.click(screen.getByRole("radio", { name: "prefix" }));
    expect(
      await screen.findByText(/None of the selected detectors can be trained/),
    ).toBeInTheDocument();
  });

  it("keeps bare sweep keys in a single-detector entry", async () => {
    // The compatibility pin at the UI end: labels like `stub_threshold[threshold=40]` are
    // quoted in the README and the ADRs, so the common case must not gain a qualifier.
    await openCompose();
    const sweep = (await screen.findByTestId("eval_e0_sw")) as HTMLSelectElement;
    expect([...sweep.options].map((option) => option.value)).toEqual(["(no sweep)", "threshold"]);
  });

  it("qualifies every knob by detector as soon as an entry holds more than one", async () => {
    // The menu used to *hide* a name two detectors shared, mirroring a server refusal, so it
    // silently shrank. Naming the detector removes the ambiguity instead.
    await openCompose();
    await userEvent.selectOptions(screen.getByTestId("eval_corpus"), "device_trimodal");
    await userEvent.click(
      await within(screen.getByTestId("eval_entry_0")).findByRole("checkbox", {
        name: "flow_burst",
      }),
    );
    const sweep = (await screen.findByTestId("eval_e0_sw")) as HTMLSelectElement;
    await waitFor(() =>
      expect([...sweep.options].map((option) => option.value)).toEqual([
        "(no sweep)",
        "flow_burst.window_minutes",
        "flow_burst.threshold",
        "stub_threshold.threshold",
      ]),
    );
  });

  it("clears an invalidated sweep WITH a message rather than silently", async () => {
    // A configured sweep that vanished with no message let the run measure an *unswept*
    // spec: the user gets a number they did not ask for.
    await openCompose();
    await userEvent.selectOptions(screen.getByTestId("eval_corpus"), "device_trimodal");
    await userEvent.selectOptions(await screen.findByTestId("eval_e0_sw"), "threshold");
    await userEvent.clear(await screen.findByTestId("eval_e0_swv"));
    await userEvent.type(await screen.findByTestId("eval_e0_swv"), "40, 80");
    expect(await screen.findByText(/2 run\(s\), one per value/)).toBeInTheDocument();

    // Adding a detector re-keys the menu to qualified names, so bare `threshold` is gone.
    await userEvent.click(
      await within(screen.getByTestId("eval_entry_0")).findByRole("checkbox", {
        name: "flow_burst",
      }),
    );
    expect(await screen.findByText(/no longer applies to this entry/)).toBeInTheDocument();
    await waitFor(() =>
      expect((screen.getByTestId("eval_e0_sw") as HTMLSelectElement).value).toBe("(no sweep)"),
    );
  });

  it("reports what a sweep will cost and sends the values with the request", async () => {
    await openCompose();
    await userEvent.selectOptions(await screen.findByTestId("eval_e0_sw"), "threshold");
    await userEvent.clear(await screen.findByTestId("eval_e0_swv"));
    await userEvent.type(await screen.findByTestId("eval_e0_swv"), "40, 80, 120");
    expect(await screen.findByText(/3 run\(s\), one per value/)).toBeInTheDocument();
    expect(await screen.findByText(/This will run/)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("eval_run"));
    await screen.findByRole("heading", { name: "Head to head" });
    const body = captured.evalBody as { specs: { label: string; sweep: Record<string, unknown[]> }[] };
    expect(body.specs[0]?.sweep).toEqual({ threshold: [40, 80, 120] });
    // Several detectors in ONE spec run together; here each entry holds one, so this is the
    // classic head-to-head — two entries, two labels.
    expect(body.specs.map((entry) => entry.label)).toEqual(["stub_threshold", "team3_zscore"]);
  });
});

describe("running and reading a report (E10–E15)", () => {
  it("latches the run and warns when the controls drift from it", async () => {
    await openEvaluate();
    await screen.findByRole("checkbox", { name: "stub" });
    expect(
      screen.getByText("Pick specs and run — results are cached, so exploring them is instant."),
    ).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("eval_run"));
    expect(await screen.findByRole("heading", { name: "Head to head" })).toBeInTheDocument();

    // Changing a control after a run leaves the latched report on screen, which reads as
    // "the control did nothing". Say so rather than re-running unasked.
    await userEvent.click(screen.getByRole("radio", { name: "prefix" }));
    // The warning names what actually ran, so the numbers on screen are never anonymous.
    const warning = await screen.findByText(/Showing the previous run/);
    expect(warning.textContent).toContain("synthetic");
    expect(warning.textContent).toContain("stub");
    expect(warning.textContent).toContain("fit protocol");
    // ...and the report is still on screen, not wiped.
    expect(screen.getByRole("heading", { name: "Head to head" })).toBeInTheDocument();
  });

  it("renders the header caption, both charts, the timeline and the per-case table", async () => {
    await openEvaluate();
    await screen.findByRole("checkbox", { name: "stub" });
    await userEvent.click(screen.getByTestId("eval_run"));

    expect(await screen.findByRole("heading", { name: "Head to head" })).toBeInTheDocument();
    expect(screen.getByText(/2 case\(s\), 1 incident\(s\)/)).toBeInTheDocument();
    expect(screen.getByText(/generated 2026-07-07 12:00 UTC/)).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Consistency across cases" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "What actually happened" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Per case" })).toBeInTheDocument();
    // The deduplicated union of case notes, rendered as warnings: a row of zeros always
    // arrives with its reason.
    expect(screen.getAllByText(/rejected 100% of input/).length).toBeGreaterThan(0);
    // The undefined score is an empty cell, not a zero.
    expect(screen.getAllByText("—").length).toBeGreaterThan(0);
  });
});
