# Design Proposal — Weeks 3–4: Legibility, Depth, and Handoff

**Status:** Proposed (revised) · **Date:** 2026-07-24 · **Author:** Andreas Tziakouris (CYENS internship)
**Audience:** Project stakeholders + engineering · **Builds on:** `week-2.md` (Phase 1 seams + Phase 2 depth)
**Supersedes:** the 2026-07-22 proposal of the same name · **ADR head at time of writing:** 0024

> **What changed in this revision.** Since the 07-22 draft, two teammate modules became
> inbound for integration before handoff — Antonia's detection rules + threat enrichment,
> and Panayiotis's meter anomaly detector (an Isolation Forest). They entangle exactly two
> of the six work items — **interpretation + evidence enrichment (§5.1)** and **the
> Isolation Forest detector (§5.3)** — and the original execution order front-loaded those
> two. This revision adds §3 (the integration constraint) and replaces the old flat
> sequence with a three-bucket order (§6) that lets work proceed while the teammate changes
> are still pending, without idling and without building anything twice.

---

## 1. Summary (the one-paragraph version)

The system has **one of everything**: three input modalities (telemetry, logs, network
flow) each normalized *and* judged, an alert engine that correlates across lanes, a
pluggable result sink, an orchestration UI, a Pipeline API, and an evaluation harness that
scores detectors against labelled data. The architecture is complete; what it lacks is not
capability but **legibility** — the system does the work but does not yet *explain* it,
does not *show* the modules it already has, is not as *easy to feed* as it should be, and
has no *front door* for the researcher who will extend it. Weeks 3–4 close that gap, add
one unit of real detection depth, and **integrate two teammate modules** so the repo hands
off as a genuinely multi-contributor spine. There are six work items — **(1) interpretation
+ evidence enrichment, (2) a pass-through normalizer, (3) an Isolation Forest detector,
(4) a React dashboard, (5) corpus enrichment, (6) contributor documentation** — plus the
teammate integrations. Two of the six are entangled with the pending teammate work; the
key realization of this revision is that we are blocked on their *code*, not on the
*integration design*, so the design that resolves the entanglement can and should be done
now. §6 sequences everything into three buckets accordingly.

---

## 2. Where the project stands

The pipeline is four swappable ports — **normalize → detect → correlate/prioritize → sink**
— written against fixed contracts, so any module honoring a contract slots in by name
(`week-2.md` §2, §7). As of the flow lane (ADR-0023 / ADR-0024) every modality the product
claims is both ingested and detected, and a single run carrying telemetry + syslog + flow
for one asset drives the alert engine's cross-lane corroboration factor to its ceiling. The
evaluation harness (ADR-0021) is a third plane that *judges* the pipeline rather than using
it, reporting precision/recall/F1 per detector against labelled corpora.

Two honest gaps are already recorded and carry into this plan:

- The alert and detection contracts reserve fields for **explanation** —
  `DetectionResult.event_kind`, `DetectionResult.evidence`, `PrioritizedAlert.threat_class`
  / `recommended_action`, and a `rationale` that is currently thin — and most of them are
  populated sparsely or not at all. The product's promise is *"prioritized, **explained**
  alerts,"* and the "explained" half is under-built.
- The flow detector "ships fitted to no benchmark" and the cross-lane corroboration claim
  "remains a demonstration, not a benchmark," because there is no labelled **flow** corpus
  and no labelled **multimodal** corpus (ADR-0021 / `week-2.md` §11.3).

**New since 07-22:** two teammate modules are inbound for integration (see §3), which is
why this revision exists.

---

## 3. The integration constraint (new)

Two teammates' modules are to be integrated before the internship ends, so the repo hands
off as a multi-contributor spine rather than a solo one. Both integrations are already
specified in `docs/integration/` (one letter per teammate, stating what they change on
their side and what we build on ours). What matters for *this* plan is where they touch the
six work items.

### 3.1 What each module contributes

- **Antonia — detection rules + threat enrichment**
  (`docs/integration/rule-detector-and-enrichment-plan.md`). Two parts are adopted: (A) her
  Sigma-style YAML rules become a new **rule-based (single-event-signature) detector** —
  a gap we do not currently fill; and (B) her **MITRE ATT&CK mapping + suggested actions**
  become the **explanation attached to an alert**, filling `threat_class` /
  `recommended_action`. We keep our ranking engine (her correlation layer drops
  non-correlating findings; our engine never drops, only ranks).
