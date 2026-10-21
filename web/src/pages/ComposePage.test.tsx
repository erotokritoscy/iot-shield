/**
 * The orchestration page's load-bearing form semantics (C4, C5, C6, C9, C10), now as a wizard.
 *
 * These are the assertions that need the *outgoing request body*, not the rendered DOM: what
 * makes a run succeed or fail is which parameters the page declared, and that is invisible
 * on screen by construction. The wizard must change none of it — every original assertion is
 * kept, reached by walking the steps instead of scrolling.
 */
import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { beforeEach, describe, expect, it } from "vitest";

import { captured, PIPELINE, SAMPLE_CSV, server } from "@/test/msw";
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

async function openCompose(): Promise<void> {
  renderApp("/compose");
  await screen.findByTestId("src_0_text");
}


/** Jump with the stepper. Only reachable steps are enabled, which is itself under test. */
async function toStep(step: 1 | 2 | 3 | 4): Promise<void> {
  const button = screen.getByTestId(`step_${step}`);
  await waitFor(() => expect(button).toBeEnabled());
  await userEvent.click(button);
}

/** Step 1 with pasted content, then on to the detectors. */
async function withContent(text = CSV): Promise<void> {
  await openCompose();
  await userEvent.type(screen.getByTestId("src_0_text"), text);
  await toStep(2);
}

async function runFromReview(): Promise<RunBody> {
  await toStep(4);
  await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
  await userEvent.click(screen.getByTestId("compose_run"));
  await screen.findByText("Run complete.");
  return captured.runBody as RunBody;
}

/** Every form label on screen with no (i) beside it — empty when every field explains itself. */
function unexplained(): string[] {
  return [...document.querySelectorAll(".field-label")]
    .filter((label) => label.parentElement?.querySelector('button[aria-label^="About "]') == null)
    .map((label) => label.textContent ?? "");
}

/** Open every "<adapter> parameters" disclosure, so the param fields are on screen too. */
async function expandParams(): Promise<void> {
  for (const button of screen.queryAllByRole("button", { name: /parameters$/, expanded: false })) {
    await userEvent.click(button);
  }
}

