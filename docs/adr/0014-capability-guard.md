# ADR 0014: Capability descriptors + the compatibility guard

Date: 2026-07-13 · Status: accepted (approved by Andreas)

## Context
The productization pivot (`docs/design/week-2.md`) turns the pipeline into something
an operator *composes* — picking a normalizer, detectors, and an alert engine from a
menu. A composed chain can be silently incoherent: pick the `syslog` normalizer (which
emits **log**-lane events) with a telemetry-only detector, and the run "succeeds" while
producing nothing. We need a pre-flight check that catches this *before* a run, and the
same knowledge must drive the orchestration UI's menus. This is the first slice of the
"build now" work; it is pure library work with no API/UI yet, so it lands on its own.

The facts needed for the check already exist in the code, but implicitly and scattered:
a normalizer's accepted format is a module constant (`stub.SOURCE_FORMAT`), the lane it
emits is stamped per-event deep in the parser, a detector's lane is an instance property
(`detector.lane`). None of them is readable without either grepping or *constructing* the
adapter — and construction can be expensive (team3 loads an artifact; a future torch model
would load weights), which rules out reading them at menu-render or pre-flight time.

## Decision
Model the pipeline as a chain of **typed connectors** and lift those scattered facts into
one declarable shape.

- **A `Capability` is `consumes`/`produces` over a tiny vocabulary of typed tokens**
  (`capabilities.py`, a package-root module depending only on `contracts`): `FormatToken` at the
  ingest joint, `LaneToken` at the normalize→detect joint, and the pass-through `DETECTIONS`/
  `ALERTS` signals. Tokens are typed (not bare strings) so the type checker catches a mismatched
  axis or a mistyped lane; each renders to a stable wire form (`format:<name>` / `lane:<name>` /
  `detections` / `alerts`) only at the `/modules` boundary. Each adapter declares its capability
  **co-located with its own code** (beside `SOURCE_FORMAT`, etc.). A detector whose lane is a
  config param declares a `capability(params)` function; the rest declare a constant.

- **Capabilities live in the registry as static metadata, resolvable from `(name, params)`
  without constructing the adapter.** A registry entry becomes an `AdapterSpec(factory,
  capability)` — one entry per adapter, so an adapter *cannot* be registered without declaring
  its shape. This is what lets the guard and the (future) `/modules` discovery endpoint read a
  stage's shape cheaply.

- **The guard (`pipeline/compat.py`) walks the known topology** — source → normalizer →
  detectors → alert engine — comparing each downstream `consumes` against the upstream
  `produces`, and returns a list of structured `CompatIssue`s (empty = clean). Two severities,
  split by the actual failure mode:
  - **ERROR (blocks):** the chosen `source_format` is not one the normalizer accepts (it would
    `raise ValueError` at runtime — a real crash), or a name/param cannot be resolved.
  - **WARNING (informs, allows):** a detector whose lane no normalizer produces will *idle*.
    This is legitimate — the syslog demo showcases normalization with zero detections by design
    (ADR-0011) — and **guaranteed-safe by the Detector port's foreign-lane law** (a detector
    must ignore foreign-lane events). Blocking it would refuse a valid, shipped config, so we
    surface it instead. (This corrected an earlier "dead-detector = hard block" proposal once
    the shipped `demo_syslog.yaml` + its `(12,3,0,0)` test proved zero-detections is a real,
    intended outcome.)

- **One function, three consumers.** `run_pipeline`/the CLI raise on any ERROR and carry
  WARNINGs into the `RunReport` (rendered, so the operator is told); the Pipeline API (ADR-0015,
  next) will return 422 on ERRORs and pass WARNINGs back; the UI (ADR-0016) will show both.

- **Zero contract changes.** `Capability`, `AdapterSpec`, `CompatIssue` are new, non-contract
  types. The five data contracts are untouched.

## Consequences
- **Adding an adapter now means declaring its shape**, enforced like the port suites: a
  meta-test (`tests/pipeline/test_capabilities.py`) asserts every registered adapter declares a
  non-empty capability, and **cross-checks the declaration against real behaviour** — declared
  format equals `SOURCE_FORMAT`, declared lane equals a built instance's `.lane`, and a live
  normalize actually emits the declared lane. So the guard never reads a stale lie.
- The guard is defence-in-depth alongside the existing layers, with no overlap: it checks
  *chain coherence*; the registry's `_build` still checks *param correctness* (unknown/bad
  params fail fast at construction); the contracts still check *field validity*.
- The runner now refuses an incompatible config before touching the database, and the syslog
  demo's report carries a visible "will not fire" warning — the intended UX made real.
- The capability vocabulary is the extension seam: a new modality lane, or a future
  lane-specific alert engine, is a new token + one join rule, not a rewrite of the guard.
