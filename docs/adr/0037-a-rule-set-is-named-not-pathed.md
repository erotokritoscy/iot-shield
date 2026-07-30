# ADR-0037: A rule set is named, not pathed

- **Status:** accepted
- **Date:** 2026-07-30
- **Amends:** ADR-0032 D2 (rules load from "a directory named by config"), ADR-0035 D1 (the
  discovered-vocabulary channel, which becomes lazy and general)
- **Related:** ADR-0018 §9-A (a request never names a path), ADR-0021 D5 (corpora are
  discovered, not registered), ADR-0015 (the write API), ADR-0036 (the sibling defect on the
  same page: a param whose meaning depends on which plane it arrived from)
- **Scope:** the `ruleset` param's meaning, one field on `AdapterSpec`, and the deployment's
  mount. No contract, no capability, no rule-language change.

## Context

Asked while reading the compose page: *what is `ruleset`, and if it is artifact-shaped why is
it free text?* It is not artifact-shaped. An artifact id names a **cached fit** from a closed
set that ships inside the package; `ruleset` named a **filesystem directory**, resolved
wherever the process happened to be running. Investigating that turned up three defects with
one cause.

**1. The offered vocabulary was empty.** A text box, no hint that blank meant the packaged
library, and nothing to type that could be guessed. A wrong value passed `/runs/validate`
clean — `_rules_capability` reads `lane` out of params and never constructs, and the guard's
one construction path (`_built_fingerprint`) runs only for a repeated detector name and
swallows the error anyway — so the refusal arrived from `build_detectors`, which `compute_run`
calls *after* `normalize_sources` has ingested the whole payload. The same late-failure shape
as `lane: telemetry` and as ADR-0036's hyperparameter trap.

**2. In the shipped stack, no value could work.** `pipeline-api` mounted `./results` and
`./data/eval` and nothing else; `config/` and `data/` were baked at build time. There was no
rule directory in that container and no writable host-visible place to create one, so the box
had exactly one working value and did not say so. The product's own claim for this feature —
detection extended by writing a text file, no rebuild — was unreachable from the product's UI.

**3. It was a caller-supplied path on an unauthenticated write API.** This is the one that
made it a defect rather than a rough edge:

```
POST /runs  ->  execute_run  ->  build_detectors  ->  RuleDetector.__init__
            ->  load_rules   ->  Path(<caller string>).rglob("*")  ->  read_text  ->  safe_load
            ->  ValueError   ->  422 with str(exc) in the body
```

Measured against the real loader, not reasoned about: a directory holding a non-rule YAML
returns `not readable as YAML:` followed by **two quoted lines of that file** (a probe file's
`password: hunter2` came back in the error string); `is not a directory` versus `contains no
.yml/.yaml rule files` versus a parse error distinguishes three states of any path; pointed at
`C:/Windows` the loader recursed into `System32`, named a full path back, and exceeded a
120-second timeout while reading and parsing everything with a matching suffix.

Bounded honestly: `_parse` uses `yaml.safe_load`, so there is **no code execution** — the
impact is a read/existence oracle over YAML-shaped files plus a cheap way to pin a worker. The
compose ports bind to loopback, so nothing was internet-facing. What makes it a defect anyway
is that this codebase had already decided the opposite three times, in writing:

- `PipelineSpec` excludes `database_url`, because "a tenant must never point a run at an
  arbitrary database" (`pipeline/config.py`).
- Corpora are addressed **by name** against the server's `IOTSHIELD_CORPUS_PATH`, because "a
  request carrying a filesystem path is a request to read arbitrary files" (`api/evaluation.py`,
  citing ADR-0018 §9-A).
- A caller-supplied sink filename is **confined** to `IOTSHIELD_OUTPUT_DIR`, validated rather
  than sanitized (`sink/file.py`).