describe("Compose & run — every field has an (i)", () => {
  it("explains every field on steps 1–3", async () => {
    await openCompose();
    await expandParams();
    expect(unexplained()).toEqual([]);
    await userEvent.selectOptions(screen.getByTestId("src_0_norm"), "flow");
    expect(screen.getByRole("button", { name: "About Normalizer" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "About Input format" })).toBeInTheDocument();
    expect(unexplained()).toEqual([]);
    await userEvent.selectOptions(screen.getByTestId("src_0_norm"), "stub");

    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await toStep(2);
    await expandParams();
    expect(screen.getByRole("button", { name: "About threshold" })).toBeInTheDocument();
    expect(unexplained()).toEqual([]);

    await toStep(3);
    await expandParams();
    expect(screen.getByRole("button", { name: "About Sinks" })).toBeInTheDocument();
    expect(unexplained()).toEqual([]);
  });

  it("tells the user what the chosen format looks like", async () => {
    await openCompose();
    await userEvent.click(screen.getByRole("button", { name: "About Normalizer" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("timestamp,asset_id,metric,value");
  });
});

describe("Compose & run — the wizard", () => {
  beforeEach(() => {
    captured.runBody = null;
    captured.validateBody = null;
  });

  it("opens on Data and gates Next until every source has content", async () => {
    await openCompose();
    expect(screen.getByTestId("step_1")).toHaveAttribute("aria-current", "step");
    expect(screen.getByTestId("wizard_next")).toBeDisabled();
    expect(screen.getByText(/Source 1 needs content/)).toBeInTheDocument();
    for (const step of [2, 3, 4]) expect(screen.getByTestId(`step_${step}`)).toBeDisabled();

    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await userEvent.click(screen.getByTestId("wizard_next"));
    expect(await screen.findByTestId("det_0_name")).toBeInTheDocument();
    expect(screen.getByTestId("step_2")).toHaveAttribute("aria-current", "step");
    // Back returns to the data with the content still there: state outlives the step.
    await userEvent.click(screen.getByTestId("wizard_back"));
    expect(await screen.findByTestId("src_0_text")).toHaveValue(CSV);
  });

  it("clamps a deep link to the first step that is not ready", async () => {
    renderApp("/compose?step=4");
    expect(await screen.findByTestId("src_0_text")).toBeInTheDocument();
    expect(screen.getByTestId("step_1")).toHaveAttribute("aria-current", "step");
  });

  it("preselects a detector the sources feed AND that is ready to run (C6)", async () => {
    // The normalizer is chosen by the format it declares, not by its name: `flow` sorts
    // before `stub`, so a name-ordered default would open on a flow chain fed telemetry CSV.
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    expect((screen.getByTestId("src_0_norm") as HTMLSelectElement).value).toBe("stub");
    await toStep(2);
    // `meter_iforest` sorts first and is *untrained* until an artifact is named.
    expect((screen.getByTestId("det_0_name") as HTMLSelectElement).value).toBe("stub_threshold");
    expect(screen.getByText(/which your sources provide/)).toBeInTheDocument();
  });

  it("says when no source feeds the chosen detector, without hiding it", async () => {
    await withContent();
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "log_burst");
    expect(await screen.findByText(/this detector will run and find nothing/)).toBeInTheDocument();
  });

  it("withholds the verdict while the edited chain is still being checked", async () => {
    // The 350 ms debounce opens a window in which a verdict on screen would describe a spec
    // that no longer exists; a green "Chain is compatible." there would be a claim the page
    // cannot back.
    await withContent();
    await toStep(4);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());

    await toStep(2);
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "log_burst");
    await toStep(4);
    expect(screen.queryByText("Chain is compatible.")).not.toBeInTheDocument();
    expect(screen.getByTestId("compose_run")).toBeDisabled();
    expect(screen.getByText("Checking the chain…")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
  });

  it("warns that an untrained detector will be refused, and blocks Next until it is not", async () => {
    // The guard reasons about a spec WITHOUT constructing adapters, so it cannot know whether
    // one is fitted. `artifact_param` is published for exactly this.
    await withContent();
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "unshipped_learner");
    expect(await screen.findByText(/has no fitted artifact, so the run/)).toBeInTheDocument();
    expect(screen.getByTestId("wizard_next")).toBeDisabled();

    await userEvent.type(screen.getByTestId("det_0_art"), "x");
    await waitFor(() => expect(screen.queryByText(/has no fitted artifact/)).not.toBeInTheDocument());
    expect(screen.getByTestId("wizard_next")).toBeEnabled();
  });

  it("offers a learning detector its artifact and no hyperparameters (ADR-0036)", async () => {
    await withContent();
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "team3_zscore");
    // The shipped id preselects: mandatory here, and a `<select>` has no blank option.
    expect(await screen.findByTestId("det_0_art")).toHaveValue("skab_zscore");
    expect(screen.queryByText(/has no fitted artifact/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("det_0_window_size")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /team3_zscore parameters/ })).not.toBeInTheDocument();
  });

  it("submits a learning detector's artifact and nothing else (ADR-0036)", async () => {
    await withContent();
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "team3_zscore");
    const body = await runFromReview();
    expect(body.spec.detectors).toEqual([{ name: "team3_zscore", params: { artifact: "skab_zscore" } }]);
  });

  it("lets an uploaded file be taken back", async () => {
    await openCompose();
    await userEvent.upload(screen.getByTestId("src_0_file"), new File([CSV], "readings.csv", { type: "text/csv" }));
    expect(await screen.findByText("readings.csv", { selector: "p" })).toBeInTheDocument();
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

  it("reviews, reports a compatible chain, runs it and renders the receipt (C9, C10)", async () => {
    await withContent();
    await toStep(4);
    expect(screen.getByText("source-1")).toBeInTheDocument(); // the review summary
    expect(await screen.findByText("Chain is compatible.")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    expect(await screen.findByText("Run complete.")).toBeInTheDocument();
    expect(screen.getByTestId("stat-events")).toHaveTextContent("1");
    expect(screen.getByTestId("stat-malformed")).toHaveTextContent("0");
    expect(screen.getByTestId("stat-detections")).toHaveTextContent("1");
    expect(screen.getByTestId("stat-alerts")).toHaveTextContent("1");
    expect(screen.getByText("Written to: postgres")).toBeInTheDocument();
    expect(screen.getByText(/see them ranked and explained under/)).toBeInTheDocument();

    const body = captured.runBody as RunBody;
    expect(body.inputs).toEqual([{ content: CSV, origin: "source-1" }]);
    expect(body.spec.sources[0]).toEqual({ normalizer: "stub", source_format: "stub-csv", params: {} });
  });

  it("names each detector's choices on Review, defaults included", async () => {
    // The request omits a default, so a summary built from it would show a bare `log_burst`
    // and hide which preset will run.
    await withContent();
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "log_burst");
    await toStep(4);
    expect(await screen.findByText(/preset: severity_spike/)).toBeInTheDocument();
  });

  it("takes content from an uploaded file and its origin from the filename (C2)", async () => {
    await openCompose();
    await userEvent.upload(screen.getByTestId("src_0_file"), new File([CSV], "pump-02.csv", { type: "text/csv" }));
    await toStep(2);
    const body = await runFromReview();
    // Provenance is the filename: an operator reading the malformed-record audit later needs
    // to know which file a bad line came from.
    expect(body.inputs).toEqual([{ content: CSV, origin: "pump-02.csv" }]);
  });

  it("sends a parameter the operator actually moved (C4, the other half)", async () => {
    await withContent();
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "log_burst");
    await userEvent.click(screen.getByRole("button", { name: /log_burst parameters/ }));
    const minCount = await screen.findByTestId("det_0_min_count");
    await userEvent.clear(minCount);
    await userEvent.type(minCount, "12");
    const body = await runFromReview();
    expect(body.spec.detectors).toEqual([{ name: "log_burst", params: { min_count: 12 } }]);
  });

  it("lets the same adapter appear twice with different params (C5)", async () => {
    await withContent();
    await userEvent.click(screen.getByTestId("compose_det_n_add"));
    await userEvent.selectOptions(await screen.findByTestId("det_0_name"), "log_burst");
    await userEvent.selectOptions(await screen.findByTestId("det_1_name"), "log_burst");
    const panels = screen.getAllByRole("button", { name: /^log_burst parameters/ });
    await userEvent.click(panels[0] as HTMLElement);
    await userEvent.selectOptions(await screen.findByTestId("det_0_preset"), "auth_failure");
    const body = await runFromReview();
    expect(body.spec.detectors).toEqual([
      { name: "log_burst", params: { preset: "auth_failure" } },
      { name: "log_burst", params: {} },
    ]);
  });

  it("removes one detector row from the middle, keeping the others' settings", async () => {
    await withContent();
    await userEvent.click(screen.getByTestId("compose_det_n_add"));
    await userEvent.click(screen.getByTestId("compose_det_n_add"));
    await userEvent.selectOptions(screen.getByTestId("det_2_name"), "log_burst");
    await userEvent.click(screen.getByTestId("det_1_remove"));
    expect(screen.queryByTestId("det_2_name")).not.toBeInTheDocument();
    expect(screen.getByTestId("det_1_name")).toHaveValue("log_burst");
  });

  it("defaults the sink to postgres and gates Next when none is chosen (C8, C10)", async () => {
    await withContent();
    await toStep(3);
    const postgres = screen.getByRole("checkbox", { name: "postgres" });
    expect(postgres).toBeChecked();
    expect(screen.getByRole("checkbox", { name: "file" })).not.toBeChecked();

    await userEvent.click(postgres);
    expect(await screen.findByText("Pick at least one sink so results have somewhere to go.")).toBeInTheDocument();
    expect(screen.getByTestId("wizard_next")).toBeDisabled();
    expect(screen.getByTestId("step_4")).toBeDisabled();
  });
});

