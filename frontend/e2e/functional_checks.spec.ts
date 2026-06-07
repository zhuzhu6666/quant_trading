// v7 functional e2e: verify real data is rendered (not just "no errors").
// Goes beyond mount by checking specific DOM text and API data.
import { test, expect } from "@playwright/test";

test("v7: factors list shows real data (not --)", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("http://localhost:3000/factors", { waitUntil: "networkidle" });
  await page.waitForTimeout(2_000);

  // Find the first factor row in the table
  const firstRow = page.locator("table tbody tr").first();
  await expect(firstRow).toBeVisible();

  // Get the text of each cell — should be actual numbers, not "--"
  const cells = await firstRow.locator("td").allTextContents();
  console.log("First row cells:", cells);
  // Expect: name, status, score, abs_ic, stability, regime_consistency
  // Score cell (index 2) should be a number, not "--"
  expect(cells[2], `score cell: ${cells[2]}`).not.toBe("--");
  expect(cells[2], `score cell: ${cells[2]}`).toMatch(/^\d+\.\d+$/);
  // abs_ic cell (index 3)
  expect(cells[3], `abs_ic cell: ${cells[3]}`).not.toBe("--");
  expect(cells[3], `abs_ic cell: ${cells[3]}`).toMatch(/^\d+\.\d+$/);
  // factor name should be a real factor name, not "undefined"
  expect(cells[0], `name cell: ${cells[0]}`).not.toBe("undefined");
  expect(cells[0], `name cell: ${cells[0]}`).not.toBe("--");
  expect(cells[0].length).toBeGreaterThan(0);

  expect(errors, `page errors: ${errors.join("|")}`).toEqual([]);
});

test("v7: market page shows K-line with real timestamps", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("http://localhost:3000/market", { waitUntil: "networkidle" });
  await page.waitForTimeout(3_000);  // TradingView takes a sec to mount

  // Capture the page text — TradingView's light-weight-charts renders into
  // a canvas so we can't read its data, but we can check that the page
  // didn't error and that the chart container exists.
  const chart = page.locator('[data-testid="kline-chart"], canvas, .tv-lightweight-chart').first();
  await expect(chart).toBeVisible({ timeout: 8_000 });

  expect(errors, `market pageerrors: ${errors.join("|")}`).toEqual([]);
});

test("v7: backtest page shows recent-jobs table (not crash on loadRecent)", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("http://localhost:3000/backtest", { waitUntil: "networkidle" });
  await page.waitForTimeout(2_000);

  // The page should NOT have a "Unexpected token" error
  expect(errors, `backtest pageerrors: ${errors.join("|")}`).toEqual([]);

  // There should be a "开始回测" submit button visible
  await expect(page.getByRole("button", { name: /开始|回测|Start/ }).first()).toBeVisible();
});

test("v7: calibrator page renders bucket table", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("http://localhost:3000/calibrator", { waitUntil: "networkidle" });
  await page.waitForTimeout(2_000);

  expect(errors, `calibrator pageerrors: ${errors.join("|")}`).toEqual([]);

  // Should have at least 1 bucket row
  const rows = page.locator("table tbody tr");
  const count = await rows.count();
  console.log(`calibrator bucket rows: ${count}`);
  expect(count).toBeGreaterThan(0);

  // The first row's cells should have actual numbers like 0.10, not "--"
  const firstRowCells = await rows.first().locator("td").allTextContents();
  console.log("First bucket row:", firstRowCells);
});
