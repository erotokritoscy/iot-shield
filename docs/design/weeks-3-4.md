# Design Proposal — Weeks 3–4: Legibility, Depth, and Handoff

**Status:** Proposed · **Date:** 2026-07-22 · **Author:** Andreas Tziakouris (CYENS internship)
**Audience:** Project stakeholders + engineering · **Builds on:** `week-2.md` (Phase 1 seams + Phase 2 depth) · **ADR head at time of writing:** 0024

---

## 1. Summary (the one-paragraph version)

The system now has **one of everything**: three input modalities (telemetry, logs, network flow) that are each normalized *and* judged, an alert engine that correlates across lanes, a pluggable result sink, an orchestration UI, a Pipeline API, and an evaluation harness that scores detectors against labelled data. The architecture is complete; what it lacks is not capability but **legibility** — the system does the work but does not yet *explain* it, does not *show* the modules it already has, is not as *easy to feed* as it should be, and has no *front door* for the researcher who will extend it after this internship. Weeks 3–4 close that gap and add one unit of real detection depth. This document lists the six pieces of work, explains each from first principles, and sequences them: **(1) interpretation + evidence enrichment, (2) a pass-through normalizer, (3) an Isolation Forest detector, (4) a React dashboard, (5) corpus enrichment, (6) contributor documentation and handoff guides.** All six are in scope for the two remaining weeks.

---

## 2. Where the project stands

The pipeline is four swappable ports — **normalize → detect → correlate/prioritize → sink** — written against fixed contracts, so any module honoring a contract slots in by name (`week-2.md` §2, §7). As of the flow lane (ADR-0023 / ADR-0024) every modality the product claims is both ingested and detected, and a single run carrying telemetry + syslog + flow for one asset drives the alert engine's cross-lane corroboration factor to its ceiling. The evaluation harness (ADR-0021) is a third plane that *judges* the pipeline rather than using it, reporting precision/recall/F1 per detector against labelled corpora.

Two honest gaps are already recorded and carry into this plan:

- The alert and detection contracts reserve fields for **explanation** — `DetectionResult.event_kind`, `DetectionResult.evidence`, `PrioritizedAlert.threat_class` / `recommended_action`, and a `rationale` that is currently thin — and most of them are populated sparsely or not at all. The product's promise is *"prioritized, **explained** alerts,"* and the "explained" half is under-built.
- The flow detector "ships fitted to no benchmark" and the cross-lane corroboration claim "remains a demonstration, not a benchmark," because there is no labelled **flow** corpus and no labelled **multimodal** corpus (ADR-0021 / `week-2.md` §11.3).

## 3. The organizing thesis: legibility over capability

With the architecture complete, the marginal value of the next two weeks is highest where the system is *opaque*, not where it is *thin*. Four opacities:

- **An alert is a number, not an explanation.** An analyst is handed a priority score and a one-line rationale, but not *what* fired it, *where* on the timeline, or *what it probably means*.
- **The module catalog is invisible.** The registry, the capability descriptors, and the compatibility guard all exist, but nothing *presents* them, so "what can this system do / what can I plug in?" has no visible answer.
- **The front door for clean data is narrow.** Every source must pass through a format-specific parser, even a source that already arrives in a structured, near-normalized shape.
- **There is no front door for a contributor.** The ADR trail explains *why* every decision was made, but there is no single guide that says *"here is how you add a module and the test it must pass."*

Every item in §4 removes one of these opacities, or adds the one piece of genuine depth (a second, complementary detector) whose value the harness can now measure. Nothing here changes a contract or the compute core; the work is additive by construction, which is what makes six items realistic in two weeks.

