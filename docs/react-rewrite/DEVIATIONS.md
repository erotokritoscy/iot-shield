# Deviation & decision log — React dashboard rewrite

The single index of everything that needs Andreas's eyes. Entries are grouped: deviations
from a default decision first, then judgment calls a reviewer would be surprised by.

**No parity item is `blocked` or `contested`.** Every row of `PARITY.md` is `done`, so this
file carries no dropped behavior — only choices.

---

## Deviations from a default decision (§3)

### D5 refined — the eval probe distinguishes "absent" from "unreachable"  [D5 / E1]

- **When:** wiring the Evaluate gate.
- **Trigger:** D5 says "404/unreachable hides the mode"; E1 says the mode must show the
  upload fallback "when the eval API is unreachable". Read literally, the two cannot both
  hold — an unreachable API would hide the very page that is supposed to degrade. The brief
  puts §3 defaults and §6 parity at the same precedence, and H7 forbids dropping a parity
  semantic, so the resolution had to satisfy both.
- **Decision:** the probe reads the *kind* of failure, because the two mean different
  things. A **404** is the deployment saying "this image has no research plane" — under
  ADR-0021 D1 the router is absent, not guarded — so the mode is hidden, exactly as an unset
  `IOTSHIELD_EVAL_UI` did. A **transport failure** means the plane is enabled but the
  Pipeline API is down or still starting, which is the situation the old page degraded
  through rather than disappeared in; the mode stays visible and offers the saved report.
- **Consequences:** D5's one-flag simplification stands and E1 is satisfied whole. Pinned by
  two tests ("hides the mode when the eval router was never registered", "offers the
  saved-report upload when the Pipeline API is unreachable"). Recorded in ADR-0031 D5.

### D2 refined — shadcn/ui's *approach*, without Radix  [D2]

- **When:** building `web/src/components/ui/`.
- **Trigger:** shadcn/ui is "you own the source" plus Radix primitives. Owning the source is
  the part that matters here; the primitives are a choice, and this app's controls are
  ordinary selects, text inputs, checkboxes and radios inside dense forms.
- **Decision:** the components live in `components/ui/` and we own them, in shadcn's style
  (CVA variants, `cn`, token-driven classes), but built on **native** form elements. No
  Radix dependency.
- **Consequences:** keyboard, mobile and assistive behaviour come from the platform rather
  than from a library we would have to keep honest; the test suite drives real form elements.
  If a future control genuinely needs a headless primitive (a combobox over hundreds of
  adapters, say), adding Radix then is a small, local change.

---

## Judgment calls worth a second opinion (§2.1)

### "How many X?" number inputs became Add / Remove buttons

Streamlit expressed the repeatable sections (1–10 sources, 1–10 detectors, 1–6 entries) as a
number input. A React form can add and remove a row directly, which states the same bound as
an action rather than as a count to retype into a clamped field — and a clamped controlled
number input is genuinely awkward (clearing it snaps back, so typing "2" over "1" yields 12).
The bounds are unchanged and still enforced. `RowCount` in `components/ui/controls.tsx`.

### The dark theme re-instances the chart palette rather than reusing its hexes

V1 says the palette is "ported exactly". It is, for the light theme — `#2a78d6`, `#d03b3b`,
`#b8b6ac` and the neutrals are the values in `charts.py`. On a near-black paper the same blue
falls under WCAG AA, and X6 requires both themes to be legible, so the dark theme re-instances
the same *roles* (single-hue series, reserved status red, truth as a neutral backdrop) at
values that pass. The roles are the invariant; the hexes were the light-theme instance of it.
`web/src/charts/palette.ts` says so at the top.

### Two constants and one parser are duplicated across the language boundary

`TIMELINE_SPAN_CAP` (2000) and the stub CSV format id (`stub-csv`) are not published on the
wire, and H5 forbids adding an endpoint to publish them. Both are duplicated with a comment
naming the Python definition that owns them. Separately, `eval/sweep.py: values` is
re-implemented in TypeScript (`pages/evaluate/lib.ts: sweepValues`) — the Python original is
*shared* between the CLI and the old page precisely so they could not disagree about
whitespace, empty items and value types, and that sharing is impossible across languages. It
is pinned by tests instead of by construction, which is weaker; a divergence would show up as
a label reading `window_size='20'` rather than `window_size=20`.

### Vitest runs single-threaded

Parallel workers raced on Vite's transform cache under Windows file locking, and the observed
failure was EBUSY errors that **silently dropped two whole test files** from the run — a green
suite that had not run a third of itself. `fileParallelism: false` in `vite.config.ts`. The
suite takes ~21 s; that is a good trade for a result that means what it says.

### Playwright is a dev dependency, and the screenshots are a script, not a test

The brief made a Playwright smoke pass a stretch goal. `web/scripts/screenshots.mjs` drives
the real compose stack and captures the walkthrough, but it asserts nothing beyond "the page
reached this state" and is **not** part of `npm run check` (which must run with nothing
listening). Promoting it to a smoke test is the obvious next step and is deliberately not
done here.

