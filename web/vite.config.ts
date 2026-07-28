/// <reference types="vitest" />
import path from "node:path";

import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// D1 — the SPA is a *same-origin* client. It only ever calls relative `/api/*` and
// `/pipeline/*` paths; who is behind those prefixes is the server's business (nginx in
// production, this proxy in dev). That is what keeps the two FastAPI apps free of CORS,
// the stance their docstrings deliberately take, and what removes per-environment API
// URLs from the frontend entirely.
const READ_API = process.env.IOTSHIELD_API_URL ?? "http://127.0.0.1:8000";
const PIPELINE_API = process.env.IOTSHIELD_PIPELINE_API_URL ?? "http://127.0.0.1:8100";

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: { "@": path.resolve(__dirname, "./src") },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": {
        target: READ_API,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/api/, ""),
      },
      "/pipeline": {
        target: PIPELINE_API,
        changeOrigin: true,
        rewrite: (p) => p.replace(/^\/pipeline/, ""),
        // Model detectors and benchmarks take real time — the old ControlClient allowed
        // 120 s, so the dev proxy must not give up sooner than the app expects.
        timeout: 600_000,
        proxyTimeout: 600_000,
      },
    },
  },
  build: {
    // vega + vega-lite are large by nature; the warning is noise, not a finding.
    chunkSizeWarningLimit: 2000,
  },
  test: {
    globals: true,
    environment: "jsdom",
    // `http://localhost/` so the app's relative `/api` and `/pipeline` paths resolve to the
    // same origins the MSW handlers register — the SPA never names a host (D1).
    environmentOptions: { jsdom: { url: "http://localhost/" } },
    setupFiles: ["./src/test/polyfills.ts", "./src/test/setup.ts"],
    css: false,
    restoreMocks: true,
    // Serial: parallel workers race on Vite's transform cache under Windows' file locking
    // (observed as EBUSY on the cache file, which silently dropped whole test FILES from the
    // run). A green suite that quietly skipped two files is worse than a slower one.
    fileParallelism: false,
  },
});
