# ADR-0036: A judge-only page offers no hyperparameters

- **Status:** accepted
- **Date:** 2026-07-30
- **Amends:** ADR-0023 D9 (what the compose page renders for a detector), ADR-0033 (the
  optional-vocabulary rule, which this page needed the exception to)
- **Related:** ADR-0022 (hyperparameters vs fitted state; an artifact is atomic), ADR-0035 D1
  (the shipped artifact ids, published as that param's `choices`), ADR-0031 (two dashboards)
- **Scope:** UI surface only. No contract, no capability, no adapter, no API field changes.

## Context

Asked while reading the registry: *compose-and-run is judge-only, so aren't a learning
detector's hyperparameters redundant there?* They are worse than redundant. The truth table for
`team3_zscore` or `meter_iforest` on the run path has one working row:

| `artifact` | hyperparameters | outcome |
|---|---|---|
| named | none | **works** |
| named | any one set | `ValueError` at construction: *"cannot override [...] while loading a trained artifact"* |
| absent | none | run refused: *"has no trained state; name its artifact in params"* |
| absent | any set | run refused, same message |

So every hyperparameter widget on that page was a **trap**: moving one turned a working chain
into a failed run, and no combination of them could ever help. The page worked only because
`_untouched` / `submittedParams` drop what nobody moved — a fix made at the *submission* layer
(for exactly this incident: an untouched `team3_zscore` row used to submit its whole prefilled
block beside the artifact id) while the *display* layer was left alone.

The failure also arrived late. `/runs/validate` returns ok, because the compatibility guard's
defining property is that it reasons about a spec **without constructing adapters** — which is
why `_ensure_fitted` lives in the runner instead. So the operator gets a green chain and then a
construction error after pasting their data.

Two facts make this a display problem rather than a design problem:

- **The registry factory is right to accept them.** `build_detector(name, params)` is the single
  construction path for three callers, and two of them need hyperparameters: the evaluation
  harness fits from them (`config/bench/team3_scratch.yaml`) and `--sweep` varies them.
- **`/modules` is introspection, not curation.** `describe_params` walks the factory signature
  and cannot know which parameters are meaningless on the run path.

And the Evaluate page **already had this right** — a Trained/From-scratch radio, with trained
mode showing the artifact alone and captioning why there is nothing to edit or sweep. The
inconsistency was between two pages of one product: the page that *can* fit modelled the modes
explicitly; the page that cannot showed both and distinguished neither.

## Decisions

### D1 — A learning detector on the compose page shows its artifact and nothing else

Both dashboards. No mode radio, unlike Evaluate: a run never fits (`Fittable` is an
evaluation-only capability, ADR-0021 D3), so trained is not one of two modes, it is the only
one. The caption says so and points at the Evaluate page for fitting.

Rendered **inline rather than inside the collapsed "parameters" panel**. It is the one parameter
without which the run is refused, so it must not be something the operator goes looking for.

### D2 — The artifact preselects, the documented exception to ADR-0033

`initialValue` / `_param_widgets` deliberately open an *optional* closed vocabulary on "not
given", because preselecting `choices[0]` fabricates a value nobody chose. That rule is right
for `meter.meter_type` and wrong here: wherever this chooser is rendered an artifact is
**mandatory** — a blank is a reported blocker, not an absent value — so the only thing opening
blank buys is a page that opens broken.

On Compose it was also internally contradictory. Neither `Select` nor `st.selectbox` renders an
empty option, so the box *displayed* `skab_zscore` while the value was `""`, directly beside a
warning saying no artifact was named.

The Evaluate page's trained mode already reasoned this way; the rule now lives in one place
(`web/src/lib/artifact.ts:initialArtifact`) that both pages read, composed over `initialValue`
rather than re-deriving it — that function has *two* rules, and only "blank vs preselect" is
excepted here. The second, "a default outside `choices` falls back to a real option", must
survive, or the widget is handed a value matching no option and reproduces the contradiction
above one field over.

**This decision justifies preselecting *something*, and does not say *which*.** With one shipped
id per adapter (the case today) "the first" is "the only" and the question does not arise. With
two it becomes whichever filename sorts first, because `packaged_artifact_ids` sorts for
determinism and publishes no preference — so the page would fabricate a choice nobody made,
which is precisely the ADR-0033 defect this exception is carved out of, and a wrongly-preselected
artifact judges *nothing* (an artifact serves only the asset it was fitted for, ADR-0034) while
captioned as if it were configured. That is not hypothetical at product scale: a fleet of 1,656
meters needs 1,656 fits. Before a second id ships for one adapter, either preselect only when
there is exactly one (which needs the blank `<option>` from ADR-0033's amendment to be coherent)
or make the preference a published fact rather than a filename accident.

### D3 — Holding no value for the knobs, not merely hiding them

For a learning detector the row's params are `{artifact: <id>}` and nothing else, so
`submittedParams` cannot name a hyperparameter even by accident: it skips a param whose value is
`undefined`. Hiding the widgets while keeping their values would leave the half-configured
request one refactor away.

### D4 — The untrained warning stays, and is now tested where it is live

`fitted()` plus its notice still exist, because the gap they cover is real and the guard cannot
see it. With D2 the warning is reachable only for an adapter that declares an artifact param and
**ships no ids** — where the free-text fallback (ADR-0035 D1) is the escape hatch. The web
fixtures gained `unshipped_learner` for exactly that shape, which is what keeps both the
fallback and the warning under test.

### D5 — Shared, not copied

The chooser is one widget on two pages, so it is written once:

- React: `ArtifactField` moved out of `pages/evaluate/ComposeEntries.tsx` into
  `components/ArtifactField.tsx`, with its pure helpers in `lib/artifact.ts` — the same split
  `ParamFields` / `lib/params.ts` already uses, and it keeps the always-present page from
  importing the optional Evaluate feature folder.
- Streamlit: `_artifact_widget`, lifted out of `_detector_plan`.

The **caption** stays with each caller, because the two genuinely differ: Evaluate can offer to
fit from scratch instead, and a run cannot.

## Consequences

- **Picking `team3_zscore` on the compose page now yields a runnable chain in one click**, where
  before it yielded a dropdown displaying an artifact, a warning contradicting it, and four
  knobs that could only break it.
- **Two hyperparameters are refused that would be safe to change.** `min_segment_length` (team3)
  and `min_episode_hours` (meter) appear only in the run-merging loop, *after* scoring; neither
  touches the feature schema, baseline, threshold or column order, so overriding one beside an
  artifact would misalign nothing. ADR-0022's rule — hyperparameters are exactly the factory's
  parameters — is clean to enforce and slightly over-broad, because atomicity is checked on the
  whole params object at once. Splitting each params model into fitted-state-bearing knobs and
  post-hoc decision knobs was **considered and rejected for now**: it amends the law, adds a
  per-field flag to `/modules`, and buys one real operator affordance ("stop reporting
  single-row blips" without a refit, which a judge-only deployment cannot perform). Recorded so
  the option stays visible rather than being rediscovered.
- **The Streamlit compose page still has no untrained warning** (the React one does). Left
  alone: that page is the superseded UI kept runnable through the changeover (ADR-0031), and
  with D2 the case is unreachable for both shipped adapters.
- **`describe_params` remains uncurated, deliberately.** The fix is at the render layer, where
  the knowledge of "this page cannot fit" actually lives. Teaching the API which params a *run*
  can use would put a client's concern in the catalogue and give `/modules` two answers.
- **The trap is closed on two pages and open for every other caller.** A hand-written
  `config/*.yaml`, the CLI, and a direct `POST /runs` can still send `{artifact: x, window_size:
  20}`; `/runs/validate` passes it clean and the refusal arrives from `build_detectors` *after*
  `normalize_sources` has ingested the whole payload. The guard's stated reason for not catching
  it — that it reasons about a spec without constructing adapters — holds for the *untrained* row
  and not for this one: "this entry names `artifact_param` and at least one other param" is a
  pure read of the spec plus `AdapterSpec.artifact_param`. (The law is already an approximation:
  `compat.py` does construct for its duplicate check, and swallows exactly the `ValueError`
  `resolve_params` raises.) One ERROR issue in the detector loop would cover every caller at
  once. **Not done here** because it is a guard behaviour change rather than a render fix, and it
  is the more valuable half of this problem — recorded so it is not rediscovered from a
  screenshot.
- **`ParamInfo.required` cannot express this and should not be made to.** It is
  `not has_default`, and a learning adapter's artifact param defaults to the `None` sentinel, so
  `required` is `False` for exactly the param that is mandatory here. It cannot be flipped
  without lying, because artifact-less construction is legal and is the Evaluate page's
  from-scratch mode, and both pages read the same `ParamInfo`. Mandatoriness is a property of
  *(page, mode)*, not of the param — which is why the exception lives in a caller-side
  initial-value function and `params.ts`'s rule stays unqualified. Worth knowing: `required` is
  currently read nowhere in `web/src` and no registry factory has a defaultless config param, so
  it is always `false` and never consulted — a published field whose name invites precisely the
  wrong generalisation.
