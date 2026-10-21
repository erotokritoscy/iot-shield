# Manual test: port scan + SSH brute force

## Steps

1. Open http://localhost:8080 and sign in.
2. Go to **Operate → Compose & run**.

### Step 1 · Data

The sources are steps of their own: **Source 1**, **Source 2**, … at the top, one on screen at a time.

3. **Source 1:**
   - Normalizer: `flow`
   - Input format: `zeek-conn`. Change it: the default is `flow-csv`.
   - Upload a file:
     `\\wsl.localhost\Ubuntu-20.04\home\erotokritos\projects\iot-shield\data\samples\portscan_flow.log`
   - At the bottom, the first lines are shown and it says *Ready*. Click **Add another source**.
4. **Source 2** replaces Source 1 on screen (Source 1 now shows ✓ at the top):
   - Normalizer: `syslog`
   - Upload a file:
     `\\wsl.localhost\Ubuntu-20.04\home\erotokritos\projects\iot-shield\data\samples\bruteforce_syslog.log`
5. Click **Next: Detection**.

### Step 2 · Detection

6. Click **+ Add detector** so there are 2 detectors.
7. **Detector 1:** `flow_burst`. Under *flow_burst parameters*, set preset = `port_scan`.
8. **Detector 2:** `log_burst`. Under *log_burst parameters*, set preset = `auth_failure`.
9. Click **Next: Alerts & output**.

### Step 3 · Alerts & output

10. Alert engine: `weighted`. Leave its parameters at their defaults.
11. Sinks: `postgres`, which is already ticked.
12. Click **Next: Review & run**.

### Step 4 · Review & run

13. Click **Run pipeline**.
14. Go to **Monitor → Triage**.

## Expected results

**Step 4 (Review), before running:**

- Detectors listed as `flow_burst — preset: port_scan` and `log_burst — preset: auth_failure`
- "Chain is compatible."

**Run receipt:**

| Events | Malformed | Detections | Alerts |
|---|---|---|---|
| 98 | 0 | 2 | 2 |

**Monitor → Triage** (the brute force is ranked first):

| Severity | Device | Priority | Threat class | Why |
|---|---|---|---|---|
| MEDIUM | `10.0.0.11` | 37.5 | Brute Force (T1110) | 8 auth failure events in a 5-minute window |
| LOW | `10.0.0.42` | 29.4 | Network Service Discovery (T1046) | 25 ports probed in a 1-minute window |

If the organization already had data, these 2 alerts appear in addition to the existing ones.

---

# Manual test: a file in the wrong format

1. **Operate → Compose & run**, Step 1, **Source 1**.
2. Leave the normalizer on `stub` and upload `data/samples/bruteforce_syslog.log` (a syslog file).
3. **Expected:** at the bottom of the card, a red **Wrong format?** error: *stub (stub-csv) could not
   read any of the N records…*, with the first rejected lines (`Line 1: expected 4 fields …`).
   The **Source 1** tab says *wrong format?* and **Next: Detection** is disabled, with *Source 1
   cannot be read by stub*.
4. Change the normalizer to `syslog`. **Expected:** the error goes away and the card says
   *Ready: syslog reads N of N records*; **Next** is enabled.
5. Upload `data/samples/broken_syslog.log` instead. **Expected:** a blue note — some records read,
   4 cannot be read and will be kept on Data health — with their line numbers; **Next** stays enabled.

---

# Manual test: OT overview for July 2023 (SensiPro meters)

**URL:** http://localhost:8080/overview/ot?start=2023-07-01&end=2023-07-31

Needs the SensiPro database running: in `~/projects/sensipro`, run `docker compose up -d sensiprodb`.
One pull holds at most 4 MB, so July goes in as **six runs** of about six days each.

## Steps

1. Go to **Operate → Compose & run**, Step 1.
2. **Data source:** `OT · SensiPro meters`. The Pull panel appears.
3. Pull **electricity** for the first range below:
   - set **From** and **To**;
   - leave **utility** as `electricity` and **Devices** empty, which means all meters;
   - click **Pull**.
4. **Step 2 · Detection:** detector `meter_quality`, with its default parameters.
5. **Step 3:** sink `postgres`. **Step 4:** **Run pipeline**.
6. Repeat steps 1–5 for each of the six ranges (start each time from Step 1 with a fresh pull):

