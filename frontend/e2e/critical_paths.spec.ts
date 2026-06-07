// v6 e2e: scan all 16 routes for runtime errors. v5's static audit missed
// factors schema mismatches because it never fetched the live API. This
// spec opens each route, checks for console errors, and reports.
import { test, expect } from "@playwright/test";

const ROUTES = [
  { path: "/", name: "overview" },
  { path: "/market", name: "market-kline" },
  { path: "/paper", name: "paper-trader" },
  { path: "/backtest", name: "backtest" },
  { path: "/live", name: "live-trader" },
  { path: "/factors", name: "factors-list" },
  { path: "/factors/rsi_14", name: "factor-detail" },
  { path: "/factors/gld_tonnes_zscore_60d", name: "factor-detail-healthy" },
  { path: "/discover", name: "discover" },
  { path: "/sync", name: "sync" },
  { path: "/tuning", name: "tuning" },
  { path: "/calibrator", name: "calibrator" },
  { path: "/shadow", name: "shadow" },
  { path: "/ab", name: "ab" },
  { path: "/reports", name: "reports" },
  { path: "/config", name: "config" },
  { path: "/jobs", name: "jobs" },
  { path: "/login", name: "login" },
];

test.describe("v6: each route renders without runtime errors", () => {
  for (const route of ROUTES) {
    test(`${route.name} (${route.path})`, async ({ page }) => {
      const errors: string[] = [];
      const pageErrors: string[] = [];

      page.on("pageerror", (err) => pageErrors.push(err.message));
      page.on("console", (msg) => {
        if (msg.type() === "error") errors.push(msg.text());
      });

      const response = await page.goto(`http://localhost:3000${route.path}`, {
        waitUntil: "networkidle",
        timeout: 15_000,
      });
      // Wait for hydration to finish so any client-side render errors surface.
      await page.waitForTimeout(2_000);

      // Take a screenshot of the final state to help debug.
      await page.screenshot({
        path: `frontend/test-results/v6-${route.name}.png`,
        fullPage: true,
      });

      // Don't fail on 404 since /backtest is the new v5 route and may 404 if
      // backend issues; we only care that the JS bundle didn't throw.
      expect(
        pageErrors,
        `Route ${route.path} threw a pageerror: ${pageErrors.join(" | ")}`
      ).toEqual([]);
      // Filter known-noisy console errors that are not real bugs.
      const realErrors = errors.filter(
        (e) =>
          !e.includes("Download the React DevTools") &&
          !e.includes("Fast Refresh")
      );
      expect(
        realErrors,
        `Route ${route.path} had console errors: ${realErrors.join(" | ")}`
      ).toEqual([]);
    });
  }
});
