# Integrating your detection rules and threat enrichment into iotshield

Hi Antonia,

This document explains how the parts of your project will plug into the iotshield
pipeline, what I'd like you to change on your side and why, what I'll build on my
side once that's done, and how we'll know we're finished. I've tried to keep it
simple and concrete. The goal is to make
our two pieces fit together cleanly, and to make sure the parts we adopt are things
you can keep maintaining after the internship ends.

---

## 1. What iotshield is (my repo)

iotshield is a pipeline that turns raw IoT/OT data into a ranked, explained list of
security alerts. It has four stages:

```
raw data  →  normalize  →  detect  →  correlate & prioritize  →  output
```

- **normalize** — turn each source's raw records into one common event shape.
- **detect** — run detection methods over those events; each one outputs "findings"
  we call _detections_.
- **correlate & prioritize** — group detections by device and time, then score and
  **rank** them, so an analyst sees the most important one first.
- **output** — store/show the ranked alerts.

The one idea you need to know about how it's built: it uses **"ports and adapters."**

- A **port** is a fixed job description for a stage (for example, "a detector takes
  events and returns detections").
- An **adapter** is one concrete implementation of that job (a specific detection
  method).
- Modules never talk to each other directly. They only exchange fixed data
  structures we call **contracts** (for example, a `NormalizedEvent` or a
  `DetectionResult`). The exact fields of these are frozen and shared.

Because of this, adding a new detection method means writing **one new adapter** that
fits the detector port and speaks the shared contracts. Everything else in the
pipeline keeps working unchanged.

---

## 2. What your project is

Your project is a SIEM/XDR-style framework for IoT/OT. It:

- normalizes many different sources into a common event schema,
- applies **Sigma-style YAML rules** to flag suspicious events,
- correlates events into higher-level findings,
- enriches findings with **MITRE ATT&CK** mappings and **suggested analyst actions**.

It's a well-organized pipeline, and two parts of it are a very good fit for iotshield.

---

## 3. Where your work fits in mine, and why

Here's the key picture. Your framework and mine overlap, but they specialize in
different halves of the same overall job:

```
Yours:   normalize → detect (rules) → correlate → enrich (MITRE + actions)
Mine:                                  correlate → prioritize (rank with a score)
```

Both of us do correlation. But **your strength is detection and enrichment**, and
**my pipeline's strength is prioritization** (turning findings into a numbered,
ranked queue). So instead of one replacing the other, we combine: I take the parts
you're strong at, and keep the ranking engine I already have.

Concretely, **two parts of your repo will be adopted:**

- **(A) Your detection rules** → they become a new **rule-based detector** in my
  pipeline. I don't currently have one, so this genuinely fills a gap.
- **(B) Your MITRE + suggested-actions enrichment** → it becomes the **explanation
  attached to each alert** (the "what kind of threat" and "what to do" fields).

**What I will _not_ adopt, and why (no action needed from you here):** my pipeline
already has a prioritization engine, and it follows a few hard rules — every
detection must always be shown (never silently dropped), more corroborating evidence
can only _raise_ an alert's priority (never lower it), and each alert gets a numeric
0–100 score that also factors in how critical the device is. Your correlation layer
is designed to _reduce noise by dropping_ findings that don't correlate, which is a
different goal. So I'll keep my ranking engine for that job. This isn't about which
is better — it's that the hard, domain-specific work (writing good rules and threat
knowledge) is exactly what I want from you, and the ranking plumbing is what I
already have.

---

## 4. The two things we're importing, in a bit more detail

### (A) Your detection rules

Your rule system is really **two things**: the small Python **engine** that checks a
rule against an event, and the **rules themselves** (the YAML files).

- The **engine** has to produce my internal data shapes and pass my test suite, so
  I'll rewrite that part on my side. You don't need to learn my internals.
- The **rules** are the valuable part — the actual knowledge of what's suspicious —
  and they stay **yours** to own and maintain.

Two things to know about which rules we keep:

1. **We keep rules that look at real signals, not dataset labels.** Some of your
   rules match on fields like `activity_name == "backdoor"` or `malicious_label`.
   Those fields are the _ground-truth answer key_ of the training dataset — they
   won't exist on real, live data, so those rules can't fire in production. The rules
   we want are the ones that look at real observable signals: byte counts, ports,
   failed-login outcomes, connection patterns, etc. (I'll go through the list with
   you so we agree on which is which.)

2. **"Count over time" patterns are already built in.** Patterns like "5 failed
   logins within 60 seconds = brute force" are already handled by my existing
   detectors. So the rule detector we're adding is for **single-event signatures**
   (one event matches a pattern). This means we don't need to re-import your
   brute-force / threshold correlators — I already have that machinery.

### (B) Your enrichment

Right now your rules and correlators carry a MITRE ATT&CK tag and a list of suggested
actions. In my pipeline, that information will **ride along with a detection** and
then get promoted onto the final alert — filling in the alert's "threat class" and
"recommended action" fields, which are currently empty. So your threat knowledge
becomes the human-readable explanation an analyst sees.

---

## 5. What I'd like you to change, and why

