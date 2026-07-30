# Documentation

Two kinds, answering two different questions.

**`guide/` answers "how do I do X".** Nine task-first guides written for somebody who will
extend this repo after handover. They describe the system as it is today, and they are meant to
be kept current: if you change behaviour, the guide covering it is part of the change.

**`adr/` answers "why is X like this".** Thirty-seven architecture decision records, numbered
and append-only. An ADR is a record of a decision at the moment it was made, including the
options rejected and what they would have cost. They are not maintained as a description of the
current system, and they are deliberately not edited when the world moves on. A decision that is
superseded gets a **new** ADR that says so, or an amendment appended to the old one with a date.
That is the whole point of the format: you can read what somebody believed at the time and judge
whether the reasoning still holds.

So: read a guide to work on the code. Read an ADR when a guide tells you something is a certain
way and you want to know what the alternative was.

## The guides

| | |
|---|---|
| [`guide/architecture.md`](guide/architecture.md) | What the product does, the four ports, the contracts, and why it is shaped this way. **Read this first**; every other guide assumes it. |
| [`guide/normalizers.md`](guide/normalizers.md) | Turning raw input text into events. Timestamps, quarantine, adding a format. |
| [`guide/detectors.md`](guide/detectors.md) | The layer that decides something is worth looking at. Presets, rules, hyperparameters, artifacts, training. The longest guide, because this is the layer people extend. |
| [`guide/alerts.md`](guide/alerts.md) | Turning detections into a ranked queue. The scoring factors, correlation, threat enrichment. |
| [`guide/pipeline-api.md`](guide/pipeline-api.md) | The write side: what adapters exist, would this chain work, run it. |
| [`guide/read-api.md`](guide/read-api.md) | The read side: what a run produced, served to any client. |
| [`guide/dashboard.md`](guide/dashboard.md) | The React app in `web/`. A client that computes nothing, and the failure taxonomy that makes it honest about it. |
| [`guide/evaluation.md`](guide/evaluation.md) | Measuring whether a detector is any good. Metrics, matching rules, fit protocols, sweeps. |
| [`guide/corpus.md`](guide/corpus.md) | Labelled data: the on-disk bundle format, the synthetic generator, and how to add a corpus. |

Three reading orders that make sense:

- **Adding an adapter.** `architecture.md`, then whichever of `normalizers.md` /
  `detectors.md` / `alerts.md` covers your port, then `evaluation.md` to find out whether the
  thing you added actually works.
- **Consuming the data.** `architecture.md`, then `read-api.md`. `dashboard.md` if you want a
  worked example of a client that only reads.
- **Measuring something.** `evaluation.md`, then `corpus.md`. They are two halves of one
  subject and the second assumes the first.

## The decision records

The ones that carry the most weight, if you are looking for a place to start rather than reading
all thirty-seven:

**The foundations.** [0003](adr/0003-data-contracts-v1.md) is the data contracts, which
everything else is built on. [0004](adr/0004-ports-and-malformed-record.md) is the port
laws and why a bad input record is quarantined rather than dropped.
[0006](adr/0006-pipeline-runner.md) is the config-driven runner.
[0014](adr/0014-capability-guard.md) is how an adapter declares its shape so a chain can be
checked without being built.

**Contracts as a boundary.** [0007](adr/0007-read-side-api.md) serves the contracts as the wire
format. [0008](adr/0008-dashboard.md) makes the client validate every response against them, so
drift fails loudly at one place instead of rendering wrong.
[0015](adr/0015-pipeline-api.md) splits the write side into its own service.
[0018](adr/0018-result-sink.md) makes the output store pluggable.

**How modules were integrated.** [0011](adr/0011-syslog-normalizer-integration.md),
[0012](adr/0012-team3-telemetry-detector-integration.md) and
[0013](adr/0013-team2-alert-engine.md) are three student modules brought in three different
ways, with the reasoning for each choice. [0030](adr/0030-meter-detector-and-scikit-learn.md)
and [0032](adr/0032-rule-detector-and-threat-enrichment.md) are the two teammate contributions
from the second half.

**Learning and measuring.** [0021](adr/0021-evaluation-harness.md) is the harness.
[0022](adr/0022-hyperparameters-and-fitted-state.md) is the law separating what a human chose
from what fitting computed, which any adapter that learns has to obey.
[0034](adr/0034-a-baseline-describes-one-device.md) is why a model belongs to one device.
[0027](adr/0027-measuring-what-actually-happened.md),
[0028](adr/0028-the-answer-keys-coordinate-system.md) and
[0029](adr/0029-an-incident-is-a-projection-not-an-interval.md) are one argument in three
parts about what ground truth is actually indexed by. Read them in order, or read the summary
in `guide/evaluation.md` instead.

**The most recent.** [0031](adr/0031-react-dashboard.md) is the React rewrite.
[0035](adr/0035-the-lane-is-not-the-whole-shape.md),
[0036](adr/0036-a-judge-only-page-offers-no-hyperparameters.md) and
[0037](adr/0037-a-rule-set-is-named-not-pathed.md) are three fixes found by using the product
rather than by testing it, which is worth knowing about the ones that came before them too.

## Writing a new ADR

Copy the numbering and the section headings from a recent one. What matters is that it records
the **context** (what was true that forced a decision), the **decision**, the **alternatives
rejected** and what they would have cost, and the **consequences** you are accepting, including
the bad ones. An ADR that only says what was built is a commit message with extra steps.

Contract changes require one. That rule is in `CLAUDE.md` and is the main thing keeping the
contracts stable enough for adapters to be swappable.
