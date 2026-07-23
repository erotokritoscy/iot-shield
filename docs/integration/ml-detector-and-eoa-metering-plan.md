# Integrating your anomaly detector into iotshield

Hi Panayiotis,

This document explains how your detector plugs into the iotshield pipeline, what
I'd like you to change on your side and why, what I'll build once that's done, and
how we'll know we're finished. I've kept it simple and concrete, and the plan is
built to keep the parts we adopt things you can keep maintaining after the internship.

The short version: there's **one detector** to integrate — your meter anomaly
pipeline — and it plugs into iotshield as **one shared detector plus one normalizer
per source** (EOA, SensiPro, and any future client). You **own the detection
pipeline**; I write the per-source normalizers and the glue that connects them to
iotshield's contracts. This mirrors your own architecture, where only the loading is
source-specific and everything after the canonical seam is shared.

---

## 1. What iotshield is (my repo)

iotshield is a pipeline that turns raw IoT/OT data into a ranked, explained list of
security alerts. It has four stages:

```
raw data  →  normalize  →  detect  →  correlate & prioritize  →  output
```

- **normalize** — turn each source's raw records into one common event shape.
- **detect** — run detection methods over those events; each one outputs findings we
  call *detections*.
- **correlate & prioritize** — group detections by device and time, then score and
  rank them.
- **output** — store/show the ranked alerts.

The one idea you need about how it's built: it uses **"ports and adapters."**

