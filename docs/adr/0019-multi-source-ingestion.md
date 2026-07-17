# ADR 0019: Multi-source ingestion & explicit per-source routing

Date: 2026-07-15 · Status: accepted (approved by Andreas)

## Context
Until now a run ingested **exactly one source**: `PipelineSpec` carried a single
`source_format`/`normalizer`, the runner made one `normalize()` call over one text blob, and
the CLI/API/dashboard each supplied one input. That is the wrong shape for the product's
core promise — *synthesizing detections across data streams*. It also left a capability
already built into the alert engine **dormant**: the `weighted` engine raises an alert's
priority when multiple *lanes* agree in a bucket (its `corroboration` factor), but a
single-normalizer run only ever carries one lane, so that factor was always exactly 1.

Making a run carry several inputs raises one load-bearing question — **how is each input
matched to a normalizer?** — plus a correctness question the single-source path let us dodge
(ordering when several inputs pool into one lane).

## Decision
A run ingests a **list of sources**; routing is **explicit**, and pooling happens per lane.

- **`SourceSpec {normalizer, source_format, path?}`; `PipelineSpec.sources: list[SourceSpec]`.**
  A `model_validator(mode="before")` **desugars** the legacy top-level `source_format`/
  `normalizer` into a one-element `sources` list (naming both is refused), so every config,
  spec, and test written before this ADR keeps working unchanged. `path` is a loading hint
  used only by the file-reading CLI; the API ignores it (content arrives in the request) and
  the guard ignores it (it is not part of the chain's shape).

- **Explicit routing, not sniffing (the load-bearing choice).** Each source is *tagged* with
  its normalizer/format; the normalizer double-checks and rejects a mismatch as a config
  error (unchanged port law). We deliberately rejected content-sniffing and try-each-parser:
  both reverse the existing "a blob has no intrinsic format, config asserts it" decision and
  reintroduce silent misclassification — the failure mode "capture, never drop" was built to
  prevent, and a trust-destroying one for a security product. Explicit tags also keep the
  chain **statically verifiable** before a run (the guard's whole value); sniffing would push
  format resolution to runtime. Sniffing survives only as an *optional UI suggestion*
  (extension-based pre-fill the operator confirms), never as authoritative server-side routing.

- **Pooling + an ordering guarantee.** `compute_run` normalizes each source, pools the events,
  then — for each detector — filters to its lane and delivers events **sorted by
  `(asset_id, timestamp)`** (a stable sort, so same-timestamp events keep ingest order). This
  is now a **Detector port guarantee**: windowed/lagged models (team3's z-score, future
  autoencoders) may rely on it even when several sources feed one lane. Everything downstream
  of the pool — the detector loop, the alert engine, the sinks, every contract — is unchanged.

- **The guard generalizes.** The ingest check runs per source (a mismatch is `sources[i]`, an
  ERROR); produced lanes are the **union** across sources; and a new **coverage** WARNING
  flags a produced lane no detector consumes ("you ingest logs but have no log detector") —
  the per-lane feedback the operator UI surfaces.

- **Transport.** The Pipeline API takes `inputs: list[SourceInput{content, origin}]` aligned
  1:1 with `spec.sources` (a mismatch is a 422 before any side effect); the CLI reads each
  source's `path` (or a single `--input` for a one-source config); the dashboard's compose
  page adds a source count with an upload-or-paste box per source.

## Consequences
- **A run can synthesize across modalities.** `config/demo_multimodal.yaml` ingests a
  telemetry CSV *and* a syslog stream for the same asset in one run — the first run that
  carries two lanes at once. The telemetry spike detects; the log lane is ingested but
  uncovered, which the guard reports.
- **The dormant fusion is now wired, not yet fired.** `corroboration` still reads 1 because no
  detector produces log-lane *detections* yet; a cross-lane (e.g. log-severity) detector is
  what makes it exceed 1. That detector is detection-depth work (week-2 §11.2); this ADR
  delivers the ingestion it needs.
- **Adding a lane stays free.** A future FLOW normalizer registers, declares its format→lane,
  and sources tagged that format route to it and pool automatically — no routing code changes.
- **Deferred:** per-file format auto-resolution (needs unique format→normalizer ownership);
  multipart/large-file upload transport; multiple files per dashboard source row.
