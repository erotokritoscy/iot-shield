# iotshield

Ranked, explained alerts from IoT/OT telemetry, logs and network flows.

Built at the CYENS Centre of Excellence (internship project, July 2026), integrating existing
graduate-student detection modules as swappable adapters behind frozen data contracts.

## The problem

Monitoring industrial and IoT estates is not ordinary security monitoring, for four reasons that
shape every decision in this repository.

**You cannot put an agent on the device.** A PLC, an IP camera or a water meter runs firmware
nobody on the defending side controls. Everything you learn about it, you learn from what it
already emits: sensor readings, log lines, and the network traffic it generates.

**Normal is device-specific.** An edge gateway reporting CPU load twice a minute and a rotor rig
sampling pressure at 1 Hz have nothing in common. There is no single model of normal that covers
a fleet, so a system that ships one threshold for everything is either deaf or unusable.

**No single modality is sufficient.** A compromised camera's sensor readings look ordinary
throughout, and it writes no log line announcing that it has been compromised. Only its network
behaviour gives it away. Watch one signal and you miss it; watch three separately and you get
three streams of unrelated noise.

**An anomaly score is not a finding.** "Score 0.79" tells an analyst nothing they can act on.
The output has to say which device, what was seen, how confident the system is, and what to do
next, or it is one more queue nobody reads.

## What it does

```
input          telemetry, syslog, network flows, utility meter exports
  normalize    raw bytes -> one contract, whatever the format
  detect       per-modality lanes: is this unusual, or is this a known-bad thing?
  correlate    group by device and time, score, rank
  serve        Postgres, a read API, and a dashboard that computes nothing itself
```

Five bets hold it together.

**Contracts are the interface, not the code.** Five frozen objects define what every stage
exchanges. No stage knows what any other stage is, which is what lets a detector be replaced
without touching anything upstream or downstream of it.

**Modalities are judged separately, then correlated.** Telemetry, logs and flows each get their
own detectors, because a temperature reading and an SSH failure are not comparable quantities.
They meet again only at correlation, where independent agreement between lanes raises an alert's
priority. That is the direct answer to the third problem above.

**Nothing is silently dropped.** A line that cannot be parsed becomes a quarantined record
carrying the reason and the original text. Input you cannot read is a fact about the world, and
a pipeline that discards it is lying about what it saw.

**Every finding carries its evidence.** Each detection explains itself in one sentence an analyst
can read, and alerts carry a threat classification and recommended actions where the detection
supports one. Where nothing supports one, they say so rather than guessing.

**It is measured, not asserted.** The repository ships an evaluation harness and labelled
corpora, so a claim that a detector works is a number somebody else can reproduce rather than a
demo that happened to go well.

## What it demonstrably does

Everything below is a measured result from a corpus or demo in this repository, reproducible
from a clean checkout.

**Cross-lane correlation is worth what it costs.** One device observed on all three lanes, same
bundle, same bytes, two configurations differing only in how many lanes are judged:

| | event F1 | precision | recall |
|---|---|---|---|
| Telemetry alone | 0.44 | 1.00 | 0.29 |
| All three lanes | 0.82 | 0.70 | 1.00 |

Judging one lane misses roughly two thirds of what happened. Judging three finds all of it and
gives up some precision to do so.

The same effect is visible in the alert queue rather than in a metric. Running the demo input
through the flow lane alone puts the camera at priority 52.7 (HIGH); adding the log and
telemetry lanes, changing nothing else, puts it at 80.0 (CRITICAL) with a corroboration factor
of 2.00. The device in the same capture with no cross-lane agreement — a gateway being
administered from outside — sits at 39.6 in both runs. That is the claim worth making: a
priority that rose everywhere would only show that scoring had changed.

**Signatures catch what rate detection cannot.** A compromised camera probes six hosts on telnet
and uploads 4.2 MB. Both are far under the behavioural boundaries (twenty distinct peers, 50 MB),
so every statistical detector correctly stays silent, and an authored rule catches it anyway,
because a camera dialling port 23 has no legitimate reason to do so even once. The resulting
alert reads *Remote Services (T1021)* with concrete next steps, rather than an unnamed anomaly.

