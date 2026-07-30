# The rule shelf

Detection rules that are **not** part of the product live here: one subdirectory per rule set,
each holding `.yml`/`.yaml` rule files (nested directories are fine).

```
config/rules/
  site-rules/
    smb-lateral-movement.yml
    dns-tunnelling.yml
  pilot-2026/
    pump-house-limits.yml
```

Each subdirectory holding at least one rule file becomes a **name** the pipeline can use:

```yaml
detectors:
  - name: rules
    params:
      lane: flow
      ruleset: site-rules
```

`ruleset: packaged` (the default) selects the library that ships inside the product, at
`src/iotshield/detect/rules/library/`. Those rules are part of the codebase and are reviewed
with it; anything here belongs to the deployment.

Rule sets are **discovered, not registered** — there is no file to edit to add one, and the
names on this shelf are what the dashboard offers in its `ruleset` dropdown. A rule set added
while the Pipeline API is running appears on the next page load; no restart.

`ruleset` is a name, never a path. The server resolves it against this directory and refuses
anything that is not on the shelf, because the same pipeline spec can arrive from a YAML file
or from an HTTP request body (ADR-0037).

Where "this directory" is depends on the deployment: it defaults to `config/rules` in a
repository checkout, and `IOTSHIELD_RULES_PATH` overrides it. `docker-compose.yml` mounts this
directory read-only into the Pipeline API and sets that variable, so on the compose stack you
add a rule set by creating a subdirectory here.

Writing the rules themselves — the operators, the grammar, worked examples, and what happens
to a rule once it fires — is `docs/guide/detectors.md`. The **field vocabulary** a rule may
name on the left of a comparison is published by the code that implements it, so it cannot
drift from what actually matches:

```
python -m iotshield.detect.rules   # every field, its kind, and what it holds
```

A rule set is compiled when a run constructs the detector, not when it is listed, so a name
offered in the dropdown can still hold a malformed rule — it is refused, and named, at
construction. The quickest way to find out is to run one: point the dashboard's Compose page at
`data/demo/camera_flow.log` with the `rules` detector on the flow lane and your `ruleset`
selected. On the compose stack this directory is mounted read-only into `pipeline-api`, so a
new subdirectory appears in that dropdown on the next page load — no rebuild, no restart.

This directory is tracked with only this file in it, so the compose mount has something to
bind and a fresh clone starts with an empty shelf rather than a missing one.