**One cause.** ADR-0032 shipped when configuration meant YAML and the CLI, where the operator
*is* the person holding the filesystem and naming a directory is the honest interface. ADR-0015
put the same registry behind HTTP and ADR-0023 rendered every registry param as a widget. At
that moment `ruleset` changed meaning without changing a character: *a path on my machine*
became *a path on the server, chosen by whoever can reach port 8100*. A YAML file and a request
body deserialize to the **same `PipelineSpec`**, so there is no trusted plane to keep the old
meaning on. The three path-shaped facts that predate the shift each got a server-configured
root; rules landed later and inherited the pre-HTTP shape.

## Decisions

### D1 — Rule sets live on a shelf the deployment designates

One directory, `IOTSHIELD_RULES_PATH`, defaulting to `config/rules` in a checkout, holding one
subdirectory per rule set. `rules_root()` has the same three steps as `corpus_root()` and
`bench_root()` — explicit argument, else env, else the repo's own — because it is the same kind
of fact: *where files live* is a deployment property, not an operator one.

Discovered, not registered: a subdirectory with at least one `.yml`/`.yaml` under it is a rule
set. Nothing to edit to add one, which is the promise the feature is built on.

### D2 — `ruleset` names a rule set; the factory is the boundary

`packaged` (the default) is the library that ships in the product; anything else must be a
**member** of the shelf listing, and `resolve_ruleset` joins it to the root. Membership, never
sanitizing — the stance `sink/file.py` argues for on the same hazard, because reducing a bad
value to a plausible one turns a wrong path into a *different* wrong path. `../secrets`, an
absolute path, a UNC path and an honest typo therefore fail identically, with the list of what
exists and no signal about whether the thing named is there.

The check lives in the **registry factory**, not in the loader. `load_ruleset` still takes a
path, because it is the library-level entry point and a Python caller who can pass a path can
already open the file; nothing is gained by hobbling it and the existing tests keep their
meaning. The factory is where the config plane ends, and it is the only way config reaches
this adapter.

A resolved absolute path is also **re-checked for containment** before it is returned. That is
redundant given membership, and kept for the reason `sink/file.py` gives: it turns the
docstring's promise into a checked property that cannot rot if the listing changes.

### D3 — The packaged library gets a name, and it is the default

`ruleset: packaged` rather than `ruleset: null`. An optional closed vocabulary would need the
blank `<option>` that neither `Select` nor `st.selectbox` renders, so the field would *display*
the first shelf entry while holding nothing — the display-vs-value incoherence ADR-0036 D2
removed one field over, reproduced one field further over. Naming the library makes the default
a real member of its own vocabulary, so nothing has to be excepted from ADR-0033's rule and no
UI change is needed to be coherent.

It also settles what ADR-0036 D2 left open for artifacts (*which* choice preselects becomes a
filename accident once there are two): here the default is a named constant, not `choices[0]`.

`None` and blank still resolve to the library, so a config written against the old field keeps
working rather than failing at construction. The name is reserved: a shelf directory called
`packaged` is unreachable, and the listing de-dupes so one dropdown cannot show it twice
meaning two different things.

### D4 — One channel for vocabularies that are found rather than declared, and it is lazy

`AdapterSpec.artifacts: tuple[str, ...]` becomes
`AdapterSpec.discovered: Mapping[str, Callable[[], Sequence[str]]]`. Artifact ids and shelf
names are one idea — a closed set the registry can enumerate and a `str` annotation cannot
express — so they travel down one channel and every form that renders a closed vocabulary as a
`<select>` renders both. Two mechanisms for one idea is the condition that produced this bug:
rules missed a treatment its two neighbours got.

**Lazy is the substantive change.** ADR-0035 D1 chose eager resolution and gave a sound reason:
package resources cannot change while the process runs, so a list taken at import stays true,
and a callable would have bought 0.2% of an import that has already happened. That premise is
exactly what fails for a directory operators are invited to drop files into — the loader
already refuses to cache a named rule set so an edit needs no restart, and a vocabulary
snapshotted at import would offer yesterday's answer to a page that reloads. The cost is one
directory listing per `/modules` call, bounded by a directory the deployment controls. The
None-versus-empty distinction that argument worried about does not arise: an absent key and a
callable returning nothing both mean "publish no choices", and `params_of` already collapses
them, which is what keeps ADR-0035 D1's free-text fallback alive for an adapter packaged
without its artifacts.