**A learned threshold is one the system can be held to.** The meter detector's alarm line is
not chosen but fitted — 0.7436 for the shipped model, derived from ten weeks of that meter's
own clean history at a declared budget of about one false alarm per meter-month. Run it back
over that history and it raises exactly one detection, which is the budget honoured rather
than a silence: a detector that found nothing there would mean the threshold had drifted
conservative and no longer described what it claims to.

What it keys on is the residual against the hour of the week, so the case it catches is
consumption that is unremarkable for an evening and extraordinary at 3am — the case a fixed
magnitude threshold cannot express, because there is no single number that is both.

**A purpose-built detector is worth measuring against a general one.** On the metering corpus,
the isolation forest and a general-purpose z-score fitted per meter from the same reserved
prefixes:

| | event F1 | precision | recall | detections |
|---|---|---|---|---|
| Isolation forest | **0.23** | 0.14 | 0.67 | 79 |
| Z-score | 0.16 | 0.09 | **1.00** | 319 |

The general detector finds every incident and speaks four times as often to do it; the
purpose-built one buys precision, not sensitivity. Both figures are modest, which is the other
half of the result — hourly metering at these operating points is a hard lane for either, and
reporting only the winner would hide that.

**The harness is capable of contradicting us.** Sweeping the flow detector's hand-chosen
boundary of 20 distinct peers across a 16-device corpus:

| threshold | 8 | 15 | 20 | 27 | 35 |
|---|---|---|---|---|---|
| event F1 | 0.16 | 0.16 | 0.26 | **0.75** | 0.57 |
| recall | 0.60 | 0.60 | 0.60 | 0.60 | 0.40 |

Recall is flat from 8 to 27, so everything between those values catches the same incidents and
differs only in how much noise it raises. On this corpus the shipped default of 20 is beaten
clearly by 27. That finding is recorded rather than acted on, because the corpus contains a
deliberately chatty control device and tuning a default to a synthetic corpus is how a number we
typed in becomes a published result. It is reported here because an evaluation instrument that
can only confirm its authors is not an evaluation instrument.

**A new detection needs no Python.** Rules are YAML files naming a closed, checked field
vocabulary. Adding one is writing a file into a directory the deployment designates; it takes
effect on the next run, with no rebuild and no restart. This is the extension path a domain
expert can use directly, and the detection knowledge currently shipped came in that way.

## What it does not do

**There is no live collector.** A run processes input that is handed to it. Continuous ingest
from a broker, a syslog listener or a network tap is not built.

**Flows identify devices by IP; telemetry and logs identify them by name.** Correlating a flow
with a log line therefore requires the two sources to already agree on an identifier. A general
asset-aliasing layer does not exist, and cross-lane correlation involving flows is limited
without it.

**Alert triage state is read-only.** The contract has acknowledged and closed states, but no
endpoint sets them, so every alert is new. Adding that is a change to the read plane before it
is a change to the dashboard.

**A detector that learns needs one fit per device, at deployment.** A baseline describes one
device, so a fleet of 1,656 meters needs 1,656 fits from each meter's own clean history. Devices
with no fitted model are skipped and named in the run report rather than judged against another
device's normal.

**Unclassified threats stay unclassified.** Where no rule or detector preset supports a threat
classification, alerts say nothing rather than inferring one from the shape of the anomaly. A
wrong classification costs more trust than an absent one.

**This is an MVP and a research spine, not a product.** It has been exercised at demo and
benchmark scale, not at estate scale, and it carries no licence file yet, which is why the
GPLv3 SKAB dataset is converted from a local checkout rather than redistributed here.

## Run it

```
docker compose up -d                          # postgres, both APIs, the dashboard
```

| | |
|---|---|
| http://localhost:8080 | the dashboard |
| http://localhost:8000/docs | the read API |
| http://localhost:8100/docs | the Pipeline API |

**Sign in first.** The compose stack requires a login (ADR-0039): open the dashboard, choose
**Register it**, and create your organization — you become its admin. Every organization sees only
its own data. To let colleagues in, open **Organization → Users** and add them with a temporary
password; they change it under **Account**. The APIs on :8000/:8100 enforce the same login, so
`/docs` there is browsable but its calls answer 401 until the browser holds a session.

