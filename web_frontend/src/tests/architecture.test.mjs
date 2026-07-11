import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";

const read = (relative) => fs.readFileSync(path.join(process.cwd(), relative), "utf8");
const app = read("src/App.tsx");
const workspaces = read("src/pages/WorkspacePages.tsx");
const pages = ["OverviewPage", "LearningPage", "ModelsPage", "RiskPage", "OpsPage", "V15CockpitPage", "V16BrainPage"]
  .map((name) => read(`src/pages/${name}.tsx`));
const dashboardBits = read("src/components/DashboardBits.tsx");
const cssEntry = read("src/main.css");
const tokens = read("src/styles/tokens.css");
const consoleCss = read("src/styles/console.css");
const apiClient = read("src/api/client.ts");
const systemApi = read("src/api/domains/system.ts");
const learningPage = read("src/pages/LearningPage.tsx");
const presentationSources = pages.join("\n") + read("src/pages/TradingPage.tsx") + read("src/pages/PnlPage.tsx") + workspaces;

assert.equal((app.match(/<ProtectedAppLayout/g) ?? []).length, 1, "受保护页面应共用一个布局壳");
assert.ok((app.match(/lazy\(\(\) => import/g) ?? []).length >= 6, "一级页面应按路由懒加载");
assert.ok((workspaces.match(/lazy\(\(\) => import/g) ?? []).length >= 6, "合并工作区的二级页面应继续懒加载");
assert.match(app, /\/performance\/:section/);
assert.match(app, /\/governance\/:section/);
assert.match(app, /\/autonomy\/:section/);
assert.match(dashboardBits, /export function CompactMetric/);
assert.match(dashboardBits, /export function SectionHead/);
assert.doesNotMatch(pages[0], /function (StatTile|Field|toneFromStatus|numberTone)\b/);
assert.doesNotMatch(pages.join("\n"), /function (LearningMiniMetric|ModelMiniMetric|RiskMiniMetric|OpsMiniMetric|CompactMetric|SectionHead)\b/);
assert.doesNotMatch(pages.join("\n"), /\["backend-readiness",|\["ops-backend-readiness"\]|\["v1[56]", "readiness"\]/);
assert.equal((tokens.match(/:root\s*\{/g) ?? []).length, 1, "设计 token 必须只有一个事实源");
assert.equal((cssEntry.match(/@import/g) ?? []).length, 7, "样式入口应只负责编排分层样式");
assert.match(cssEntry, /console\.css/);
for (const metricGrid of ["learning-mini-grid", "model-mini-grid", "risk-mini-grid", "overview-mini-grid", "ops-mini-grid", "v15-mini-grid"]) {
  assert.match(consoleCss, new RegExp(`\\.${metricGrid}`), `${metricGrid} 必须受紧凑网格基线约束`);
}
assert.match(consoleCss, /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(168px,\s*1fr\)\)/);
assert.match(consoleCss, /grid-auto-rows:\s*min-content/);
assert.match(consoleCss, /align-items:\s*start/);
assert.doesNotMatch(apiClient, /export (async function|const) get(SystemLoad|SystemDbHealth|OpsAlerts)/);
assert.match(systemApi, /getSystemLoad/);
assert.match(learningPage, /learning_effect_quality/);
assert.match(learningPage, /重试资格只在出现新复盘证据/);
assert.match(learningPage, /看板不会自动改权重、参数或智能体权限/);
assert.match(learningPage, /experience_prior/);
assert.match(learningPage, /awe_mutation_coverage/);
assert.match(learningPage, /runtime_factor_budget/);
assert.doesNotMatch(presentationSources, /"--"/, "展示层不得用双横线伪造缺失字段");

console.log("web_frontend architecture test: ok");
