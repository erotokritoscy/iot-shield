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

import type { MeResponse } from "@/lib/api/types";
import { RESULT_HELP } from "@/pages/compose/help";
import { captured, PIPELINE, RUN_ID, SAMPLE_CSV, server } from "@/test/msw";
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
  email_result?: boolean;
}

/** Choose how source `index`'s data arrives — the first decision on every source card. */
async function choose(index: number, mode: "pull" | "sample" | "upload" | "paste"): Promise<void> {
  await userEvent.click(await screen.findByTestId(`src_${index}_mode_${mode}`));
}

/** Open the wizard with source 1 set to `mode` (paste by default, which most tests type into). */
async function openCompose(mode: "sample" | "upload" | "paste" | null = "paste"): Promise<void> {
  renderApp("/compose");
  await screen.findByTestId("src_0_mode_upload");
  if (mode) await choose(0, mode);
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

/** The detector picker is an InfoSelect: open it, then choose by name. */
async function pickDetector(index: number, name: string): Promise<void> {
  await userEvent.click(await screen.findByTestId(`det_${index}_name`));
  await userEvent.click(screen.getByTestId(`det_${index}_name_option_${name}`));
}

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
    expect(await screen.findByTestId("src_0_mode_paste")).toBeInTheDocument();
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
    expect(screen.getByTestId("det_0_name")).toHaveTextContent("stub_threshold");
    expect(screen.getByText(/which your sources provide/)).toBeInTheDocument();
  });

  it("says when no source feeds the chosen detector, without hiding it", async () => {
    await withContent();
    await pickDetector(0, "log_burst");
    expect(await screen.findByText(/this detector will run and find nothing/)).toBeInTheDocument();
  });

  it("explains every detector in the open list, without choosing it", async () => {
    await withContent();
    await userEvent.click(screen.getByTestId("det_0_name"));
    await userEvent.hover(screen.getByRole("button", { name: "About log_burst" }));
    expect(screen.getByRole("tooltip")).toHaveTextContent("flags too many matching log lines");
    expect(screen.getByRole("tooltip")).toHaveTextContent("Reads lane:log");
    expect(screen.getByTestId("det_0_name")).toHaveTextContent("stub_threshold");
  });

  it("keeps the engine a native dropdown and explains every engine in its (i)", async () => {
    await withContent();
    await toStep(3);
    const picker = screen.getByTestId("compose_engine") as HTMLSelectElement;
    expect(picker.tagName).toBe("SELECT");
    await userEvent.click(screen.getByRole("button", { name: "About Alert engine" }));
    const tip = screen.getByRole("tooltip");
    expect(tip).toHaveTextContent("simple template score");
    expect(tip).toHaveTextContent("multiplies explained factors");
    await userEvent.selectOptions(picker, "weighted");
    expect(picker.value).toBe("weighted");
  });

  it("shows a progress bar while the run is going, polled by the id the run was posted with", async () => {
    // Hold the run open until the bar has been seen, as a long pull would.
    let finish: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      finish = resolve;
    });
    let polledId: string | null = null;
    server.use(
      http.get(`${PIPELINE}/runs/progress/:id`, ({ params }) => {
        polledId = String(params["id"]);
        return HttpResponse.json({ phase: "reading", done: 412_000, total: 968_000, elapsed_s: 102 });
      }),
      http.post(`${PIPELINE}/runs`, async ({ request }) => {
        captured.runBody = await request.json();
        await held;
        return HttpResponse.json({
          run_id: RUN_ID,
          events: 1,
          malformed: 0,
          detections: 1,
          alerts: 1,
          warnings: [],
          destinations: ["postgres"],
          top_alerts: [],
        });
      }),
    );
    await withContent();
    await toStep(4);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));

    expect(await screen.findByText(/Reading 412,000 \/ 968,000 rows · 1:42 elapsed/)).toBeInTheDocument();
    const bar = screen.getByRole("progressbar", { name: "Run progress" });
    // Reading is 2–60% of the bar: 412k of 968k rows lands at 27%.
    expect(bar).toHaveAttribute("aria-valuenow", "27");
    const body = captured.runBody as { progress_id?: string };
    expect(body.progress_id).toMatch(/^[0-9a-f-]{36}$/);
    expect(polledId).toBe(body.progress_id);

    finish();
    expect(await screen.findByText("Run complete.")).toBeInTheDocument();
    expect(screen.queryByTestId("compose_progress")).not.toBeInTheDocument();
  });

  it("reads a progress 404 as not started yet, not as a failure", async () => {
    let finish: () => void = () => {};
    const held = new Promise<void>((resolve) => {
      finish = resolve;
    });
    server.use(
      http.get(`${PIPELINE}/runs/progress/:id`, () =>
        HttpResponse.json({ detail: { message: "no run with that progress id", issues: [] } }, { status: 404 }),
      ),
      http.post(`${PIPELINE}/runs`, async () => {
        await held;
        return HttpResponse.json({ detail: { message: "boom", issues: [] } }, { status: 500 });
      }),
    );
    await withContent();
    await toStep(4);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
    await userEvent.click(screen.getByTestId("compose_run"));
    expect(await screen.findByText("Starting…")).toBeInTheDocument();
    expect(screen.queryByText(/Run failed/)).not.toBeInTheDocument();
    finish();
    expect(await screen.findByText(/Run failed/)).toBeInTheDocument();
  });

  it("withholds the verdict while the edited chain is still being checked", async () => {
    // The 350 ms debounce opens a window in which a verdict on screen would describe a spec
    // that no longer exists; a green "Chain is compatible." there would be a claim the page
    // cannot back.
    await withContent();
    await toStep(4);
    await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());

    await toStep(2);
    await pickDetector(0, "log_burst");
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
    await pickDetector(0, "unshipped_learner");
    expect(await screen.findByText(/has no fitted artifact, so the run/)).toBeInTheDocument();
    expect(screen.getByTestId("wizard_next")).toBeDisabled();

    await userEvent.type(screen.getByTestId("det_0_art"), "x");
    await waitFor(() => expect(screen.queryByText(/has no fitted artifact/)).not.toBeInTheDocument());
    expect(screen.getByTestId("wizard_next")).toBeEnabled();
  });

  it("offers a learning detector its artifact and no hyperparameters (ADR-0036)", async () => {
    await withContent();
    await pickDetector(0, "team3_zscore");
    // The shipped id preselects: mandatory here, and a `<select>` has no blank option.
    expect(await screen.findByTestId("det_0_art")).toHaveValue("skab_zscore");
    expect(screen.queryByText(/has no fitted artifact/)).not.toBeInTheDocument();
    expect(screen.queryByTestId("det_0_window_size")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /team3_zscore parameters/ })).not.toBeInTheDocument();
  });

  it("submits a learning detector's artifact and nothing else (ADR-0036)", async () => {
    await withContent();
    await pickDetector(0, "team3_zscore");
    const body = await runFromReview();
    expect(body.spec.detectors).toEqual([{ name: "team3_zscore", params: { artifact: "skab_zscore" } }]);
  });

  it("lets an uploaded file be taken back", async () => {
    await openCompose("upload");
    await userEvent.upload(screen.getByTestId("src_0_file"), new File([CSV], "readings.csv", { type: "text/csv" }));
    expect(await screen.findByText("readings.csv", { selector: "p" })).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("src_0_clear"));
    await waitFor(() => expect(screen.queryByTestId("src_0_clear")).not.toBeInTheDocument());
    expect(screen.getByText("source-1")).toBeInTheDocument();
    expect(screen.getByText(/Source 1 needs content/)).toBeInTheDocument();
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

  it("opens each non-zero result in a new tab, on that run's own list (ADR-0045)", async () => {
    await withContent();
    await runFromReview();
    for (const list of ["events", "detections", "alerts"]) {
      const tile = screen.getByTestId(`stat-${list}`);
      expect(tile.tagName).toBe("A");
      expect(tile).toHaveAttribute("href", `/runs/${RUN_ID}#${list}`);
      expect(tile).toHaveAttribute("target", "_blank");
      expect(tile).toHaveAttribute("rel", "noopener noreferrer");
    }
    // Nothing behind a zero to open, so it stays a number rather than a link to an empty list.
    expect(screen.getByTestId("stat-malformed").tagName).toBe("DIV");
  });

  it("explains each result tile behind an (i) that is not part of the link", async () => {
    await withContent();
    await runFromReview();
    for (const list of ["events", "malformed", "detections", "alerts"]) {
      const button = screen.getByRole("button", { name: `About ${list}` });
      // A button inside the link would open the run on click instead of the explanation.
      expect(button.closest("a")).toBeNull();
      await userEvent.click(button);
      expect(screen.getByRole("tooltip")).toHaveTextContent(RESULT_HELP[list as keyof typeof RESULT_HELP]);
      await userEvent.keyboard("{Escape}");
    }
  });

  it("links nothing when the run was not written to the store the read API serves", async () => {
    server.use(
      http.post(`${PIPELINE}/runs`, () =>
        HttpResponse.json({
          run_id: RUN_ID,
          events: 1,
          malformed: 0,
          detections: 1,
          alerts: 1,
          warnings: [],
          destinations: ["file"],
          top_alerts: [],
        }),
      ),
    );
    await withContent();
    await runFromReview();
    expect(screen.getByTestId("stat-alerts").tagName).toBe("DIV");
  });

  it("names each detector's choices on Review, defaults included", async () => {
    // The request omits a default, so a summary built from it would show a bare `log_burst`
    // and hide which preset will run.
    await withContent();
    await pickDetector(0, "log_burst");
    await toStep(4);
    expect(await screen.findByText(/preset: severity_spike/)).toBeInTheDocument();
  });

  it("takes content from an uploaded file and its origin from the filename (C2)", async () => {
    await openCompose("upload");
    await userEvent.upload(screen.getByTestId("src_0_file"), new File([CSV], "pump-02.csv", { type: "text/csv" }));
    await toStep(2);
    const body = await runFromReview();
    // Provenance is the filename: an operator reading the malformed-record audit later needs
    // to know which file a bad line came from.
    expect(body.inputs).toEqual([{ content: CSV, origin: "pump-02.csv" }]);
  });

  it("sends a parameter the operator actually moved (C4, the other half)", async () => {
    await withContent();
    await pickDetector(0, "log_burst");
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
    await pickDetector(0, "log_burst");
    await pickDetector(1, "log_burst");
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
    await pickDetector(2, "log_burst");
    await userEvent.click(screen.getByTestId("det_1_remove"));
    expect(screen.queryByTestId("det_2_name")).not.toBeInTheDocument();
    expect(screen.getByTestId("det_1_name")).toHaveTextContent("log_burst");
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

  it("opens a source on how its data arrives, with nothing chosen and no fields yet", async () => {
    await openCompose(null);
    const card = screen.getByTestId("src_0_card");
    const ways = within(card).getAllByRole("radio");
    // The test catalogue has samples but no data sources, so Pull is not offered.
    expect(ways.map((way) => way.getAttribute("data-testid"))).toEqual([
      "src_0_mode_sample",
      "src_0_mode_upload",
      "src_0_mode_paste",
    ]);
    for (const way of ways) expect(way).toHaveAttribute("aria-checked", "false");
    expect(within(card).getByText("Choose one to see its fields.")).toBeInTheDocument();
    for (const id of ["src_0_norm", "src_0_file", "src_0_text", "src_0_sample"]) {
      expect(within(card).queryByTestId(id)).not.toBeInTheDocument();
    }
    expect(screen.getByText(/Source 1 needs content/)).toBeInTheDocument();
  });

  it("draws only the chosen way's fields", async () => {
    await openCompose(null);
    const card = screen.getByTestId("src_0_card");
    const shown = () =>
      ["src_0_sample", "src_0_norm", "src_0_file", "src_0_text"].filter((id) => within(card).queryByTestId(id));

    await choose(0, "paste");
    expect(screen.getByTestId("src_0_mode_paste")).toHaveAttribute("aria-checked", "true");
    expect(shown()).toEqual(["src_0_norm", "src_0_text"]);
    await choose(0, "upload");
    expect(shown()).toEqual(["src_0_norm", "src_0_file"]);
    await choose(0, "sample");
    // A sample brings its own reader, so no normalizer is offered beside it.
    expect(shown()).toEqual(["src_0_sample"]);
  });

  it("drops what the previous way supplied when the way changes, keeping the reader", async () => {
    await openCompose();
    await userEvent.selectOptions(screen.getByTestId("src_0_norm"), "syslog");
    await userEvent.type(screen.getByTestId("src_0_text"), "a pasted line");
    await choose(0, "upload");
    // Hidden behind the upload, the pasted line would still have been sent with the run.
    expect(screen.getByText(/Source 1 needs content/)).toBeInTheDocument();
    expect(screen.getByTestId("src_0_norm")).toHaveValue("syslog");
    await choose(0, "paste");
    expect(screen.getByTestId("src_0_text")).toHaveValue("");
  });

  it("previews what will be read on the source's card", async () => {
    await openCompose();
    expect(screen.getByTestId("src_0_tab")).toHaveAttribute("aria-current", "step");
    const card = screen.getByTestId("src_0_card");
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

    await choose(1, "upload");
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
    await choose(1, "paste");
    await userEvent.type(screen.getByTestId("src_1_text"), "b");
    await userEvent.click(screen.getByTestId("compose_src_add"));
    await choose(2, "paste");
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
    await openCompose("sample");
    await userEvent.selectOptions(await screen.findByTestId("src_0_sample"), "bruteforce_syslog");
    expect(await screen.findByText("Failed root passwords.")).toBeInTheDocument();
    expect(screen.getByTestId("wizard_next")).toBeEnabled();
    // The sample's reader is stated, not offered: changing it is what Upload / Paste are for.
    expect(screen.getByTestId("src_0_reader")).toHaveTextContent("Read by syslog");
    expect(screen.queryByTestId("src_0_norm")).not.toBeInTheDocument();
    expect(screen.getByTestId("src_0_tab")).toHaveTextContent("bruteforce_syslog");
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
    await pickDetector(0, "log_burst");
    await toStep(4);
    expect(screen.queryByText("Scenario: Overheating pump")).not.toBeInTheDocument();
  });

  describe("a scenario that pulls from a live database", () => {
    const SOURCE_ID = "697d9636-f26c-4016-89fa-47effcdede36";
    const dataSource = (connection: string) => ({
      source_id: SOURCE_ID,
      name: "SensiPro meters",
      type: "OT",
      description: null,
      normalizer: "stub",
      source_format: "stub-csv",
      connection,
      has_credentials: false,
      created_at: "2026-10-21T16:55:21+00:00",
    });
    const sensipro = {
      name: "sensipro",
      connector: "sensipro",
      description: "Meters.",
      normalizer: "stub",
      source_format: "stub-csv",
      params: [{ name: "utility", type: "str", default: "electricity", required: false, choices: ["electricity", "water"] }],
      has_devices: false,
    };
    const registered = (sources: unknown[], connections: unknown[]) =>
      server.use(
        http.get(`${PIPELINE}/data-sources`, () => HttpResponse.json(sources)),
        http.get(`${PIPELINE}/data-sources/connections`, () => HttpResponse.json(connections)),
      );

    it("fills the source as a window, and sends the run a pull and no content", async () => {
      registered([dataSource("sensipro")], [sensipro]);
      await openCompose();
      await waitFor(() => expect(screen.getByTestId("scenario_live_meters")).toBeEnabled());
      await userEvent.click(screen.getByTestId("scenario_live_meters"));

      expect(await screen.findByText("Scenario: Live meters")).toBeInTheDocument();
      expect(screen.getByText(/reads this from the live database/)).toBeInTheDocument();
      expect(screen.queryByText(/Tested to produce/)).not.toBeInTheDocument();

      await waitFor(() => expect(screen.getByTestId("compose_run")).toBeEnabled());
      await userEvent.click(screen.getByTestId("compose_run"));
      await waitFor(() => expect(captured.runBody).not.toBeNull());
      expect((captured.runBody as unknown as RunBody).inputs).toEqual([
        {
          pull: { source_id: SOURCE_ID, start: "2023-07-01", end: "2023-07-30", params: { utility: "water" } },
          origin: "SensiPro meters 2023-07-01..2023-07-30",
        },
      ]);
      // It promised nothing, so there is nothing for the result to match or differ from.
      expect(await screen.findByText(/Written to:/)).toBeInTheDocument();
      expect(screen.queryByText(/the scenario is tested to produce/)).not.toBeInTheDocument();
    });

    it("shows the window it filled in when you go back to the source", async () => {
      // The form must state what the run will read, or `electricity` would sit beside a water pull.
      registered([dataSource("sensipro")], [sensipro]);
      await openCompose();
      await waitFor(() => expect(screen.getByTestId("scenario_live_meters")).toBeEnabled());
      await userEvent.click(screen.getByTestId("scenario_live_meters"));
      await screen.findByText("Scenario: Live meters");

      await userEvent.click(screen.getByTestId("step_1"));
      expect(await screen.findByTestId("src_0_pull_start")).toHaveValue("2023-07-01");
      expect(screen.getByTestId("src_0_pull_end")).toHaveValue("2023-07-30");
      expect(screen.getByTestId("src_0_pull_utility")).toHaveValue("water");
      expect(screen.getByTestId("src_0_source")).toHaveValue(SOURCE_ID);
      expect(screen.getByTestId("src_0_pulled")).toHaveTextContent("A window of SensiPro meters 2023-07-01..2023-07-30");
    });

    it.each([
      ["no data source is on that connection", [dataSource("biology")], [sensipro]],
      ["the organization may not use that connection", [dataSource("sensipro")], []],
    ])("is disabled and says what is missing when %s", async (_why, sources, connections) => {
      registered(sources, connections);
      await openCompose();
      await waitFor(() =>
        expect(screen.getByText(/Needs a data source on the sensipro connection/)).toBeInTheDocument(),
      );
      expect(screen.getByTestId("scenario_live_meters")).toBeDisabled();
      // A file scenario is unaffected.
      expect(screen.getByTestId("scenario_overheating_pump")).toBeEnabled();
    });

    it("says live data rather than an alert count behind its (i)", async () => {
      await openCompose();
      await userEvent.click(await screen.findByRole("button", { name: "About Live meters" }));
      expect(screen.getByRole("tooltip")).toHaveTextContent("1 source · 1 detector · live data, so no expected counts");
    });
  });
});

describe("Compose & run — Email me the result (ADR-0052 amendment)", () => {
  const signedIn = (username: string): void => {
    const me: MeResponse = {
      user_id: "22222222-2222-4222-8222-222222222222",
      username,
      role: "member",
      organization: { org_id: "acme", name: "Acme" },
    };
    server.use(http.get(`${PIPELINE}/auth/me`, () => HttpResponse.json(me)));
  };

  it("is ticked by default, names the address and asks the server to send it", async () => {
    signedIn("ana@example.com");
    await withContent();
    await toStep(4);
    expect(screen.getByTestId("compose_email_result")).toBeChecked();
    expect(screen.getByTestId("compose_email_note")).toHaveTextContent(
      "Sent to ana@example.com when the run finishes or fails.",
    );
    const body = await runFromReview();
    expect(body.email_result).toBe(true);
  });

  it("unticked, tells the server to send nothing", async () => {
    signedIn("ana@example.com");
    await withContent();
    await toStep(4);
    await userEvent.click(screen.getByTestId("compose_email_result"));
    const body = await runFromReview();
    expect(body.email_result).toBe(false);
  });

  it("says nothing will arrive when the username is not an address", async () => {
    signedIn("stelios");
    await withContent();
    await toStep(4);
    expect(screen.getByTestId("compose_email_note")).toHaveTextContent(/“stelios” is not an email address/);
  });
});