describe("Compose & run — the source wizard", () => {
  beforeEach(() => {
    captured.runBody = null;
  });

  it("shows every field of a source on one card, with a preview of what will be read", async () => {
    await openCompose();
    expect(screen.getByTestId("src_0_tab")).toHaveAttribute("aria-current", "step");
    const card = screen.getByTestId("src_0_card");
    for (const id of ["src_0_norm", "src_0_file", "src_0_text"]) expect(within(card).getByTestId(id)).toBeInTheDocument();
    expect(within(card).queryByTestId("src_0_preview")).not.toBeInTheDocument();

    await userEvent.type(screen.getByTestId("src_0_text"), `${CSV}\n${CSV}`);
    expect(within(card).getByTestId("src_0_preview")).toHaveTextContent("pump-02,Pressure,96.0");
    expect(await within(card).findByText("Ready: stub reads 2 of 2 records.")).toBeInTheDocument();
    // The last source offers a new one, not a "next" that does not exist.
    expect(screen.getByTestId("src_0_add_next")).toBeInTheDocument();
    expect(screen.queryByTestId("src_0_next_source")).not.toBeInTheDocument();
  });

  it("shows one source at a time: a new source replaces the first on screen", async () => {
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await userEvent.click(screen.getByTestId("compose_src_add"));

    expect(screen.queryByTestId("src_0_card")).not.toBeInTheDocument();
    expect(screen.getByTestId("src_1_card")).toBeInTheDocument();
    expect(screen.getByTestId("src_1_tab")).toHaveAttribute("aria-current", "step");
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("source-1");
    expect(screen.getByTestId("src_1_tab")).toHaveTextContent("needs content");

    // Back to Source 1 through its step: what it held is still there.
    await userEvent.click(screen.getByTestId("src_0_tab"));
    expect(screen.getByTestId("src_0_text")).toHaveValue(CSV);
    expect(screen.queryByTestId("src_1_card")).not.toBeInTheDocument();
    await userEvent.click(screen.getByTestId("src_0_next_source"));
    expect(screen.getByTestId("src_1_card")).toBeInTheDocument();
  });

  it("shows the chain check's finding about this source on its card and its tab", async () => {
    server.use(
      http.post(`${PIPELINE}/runs/validate`, () =>
        HttpResponse.json({
          ok: false,
          issues: [{ level: "error", stage: "sources[0]", message: "no normalizer accepts format 'x'" }],
        }),
      ),
    );
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    expect(await screen.findByText("no normalizer accepts format 'x'")).toBeInTheDocument();
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("has an error");
  });

  it("adds a second source from the card, gates Next on it, and sends both in order", async () => {
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await userEvent.click(screen.getByTestId("src_0_add_next"));
    expect(screen.getByTestId("src_1_tab")).toHaveAttribute("aria-current", "step");
    // The new, empty source holds the step back, exactly like the first one did.
    expect(screen.getByText(/Source 2 needs content/)).toBeInTheDocument();
    expect(screen.getByTestId("wizard_next")).toBeDisabled();

    await userEvent.upload(screen.getByTestId("src_1_file"), new File([CSV], "second.csv", { type: "text/csv" }));
    await waitFor(() => expect(screen.getByTestId("wizard_next")).toBeEnabled());
    await toStep(2);
    const body = await runFromReview();
    expect(body.inputs).toEqual([
      { content: CSV, origin: "source-1" },
      { content: CSV, origin: "second.csv" },
    ]);
  });

  it("removes a source from the middle, keeping the others", async () => {
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), "a");
    await userEvent.click(screen.getByTestId("compose_src_add"));
    await userEvent.type(screen.getByTestId("src_1_text"), "b");
    await userEvent.click(screen.getByTestId("compose_src_add"));
    await userEvent.type(screen.getByTestId("src_2_text"), "c");

    await userEvent.click(screen.getByTestId("src_1_tab"));
    await userEvent.click(screen.getByTestId("src_1_remove"));
    expect(screen.queryByTestId("src_2_tab")).not.toBeInTheDocument();
    expect(screen.getByTestId("src_0_text")).toHaveValue("a");
    await userEvent.click(screen.getByTestId("src_1_tab"));
    expect(screen.getByTestId("src_1_text")).toHaveValue("c");
  });
});

