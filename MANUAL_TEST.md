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
