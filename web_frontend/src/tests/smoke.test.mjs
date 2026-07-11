import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";

const requiredFiles = [
  "src/main.tsx",
  "src/App.tsx",
  "src/main.css",
  "src/api/client.ts",
  "src/api/domains/readiness.ts",
  "src/api/domains/system.ts",
  "src/contexts/AuthContext.tsx",
  "src/hooks/useLiveState.ts",
  "src/pages/LoginPage.tsx",
  "src/pages/OverviewPage.tsx",
  "src/pages/TradingPage.tsx",
  "src/pages/PnlPage.tsx",
  "src/pages/RiskPage.tsx",
  "src/pages/OpsPage.tsx",
  "src/pages/LearningPage.tsx",
  "src/pages/ModelsPage.tsx",
  "src/pages/V15CockpitPage.tsx",
  "src/pages/V16BrainPage.tsx",
  "src/pages/WorkspacePages.tsx",
  "src/styles/console.css",
];

let fail = false;

for (const relative of requiredFiles) {
  const full = path.join(process.cwd(), relative);
  if (!fs.existsSync(full)) {
    console.error(`缺少文件: ${relative}`);
    fail = true;
  }
}

if (fail) {
  process.exit(1);
}

const formatSource = fs.readFileSync(path.join(process.cwd(), "src/lib/format.ts"), "utf8");
assert.match(formatSource, /normalizeCurrency/);
assert.match(formatSource, /\^\[A-Z\]\{3\}\$/);
assert.match(formatSource, /catch \{/);

const apiSource = fs.readFileSync(path.join(process.cwd(), "src/api/client.ts"), "utf8");
assert.match(apiSource, /confirmed\s*\?\s*\{\s*"X-Confirm":\s*"start-live"\s*\}/);
assert.match(apiSource, /confirmed\s*\?\s*\{\s*"X-Confirm":\s*"emergency"\s*\}/);
assert.match(apiSource, /getReplayLatest/);
assert.match(apiSource, /getV15Phase0/);

const tradingSource = fs.readFileSync(path.join(process.cwd(), "src/pages/TradingPage.tsx"), "utf8");
assert.match(tradingSource, /startTrading\("ctrader",\s*strategy\s*\|\|\s*"live",\s*true\)/);
assert.match(tradingSource, /emergencyClose\(true\)/);

const appSource = fs.readFileSync(path.join(process.cwd(), "src/App.tsx"), "utf8");
assert.match(appSource, /path="\/v15"/);
assert.match(appSource, /path="\/v16"/);
assert.match(appSource, /lazy\(\(\) => import/);

const v15Source = fs.readFileSync(path.join(process.cwd(), "src/pages/V15CockpitPage.tsx"), "utf8");
assert.match(v15Source, /order_outcome_causality_replay/);
assert.match(v15Source, /supervisor_counterfactual_replay/);

console.log("web_frontend smoke test: ok");