| Run | From → To | Rows pulled | Expected anomalies |
|---|---|---|---|
| 1 | 2023-07-01 → 2023-07-06 | 63,072 | high 30 |
| 2 | 2023-07-07 → 2023-07-12 | 63,072 | high 30 |
| 3 | 2023-07-13 → 2023-07-18 | 65,032 | valueError 131, small 7, high 47, heartbeat 2 |
| 4 | 2023-07-19 → 2023-07-24 | 80,547 | valueError 992, zero 3, small 64, high 79, heartbeat 15 |
| 5 | 2023-07-25 → 2023-07-30 | 80,652 | valueError 1,164, small 70, zero 1, high 66, heartbeat 14 |
| 6 | 2023-07-31 → 2023-07-31 | 13,437 | valueError 189, small 12, high 13, heartbeat 3 |

7. Open http://localhost:8080/overview/ot?start=2023-07-01&end=2023-07-31

## Expected results

**The six runs together** (dry run on 2026-10-21):

| Type | SensiPro code | Count | Found by |
|---|---|---|---|
| heartbeat | 1,0 | 34 | meter_quality detector |
| valueError | 2,0 | 2,476 | rejected at ingest |
| zero | 2,1 | 4 | rejected at ingest |
| negative | 2,2 | 0 | — |
| high | 2,3 | 265 | meter_quality detector |
| small | 2,4 | 153 | rejected at ingest |
| **total** | | **2,932** | |

**On the page:**

- **Anomalies:** about 2,930. SensiPro's days are Cyprus time (UTC+3) and the page counts UTC
  days, so anomalies from the first three hours of 1 July fall on 30 June UTC, outside the range.
- **Without a meter or time:** 0.
- **Anomalies per day:** quiet (only `high`) until **18 July**. From then on the line jumps,
  because about 10 meters stop sending usable readings.
- **By type:** valueError is the largest slice.
- **Meters with the most anomalies:** the failing meters (`hm102`, `bw562`, `bw351`, `bw62e2`,
  `bw62e3`, `bw092`, `bw093`, `bw512`, and others) plus the big consumers flagged `high`.
- **Click a meter** (for example `bw562`): its page shows one line per type, from 18 July on.

## Replay

8. On the OT overview, click **▶ Replay**, and set the speed to **4×**. A month replays by day,
   31 steps.
9. **Expected:**
   - the "Anomalies per day" line grows from left to right behind a dashed "now" line;
   - the counters, the pie and the meter bars grow;
   - **Arriving now** lists the newest anomalies;
   - the jump on **18 July** is visible as it happens.
10. When it ends, the page shows all of July again. **⟲ Reset** does the same at any point.

## Cleaning up

These runs add `high` alerts for the big consumers to **Monitor → Triage**. To start over, run
`docker compose down -v`. That deletes all stored data and recreates the seeded UCY
organization on the next `up`. It also deletes the data sources. To recreate SensiPro, go to
**Operate → Data sources** and add `SensiPro meters`: type OT, connection `sensipro`.

---

# Manual test: IT overview (Biology network capture)

> These numbers are for Biology's **real** data only. If the synthetic attack below is inserted,
> remove it first (`python3 scripts/biology_fake_attack.py remove | …`) or use the presentation
> test's numbers instead.

**URL:** http://localhost:8080/overview/it?start=2023-06-20&end=2023-07-22

Needs the Biology database running: in `~/projects/biology`, run `docker compose up -d db`.

Biology holds only **40 flows**: 2 on 20 Jun 2023 and 38 on 22 Jul 2023. They are BACnet
(building automation, UDP 47808), almost all from `192.168.0.140`. With default settings
`flow_burst` finds nothing in them, which is correct for routine traffic, so this test lowers
its fan-out limit to 3 to show it working.

## Steps

1. Go to **Operate → Compose & run**, Step 1.
2. **Data source:** `IT · Biology TCP dump`. The Pull panel appears.
3. Set **From 2023-06-20** and **To 2023-07-22**, leave **Devices** empty, and click **Pull**.
   - **Expected:** *Pulled 40 rows into this source.*
   - **Expected:** the reader is `flow` / `cicflowmeter-csv`.
   - **Expected:** the card notes 2 records that cannot be read and will go to Data health.
     These are the DHCP requests from `0.0.0.0`.
4. **Step 2 · Detection:** click **+ Add detector**, so there are 2.
   - **Detector 1:** `rules`, with lane `flow` and ruleset `packaged`.
   - **Detector 2:** `flow_burst`. Under its parameters, set preset `fan_out` and threshold `3`.
5. **Step 3:** alert engine `weighted`, sink `postgres`. **Step 4:** **Run pipeline**.

## Expected results