### Inter and JetBrains Mono are requested, not bundled

The design brief names them. Bundling would mean either a network fetch at build time or
~400 KB of committed font binaries, neither of which earns its place in this change. The font
stacks request them first and fall back to the system UI and mono faces, so a machine with
Inter installed gets the intended type and every other machine gets something reasonable.

### Two small additions to the ported chart specs

The severity chart's x labels are forced horizontal (`labelAngle: 0`) and its y axis ticks in
whole alerts (`tickMinStep: 1`). Vega rotated the labels this chart's identity rides on, and
with two alerts in the tallest bar it ticked at 0.5 — half an alert is not a thing. The
Altair original would have done both; these are the fixes, not a restyle.

### `formatCell` renders an empty list as blank, not as `[]`

The per-case table printed `[]` in the `notes` column of every clean row — pandas' repr,
faithfully ported. A list cell now reads as its contents. The column's whole point is that a
row of zeros arrives with its reason, and `[]` is noise in front of it.

### Commit granularity: one large first commit

The brief asks for reviewable slices. The router couples every page (`App.tsx` imports all
five), so a Monitor-only commit would not build, and "gates green at every commit" is a hard
requirement. The result is four commits — the app, packaging, the fixes full-stack
verification found, and docs — rather than one per plane. Reviewing by directory
(`src/pages/`, `src/lib/api/`, `src/charts/`) is the practical substitute.

---

## Found by verification, fixed, worth knowing

### The SPA route `/assets` collided with Vite's build directory

Caught only by the real browser against the real stack: nginx matched the built `assets/`
directory before the SPA fallback and issued a directory redirect built from its own listen
port — an address that does not exist outside the container. The page was unreachable in
production and perfectly healthy in `npm run dev`. Fixed by building to `static/` and setting
`absolute_redirect off`. It is the argument for the last gate being a browser, not a mock.

### `File.text()` does not exist in jsdom

Both upload paths (a run's input source, a saved report) used it, so neither could be tested —
and the failure mode was a *plausible* error message rather than a crash, which is the kind
that survives review. Both now use `FileReader` via `lib/files.ts`, and both paths have tests.

---

# Post-ship review (2026-07-29)

Three independent reviewers over `web/`, then one adversarial verifier over every finding
*and* every proposed fix. 21 findings were raised; 20 were implemented, one was withdrawn as
a misreading, and one defect can only be fixed on the Python side. Tests: 87 → 102.

## The pattern behind six of them

The app repeatedly **computed a truth signal and then never consumed it**: `validation.stale`
(returned, unread), `isUnreachable` (exported, zero call sites), `EvalPlane.loading`
(documented as existing to stop nav flicker, unread by the shell), `ready()` (used only to
preselect, never to warn), and the eval plane's truncation warning (present there, absent from
every read-plane list). The modelling was right throughout; the last-mile wiring was not.

## Withdrawn — the claim was wrong

**List-typed params are NOT broken.** The review claimed `lag_steps` / `channels` render as
text boxes whose contents 422. They do not: `src/iotshield/detect/team3/params.py` wraps them
in `BeforeValidator(_sequence)`, whose docstring says *"the dashboard's generic param widgets
are text boxes, so `"1, 5, 10"` has to mean the same thing as `[1, 5, 10]`"* — the server
accommodates this form deliberately, and it is on the **model** so it holds for config, the
artifact loader and the harness alike. The proposed fix (coerce per item, reusing
`sweepValues`) would have *introduced* a bug: `channels` is `tuple[str, ...]`, so a channel
named `"1"` would have been sent as the integer `1` and refused. Nothing to fix.

## Cannot be fixed here — H1 (read-only Python)