### D5 — The shelf is mounted, so the promise is true end to end

`docker-compose.yml` bind-mounts `./config/rules` read-only into `pipeline-api` and sets
`IOTSHIELD_RULES_PATH`; the `runner` gets it explicitly rather than relying on the repo-shaped
default, which is only correct for an editable install (the failure `bench.py` already warns
about). `config/rules/README.md` is tracked so the mount has something to bind and a fresh
clone starts with an empty shelf rather than a missing one. Creating a directory on the host is
now the whole procedure: no rebuild, no restart, and it appears in the dropdown on the next
page load.

### D6 — One definition of the repository root

Three subsystems default a directory to one shipping in the repo, and each counted `__file__`'s
parents. Two were at the same depth; the rules loader is one package deeper, so its copy would
have been the first with a different index — and an index off by one yields a directory that
does not exist rather than an error, so every listing comes back empty and the subsystem blames
its missing data. `iotshield/_paths.REPO_ROOT` is counted once, and `corpus.py` and `bench.py`
now read it instead of deriving their own. Behaviour-identical; both keep their public
`DEFAULT_*_ROOT` names and their env overrides.

## Consequences

- **`ruleset: config/my-rules` no longer works, and that is the point.** The break is
  documentation-only: no committed config or bench spec set the field (both rules configs left
  it unset), and the two guides that showed a path now show a name. A deployment whose rules
  live elsewhere points `IOTSHIELD_RULES_PATH` at that directory and names the subdirectory —
  the same move as `IOTSHIELD_CORPUS_PATH`.
- **The shelf is read on every `/modules` call.** One `iterdir` plus one short-circuiting walk
  per candidate subdirectory. A subdirectory holding no rule files at all is walked in full
  before being rejected, which is the only unbounded case and is bounded by content the
  deployment put there.
- **A rule set is still validated only when a detector is built.** Listing never compiles, so
  a name can be offered whose rules are malformed; construction refuses it, named, before the
  run. Compiling every shelved rule set to filter the dropdown would make a catalogue call
  parse arbitrary YAML on the operator's behalf, which is a worse trade than a named failure.
- **A malformed rule's message still reaches the caller, and should.** The 422 for a shelved
  rule set that does not compile quotes the offending file's path and the parser's own excerpt
  of it. That is the *deployment's* rule file, mounted for exactly this purpose, and the reason
  is what the operator needs; the disclosure that mattered was the ability to choose **which**
  directory got parsed, and that is what a name removes.
- **`lane` and `ruleset` are still independent vocabularies.** The `lane` Literal is fixed at
  what the *packaged* library covers, so a shelved rule set for a lane the library does not
  cover cannot be selected with that lane from the dashboard, only from config. The honest fix
  is a vocabulary conditional on another param's value, which `ParamInfo.choices` cannot
  express; recorded rather than attempted.
- **A new law is now tested for every adapter:** a param whose default is not `None` must be a
  member of its own `choices`. A default outside them is a form that opens on a value it cannot
  display, which is the class of bug ADR-0036 D2 fixed once for artifacts, and it is now
  impossible to reintroduce for any param without failing `test_capabilities.py`.
- **The Streamlit compose page needs no change** and got none (that app is frozen, ADR-0031):
  `st.selectbox` renders the published choices like any other closed vocabulary. Its module
  docstring still refers to `AdapterSpec.artifacts` by the old name; the file is not edited on
  purpose.
- **The guard still does not catch this before ingest.** `/runs/validate` passes a bad rule set
  name, because the guard reasons about a spec without constructing adapters. Unlike ADR-0036's
  open item this one is *cheap to leave*: the refusal no longer reads anything, so a late
  failure costs an ingest and reveals nothing. Worth folding into the same detector-loop ERROR
  issue if that gets written.