Each item below is tied to something on my side. None of it requires touching my
code — it's all in your repo and your rule files.

**For the rules:**

1. **Mark which rules are observable-based vs label-based.**
   _Why:_ label-based rules won't work on real data; we only promote the
   observable ones into the production rule set.

2. **Use one consistent field-addressing style across all rules** (your V2 layered
   style everywhere, not a mix of V1 and V2).
   _Why:_ my engine needs one predictable way to read a field out of an event.

3. **Make `confidence` one consistent type in every rule** (right now some rules use
   a number like `0.8` and others use a word like `medium` — pick one; a word/tier is
   fine).
   _Why:_ anything reading that field breaks if the type changes rule to rule.

4. **Make `mitre` one consistent shape in every rule** — the structured form is best:
   `{tactic, technique, technique_id}`. (Some of your rules use a bare string like
   `T0869` and others use the full structured object.)
   _Why:_ this is what becomes the alert's threat label, so it needs one shape.

5. **Have each rule declare which "lane" it applies to** — i.e. what kind of data:
   `flow` (network), `log` (logs/auth), or `telemetry` (sensors).
   _Why:_ my pipeline routes events by their type, so a rule has to say which type of
   data it applies to.

6. **Write one line per rule describing what it means** — the real-world signal it's
   looking for and the threshold.
   _Why:_ so I can port it faithfully, and so whoever maintains it later understands
   it.

**For the enrichment:**

7. **Provide, for each rule, a clean MITRE mapping + suggested-actions list**, in the
   one consistent shape from item 4.
   _Why:_ this is the content that becomes the threat class and recommended action on
   the alert.

**One packaging fix:**

8. **Make your files importable without side effects** — add
   `if __name__ == "__main__":` guards so that importing a file doesn't run the whole
   pipeline and write output files. (A couple of your correlator scripts currently run
   on import.)
   _Why:_ I need to be able to reference your rule format and logic without it
   executing.

One note on **severity**: in your repo, a rule's `severity` is the final answer. In
my pipeline, the final priority is a computed 0–100 score, so a rule's `severity`
becomes a **hint** that feeds into that score rather than the last word. You don't
need to change anything for this — just know that's why I may call it a "severity
hint."

_(Separately, cleaning up the two parallel pipelines in your repo and giving findings
stable IDs would help your project regardless of this integration — but those aren't
required for us. Up to you.)_

---

## 6. What I will do once that's done

- **Write the rule-detector adapter** — the code that reads your cleaned rules and
  runs them inside my pipeline, producing my internal detection objects.
- **Publish a "field list"** — the exact names a rule uses to refer to a field on my
  event objects, per lane, with examples. You'll write future rules against this.
- **Port your keeper rules** onto that field list (I'll do the first batch; after
  that it's just YAML).
- **Carry your MITRE + actions through to the alert** — filling in the threat class,
  recommended action, and explanation text.
- **Write the tests** that make the rule detector safe to plug in like every other
  module.

---

## 7. How we'll know it's done (sign-off checklist)

We consider this integration complete when:

- [ ] Every rule uses one field-addressing style, one `confidence` type, and one
      `mitre` shape.
- [ ] Every rule declares its lane and has a one-line description.
- [ ] Observable-based rules are separated from label-based ones; only observable
      rules are in the production set (label ones clearly marked demo/eval only).
- [ ] Each production rule has a MITRE mapping and suggested actions in the agreed
      shape.
- [ ] Your files can be imported without running or writing anything.
- [ ] (My side) the rule detector passes my port test suite.
- [ ] (My side) a rule that fires flows all the way through to a **ranked, explained
      alert** end-to-end, with the threat class and recommended action filled in from
      your enrichment. We demo this together.

---

## 8. How to add a new rule later (the ongoing guide)

Once the above is in place, adding a new detection is just writing a YAML file — no
code changes. The recipe:

1. **Write a rule file** in the agreed format (example below).
2. **Address fields using the published field list** for that lane. Only use real
   observable fields — never dataset labels.
3. **Set a lane** (`flow`, `log`, or `telemetry`).
4. **Add the MITRE mapping and suggested actions** in the structured shape.
5. **Drop the file in the rules folder.** The detector picks it up automatically.

Example of a rule in the target shape (the exact field names come from the field list
I'll publish — here the _shape_ is what matters):

```yaml
id: high-upload-volume
title: High Outbound Upload Volume
lane: flow # which kind of data this applies to
detection:
  selection:
    metadata.flow.src_bytes: # a real observable field (final name from the field list)
      operator: ">"
      value: 10000
  condition: selection
severity_hint: medium # a hint; final priority is computed by the pipeline
mitre:
  - tactic: Exfiltration
    technique: Exfiltration Over C2 Channel
    technique_id: T1041
suggested_actions:
  - Review the source device for unusual outbound traffic.
  - Confirm whether the destination is expected.
```

I'll also give you a fuller "add your own module" guide for iotshield, but for rules
specifically, that's the whole workflow.

---

Let me know if anything here is unclear or if you'd change the split. Once we agree,
a good first step is going through your rule list together and tagging each one
observable vs label, and lane. From there the changes are small and mostly mechanical.
