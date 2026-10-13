# Manual test: port scan + SSH brute force

## Steps

1. Open http://localhost:8080 and sign in.
2. Go to **Operate → Compose & run**.

### Step 1 · Data

3. Click **+ Add source** so there are 2 sources.
4. **Source 1:**
   - Normalizer: `flow`
   - Input format: `zeek-conn`. Change it: the default is `flow-csv`.
   - Upload a file:
     `\\wsl.localhost\Ubuntu-20.04\home\erotokritos\projects\iot-shield\data\samples\portscan_flow.log`
5. **Source 2:**
   - Normalizer: `syslog`
   - Upload a file:
     `\\wsl.localhost\Ubuntu-20.04\home\erotokritos\projects\iot-shield\data\samples\bruteforce_syslog.log`
6. Click **Next: Detection**.

### Step 2 · Detection

7. Click **+ Add detector** so there are 2 detectors.
8. **Detector 1:** `flow_burst`. Under *flow_burst parameters*, set preset = `port_scan`.
9. **Detector 2:** `log_burst`. Under *log_burst parameters*, set preset = `auth_failure`.
10. Click **Next: Alerts & output**.

### Step 3 · Alerts & output

11. Alert engine: `weighted`. Leave its parameters at their defaults.
12. Sinks: `postgres`, which is already ticked.
13. Click **Next: Review & run**.

### Step 4 · Review & run

14. Click **Run pipeline**.
15. Go to **Monitor → Triage**.

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
