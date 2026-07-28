// Walk the running stack and capture the design review (brief §7's self-review loop).
//
//   docker compose up -d --build
//   docker compose run --rm runner python -m iotshield.pipeline \
//       --input=data/demo/demo_triage.csv --config=config/demo_triage.yaml
//   node web/scripts/screenshots.mjs
//
// Not a test: it asserts nothing beyond "the page reached this state", and it drives the
// REAL stack rather than MSW, which is exactly what the mocked suite cannot prove.
import { mkdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { chromium } from "playwright";

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const shots = path.resolve(web, "..", "docs", "react-rewrite", "screenshots");
// 127.0.0.1 rather than localhost: compose publishes this service on IPv4 only, and a
// browser that resolves localhost to ::1 gets a connection refusal mid-walkthrough.
const BASE = process.env.IOTSHIELD_WEB_URL ?? "http://127.0.0.1:8080";

mkdirSync(shots, { recursive: true });

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } });

const shot = async (name) => {
  await page.waitForTimeout(600); // let charts finish their first render
  await page.screenshot({ path: path.join(shots, `${name}.png`), fullPage: true });
  console.log(`captured ${name}`);
};

const theme = async (want) => {
  const current = await page.getAttribute("html", "data-theme");
  if (current !== want) {
    await page.getByRole("button", { name: /Switch to (light|dark) theme/ }).click();
    await page.waitForTimeout(200);
  }
};

// `node web/scripts/screenshots.mjs empty` right after `docker compose up -d` captures the
// state every fresh stack starts in — the empty states are a shipped feature, not an
// accident, so they are part of the review.
if (process.argv[2] === "empty") {
  await page.goto(`${BASE}/`);
  await page.getByText(/No alerts yet/).waitFor({ timeout: 30_000 });
  await shot("00a-triage-empty");
  await page.goto(`${BASE}/assets`);
  await page.getByText(/No assets yet/).waitFor();
  await shot("00b-assets-empty");
  await page.goto(`${BASE}/data-health`);
  await page.getByText("No malformed inputs recorded.").waitFor();
  await shot("00c-data-health-empty");
  await browser.close();
  console.log(`screenshots in ${shots}`);
  process.exit(0);
}

// --- Monitor -> Triage --------------------------------------------------------
await page.goto(`${BASE}/`);
await page.getByRole("heading", { name: "Alert triage" }).waitFor();
await page.getByRole("heading", { name: "Ranked queue" }).waitFor();
await theme("light");
await shot("01-triage-light");
await theme("dark");
await shot("02-triage-dark");
await theme("light");

// The per-detection evidence payload — the thing that makes a finding actionable.
await page.selectOption("select", { index: 0 });
const why = page.getByRole("button", { name: /why it fired/ }).first();
if (await why.count()) {
  await why.click();
  await shot("03-triage-evidence");
}

// --- Monitor -> Assets & telemetry -------------------------------------------
await page.goto(`${BASE}/assets`);
await page.getByRole("heading", { name: "Assets & telemetry" }).waitFor();
const assetPicker = page.locator("select").first();
const assets = await assetPicker.locator("option").allTextContents();
const preferred = assets.find((label) => /boiler-pump|reactor-pump|10\.0\.0\.24/.test(label));
if (preferred) {
  await assetPicker.selectOption({ label: preferred });
}
await page.waitForTimeout(800);
await shot("04-assets");

// --- Monitor -> Data health ---------------------------------------------------
await page.goto(`${BASE}/data-health`);
await page.getByRole("heading", { name: "Data health" }).waitFor();
await shot("05-data-health");

// --- Operate -> Compose & run -------------------------------------------------
await page.goto(`${BASE}/compose`);
await page.getByRole("heading", { name: "Compose & run a pipeline" }).waitFor();
await page.getByText("Chain is compatible.").waitFor({ timeout: 20_000 });
await shot("06-compose");

await page
  .getByPlaceholder("CSV for stub, log lines for syslog.")
  .fill(
    [
      "2026-07-08T09:00:00+00:00,demo-pump,Pressure,42.0",
      "2026-07-08T09:00:30+00:00,demo-pump,Pressure,910.0",
      "2026-07-08T09:01:00+00:00,demo-pump,Pressure,44.0",
    ].join("\n"),
  );
await page.getByRole("button", { name: "Run pipeline" }).click();
await page.getByText("Run complete.").waitFor({ timeout: 60_000 });
await shot("07-compose-run-receipt");

// --- Evaluate -----------------------------------------------------------------
await page.goto(`${BASE}/evaluate`);
await page.getByRole("heading", { name: /Evaluate/ }).waitFor();
await page.getByText("Bench specs").waitFor({ timeout: 20_000 });
await page.selectOption("#eval_corpus", "synthetic");
await page.waitForTimeout(500);
await page.getByRole("button", { name: /Spec sent to the API/ }).click();
await shot("08-evaluate-committed-spec");

await page.getByRole("button", { name: "Run benchmark" }).click();
await page.getByRole("heading", { name: "Head to head" }).waitFor({ timeout: 180_000 });
await shot("09-evaluate-report");
await theme("dark");
await shot("10-evaluate-report-dark");
await theme("light");

// Compose mode: entries, the two weights modes, and a sweep.
await page.getByRole("radio", { name: "Compose my own" }).click();
await page.locator('[data-testid="eval_entry_n_add"]').waitFor({ timeout: 30_000 });

// The page opens entry 1 on `meter_iforest` — the first telemetry detector the registry
// reports — which is *untrained by default*, so it is a blocker until an artifact is named
// and it contributes nothing to the sweep menu (an artifact is atomic). Swap it for the rule
// detector to show the sweep, and put the learning detector in entry 2 to show both modes.
const entry0 = page.locator('[data-testid="eval_entry_0"]');
const entry1 = page.locator('[data-testid="eval_entry_1"]');
await entry0.getByRole("checkbox", { name: "stub_threshold", exact: true }).check();
await entry0.getByRole("checkbox", { name: "meter_iforest", exact: true }).uncheck();
await entry1.getByRole("checkbox", { name: "team3_zscore", exact: true }).check();
const strayInEntry1 = entry1.getByRole("checkbox", { name: "stub_threshold", exact: true });
if (await strayInEntry1.isChecked()) await strayInEntry1.uncheck();
await page.waitForTimeout(300);

await page.selectOption("#eval_e0_sw", "threshold");
await page.fill("#eval_e0_swv", "40, 80, 120");
await entry1.getByRole("button", { name: /team3_zscore — hyperparameters/ }).click();
await shot("11-evaluate-compose-entries");

await browser.close();
console.log(`screenshots in ${shots}`);