- A **port** is a fixed job description for a stage (e.g. "a detector takes events
  and returns detections").
- An **adapter** is one concrete implementation of that job (a specific detector, or
  a specific source's normalizer).
- Modules never talk to each other directly. They exchange fixed data structures we
  call **contracts** — the main ones for you are `NormalizedEvent` (one observation)
  and `DetectionResult` (one anomaly judgment over a time window). Their fields are
  frozen and shared.

Two rules of the detector port matter for you: a detection describes a **time
window** (not a single row), and all timestamps are **timezone-aware UTC**.

---

## 2. What your project is

Your project is an unsupervised anomaly detector for smart water/electricity meters.
It reads from more than one source (an **EOA** CSV export and the **SensiPro**
database), turns a cumulative meter counter into hourly consumption, builds features
(rolling statistics, an hour-of-week usage profile, a seasonal residual), runs
**Isolation Forest** (LOF next) per meter, collapses flagged hours into episodes, and
explains each one with **feature attribution** ("which features made this hour stand
out"). It's solid, genuinely unsupervised ML with an honest evaluation.

Your own pipeline documentation already draws the line we care about: *only the
loading is source-specific; everything after the canonical seam is shared.* That's
exactly how iotshield is built, which is why this fits so cleanly.

---

## 3. Where your work fits, and why

Your architecture and iotshield's are the same shape, so your pipeline maps onto our
two ports almost one-to-one:

| Your stage | iotshield equivalent |
|---|---|
| Adapter (EOA CSV / SensiPro DB / future source) | a **normalizer** — one per source |
| Cleaning (counter → hourly consumption) | part of the **normalizer** (see §4) |
| Canonical frame | our `NormalizedEvent` stream |
| Profile + features + Isolation Forest + attribution | **one detector** |

So your work becomes **one shared meter detector, fed by one normalizer per source.**
EOA and SensiPro are two normalizers pointing at the *same* detector; adding a new
client later is just adding another normalizer. Nothing about the detector changes
when a source is added — which is the same promise your `source_loader` already makes
("adding a source = registering one adapter").

Water vs electricity is **not** two detectors either. Like the way you gate the
water-only leak rule on `meter_type`, the one detector handles both by carrying the
meter type through and branching only where it must. One detector, many sources, both
utilities.

---

## 4. What we're importing, mapped to iotshield's two ports

### (A) Your loading + cleaning → our normalizers (one per source)

A normalizer's job in iotshield is to produce the **meaningful observation**. For a
meter, the meaningful quantity is **hourly consumption**, not the raw cumulative
counter — so the normalizer is where the counter gets differenced into consumption,
and it emits consumption events. One normalizer per source: EOA (from the CSV) and
SensiPro (from the database).

*Why the differencing goes in the normalizer, not the detector:* consumption is the
real signal — it's the *required* field in your own canonical contract, and the raw
counter is just a passthrough you difference away immediately. Doing it once at the
boundary means every detector downstream (Isolation Forest now, a data-quality check
later) consumes clean consumption and never has to know whether a source was
cumulative or already-hourly. Your gap-safe differencing logic becomes a shared
helper both meter normalizers use; an already-interval source (like LEAD) just skips
it.

### (B) Your features + Isolation Forest + attribution → our meter detector (one, shared)

Your `meter_profile` (hour-of-week), `features` (rolling stats), `seasonals`
(residual), the Isolation Forest itself, and `attribution.py` become the meter
detector. **You own this logic upstream.** I vendor a copy into iotshield and adapt it
to our contract, and re-sync when you improve it — the same way we already integrate
another teammate's detector (vendor a snapshot, keep provenance, pull updates).

Your `attribution.py` is worth calling out on its own: it's model-agnostic (it
standardizes each feature against the meter's own history), so it becomes the
**explanation attached to every detection** — the "why" an analyst reads.

---

## 5. What I'd like you to change, and why

None of this requires touching my code — it's all in your repo, and most of it makes
your project cleaner regardless.

1. **Make it an installable package with a real name** (right now the importable
   package is literally called `src`).
   *Why:* so I can vendor from it cleanly and you can keep shipping improvements to it.

2. **Make timestamps timezone-aware UTC.**
   *Why:* iotshield correlates everything by UTC time and rejects naive timestamps at
   its boundary.

3. **Expose the raw (un-normalized) anomaly score, and let the threshold be set from
   outside.**
   *Why:* your event's `anomaly_score` is min-max normalized *per meter* to [0,1],
   which only has meaning within one batch. Our detector needs an absolute
   `score > threshold`. The clean way to get an absolute threshold is to **learn it on
   known-normal history** (fit on a clean stretch, set the threshold there) and then
   judge new data against it. That's also more honest than fitting and scoring the
   same window — otherwise the model learns "normal" partly from the very anomalies
   it's meant to catch. Your raw `if_score` already exists; I just need it exposed
   rather than only the normalized value.

4. **Keep the static-profile residual as the default; keep MSTL behind the switch.**
   *Why:* MSTL pulls in `statsmodels` and costs a few seconds per meter, and your own
   notes say whether it beats the static profile is an open question. So let's default
   to the free option and turn MSTL on only if the evaluation shows it helps.

5. **Keep your loading, cleaning, and feature functions importable and
   side-effect-free** (no work triggered just by importing a file).
   *Why:* those functions are exactly what I vendor — the cleaning/differencing into
   the normalizer, the features into the detector. If importing them runs a pipeline
   or writes files, I can't lift them cleanly.

6. **Own the meter logic going forward** — the counter differencing, the hour-of-week
   profile, the holiday calendar, the residual.
   *Why:* that's your domain expertise, and after the internship it's the part only
   you can keep correct (e.g. next year's moving holiday dates).

---

## 6. What I will do once that's done

- **Write the normalizers** — one per source. EOA reads the CSV; SensiPro reads the
  database (this one needs a read/export step, since our ingestion is built for files
  and streams — EOA is unaffected). Both do your gap-safe differencing and emit
  timezone-aware **consumption** events, with unreadable rows quarantined.
- **Build the meter detector** — vendor your features + Isolation Forest +
  attribution, add the learned-on-clean threshold, collapse flagged hours into
  detection windows, and attach your attribution as the explanation. One detector,
  used by every meter source.
- **Validate on LEAD** — your already-interval support means we can run the labelled
  LEAD dataset through our evaluation harness and get a real precision/recall number
  for the meter detector.
- **Add a small data-quality detector later** (a follow-on) for your rollback and gap
  findings, which are real anomalies worth surfacing on their own.
- **Bring scikit-learn into our runtime** (an internal decision record); `statsmodels`
  stays optional, only pulled in if MSTL is turned on.
- **Write the tests** that make the detector and normalizers safe to plug in like
  every other module.

---

## 7. When we'll know it's done (sign-off checklist)

- [ ] Your repo is an installable package, timezone-aware, and importable without
      side effects.
- [ ] The raw anomaly score is exposed and a threshold can be set from outside.
- [ ] (My side) the meter detector passes the port test suite and scores the labelled
      **LEAD** dataset through the evaluation harness.
- [ ] (My side) EOA and SensiPro both flow end to end: source → normalizer → the same
      meter detector → a detection that reaches a ranked, explained alert. We demo it.
- [ ] Your feature attribution shows up as the explanation on detections.

A note on validation: **LEAD** is labelled, so it's what proves the detector is sound
with a real number. EOA and SensiPro have no ground-truth labels, so on those we
validate the way you already do — synthetic anomaly injection plus an operator demo.
Same detector in all three cases; LEAD is just the one where we can measure it.

---

## 8. How to extend it later (ongoing ownership)

Once this is in place:

1. **To add a new client** (a different utility, format, or database): write **one
   normalizer** for how their data is read and turned into consumption events. The
   detector doesn't change — this is the same "add a source = one adapter" pattern
   your own repo already uses.
2. **To improve detection:** change your feature or model logic in *your* repo; we
   re-sync the vendored copy. No iotshield internals needed.
3. **To tune the model:** the hyperparameters (number of trees, contamination, the
   threshold quantile, residual choice) are set in config and can be swept by our
   evaluation harness.

I'll also give you a fuller "add your own module" guide for iotshield; this is the
metering-specific short version, and it lines up with your own `adapter_authoring_guide`.

---

Let me know if anything's unclear or you'd change the split. A good first step is the
packaging + timezone + raw-score items (1–3 above) — once those are done I can start
writing the normalizers and wire your detector in.