/** `/runs/preview` answering every source with this verdict. */
function previewSays(result: {
  events: number;
  malformed: number;
  level: "empty" | "clean" | "some_rejected" | "mostly_rejected" | "unreadable";
  rejected?: { line: number | null; raw: string; error: string }[];
}): void {
  server.use(http.post(`${PIPELINE}/runs/preview`, () => HttpResponse.json({ rejected: [], ...result })));
}

describe("Compose & run — a file in the wrong format", () => {
  beforeEach(() => {
    captured.previewBodies = [];
  });

  it("asks the source's own normalizer to read it, and nothing else", async () => {
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    await screen.findByText("Ready: stub reads 1 of 1 records.");
    expect(captured.previewBodies.at(-1)).toEqual({
      source: { normalizer: "stub", source_format: "stub-csv", params: {} },
      content: CSV,
    });
  });

  it("calls a file nothing can be read of an error, by line, and blocks Next", async () => {
    previewSays({
      events: 0,
      malformed: 2,
      level: "unreadable",
      rejected: [
        { line: 1, raw: "<34>1 2026-07-07T12:00:00Z host-a app", error: "expected 4 fields, got 1" },
        { line: 2, raw: "<34>1 2026-07-07T12:01:00Z host-a app", error: "expected 4 fields, got 1" },
      ],
    });
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), "syslog line");

    const error = await screen.findByTestId("src_0_read_error");
    expect(error).toHaveTextContent("Wrong format?");
    expect(error).toHaveTextContent("stub (stub-csv) could not read any of the 2 records");
    expect(within(error).getByTestId("src_0_rejected")).toHaveTextContent("Line 1: expected 4 fields, got 1");
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("wrong format?");
    expect(screen.getByText(/Source 1 cannot be read by stub/)).toBeInTheDocument();
    expect(screen.getByTestId("wizard_next")).toBeDisabled();
    expect(screen.getByTestId("step_2")).toBeDisabled();
    expect(screen.queryByText(/^Ready:/)).not.toBeInTheDocument();
  });

  it("warns, without blocking, when most of a file cannot be read", async () => {
    previewSays({
      events: 1,
      malformed: 4,
      level: "mostly_rejected",
      rejected: [{ line: 2, raw: "junk", error: "expected 4 fields, got 1" }],
    });
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);

    const warning = await screen.findByTestId("src_0_read_warning");
    expect(warning).toHaveTextContent("could read only 1 of 5 records");
    expect(warning).toHaveTextContent("…and 3 more.");
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("mostly unreadable");
    await waitFor(() => expect(screen.getByTestId("wizard_next")).toBeEnabled());
  });

  it("notes a few bad lines and still says the source is ready", async () => {
    previewSays({
      events: 9,
      malformed: 1,
      level: "some_rejected",
      rejected: [{ line: 4, raw: "not,a,reading", error: "expected 4 fields, got 3" }],
    });
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);

    const note = await screen.findByTestId("src_0_read_note");
    expect(note).toHaveTextContent("9 of 10 records read; 1 cannot be read");
    expect(note).toHaveTextContent("Line 4: expected 4 fields, got 3");
    expect(await screen.findByText("Ready: stub reads 9 of 10 records.")).toBeInTheDocument();
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("source-1");
  });

  it("says when the check itself failed, without blocking", async () => {
    server.use(
      http.post(`${PIPELINE}/runs/preview`, () =>
        HttpResponse.json({ detail: { message: "unknown normalizer 'x'", issues: [] } }, { status: 422 }),
      ),
    );
    await openCompose();
    await userEvent.type(screen.getByTestId("src_0_text"), CSV);
    expect(await screen.findByText(/Could not check what stub \(stub-csv\) can read/)).toBeInTheDocument();
    await waitFor(() => expect(screen.getByTestId("wizard_next")).toBeEnabled());
  });
});

