import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  timeout: 60_000,
  expect: { timeout: 5_000 },
  fullyParallel: false,
  workers: 1,
  reporter: "list",
  use: {
    baseURL: "http://localhost:3000",
    trace: "on-first-retry",
    screenshot: "only-on-failure",
  },
  projects: [
    // v7: prefer system Chrome (already installed, ~no extra download) over
    // the bundled chromium that requires playwright.azureedge.net (often
    // unreachable in this user's network). Falls back to chromium if Chrome
    // is missing — see README.
    { name: "chrome", use: { ...devices["Desktop Chrome"], channel: "chrome" } },
    { name: "chromium", use: { ...devices["Desktop Chrome"] } },
  ],
  // Backend must be running on :8000 + frontend on :3000 before tests.
  // The script `start-e2e.sh` (in project root) handles this.
  webServer: [
    { command: "python -m backend --port 8000", url: "http://localhost:8000/api/health", reuseExistingServer: true, timeout: 10_000 },
    { command: "npm run dev", url: "http://localhost:3000", reuseExistingServer: true, timeout: 30_000 },
  ],
});