`api/control.py: _literal_choices` does not unwrap `X | None` the way `_type_name` does, so
the `meter` normalizer's `meter_type` is published as `type: "Literal", choices: null` and the
UI renders a **free-text box for a closed two-value vocabulary**. `passthrough.lane`, declared
without `| None`, works. This is the exact failure ADR-0023 D9 exists to prevent, and it is a
one-line fix in the Pipeline API. Reported, not worked around: guessing the vocabulary
client-side would violate H3 and H6.

## Decisions worth a second look

- **Truncation is detected with `limit + 1`, not by comparing against `/summary.counts`.**
  The counts are global; `/events` and `/detections` are filtered per asset and per lane, so
  comparing them would have reported truncation on every asset with fewer than 5,000 readings.
  Asking for one row more than we draw is the only test that generalises, and it needs no
  second request. Where a trustworthy total *does* exist (alerts, malformed) it is shown too.
- **The severity chart is relabelled rather than suppressed when truncated.** "Count per
  severity" and "count per severity among the 1,000 highest-priority" are different claims;
  only one is true under a cap. Deleting information to avoid mislabelling it is the worse
  trade.
- **Charts moved off the UTC wall-clock trick to real instants + `scale: {type: "utc"}` and
  `timeUnit: "utc…"`.** The old approach stripped the zone so Vega's local parse would
  reproduce the UTC digits — an identity that fails in the DST spring-forward gap, where
  ECMA-262 resolves a skipped local time with the pre-transition offset. One hour a year, and
  silent. Both mechanisms are needed: a UTC scale governs axes and does **not** reach tooltip
  formatting.
- **The case timeline now carries `end` and `drawn_end` separately.** Geometry may exaggerate
  a span for visibility; the tooltip may not. Previously a zero-length detection reported
  `start 03:00:00 / end 03:04:48 / seconds 0` — three numbers that cannot all be true. A
  `floored` field names the one thing the drawing cannot say for itself.
- **`caseVariance` keeps dropping undefined scores, and now says how many it dropped.** The
  filter is right (ADR-0021 D2: a dot at 0 claims the detector scored nothing, when the truth
  is nothing was measurable). Silence was the defect. Tie *encoding* was considered and
  rejected: aggregating to a count destroys the per-case tooltip, which is this chart's entire
  purpose, and jitter would place a mark at a score that does not exist.
- **`refetchOnWindowFocus` was proposed and rejected.** It contradicts the written decision at
  `App.tsx:12-15`, and `/alerts` is priority-descending — a focus refetch re-ranks the queue
  under the analyst's cursor and can silently swap which alert the inspector is showing. An
  explicit **Refresh data** control plus a "data as of" line gives the same recovery at a
  moment the operator chooses.
- **Row activation is a `<button>` inside the first cell, not `role="button"` on the `<tr>`.**
  The latter replaces the row's own role, makes it an invalid child of `rowgroup`, and severs
  the row↔cell relationships assistive technology navigates by — trading one accessibility
  defect for a worse one.
- **Queue filters, yes; queue sorting, never.** Filtering is a view over rows the server
  already ranked. Sorting would replace the product's output with a preference (H3). Filters
  deliberately ship *with* the truncation notice, since a confident "no critical alerts" over a
  capped list is worse than no filter at all.
- **The latched benchmark lives in a context on the shell route.** A module-scoped variable was
  tried first and the test suite caught it immediately: it leaked one test's run into the next,
  which is the same defect as leaking one session's run into another.
- **`Satisfies` is now mutual (`[Server] extends [Parsed]` both ways).** One-way assignability
  is satisfied by a server type with *extra* fields, so an added contract field compiled clean
  and was then stripped at runtime by zod — precisely what the comment above it promised could
  not happen. The tuple wrappers are load-bearing: a bare conditional distributes over unions
  and fails on every enum. Verified against all 14 assertions: zero existing breakage.
- **`accept` was proposed for the upload input and dropped.** This input takes stub CSV, two
  syslog dialects, Zeek `conn.log` (no extension), meter CSVs and `NormalizedEvent` JSONL — any
  `accept` list either hides real formats or filters nothing. The size cap is 8 MB per source
  and is deliberately *not* nginx's 64 MB: that bounds the whole request, and `JSON.stringify`
  over tens of MB locks the tab long before the proxy would answer.

## Known gap in the test fixtures

`test/msw.ts` answers `POST /runs` with 200 unconditionally, so `ComposePage.test.tsx` asserts
a successful run for a `team3_zscore` chain that the real runner refuses (`_ensure_fitted`).
That is why the untrained-detector defect was invisible to a green suite. The new warning is
tested directly; making the fixture model the refusal is a separate change.