**The platform's super admin** manages every organization — creates, suspends, deletes them and
manages their users (ADR-0040). The compose stack **seeds on first start: the `UCY` organization,
a super admin at home in it — `admin@admin.com` / `password` — and the walkthrough's data in
UCY**. Signed in, that account sees UCY's data like UCY's admin and manages every organization,
but sees no other organization's data. Those are well-known local-development credentials
(every port binds to 127.0.0.1): change the password on the **Account** page, and set
`IOTSHIELD_SEED_SUPERADMIN_USERNAME` / `IOTSHIELD_SEED_SUPERADMIN_PASSWORD` in `.env` for any stack
someone else can reach (empty seeds nothing; `IOTSHIELD_SEED_ORGANIZATION=` seeds a super admin
with no home, who sees no organization's data at all). The seed only ever creates — anything that
exists is left untouched, so a changed password survives restarts and the demo loads once. UCY
cannot be suspended or deleted while it is the super admin's home. More super admins, from the
server:

```
docker compose run --rm runner python -m iotshield.auth.superadmin create --username root
```

`IOTSHIELD_OPEN_REGISTRATION=0 docker compose up -d` closes public registration, leaving the super
admin as the only way to create an organization.

Data is added from the dashboard: **Operate → Compose & run** is a four-step wizard (data,
detection, alerts & output, review & run). The quickest test is a **scenario** on its first step —
compromised camera, port scan, SSH brute force, overheating pump, a quiet control that must find
nothing, and broken input — which fills every step with sample data and the right detectors; Review
shows what it is tested to produce. Or pick a sample file per source, or upload your own. Results
appear under **Monitor → Triage**. The samples live in `data/samples/` (and are served to the
dashboard from `web/public/samples/`); `uv run python scripts/make_sample_data.py` rebuilds them.

Those inputs are committed, so a fresh checkout already has them and Docker is the only
prerequisite. `uv run python scripts/make_demo_data.py` rebuilds them from the scenario declared
at the top of that script, which you need only if you change what the demo is meant to show.

The same run without a browser, for a shell or a scheduler:

```
IOTSHIELD_DEMO_ORG=acme-water docker compose run --rm runner      # into Postgres, your org
uv run python -m iotshield.pipeline --config config/demo.yaml     # into local SQLite
```

`IOTSHIELD_DEMO_ORG` names the organization the results belong to: its id, shown on the
**Account** page (`Acme Water` registers as `acme-water`). Left unset, the run lands in an
organization called `default` that nobody can log into. Off the compose stack — no
`IOTSHIELD_AUTH` — there is no login and everything is that one `default` organization.

`config/demo.yaml` is the walkthrough as a pipeline config. It reads the same three files and
produces the same queue, which is what keeps the CLI path honest about what the dashboard does.

Start with `camera_flow.log` and the `rules` detector on the flow lane, then run it again with
`camera_syslog.log` and `camera_telemetry.csv` added — the second run is where cross-lane
corroboration is visible. **Evaluate** runs the benchmarks in `config/bench/` against the
labelled corpora in `data/eval/`.

Everything else, including running without Docker: `docs/guide/architecture.md`.

## Extend it

Four things you might add, in rough order of effort. None of them requires changing the pipeline,
the APIs or the dashboard, which is the property the architecture exists to protect.

| | | |
|---|---|---|
| A detection rule | A YAML file on the rule shelf. No Python. | `docs/guide/detectors.md` |
| A source format | One normalizer adapter and its contract test suite. | `docs/guide/normalizers.md` |
| A detector | One adapter, one declared capability, one parameters model. | `docs/guide/detectors.md` |
| A corpus to measure it against | A bundle on disk, or a scenario for the generator. | `docs/guide/corpus.md` |

## Documentation

`docs/README.md` is the index.

- **`docs/guide/`** answers *how do I do X*. Nine task-first guides covering the architecture,
  each layer, both APIs, the dashboard, the evaluation harness and corpora. Start with
  `architecture.md`.
- **`docs/adr/`** answers *why is X like this*. Thirty-eight decision records, numbered and
  append-only, each stating what was decided, what was rejected, and what it cost.
- **`CLAUDE.md`** is the working developer guide: golden rules, every command, and the
  accumulated detail behind the decisions.

## Provenance

The detection knowledge here comes from graduate-student modules developed at CYENS, integrated
as adapters rather than vendored wholesale: a syslog normalizer, a telemetry z-score detector, a
weighted alert engine, an isolation-forest meter detector, and a rule library with its MITRE
mappings. Each integration has a decision record stating what was adopted, what was rewritten,
and why.