A note on provenance that governs two of the items: reference modules under `../reference/` are **read-only** (golden rule #3). Where this plan draws on team3's post-processing, the *ideas* are re-implemented against our contracts, not imported — with provenance noted in the adapter docstring.

---

## 4. The work, explained (in execution order)

### 4.1 Interpretation + evidence enrichment

**What.** Fill the explanation fields the contracts already reserve. Two layers:

- *Evidence* — for each detection, record **which signal drove it**: the top contributing metric/feature/channel, its value and score at the window's peak, and the window bounds. This is the attribution an analyst needs to trust or dismiss an alert.
- *Interpretation* — a heuristic operational label derived from the **shape** of the anomaly (a single-window spike reads as a short transient; a short contextual deviation as mode-specific behavior; a long collective span that runs to the horizon as drift or a setpoint shift; a sustained collective span as degradation or a lasting fault), carried into `event_kind` / `threat_class` and a human-readable `rationale`.

**Why (first principles).** The alert is the product. Today it says "asset 7, score 76"; enriched, it says "sustained pressure fault on the camera, corroborated across telemetry and flow, driven by the pressure channel's rolling z-score of 8.2 over this 40-second window." The contracts were designed for exactly this — the fields exist and sit empty — so this is the highest value per unit of work available and it requires no contract change.

**A design principle this settles.** A detector's *feature representation is private to that detector*: the z-score path engineers rolling / lag / FFT features (`detect/team3/_features.py`), the flow detector counts distinct peers and ports, an autoencoder consumes raw scaled windows. There is no single feature representation that serves all detectors, so there is deliberately **no shared feature-extraction stage** between normalize and detect — such a stage would either be a lowest-common-denominator that helps nobody or a fifth port every detector must ignore. What crosses the port boundary is not the raw feature vector but its **human-readable attribution** (the driving metric/feature/channel), emitted as `DetectionResult.evidence`. Feature-level insight is therefore a *detector output*, not a *pipeline stage*.

**Source material.** team3's `evaluation/interpretation.py` (window-shape → operational label + rationale) and `evaluation/explanations.py` (top-feature / top-channel attribution per event) are the shape to re-implement. The interpretation labels are heuristic and must be marked as such (the reference sets an explicit `is_heuristic` flag) so the dashboard never presents a guess as a diagnosis.

**Touches.** Detector adapters (populate `evidence` / `event_kind` at detection time); the alert engine (fold interpretation and corroboration into `threat_class` / `recommended_action` / `rationale`); the dashboard (render it). A new ADR standardizes the **evidence schema** — the keys every detector agrees to emit — so downstream consumers and future adapters can rely on its shape. Tests cover the interpretation heuristic and each detector's evidence output; the detector port suite gains an evidence-shape check.

### 4.2 Pass-through normalizer

**What.** A normalizer adapter that accepts input which already arrives in the normalized shape (JSONL matching `NormalizedEvent`, or a small pre-agreed structured form), **validates** each record against the contract, and **quarantines** anything that fails as a `MalformedRecord` — rather than parsing a raw wire format.

**Why.** Not every source needs parsing; some arrive already structured. The correct way to accept such data is a new adapter *behind the existing port*, not a bypass of the stage. The trust boundary is preserved exactly: every record is still validated (timezone-aware UTC timestamps per golden rule #4, finite values, required fields) and malformed input is still captured, never dropped (ADR-0004). "Normalization" for this source is validation-and-enveloping instead of parsing — the four-port model is untouched, and the pipeline still always normalizes.

**Bonus.** It is the simplest possible normalizer, which makes it the natural **hello-world example** in the contributor guide (§4.6): the least code a working adapter can be.

**Touches.** `normalize/` (new adapter, capability descriptor, `params_type` if any), the normalizer port contract test suite, a demo config + a tiny demo input, and the README / CLAUDE map. Small.

### 4.3 Isolation Forest detector

**What.** A second telemetry detector — an unsupervised Isolation Forest — as a credible complement to the robust z-score, benchmarked head-to-head through the harness.

**Why.** The z-score flags **single-feature spikes**; an Isolation Forest flags **unusual combinations** of engineered features (it is multivariate). They are complementary, and the harness lets us *show* the difference rather than assert it — the answer to "which detector do we ship, and for what?" (`week-2.md` §11.2 named Isolation Forest as the next detector; ADR-0012 froze the runtime at numpy-only with scikit-learn deferred to a per-model ADR — this is that ADR).

**Honest constraints, stated plainly.**

- It pulls **scikit-learn** into the runtime dependency surface for the first time — a deliberate step, recorded in its ADR with the image-size cost stated.
- It is **unsupervised** (needs no labels), which is the right fit for the product's reality of unlabelled customer data — as opposed to a *supervised* model (e.g. gradient boosting), which would need labels the deployment does not have and would therefore be a research-plane-only artifact. Choosing Isolation Forest over a supervised learner is a fit-for-purpose decision, not a sophistication ceiling.
- It must be **judged, not assumed better.** It can be benchmarked on SKAB (telemetry) today, so it is not blocked; corpus enrichment (§4.5) then broadens the comparison.
- It **learns**, so it obeys the hyperparameters-vs-fitted-state law (ADR-0022): its hyperparameters are exactly its factory parameters, it implements `fit()` and an artifact, and it is sweepable. team3's Isolation Forest fragments and the engineered-feature path (`_features.py`) are the ideas to re-implement.

**Touches.** `detect/` (new adapter, params, artifact, train entry point), the runtime dependency set (scikit-learn), a new ADR, the detector port suite plus the `Fittable` behavioral test, and a benchmark spec + demo.

### 4.4 React dashboard

**What.** Replace the Streamlit dashboard with a React single-page app over the *same* read and Pipeline APIs — the same three planes (Monitor / Operate / Evaluate), plus a **module-library view** and the **evidence rendering** from §4.1.

**Why.** Streamlit is a rapid-prototyping surface; React gives control over layout, information density, and a presentation appropriate to a stakeholder demo and an eventual product surface. Critically, the dashboard consumes **only** the APIs (`dashboard/client.py`, `dashboard/control_client.py`) — it never touches the compute core or the contracts — so this is a presentation-layer swap at the cleanest possible boundary: no risk to the pipeline, the storage, or the eval plane.

**What it is the natural home for.**

- The **module-library view** — surface the registry and capability descriptors so a viewer sees what is installed, what each module consumes and produces, and its tunable parameters. This makes the existing catalog visible (thesis opacity #2) and doubles as a live reference for a prospective contributor.
- **Alert evidence** — render §4.1's output: the telemetry (or flow) window with the anomalous span highlighted, the driving metric/feature, the interpretation label, and the raw records behind the alert, so the analyst does not pivot away to reconstruct it.

**Engineering notes (design considerations, not caveats to relitigate).**

- Keep the Streamlit app working until the React app reaches feature parity, so the system stays demoable throughout (golden rule #5).
- It is sequenced **after** the substance items (§4.1, §4.3) so it is built once, against a settled feature set, rather than chased against a moving one.
- It introduces a Node/React toolchain and a new container in compose — a real addition to the build surface, recorded in its ADR.

**Touches.** A new web app (e.g. `dashboard-web/`), compose topology (a new service), the two API clients' equivalents in JS. No contract or compute change.

### 4.5 Corpus enrichment

**What.** Add labelled corpora — most importantly a **flow** corpus and a **multimodal** corpus — using the synthetic generator the harness already has, and by converting additional public datasets where licensing permits.

**Why.** This closes the two gaps the harness already confesses (§2): with a labelled flow corpus the flow detector can finally be scored rather than merely fitted, and with a labelled multimodal corpus the cross-lane corroboration claim becomes a *measured* result rather than a demonstration. It also supplies the ground truth that strengthens §4.3's Isolation-Forest-vs-z-score comparison beyond SKAB. Corpora are **discovered bundles** — a manifest, gzipped readings in a real ingest format, and interval labels, dropped under `data/eval/` — so adding one is data, not code.

**Licensing constraint.** SKAB is GPLv3 and this repo carries no LICENSE, so third-party datasets are *fetched and converted locally*, never committed; synthetic corpora are committed and always work (ADR-0021). New corpora respect this split — synthetic committed, third-party fetched.

**Touches.** The `eval/` generator and `fetch` converters, `data/eval/` bundles, benchmark specs, and the eval docs. Re-running §4.3's comparison with flow / multimodal coverage is the payoff.

### 4.6 Contributor documentation and handoff guides

**What.** The documentation that makes the repo extendable after the internship: a **"port your own module" guide** covering the four ports, the contracts, the capability descriptor, and the port contract test suite an adapter must pass; a **tour of the module catalog**; and a navigable **index over the ADR trail**.

**Why.** The project's purpose is to be the spine that researchers extend via adapters and docs. The ADRs record *why* every decision was made, but a newcomer has no single *front door* that says "here is how you add a normalizer / detector / alert engine / sink, here is the contract test you must pass, here is a worked example." The pass-through normalizer (§4.2) is the natural **hello-world** (the smallest adapter), and the Isolation Forest detector (§4.3) is the worked example of a **learning** adapter (the hyperparameters-vs-fitted-state law, ADR-0022). It comes last because it documents the *settled* surface — including everything above — rather than a moving one.

**Touches.** `docs/` (the guide(s) and an index), the README, and a docs entry point. No code.

---

## 5. Sequencing and dependencies

The order is: **evidence → pass-through → Isolation Forest → React → corpus → docs.** The rationale:

| # | Item | Why here |
|---|------|----------|
| 1 | Interpretation + evidence | Highest value per unit of work, additive, and it produces the content every later demo and the dashboard depend on. Do it first so everything downstream benefits. |
| 2 | Pass-through normalizer | Small, and it becomes the hello-world in the contributor guide — so building it early means the docs' worked example already exists when §4.6 arrives. |
| 3 | Isolation Forest | The one unit of genuine detection depth; benchmarkable on SKAB immediately, so not blocked; records the first scikit-learn dependency decision. |
| 4 | React dashboard | A presentation-layer swap, deliberately after the substance items (1, 3) so it is built once against a settled feature set. It renders §4.1's evidence and surfaces the module catalog. |
| 5 | Corpus enrichment | Broadens evaluation to flow and multimodal, closing the harness's admitted gaps and strengthening §4.3's comparison beyond SKAB. |
| 6 | Documentation & guides | Last, because it documents the finished surface — the ports, the hello-world (§4.2), the learning example (§4.3), the catalog, and the eval plane (§4.5). |

**One dependency to state honestly.** The *evaluation* of the Isolation Forest (§4.3) is stronger after corpus enrichment (§4.5), yet it precedes it in the order. This is deliberate and workable: the detector can be *constructed and benchmarked on SKAB* on day one, and corpus enrichment later *broadens* the comparison (flow, multimodal) rather than *unblocking* it. If a scheduling conflict forces a choice, §4.5 can move ahead of §4.3 without disturbing anything else.

---

## 6. Out of scope (deferrals carried forward)

`week-2.md` §8 deferred several items behind explicit triggers; none of those triggers has fired, so they remain out of scope for weeks 3–4:

- **Splitting the pipeline into per-stage services** — awaits a measured bottleneck.
- **A message broker / streaming ingestion backbone** — awaits a genuinely real-time, high-volume product model.
- **Multi-tenancy, authentication, billing** — await the hosted-vs-on-prem business decision.
- **Cross-asset incident correlation** — awaits trustworthy detectors; correlating weak signals across assets manufactures false campaigns. (Within-asset, cross-lane correlation already exists.)
- **Analyst feedback loop → learned triage** — awaits real usage producing labels; the alert `status` field is kept clean so this stays additive.

The React rewrite, previously deferred in `week-2.md` §8, is **promoted into scope here** (§4.4): the feature set it would have chased is now settled, and the boundary it crosses (APIs only, no compute or contract change) makes it safe to build against that settled surface.