**Run receipt:**

| Events | Malformed | Detections | Alerts |
|---|---|---|---|
| 38 | 2 | 6 | 3 |

**The 6 detections,** all on `192.168.0.140` (times UTC):

| When | Detector | Finding |
|---|---|---|
| 06-20 07:35 | rules:flow | Outbound bytes on one flow exceed 10000 |
| 07-22 07:31–07:33 | rules:flow | Outbound bytes on one flow exceed 10000 |
| 07-22 07:33 | flow_burst:fan_out | 4 peers in a 1-minute window |
| 07-22 11:40–11:44 | rules:flow | Outbound bytes on one flow exceed 10000 |
| 07-22 11:40 | flow_burst:fan_out | 5 peers in a 1-minute window |
| 07-22 11:44–11:45 | flow_burst:fan_out | 4 peers in a 1-minute window |

**Monitor → Triage:** 3 new alerts, all MEDIUM on `192.168.0.140`, all
*Exfiltration Over C2 Channel (T1041)*.

| Alert at | Priority |
|---|---|
| 07-22 11:40 | 41.7 |
| 07-22 07:31 | 36.5 |
| 06-20 07:35 | 30.3 |

**IT overview** for 2023-06-20 → 2023-07-22 (33 days, so drawn by day):

| Tile | Expected |
|---|---|
| flows | **38** |
| inside a detection | **32 (84%)** |
| detections | **6** |

- **Detections per day:** a bar of 1 on **20 Jun** and of 5 on **22 Jul**; every other day is 0.
- **By detector:** `rules:flow 3 · flow_burst:fan_out 3`.
- **Flows table:** 38 rows, no cap notice.
- **Data health:** 2 rejected lines, *neither endpoint is inside the monitored network
  (0.0.0.0 -> 255.255.255.255)*.

**One day, by hour:** http://localhost:8080/overview/it?start=2023-07-22&end=2023-07-22

- **Tiles:** 36 flows · 30 inside a detection (83%) · 5 detections.
- **Detections per hour:** 2 at **07:00** and 3 at **11:00**. Flows arrive 18 at 07:00 and
  18 at 11:00.

## Replay

6. On the one-day page, click **▶ Replay**. A day is 24 steps.
7. **Expected:**
   - the bars appear at 07:00 and 11:00 as the dashed "now" line passes them;
   - the flow counters jump at those two hours;
   - **Flows arriving now** lists `192.168.0.140` BACnet flows, for example
     *192.168.0.140:47808 -> 192.168.0.104:47808 udp 72/72 pkt 4320/4617 B*;
   - when it ends, the page shows the whole day again.

## Cleaning up

The run adds 3 alerts to Triage. `docker compose down -v` removes them, as described in the
SensiPro test above.

---

# Presentation: a synthetic attack on the Biology network

**This attack is fake.** `scripts/biology_fake_attack.py` inserts 60 invented flows into
Biology's `packets` table, so the IT charts have an attack to show beside Biology's 40 real,
quiet BACnet flows. Every fake row comes from `192.168.0.66` and has `created_at` =
`2000-01-01 00:00:00`, so `remove` deletes exactly those rows and nothing real.

The story, on **22 Jul 2023 (UTC)**, after the real morning traffic:

| Time | What the attacker `192.168.0.66` does | Caught by |
|---|---|---|
| 13:00 | port-scans the controller `192.168.0.140` (20 ports) | `flow_burst:port_scan` |
| 14:00 | sweeps the BACnet subnet (30 devices in a minute) | `flow_burst:fan_out` |
| 14:30 | telnets out to six outside hosts | `rules` (telnet) |
| 15:30 | uploads 64 MB to an outside host | `flow_burst:exfiltration` + `rules` (upload) |

## Insert and remove

The Biology database must be running: in `~/projects/biology`, run `docker compose up -d db`.

```
# back up first (done on 2026-10-21: ~/projects/biology/backups/packets_before_fake_attack_2026-10-21.sql)
cd ~/projects/biology && docker compose exec -T db sh -c \
  'mysqldump -uroot -p"$MYSQL_ROOT_PASSWORD" laravel packets' > backups/packets_backup.sql

# insert (running it twice does not double the attack)
cd ~/projects/iot-shield && python3 scripts/biology_fake_attack.py insert \
  | (cd ~/projects/biology && docker compose exec -T db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" laravel')

# remove, after the presentation
cd ~/projects/iot-shield && python3 scripts/biology_fake_attack.py remove \
  | (cd ~/projects/biology && docker compose exec -T db sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" laravel')
```