describe("Compose & run — samples and scenarios", () => {
  beforeEach(() => {
    captured.runBody = null;
  });

  it("loads a sample file with its own normalizer and filename", async () => {
    await openCompose();
    await userEvent.selectOptions(await screen.findByTestId("src_0_sample"), "bruteforce_syslog");
    expect(await screen.findByText("Failed root passwords.")).toBeInTheDocument();
    expect(screen.getByText(/1 lines\s+loaded from/)).toBeInTheDocument();
    expect(screen.getByTestId("wizard_next")).toBeEnabled();
    expect(screen.getByTestId("src_0_norm")).toHaveValue("syslog");
  });

  it("shows a scenario by its title, with its description behind the (i)", async () => {
    await openCompose();
    const card = (await screen.findByTestId("scenario_overheating_pump")).parentElement as HTMLElement;
    expect(within(card).getByText("Overheating pump")).toBeInTheDocument();
    expect(within(card).queryByRole("tooltip")).toBeNull();
    await userEvent.click(within(card).getByRole("button", { name: "About Overheating pump" }));
    expect(within(card).getByRole("tooltip")).toHaveTextContent(/expects 1 alert/);
  });

  it("applies a scenario to every step, lands on Review, and confirms the result", async () => {
    await openCompose();
    await userEvent.click(await screen.findByTestId("scenario_overheating_pump"));

    expect(await screen.findByText("Scenario: Overheating pump")).toBeInTheDocument();
    expect(screen.getByTestId("step_4")).toHaveAttribute("aria-current", "step");
    expect(screen.getByText("pump_telemetry.csv")).toBeInTheDocument();

    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));
    expect(await screen.findByText("Matches what the scenario is tested to produce.")).toBeInTheDocument();

    const body = captured.runBody as RunBody;
    expect(body.inputs).toEqual([{ content: SAMPLE_CSV, origin: "pump_telemetry.csv" }]);
    expect(body.spec.sources).toEqual([{ normalizer: "stub", source_format: "stub-csv", params: {} }]);
    expect(body.spec.detectors).toEqual([{ name: "stub_threshold", params: {} }]);
    // The scenario's `bucket_minutes: 5` IS the catalogue default, so — exactly as if the
    // operator had clicked it — it is not sent (C4): the server applies the same default.
    expect(body.spec.alert_engine).toEqual({ name: "weighted", params: {} });
  });

  it("stops promising the scenario's numbers once the operator changes something", async () => {
    await openCompose();
    await userEvent.click(await screen.findByTestId("scenario_overheating_pump"));
    expect(await screen.findByText("Scenario: Overheating pump")).toBeInTheDocument();
    await toStep(2);
    await userEvent.selectOptions(screen.getByTestId("det_0_name"), "log_burst");
    await toStep(4);
    expect(screen.queryByText("Scenario: Overheating pump")).not.toBeInTheDocument();
  });
});