- **Panayiotis — meter anomaly detector**
  (`docs/integration/ml-detector-and-eoa-metering-plan.md`). His unsupervised **Isolation
  Forest** over meter features (hour-of-week profile, rolling stats, seasonal residual)
  becomes **one shared meter detector fed by one normalizer per source** (EOA, SensiPro).
  His `attribution.py` (model-agnostic feature attribution) becomes the **explanation on
  every meter detection**. His integration also **brings scikit-learn into the runtime**
  and adds the labelled **LEAD** corpus.

### 3.2 The two entanglements, precisely

The reason two of our six items are entangled is structural, not incidental: **the 07-22
plan assumed we would *build* enrichment and a second Isolation Forest from team3 fragments;
the teammate modules mean we will *receive* better-built, already-evaluated versions of
both.** The design shifts from "build from scratch" to "design the seam so their modules
slot in; team3 is the interim reference."

- **Enrichment (§5.1) is three-way.** The explanation surface has *three* producers, not
  one. The `evidence` field (`dict[str, Any]`, deliberately not yet frozen) must carry
  team3's z-score channel attribution (here now), Panayiotis's meter feature attribution
  (pending), and Antonia's rule-match evidence (pending). And `threat_class` /
  `recommended_action` have two competing sources: our **heuristic** guess from anomaly
  shape (team3's `interpretation.py`) versus Antonia's **authored** MITRE + actions. These
  are not mutually exclusive — they partition by detector type: a *rule* detection *knows*
  its threat authoritatively; an *unsupervised* detection can only *guess* from shape. The
  conflict is that §5.1, if built first against team3 alone, designs no **precedence** for
  an authoritative source, and freezes an evidence schema that may not generalize.
- **Isolation Forest (§5.3) shares machinery with Panayiotis's.** They are *not* the same
  detector — different domain (OT telemetry / SKAB vs metering / LEAD), features
  (rolling·lag·FFT vs hour-of-week·seasonal), and corpus. But they share *everything else*:
  the scikit-learn onboarding (both claim to be "the first scikit-learn dependency
  decision" — only one ADR can be), the `Fittable`/artifact/sweep scaffolding (ADR-0022),
  and the attribution-into-evidence path. Building ours from team3 fragments *before*
  Panayiotis's lands duplicates the exact onboarding his integration also does.

### 3.3 The key unlock

Both integration letters **already specify the shapes** we need (Antonia's enrichment is
`{tactic, technique, technique_id}` + a suggested-actions list; Panayiotis's is per-feature
attribution standardized against the meter's own history). So the *design* that reconciles
the entanglement — an evidence-schema ADR with a precedence rule, and a scikit-learn
runtime ADR — **is not blocked**. Only the vendoring of their actual code/rules is. This is
what makes the three-bucket order in §6 possible: we do the integration *design* now, so
that when the pending changes land, integration is mechanical.

---

## 4. The organizing thesis: legibility over capability

With the architecture complete, the marginal value of the next two weeks is highest where
the system is *opaque*, not where it is *thin*. Four opacities:

- **An alert is a number, not an explanation.** An analyst is handed a priority score and a
  one-line rationale, but not *what* fired it, *where* on the timeline, or *what it probably
  means*.
- **The module catalog is invisible.** The registry, the capability descriptors, and the
  compatibility guard all exist, but nothing *presents* them, so "what can this system do /
  what can I plug in?" has no visible answer.
- **The front door for clean data is narrow.** Every source must pass through a
  format-specific parser, even a source that already arrives structured.
- **There is no front door for a contributor.** The ADR trail explains *why* every decision
  was made, but there is no single guide that says *"here is how you add a module and the
  test it must pass."*

Every item in §5 removes one of these opacities, or adds the one piece of genuine depth
(a second, complementary detector) whose value the harness can now measure. Nothing here
changes a contract or the compute core; the work is additive by construction.

A note on provenance that governs the reference-derived items: reference modules under
`../reference/` are **read-only** (golden rule #3); their *ideas* are re-implemented against
our contracts, not imported, with provenance noted in the adapter docstring. Teammate
modules are vendored (a snapshot with provenance, re-synced on their improvements), the same
way team3 already is.

---

## 5. The work, explained

Each item notes its **teammate entanglement** and the **bucket** (§6) it lands in.

### 5.1 Interpretation + evidence enrichment · *entangled (Antonia + Panayiotis) · Buckets A+B+C*

**What.** Fill the explanation fields the contracts already reserve. Two layers:

- *Evidence* — for each detection, record **which signal drove it**: the top contributing
  metric/feature/channel, its value and score at the window's peak, and the window bounds.
  This is the attribution an analyst needs to trust or dismiss an alert.
- *Interpretation* — a heuristic operational label derived from the **shape** of the
  anomaly (a single-window spike reads as a short transient; a short contextual deviation as
  mode-specific behavior; a long collective span running to the horizon as drift or a
  setpoint shift; a sustained collective span as degradation or a lasting fault), carried
  into `event_kind` / `threat_class` and a human-readable `rationale`.

**Why (first principles).** The alert is the product. Today it says "asset 7, score 76";
enriched, it says "sustained pressure fault on the camera, corroborated across telemetry and
flow, driven by the pressure channel's rolling z-score of 8.2 over this 40-second window."
The contracts were designed for exactly this — the fields exist and sit empty — so this is
high value per unit of work and requires no contract change.

**A design principle this settles.** A detector's *feature representation is private to that
detector*: the z-score path engineers rolling/lag/FFT features, the flow detector counts
distinct peers and ports, an autoencoder consumes raw scaled windows. There is deliberately
**no shared feature-extraction stage** — it would be a lowest-common-denominator or a fifth
port every detector ignores. What crosses the port boundary is not the raw feature vector
but its **human-readable attribution**, emitted as `DetectionResult.evidence`. Feature-level
insight is a *detector output*, not a *pipeline stage*.

**The entanglement (see §3.2).** Three producers feed `evidence` (team3 z-score, Panayiotis
meter attribution, Antonia rule-match), and two sources feed `threat_class` /
`recommended_action` (our heuristic shape-label vs Antonia's authored MITRE + actions). The
resolution is a single ADR (§7) that (a) defines the evidence schema against **all three**
producers, and (b) defines the **precedence rule**: authored enrichment (a rule's own MITRE
+ actions) outranks the heuristic; the heuristic is the fallback for unsupervised detectors
that have no authored label. Designing this precedence up front is what stops §5.1 being
reworked when Antonia's module lands.

**What proceeds now vs later.** *Now (Bucket A):* populate `evidence` + `event_kind` for the
detectors that already exist (team3 z-score, flow_burst, log_burst) and build the
shape-based interpretation heuristic (needed for unsupervised detectors regardless of any
teammate). *Now (Bucket B):* the evidence-schema + precedence ADR, validated on paper
against the teammate shapes, frozen when their code pins them. *Later (Bucket C):* wire
Antonia's authored enrichment and Panayiotis's attribution through the frozen schema.

**Source material.** team3's `evaluation/interpretation.py` (window-shape → operational
label + rationale) and `evaluation/explanations.py` (top-feature/top-channel attribution)
are the shape to re-implement. Interpretation labels are heuristic and must be marked as such
(an explicit `is_heuristic` flag) so the dashboard never presents a guess as a diagnosis.

### 5.2 Pass-through normalizer · *independent · Bucket A*

**What.** A normalizer adapter that accepts input already in the normalized shape (JSONL
matching `NormalizedEvent`), **validates** each record against the contract, and
**quarantines** anything that fails as a `MalformedRecord` — rather than parsing a raw wire
format.

**Why.** Not every source needs parsing; some arrive already structured. The correct way to
accept such data is a new adapter *behind the existing port*, not a bypass of the stage. The
trust boundary is preserved exactly: every record is still validated (timezone-aware UTC
timestamps per golden rule #4, finite values, required fields) and malformed input is still
captured, never dropped (ADR-0004). "Normalization" for this source is
validation-and-enveloping instead of parsing — the four-port model is untouched, and the
pipeline still always normalizes.

**Bonus.** It is the simplest possible normalizer, which makes it the natural
**hello-world** in the contributor guide (§5.6): the least code a working adapter can be.

**Touches.** `normalize/` (new adapter, capability descriptor), the normalizer port contract
test suite, a demo config + a tiny demo input, and the README / CLAUDE map. Small. Fully
independent of the teammate work, so it is the first thing built.

### 5.3 Isolation Forest detector · *entangled (Panayiotis) · Buckets B+C*

**What.** A second telemetry detector — an unsupervised Isolation Forest — as a credible
complement to the robust z-score, benchmarked head-to-head through the harness on SKAB.

**Why.** The z-score flags **single-feature spikes**; an Isolation Forest flags **unusual
combinations** of engineered features (it is multivariate). They are complementary, and the
harness lets us *show* the difference rather than assert it — the answer to "which detector
do we ship, and for what?" (ADR-0012 froze the runtime at numpy-only with scikit-learn
deferred to a per-model ADR — this is that ADR).

**Honest constraints, stated plainly.**

- It pulls **scikit-learn** into the runtime for the first time — a deliberate step,
  recorded in an ADR with the image-size cost stated. **This is the same scikit-learn
  decision Panayiotis's meter detector needs (§3.2).** One ADR (§7), two users; whoever's
  code lands first is the first user.
- It is **unsupervised** (needs no labels), the right fit for unlabelled customer data — as
  opposed to a supervised model that would need labels the deployment does not have. This is
  a fit-for-purpose choice, not a sophistication ceiling.
- It must be **judged, not assumed better** — benchmarkable on SKAB (telemetry) today, so it
  is not blocked; corpus enrichment (§5.5) then broadens the comparison.
- It **learns**, so it obeys the hyperparameters-vs-fitted-state law (ADR-0022): its
  hyperparameters are exactly its factory parameters, it implements `fit()` and an artifact,
  and it is sweepable. team3's Isolation Forest fragments and the engineered-feature path
  (`team3/_features.py`) are the ideas to re-implement.

**The entanglement (see §3.2), and the decision it raises.** Our SKAB telemetry IForest and
Panayiotis's meter IForest share the scikit-learn onboarding and the `Fittable`/artifact
scaffolding. So the scaffolding is built **once** in Bucket B (with the shared ADR), and
both detectors are its users. This leaves one genuine decision (§8): **keep our SKAB
telemetry IForest** — its only unique value is the head-to-head-vs-z-score demo on the *same*
telemetry corpus, which Panayiotis's cannot provide — **or drop it and let his be the
canonical IForest.** Recommendation: keep it, built on the shared scaffolding, because the
scaffolding cost is already paid for Panayiotis's and the SKAB comparison is a real demo.

**Touches.** `detect/` (new adapter, params, artifact, train entry point), the runtime
dependency set (scikit-learn), the shared scaffolding + ADR, the detector port suite plus the
`Fittable` behavioral test, and a benchmark spec + demo.

### 5.4 React dashboard · *soft dependency (renders §5.1; lists teammate adapters) · Buckets A+C*

**What.** Replace the Streamlit dashboard with a React single-page app over the *same* read
and Pipeline APIs — the same three planes (Monitor / Operate / Evaluate), plus a
**module-library view** and the **evidence rendering** from §5.1.

**Why.** Streamlit is a rapid-prototyping surface; React gives control over layout,
information density, and a presentation appropriate to a stakeholder demo and an eventual
product surface. Critically, the dashboard consumes **only** the APIs
(`dashboard/client.py`, `dashboard/control_client.py`) — never the compute core or the
contracts — so this is a presentation-layer swap at the cleanest possible boundary.

**What it is the natural home for.**

- The **module-library view** — surface the registry and capability descriptors so a viewer
  sees what is installed, what each module consumes/produces, and its tunable parameters.
  This makes the catalog visible (thesis opacity #2) and doubles as a live contributor
  reference. It will list the teammate adapters once they land.
- **Alert evidence** — render §5.1's output: the telemetry (or flow) window with the
  anomalous span highlighted, the driving metric/feature, the interpretation label, and the
  raw records behind the alert.

**The dependency.** The **shell** (Monitor/Operate/Evaluate + module-library view over the
existing registry) is independent and can be built in Bucket A. The **evidence-rendering
view** consumes §5.1's frozen schema, so it lands in Bucket C after the schema settles.

**Engineering notes.** Keep the Streamlit app working until React reaches parity (golden rule
#5). It introduces a Node/React toolchain and a new compose service — recorded in its ADR.

**Touches.** A new web app (e.g. `dashboard-web/`), compose topology, the two API clients'
JS equivalents. No contract or compute change.

### 5.5 Corpus enrichment · *overlaps Panayiotis's LEAD · Buckets A+C* — **Bucket A SHIPPED (ADR-0026)**

> **Status 2026-07-27.** The Bucket-A half is done, and it was materially larger than this
> section assumed: not "add labelled corpora" but a breaking change to what an answer key
> *means*. Truth is now per asset and scoring matches on (asset, time), because a fleet corpus
> judged on time alone scores an innocent device's false alarm as a correct catch. The generator
> was rebuilt as a world plus two ports (`Behaviour`, `Renderer`) so a lane, a dialect, a fault
> or a corpus is each a single addition. `fleet_scan` and `device_trimodal` ship committed.
>
> Both gaps this section names are closed, and both measurements went somewhere useful: the
> flow lane's shipped 20-peer threshold is **not** the best operating point on labelled data
> (27 holds recall at precision 1.00), and corroboration is now a benchmark — true-alert
> priority 74.95 → 86.74 over byte-identical evidence, 1 → 3 backing lanes. LEAD remains
> Bucket C. See ADR-0026 for the full result set and its caveats.

**What.** Add labelled corpora — most importantly a **flow** corpus and a **multimodal**
corpus — using the synthetic generator the harness already has, and by converting additional
public datasets where licensing permits. Panayiotis's **LEAD** metering corpus is part of
this scope.

**Why.** This closes the two gaps the harness confesses (§2): a labelled flow corpus lets the
flow detector be *scored* not merely fitted, and a labelled multimodal corpus turns the
cross-lane corroboration claim into a *measured* result. It also strengthens §5.3's
IForest-vs-z-score comparison beyond SKAB, and LEAD gives Panayiotis's detector a real
precision/recall number. Corpora are **discovered bundles** dropped under `data/eval/` — so
adding one is data, not code.

**Licensing constraint.** SKAB is GPLv3 and this repo carries no LICENSE, so third-party
datasets are *fetched and converted locally*, never committed; synthetic corpora are
committed and always work (ADR-0021). New corpora respect this split.

**What proceeds now vs later.** *Now (Bucket A):* the synthetic flow + multimodal corpora,
which depend on nothing external. *Later (Bucket C):* LEAD conversion, which arrives with
Panayiotis's module.

**Touches.** The `eval/` generator and `fetch` converters, `data/eval/` bundles, benchmark
specs, and the eval docs.

### 5.6 Contributor documentation and handoff guides · *documents the final surface · Bucket C*

**What.** The documentation that makes the repo extendable after the internship: a **"port
your own module" guide** covering the four ports, the contracts, the capability descriptor,
and the port contract test suite; a **tour of the module catalog**; and a navigable **index
over the ADR trail**.

**Why.** The project's purpose is to be the spine that researchers extend via adapters and
docs. The ADRs record *why* every decision was made, but a newcomer has no single *front
door*. The worked examples are strongest when they are the real, final adapters: the
pass-through normalizer (§5.2) is the **hello-world** (smallest adapter); the Isolation
Forest (§5.3) is the **learning** example (the ADR-0022 law); **Antonia's rule detector** is
the **config-only** example (a new detection is a YAML file, no code); **Panayiotis's meter
detector** is the **vendored-detector-plus-normalizer-per-source** example. It comes last
because it documents the *settled* surface, including the teammate adapters.

**Touches.** `docs/` (the guide(s) and an index), the README, a docs entry point. No code.

---

## 6. Sequencing: three buckets

The 07-22 order (evidence → pass-through → IForest → React → corpus → docs) front-loaded the
two most entangled items (§5.1, §5.3) and deferred the independent ones — backwards for a
reality in which the teammate changes are still pending. The revised order partitions the
work by *what it depends on*, so nothing idles and nothing is built twice.

| Bucket | Principle | Items |
|---|---|---|
| **A — do now (teammate-independent)** | Real, non-throwaway progress that needs nothing from the teammates. | §5.2 pass-through normalizer · §5.5 synthetic flow + multimodal corpora · §5.1 groundwork (evidence for existing detectors + the shape heuristic) · §5.4 React shell + module-library view |
| **B — design the seam now (freeze on landing)** | The integration *design* — not blocked (§3.3). Turns Bucket C into mechanical work. | The **evidence-schema + precedence ADR** (§7) · the **scikit-learn runtime ADR + shared `Fittable`/artifact scaffolding** (§7) |
| **C — vendor when they land (mechanical if B is done)** | Blocked on the teammates' pending code/rules. | Vendor Antonia's rules → rule detector; wire her enrichment through the precedence path · Vendor Panayiotis's detector → EOA/SensiPro normalizers; attribution through the schema; convert LEAD · §5.3 our SKAB IForest on the shared scaffolding (or fold in — see §8) · §5.4 evidence-rendering view · §5.6 contributor docs |

**Within Bucket A**, the order is: pass-through normalizer first (smallest, and the
hello-world the docs will need), then the §5.1 groundwork and synthetic corpora (which the
IForest comparison and the dashboard will use), then the React shell (built against the
settled existing surface).

**The one dependency to state honestly.** §5.3's *evaluation* is stronger after corpus
enrichment (§5.5), yet the detector can be constructed and benchmarked on SKAB immediately,
so it is not blocked; corpus enrichment *broadens* the comparison rather than *unblocking*
it.

---

## 7. The two design artifacts pulled forward (Bucket B)

These are the artifacts that dissolve the two entanglements. They are authored now because
their inputs (the teammate shapes) are already specified in `docs/integration/`.

> **Numbering correction (2026-07-27).** The provisional numbers below were claimed by work
> that landed first: **0025** is the pass-through normalizer (§5.2) and **0026** is multi-asset
> ground truth + the corpus generator (§5.5). So the two records here are **0027** (evidence
> schema) and **0028** (scikit-learn). Numbers are first-come; the *content* of both is
> unchanged.

- **ADR-0027 (proposed) — the evidence schema + enrichment precedence.** Defines the keys
  every detector agrees to emit in `DetectionResult.evidence`, designed against all three
  producers (team3 z-score channel attribution, Panayiotis meter feature attribution,
  Antonia rule-match evidence). Defines the precedence for `threat_class` /
  `recommended_action`: **authored rule enrichment > heuristic shape label**; the heuristic
  is the fallback for unsupervised detectors. Frozen once the teammate shapes are pinned by
  their landed code. (Number provisional; assigned when written.)
- **ADR-0028 (proposed) — scikit-learn in the runtime.** Records the first scikit-learn
  runtime dependency with its image-size cost, names **both** Isolation Forests as its users,
  and establishes the shared `Fittable`/artifact/attribution scaffolding both reuse. Whoever's
  code lands first is the first user; the other references it. (Number provisional.)

Numbering note: the pass-through normalizer (§5.2), built first in Bucket A, is a routine
adapter behind an existing port; whether it warrants its own ADR is a small open question
carried into its implementation plan (it establishes the "produced-lane-as-a-param"
precedent, which argues for a short ADR for consistency with the repo's per-adapter ADR
discipline).

---

## 8. Decision points

- **§5.3 — keep or fold the SKAB telemetry IForest.** Once Panayiotis's meter IForest is
  in, our from-team3 telemetry IForest's only unique value is the head-to-head-vs-z-score
  demo on the *same* SKAB telemetry corpus. **Recommendation: keep it**, built on Bucket B's
  shared scaffolding (the scaffolding cost is already paid for his detector, and the SKAB
  comparison is a genuine demo the meter detector cannot provide). The alternative — fold it
  in and let his be the canonical IForest — is viable if scope tightens.
- **§7 — ADR ownership of scikit-learn.** Resolved by authoring one ADR with two users; the
  only real choice is which code lands first, which follows the Bucket order (ours, on the
  shared scaffolding, precedes the vendoring of his).

---

## 9. Out of scope (deferrals carried forward)

`week-2.md` §8 deferred several items behind explicit triggers; none has fired:

- **Splitting the pipeline into per-stage services** — awaits a measured bottleneck.
- **A message broker / streaming ingestion backbone** — awaits a real-time, high-volume
  product model.
- **Multi-tenancy, authentication, billing** — await the hosted-vs-on-prem business
  decision.
- **Cross-asset incident correlation** — awaits trustworthy detectors; correlating weak
  signals across assets manufactures false campaigns. (Within-asset, cross-lane correlation
  already exists.)
- **Analyst feedback loop → learned triage** — awaits real usage producing labels; the alert
  `status` field is kept clean so this stays additive.

The React rewrite, previously deferred in `week-2.md` §8, is **promoted into scope** (§5.4):
the feature set it would have chased is now settled, and the boundary it crosses (APIs only)
makes it safe to build against that settled surface.