**Expected:** after `insert`, `fake_flows 60`; after `remove`, `fake_flows_left 0`.

## Steps

1. Go to **Operate → Compose & run**. Under **Data source**, choose `IT · Biology TCP dump`, and
   pull **2023-06-20 → 2023-07-22**. **Expected:** *Pulled 100 rows.*
2. **Step 2 · Detection:** 4 detectors.
   - `rules`: lane `flow`, ruleset `packaged`.
   - `flow_burst`: preset `port_scan`.
   - `flow_burst`: preset `fan_out`.
   - `flow_burst`: preset `exfiltration`.

   Leave every threshold at its default: the attack crosses the real limits.
3. **Step 3:** alert engine `weighted`, sink `postgres`. **Step 4:** **Run pipeline**, **once**.

> **Run it only once.** Every run stores its results again, so a second run of the same pull
> doubles every count on the IT overview (on 2026-10-22, three runs showed 228 flows and 16
> detections instead of 96 and 7). To start over, delete UCY's Biology data first:
> its `cicflowmeter-csv` events and rejected lines, and its 2023 flow-lane detections with their
> alerts.

## Expected results

**Run receipt:**

| Events | Malformed | Detections | Alerts |
|---|---|---|---|
| 98 | 2 | 8 | 7 |

**The 8 detections** (times UTC):

| When | Device | Detector | Finding |
|---|---|---|---|
| 06-20 07:35 | 192.168.0.140 | rules:flow | Outbound bytes on one flow exceed 10000 (real) |
| 07-22 07:31 | 192.168.0.140 | rules:flow | same (real) |
| 07-22 11:40 | 192.168.0.140 | rules:flow | same (real) |
| 07-22 13:00 | 192.168.0.66 | flow_burst:port_scan | 20 ports probed in a 1-minute window |
| 07-22 14:00 | 192.168.0.66 | flow_burst:fan_out | 30 peers contacted in a 1-minute window |
| 07-22 14:30 | 192.168.0.66 | rules:flow | the device originated a connection to port 23 (telnet) |
| 07-22 15:30 | 192.168.0.66 | rules:flow | Outbound bytes on one flow exceed 10000 |
| 07-22 15:30 | 192.168.0.66 | flow_burst:exfiltration | 64,000,000 bytes sent in a 30-minute window |

**Monitor → Triage:** 7 alerts.

| Priority | Device | Time | Threat class |
|---|---|---|---|
| 47.4 MEDIUM | 192.168.0.66 | 14:30 | Remote Services (T1021) |
| 30.3 MEDIUM | 192.168.0.66 | 15:30 | Exfiltration Over C2 Channel (T1041) |
| 24.5 LOW | 192.168.0.66 | 14:00 | Remote System Discovery (T1018) |
| 18.9 LOW | 192.168.0.66 | 13:00 | Network Service Discovery (T1046) |
| 30.3 MEDIUM (×3) | 192.168.0.140 | | Exfiltration Over C2 Channel (T1041), Biology's own traffic |

**The day of the attack:** http://localhost:8080/overview/it?start=2023-07-22&end=2023-07-22

| Tile | Expected |
|---|---|
| flows | **96** |
| inside a detection | **88 (92%)** |
| detections | **7** |

| Hour (UTC) | Flows | Detections |
|---|---|---|
| 07:00 | 18 | 1 |
| 11:00 | 18 | 1 |
| 13:00 | 20 | 1 |
| 14:00 | 36 | 2 |
| 15:00 | 4 | 2 |

- **By detector:** `rules:flow 4 · flow_burst:port_scan 1 · flow_burst:fan_out 1 ·
  flow_burst:exfiltration 1`.

**The whole range:** http://localhost:8080/overview/it?start=2023-06-20&end=2023-07-22 shows
98 flows, 90 inside a detection, and 8 detections.

## The presentation: Replay

4. On the one-day page, set the speed to **2×** and click **▶ Replay**.
5. **Expected:**
   - **07:00–11:00:** the real morning BACnet traffic, with one small bar at each hour.
   - **From 13:00:** the attack. The bars climb, 1 → 2 → 2, the counters jump, and
     **Flows arriving now** fills with `192.168.0.66` lines: the scan, then the sweep, the
     port-23 attempts, and finally the 16 MB uploads to 203.0.113.50.
   - When it ends, the whole day shows again.
6. Afterwards, remove the attack (see **Insert and remove**).
   `docker compose down -v` clears the 7 alerts from iot-shield.
