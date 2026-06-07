import { test, expect } from "@playwright/test";

// Mock /api/factor-health/latest so we don't need to actually run the eval
const FAKE_FACTOR_REPORT = {
  report: {
    healthy: 2,
    watch: 45,
    decaying: 18,
    factors: [
      { name: "gld_tonnes_zscore_60d", status: "HEALTHY", score: 95.2, abs_ic: 0.0359, stability: 0.9, decay: 0.85, regime_consistency: 0.85, independence: 0.72 },
      { name: "cot_mm_net_pct_oi", status: "HEALTHY", score: 83.8, abs_ic: 0.0334, stability: 0.8, decay: 0.75, regime_consistency: 0.75, independence: 0.68 },
      { name: "rsi_14", status: "DECAYING", score: 23.5, abs_ic: 0.0001, stability: 0.1, decay: 0.1, regime_consistency: 0.15, independence: 0.30 },
    ],
  },
  report_path: "data/charts/factor_health_report.json",
};

test.beforeEach(async ({ page }) => {
  // Mock factor health latest (fast)
  await page.route("**/api/factor-health/latest", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(FAKE_FACTOR_REPORT) });
  });
  // Mock factor health run (so click doesn't actually start a long job)
  await page.route("**/api/factor-health/run", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ job_id: "mock_job", status: "queued" }) });
  });
});

test("overview page loads and shows 6 cards", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByText("总览")).toBeVisible();
  await expect(page.getByText("账户权益")).toBeVisible();
  await expect(page.getByText("今日 PnL")).toBeVisible();
  await expect(page.getByText("当前持仓")).toBeVisible();
  await expect(page.getByText("风控")).toBeVisible();
  await expect(page.getByText("回测")).toBeVisible();
  await expect(page.getByText("时间")).toBeVisible();
});

test("factor health page shows 2 HEALTHY 45 WATCH 18 DECAYING", async ({ page }) => {
  await page.goto("/factors");
  await expect(page.getByText("2 HEALTHY")).toBeVisible();
  await expect(page.getByText("45 WATCH")).toBeVisible();
  await expect(page.getByText("18 DECAYING")).toBeVisible();
  // Factor rows
  await expect(page.getByText("gld_tonnes_zscore_60d")).toBeVisible();
});

test("config page allows YAML edit and save", async ({ page }) => {
  // Mock initial get
  await page.route("**/api/config", (route) => {
    if (route.request().method() === "GET") {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ yaml: "test: 1\n", parsed: { test: 1 }, path: "config/settings.yaml", exists: true }) });
    } else if (route.request().method() === "PUT") {
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ok: true, changes: ["test: 1 → 2"], path: "config/settings.yaml" }) });
    } else {
      route.continue();
    }
  });
  await page.goto("/config");
  await expect(page.getByText("配置 (settings.yaml)")).toBeVisible();
  // Edit textarea
  const textarea = page.locator("textarea").first();
  await textarea.fill("test: 2\n");
  // Save
  await page.getByRole("button", { name: "保存" }).click();
  await expect(page.getByText(/已保存/)).toBeVisible({ timeout: 5000 });
});

test("paper page renders with start/stop buttons", async ({ page }) => {
  // Mock status
  await page.route("**/api/paper/status", (route) => {
    route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ status: "stopped" }) });
  });
  await page.goto("/paper");
  await expect(page.getByText("模拟盘")).toBeVisible();
  await expect(page.getByRole("button", { name: "▶ 启动" })).toBeVisible();
  await expect(page.getByRole("button", { name: "⏹ 停止" })).toBeVisible();
  await expect(page.getByRole("button", { name: "⏮ 紧急停止" })).toBeVisible();
});
