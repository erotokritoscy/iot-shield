# Design Proposal — Operator Orchestration & Deployment-Agnostic Serving

**Status:** Phase 1 shipped · Phase 2 proposed · **Date:** 2026-07-13 · **Revised:** 2026-07-15 · **Author:** Andreas Tziakouris (CYENS internship)
**Audience:** Project stakeholders + engineering · **Ratified into:** ADR-0014 (compatibility guard), ADR-0015 (Pipeline API), ADR-0016 (orchestration UI), ADR-0018 (ResultSink)

---

## 1. Summary (the one-paragraph version)

Following the architecture review, the project is taking on a second purpose: in addition to being a research harness where engineers benchmark detection modules, it should be able to serve anomaly detection as a **product** to companies operating IoT devices. This document proposes the first concrete step toward that dual goal: an **operator-facing orchestration layer** (a UI + API that lets a non-technical operator compose a detection pipeline from a menu of available modules, with a safety check that refuses invalid configurations), and a small but important architectural change — a **deployment-agnostic "result sink"** — that lets the *same* pipeline serve either an on-premise install (results stored in the customer's database) or a hosted service (results returned to the caller), **without committing to that business decision now.** Larger items raised in the review (splitting the pipeline into independently scaled services; an asynchronous streaming backbone) are deliberately deferred, with the technical triggers that would justify them stated explicitly.

**Update (2026-07-15):** the orchestration layer, compatibility guard, Pipeline API, and ResultSink described in §5–§6 are now built and merged (ADR-0014 / 0015 / 0016 / 0018) — this revision records them as delivered. It also adds **§11, a Phase 2 "product-depth" program** (multi-source ingestion, deeper detection, an evaluation harness, richer alert evidence) that strengthens the shared compute core those seams wrap.

---

## 2. Where the project stands today

The current system is best understood as **two programs joined only by a database**:

- **The write side** — a *pipeline* that takes a raw input file and runs it through four stages: **normalize → detect → correlate/prioritize → store**. Each stage is a swappable module ("adapter") selected by name in a configuration file. The whole run is a single database transaction: it either completes fully or writes nothing.
- **The read side** — a read-only **API** and a **dashboard** that query the stored results (alerts, detections, the audit trail of malformed inputs) for a human analyst.

The design principle underneath this is **ports and adapters**: the pipeline is written against *contracts* (fixed data shapes) and *ports* (fixed interfaces), so any module that honors the contract can be swapped in by changing one name in configuration — the pipeline code never changes. Three real graduate-student modules have already been integrated this way, each with zero changes to the contracts. **This proposal builds on that foundation rather than replacing it.**

*(A short glossary of the italicized terms is in Appendix A for readers who don't live in the code.)*

---

## 3. The strategic shift from the review

The review reframed the project's ambition. The original goal — a platform for researchers to plug in and evaluate detection modules — remains valid. Added to it is a **commercial goal**: package a "good enough" pipeline and sell cybersecurity anomaly detection to companies with IoT fleets, on the promise of *"give us your raw device data, we give you back prioritized, explained security insights."*

This is a genuine change of product identity, and nearly every technical idea raised in the review is a *consequence* of it. Sorting those consequences by what matters first is the purpose of §4.

A strategic question worth surfacing (see §9): a **research benchmarking tool** and a **commercial product** optimize for different things — the former wants *many* swappable modules and rich comparison; the latter wants a *few* rock-solid, supported modules plus reliability, tenant isolation, and security. The core of the system is shared by both; they diverge only at the edges. Stakeholders should decide whether the commercial track is *the same repository with a product surface* or *a productized fork*.

---

## 4. Ideas from the review, and how we sequence them

The review produced four substantive ideas. All four have merit; they differ in *timing*. The industry-standard principle we apply is **"monolith first"** — you adopt distributed-systems complexity when a measured need earns it, not on spec, because you pay its cost immediately and collect its benefit only under real load.

| Idea raised | Legitimate need underneath it | Decision | Rationale |
|---|---|---|---|
| **Operator orchestration layer + UI** | Non-technical operators must compose and run pipelines without editing code | **Build now** | Highest value; a natural front-end over machinery we already have; serves research, on-prem, and SaaS identically |
| **Serve results without holding customer data** | Data ownership differs by business model (on-prem vs hosted) | **Build the seam now, decide the model later** | The difference reduces to *where results go*; we can make that a swappable choice and defer the business decision |
| **Split each pipeline stage into its own service** | Independent scaling & fault isolation of heavy stages (e.g. detection) | **Defer** | Real benefit, but only under production load. Trigger: a *measured* bottleneck (detection is the first candidate). Until then it adds network, failure, and versioning cost for no gain, and would forfeit our single-transaction guarantee |
| **Asynchronous messaging (e.g. RabbitMQ) / streaming ingest** | Continuous, bursty, live device telemetry at volume | **Defer** | This is the correct backbone for a *real-time* product. Trigger: the product model becomes continuous/streaming (not batch) *and* volume justifies buffering + worker fan-out. We will keep the ingestion boundary clean so this slots in later without a rewrite |

The two "build now" rows are the subject of this document. The two deferrals are scoped in §8.

*Update (2026-07-15): both "build now" rows are now shipped (§5–§6); the two deferrals remain deferred (§8), where they are joined by the Phase 2 deferrals. The active work is now the Phase 2 depth program (§11).*

---

## 5. Phase 1 — the productization seams (shipped)

*All four items below are built and merged; the ADR for each is noted inline. They are recorded here as delivered — the forward-looking work is now §11.*

### 5.1 The orchestration layer + operator UI *(shipped — ADR-0016)*

A front-end (and the API behind it) where an operator **composes a pipeline by selecting modules** — which normalizer, which detector(s), which alert engine — from the catalog of installed modules, instead of hand-writing a configuration file. This is a direct, friendly surface over the existing module registry; the architecture was built for exactly this.

Design intent:
- **Non-technical-operator-friendly.** The operator picks from named, described options; the UI produces the configuration that the pipeline already understands.
- **The output is a pipeline configuration**, which the Pipeline API (§5.3) executes. Nothing about the pipeline's internals is exposed.

### 5.2 The compatibility guard (a real feature, not just validation) *(shipped — ADR-0014)*

Today, an operator *could* select an input type and a set of modules that don't fit together — e.g. feed **log** data but enable only a **telemetry** detector — and the pipeline would run happily and produce **zero results, silently.** For a researcher that is acceptable; for a paying operator it looks like a broken product.

We therefore add a **pre-flight compatibility check**. Each module declares a small **capability descriptor** — what it *consumes* and *produces* (e.g. *"syslog normalizer: syslog text → log-lane events"*, *"telemetry detector: consumes telemetry-lane events"*). Before a run, the orchestrator verifies the selected chain is actually connected end-to-end (input format → normalizer → data lane → detector). If it isn't, the run is **refused with an actionable message** ("your input is syslog; you selected only a telemetry detector — add a log detector or a syslog normalizer"), rather than executed into an empty result.

This capability metadata is a modest, bounded piece of work, and it *also* benefits the research goal: a self-describing module catalog is exactly what a researcher browsing "what can I plug in?" needs.

### 5.3 The Pipeline API *(shipped — ADR-0015)*

A service that accepts a pipeline configuration (from the orchestrator UI), runs the compatibility guard, executes the pipeline, and dispatches the results. This is distinct from the existing read-only API: the existing one **reads stored results out**; this one **takes data/config in and triggers processing.** They are complementary — the interaction models are inverted.

### 5.4 The deployment-agnostic Result Sink (the core technical change) *(shipped — ADR-0018)*

This is the mechanism that lets us proceed **before** the SaaS-vs-on-prem decision is made.

Today the destination of results is hard-wired: the pipeline always writes to the database. We make that destination a **swappable port** — a *result sink* — with two interchangeable adapters, selected exactly like every other module:

- **`DatabaseSink`** *(on-premise)* — persists the full result set to the database, as today. This is what powers the existing read-API and dashboard, running inside the customer's own environment on the customer's own database.
- **`ResponseSink`** *(hosted / SaaS)* — returns the results to the caller and persists nothing on our side (or only tenant-scoped and transient), respecting the data-ownership constraint of a hosted product.

The payoff: **the business decision the stakeholders cannot yet make becomes a decision the engineering does not need to wait on.** We build the seam; whichever model is chosen later plugs in as a one-line configuration change, not a re-architecture. The abstraction also permits *both at once* (persist *and* return) should a hosted-dashboard product ever want it.

**How it shipped (ADR-0018).** The seam landed as a **pluggable multi-sink fan-out**: a run writes to a *list* of sinks (`sinks: list[SinkConfig]`, defaulting to `[postgres]` so every existing config is unchanged). `DatabaseSink` shipped as **`PostgresSink`** — the default, and what the read API and dashboard still consume, with the persistence transaction moved *into* the sink where it belongs. The SaaS "return, persist nothing" idea generalized into the pluggable store itself: the first non-database sink is a lossless **`FileSink`** (one JSON object per run), which proves a deployment can run with **no database and no dashboard** at all; a webhook/SIEM sink is the next drop-in and appears in the catalog and compose UI automatically. The open question of *how the sink is selected* (§9-A) was resolved here too — see below.

---

## 6. The enabling refactor (done — ADR-0018)

*Done: `compute_run` (pure, store-agnostic) followed by `execute_run` (fan-out to sinks) now implement exactly the split below, and `resolve_assets` moved asset discovery in-memory. The account of the work is kept here as the design rationale.*

Making the sink swappable is not a change "at the very end" of the pipeline — because today the pipeline **braids computation and storage together**. It normalizes and immediately writes, detects and immediately writes, and — critically — reads asset records *back* from the database mid-run to build alerts. The database is currently woven *through* the computation, not merely attached to its end.

The refactor un-braids this into two clean phases:

```
COMPUTE CORE   (pure: config + input  ->  results; no database, no I/O)
     normalize -> detect -> correlate/prioritize
     produces one PipelineResult { events, malformed, detections, alerts }
                              |
                              v
RESULT SINK    (swappable: decides what happens to the PipelineResult)
     DatabaseSink  -> persist everything        (on-prem)
     ResponseSink  -> return to the caller      (SaaS)
```

Two points of note:
- The sink receives the **entire result bundle** — events, malformed records, detections, *and* alerts — not just the final alerts, because on-prem persistence and a customer's returned payload may each want different subsets.
- **The one knot to untie:** the alert stage currently depends on reading asset records back from the database (assets are auto-discovered as a side effect of storing events). A pure compute core has no database, so asset discovery moves **in-memory** (seed from configuration + discover from the events in-process). This is the single real coupling point between computation and storage, and untangling it is the substance of the refactor. It is bounded and low-risk, but it is not free, and we state it plainly.

The result is a cleaner architecture regardless of the business outcome: the pipeline's *logic* is separated from its *side effects*.

---

## 7. How one design serves all three futures

The value of this approach is that it does not fork the product prematurely. The same compute core serves every path; only the edges differ.

| | Research harness | On-premise product | Hosted (SaaS) product |
|---|---|---|---|
| Compute core (normalize→detect→alert) | **same** | **same** | **same** |
| Module selection | config / UI | operator UI | operator UI |
| Compatibility guard | useful | required | required |
| Result sink | `DatabaseSink` | `DatabaseSink` | `ResponseSink` |
| Read-API + dashboard | yes | yes (customer-hosted) | client consumes returned payload |
| Data residency | n/a | customer's own environment | minimized / tenant-isolated |

Everything in the "same" row is what we protect and reuse. Everything that varies is isolated behind a port. Phase 2 (§11) invests precisely in that "same" row — the shared compute core — so its returns accrue to all three futures at once.

---

## 8. What we are explicitly NOT doing now (and why)

Stating scope boundaries protects the timeline and prevents accidental over-building.

- **No splitting the pipeline into per-stage services.** Deferred until a *measured* bottleneck justifies it. We will keep stage boundaries clean so a single stage (detection, most likely) can later be extracted, but we build one deployable now. Splitting on spec would forfeit our single-transaction guarantee and add distributed-systems cost with no current benefit.
- **No message broker / streaming backbone (e.g. RabbitMQ).** Deferred until the product is genuinely real-time and volume justifies it. We will keep the ingestion boundary designed so a queue can be introduced without a rewrite.
- **No multi-tenancy, authentication, or billing yet.** These are required for a *hosted* product and depend on the business-model decision (§9). They are downstream of, and enabled by, the sink seam and the API built here.

The Phase 2 program (§11) carries its own deferrals, on the same "sequencing, not rejection" basis — each with an explicit trigger:

- **React dashboard rewrite.** Trigger: an approaching stakeholder demo. The dashboard consumes only the read and Pipeline APIs, so this is a skin swap that is safe at any time; it is sequenced last precisely so it isn't rebuilt against a still-moving feature set.
- **Cross-asset incident correlation** (grouping alerts across *different* devices into a single campaign). Trigger: the detectors are trustworthy. Within-asset, cross-lane correlation already exists in the alert engine; correlating *across* assets on top of weak detectors would manufacture false campaigns, so it waits on detection depth (§11.2).
- **Analyst feedback loop → learned triage** (an analyst marks alerts true/false-positive; that signal suppresses noise and, later, trains a learned prioritizer). Trigger: real usage producing labels. Deferred as premature with no users; the alert `status` field is kept clean so this stays an additive change, not a rewrite.
- **Synthetic scenario generator.** Trigger: benchmarking on a *customer's own* data, where anomalies injected by construction supply the labels. The evaluation harness itself is *not* gated on this — public labeled datasets suffice (§11.3).

---

## 9. Open decisions

**A — Technical fork: how is the result sink selected? — RESOLVED (ADR-0018).**
Decision: the **set of sinks is operator/config-chosen** (part of the pipeline spec the orchestration UI produces), while the **database location stays server-side** — `database_url`, defaulted in configuration and overridable by an environment variable at deploy time, is never a caller-supplied value. Rationale (unchanged from the original recommendation): a run's *store location* is determined by *which product was deployed* (on-prem vs hosted), an environment property, not a per-request choice; making the database a request parameter would let an arbitrary caller dictate where their data lands — a tenancy/security footgun in a hosted setting. The multi-sink list generalizes the original single-adapter recommendation: a deployment may fan one run out to several destinations (e.g. persist *and* export) while still never naming the database. A per-tenant override can be added later if genuine per-tenant policies emerge.

**B — Business decisions (need stakeholder input; block later phases, not this one):**
1. **Hosted (SaaS) vs on-premise** as the primary commercial model — determines the tenancy, data-residency, and security work that follows.
2. **One product or two tracks** — is the commercial offering the same repository with a product surface, or a separate productized fork (see §3)?
3. **Async vs synchronous serving** in a hosted model — a synchronous "submit → get results back" call is simplest but can time out on large inputs; heavy runs may later need a "submit job → poll/notify" pattern. This lives at the API layer, above the sink, and is deferrable.

---

## 10. Rough phasing

- **Phase 1 (done):** the compute/sink refactor (§6); the `PostgresSink` + `FileSink` adapters (§5.4); the Pipeline API (§5.3); module capability descriptors + the compatibility guard (§5.2); the orchestrator UI (§5.1). Delivered as ADR-0014 / 0015 / 0016 / 0018 — a self-contained, demoable slice that moved the product forward *without* requiring any of the deferred business decisions.
- **Phase 2 (now — this revision's new subject): product depth** — see §11: multi-source / mixed-modality ingestion, deeper detection, an evaluation harness, and richer alert evidence. It strengthens the shared compute core that the Phase 1 seams wrap, so it pays off across all three futures (§7).
- **Post-decision:** whichever of tenancy / auth / data-residency the chosen business model requires; hardening the deployment images.
- **Later (load- or feature-gated):** extract detection into its own scalable service; introduce an asynchronous streaming ingestion backbone; and the Phase 2 deferrals in §8 (cross-asset incidents, feedback loop, React) — each behind its stated trigger.

---

## 11. Phase 2 — the product-depth program

Phase 1 built the *seams* that make the pipeline a product: compose modules, guard the chain, choose where results go. Phase 2 deepens the **core those seams wrap** — the detection itself and the data it runs on. The organizing judgment: for a detection product with no users yet, the binding risk is not a thin feature set *around* the pipeline but that the **detection itself is not yet deep or trustworthy enough to be worth selling.** Two facts in the current system set the order of work:

1. **The cross-lane fusion the alert engine was built for has never actually run.** The alert engine already correlates detections by `(asset, time-window)` and *raises an alert's priority when multiple data lanes agree* (its "corroboration" factor). But every run today uses a single normalizer, so a run carries a single data lane — and that factor is therefore always exactly 1. The fusion machinery is built and idle for want of multi-lane input.
2. **The detection lane currently offers one deliberately simple detector** (a z-score). Depth here — more detectors, better detectors, and a way to *measure* them — is the main gap between a demo and a product.

One constraint follows from these and fixes the sequence: **correlation quality is bounded by detection quality.** Fusing or correlating weak signals yields confident noise, so detector depth must precede any higher-order correlation. And, consistent with §7, every item below strengthens the *shared* core — it accrues to the research harness, the on-prem product, and the hosted product alike.

The work, in order:

**11.1 · Multi-source / mixed-modality ingestion** (with file upload) — *shipped (ADR-0019)*
A single run now accepts mixed inputs — e.g. a telemetry CSV alongside syslog — routing each input to its normalizer by explicit format tag (never by sniffing the content, which would reintroduce silent misclassification) and pooling the results into the detector lanes. The Pipeline API takes one input per source, the CLI reads a path per source, and the operator page composes a run from an upload-or-paste box per source; legacy single-source configs keep working unchanged. This is the enabler the rest of Phase 2 builds on, and it is what finally feeds the alert engine two lanes at once (fact 1). One honest note: the engine's corroboration factor still reads 1, because it counts agreeing *detection* lanes and no detector yet produces log-lane detections — a cross-lane detector (11.2) is what makes it exceed 1. This item delivers the ingestion that detector will need.

**11.2 · Detection depth**
Move from one naive detector to a credible, benchmarked menu per lane — Isolation Forest first, then an autoencoder (LSTM/CNN) once the deep-learning case earns its heavier runtime. Each detector is its own ADR; ADR-0012 anticipated exactly this, having frozen the runtime at numpy-only with scikit-learn / torch deferred to per-model decisions. This is where the product's substance lives.

**11.3 · Evaluation / benchmarking harness** — *shipped (ADR-0021)*
Replays labeled data and reports precision, recall, F1, and latency per module, head-to-head — the provability a security buyer and a researcher both need, and the answer to the question 11.2 raises: *which detector do we ship?* It runs on public labeled datasets already on hand (SKAB), so it was never blocked on the labeled-data problem.

What shipped is a **third plane** alongside Monitor and Operate: those *use* the pipeline, this *judges* it. That distinction is not stylistic — evaluation needs ground truth, which production data by definition never has, so it cannot run on a customer's live feed at all. It is therefore a research instrument, off by default in the dashboard and structurally unable to write to a customer's store.

Three results are worth recording, because each corrects something we previously assumed:

- **Scoring happens at two levels, and only the pair is informative.** *Did the detector find the fault* and *did the alert queue stay sane* are different jobs — the alert engine is a compressor, so a detector that fragments badly can still produce few alerts. Measured: one naive detector turns a single fault into ~1135 detections, which the alert layer compresses to ~5 alerts. Its per-row score looks respectable (F1 0.52) while its per-incident precision does not (0.35), which is exactly the gap between what a paper reports and what an analyst experiences.
- **Training a detector on each recording's own clean history made it *worse*, not better.** Recall fell from 1.00 to 0.81, because 3 of 16 recordings have an unstable baseline period that inflates the learned threshold until the fault becomes unreachable. The honest conclusion is that per-asset fitting is *high-variance* and needs a guard against a pathological baseline — a requirement we would not have known to state without measuring it.
- **We deliberately do not measure two things**, and say so rather than let a scoreboard imply otherwise. Whether an alert's *priority number* is correct has no answer key (a corpus records that a fault happened, never how much it mattered) — so we measure whether the ranking separates true from false, never whether it is calibrated. And our headline cross-modal claim cannot be benchmarked on SKAB, which is telemetry-only: proving that cross-lane agreement improves the queue needs a labeled *multimodal* corpus, which does not exist yet. Until then the multimodal demo remains a demonstration, not a benchmark.

Adding a labeled dataset is a registry entry, and making a detector trainable is one optional method — deliberately the same gestures as adding an adapter, so the harness extends the way the rest of the system does.

**11.4 · Dashboard evidence**
Link an alert directly to its evidence — the telemetry window that fired it (with the anomalous span highlighted), the raw records behind it, and log-lane evidence — rather than requiring the analyst to pivot to a separate asset view. This is trust made visible, and it bundles with the next dashboard work; it is a companion to, and independent of, the React rewrite deferred in §8.

The Phase 2 deferrals — cross-asset incident correlation, the analyst feedback loop, the synthetic generator, and the React rewrite — are recorded with their triggers in §8.

---

## Appendix A — Glossary

- **Contract** — a fixed, shared data shape (e.g. an "alert") that every module agrees to speak, so modules that never met can be chained together.
- **Port** — a fixed interface ("what a normalizer must be able to do"), independent of any specific implementation.
- **Adapter** — a concrete module that fulfills a port (e.g. a specific normalizer). Adapters are swapped by name in configuration.
- **Result sink** — the new port introduced here: the interchangeable destination for a pipeline's results (a database, or a response to the caller).
- **Orchestration layer** — the operator-facing surface for choosing which modules run and validating that the choice is coherent before running.
- **On-premise vs hosted (SaaS)** — whether the customer runs the software in their own environment (their data never leaves) or sends data to a service we host.
