import fs from "node:fs";
import path from "node:path";
import assert from "node:assert/strict";

const read = (relative) => fs.readFileSync(path.join(process.cwd(), relative), "utf8");
const app = read("src/App.tsx");
const workspaces = read("src/pages/WorkspacePages.tsx");
const pages = ["OverviewPage", "LearningPage", "ModelsPage", "RiskPage", "OpsPage", "EvidencePage", "V16BrainPage"]
  .map((name) => read(`src/pages/${name}.tsx`));
const dashboardBits = read("src/components/DashboardBits.tsx");
const cssEntry = read("src/main.css");
const tokens = read("src/styles/tokens.css");
const consoleCss = read("src/styles/console.css");
const apiClient = read("src/api/client.ts");
const systemApi = read("src/api/domains/system.ts");
const learningPage = read("src/pages/LearningPage.tsx");
const modelsPage = read("src/pages/ModelsPage.tsx");
const v16Page = read("src/pages/V16BrainPage.tsx");
const v16Views = read("src/features/v16/V16BrainViews.tsx");
const tradingPage = read("src/pages/TradingPage.tsx");
const overviewPage = read("src/pages/OverviewPage.tsx");
const pnlPage = read("src/pages/PnlPage.tsx");
const opsPage = read("src/pages/OpsPage.tsx");
const riskPage = read("src/pages/RiskPage.tsx");
const evidencePage = read("src/pages/EvidencePage.tsx");
const queryErrorList = read("src/components/QueryErrorList.tsx");
const autonomyDecoder = read("src/api/fact.ts");
const riskDecoder = read("src/api/riskSnapshot.ts");
const presentationSources = pages.join("\n") + v16Views + tradingPage + read("src/pages/PnlPage.tsx") + workspaces;

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
for (const metricGrid of ["learning-mini-grid", "model-mini-grid", "risk-mini-grid", "overview-mini-grid", "ops-mini-grid", "brain-mini-grid"]) {
  assert.match(consoleCss, new RegExp(`\\.${metricGrid}`), `${metricGrid} 必须受紧凑网格基线约束`);
}
assert.match(consoleCss, /grid-template-columns:\s*repeat\(auto-fit,\s*minmax\(168px,\s*1fr\)\)/);
assert.match(consoleCss, /grid-auto-rows:\s*min-content/);
assert.match(consoleCss, /align-items:\s*start/);
assert.doesNotMatch(apiClient, /export (async function|const) get(SystemLoad|SystemDbHealth|OpsAlerts)/);
assert.match(systemApi, /getSystemLoad/);
assert.match(learningPage, /learning_effect_quality/);
assert.match(learningPage, /只有出现新的复盘证据/);
assert.match(learningPage, /看板不会自动改权重、参数或智能体权限/);
assert.match(learningPage, /experience_prior/);
assert.match(learningPage, /awe_mutation_coverage/);
assert.match(learningPage, /runtime_factor_budget/);
assert.match(learningPage, /readFact\(readinessQuery\.data, "ops\.backend-readiness\.v2"\)/);
assert.match(learningPage, /const readinessRequestFailed = readinessQuery\.isError \|\| readinessQuery\.isRefetchError/);
assert.match(learningPage, /const hasError = learningQueries\.some\(\(query\) => query\.isError \|\| query\.isRefetchError\)/);
assert.match(learningPage, /const learningFactsKnown = readinessKnown && \[/);
for (const contract of [
  "learning.summary.v2",
  "learning.suggestions.v2",
  "learning.applications.v2",
  "learning.lifecycle.v2",
  "learning.reviews.v2",
  "learning.autonomous-samples.v2",
]) {
  assert.match(learningPage, new RegExp(`readFact\\([^)]*"${contract.replaceAll(".", "\\.")}"`), `Learning ${contract} 必须按端点契约校验`);
}
assert.match(learningPage, /className="dashboard learning-dashboard"/);
assert.doesNotMatch(learningPage, /learning-dashboard \$\{learningFactsKnown \? "" : "fact-unverified"\}/, "单个学习接口不得把整页正常卡片染黄");
assert.match(learningPage, /部分数据更新中/);
assert.match(learningPage, /部分数据待确认/);
for (const queryName of ["lifecycle", "reviews", "samples", "backend-readiness"]) {
  assert.match(learningPage, new RegExp(`label: "${queryName}"`), `Learning ${queryName} 错误必须可见`);
}
assert.match(modelsPage, /readFact\(readinessQuery\.data, "ops\.backend-readiness\.v2"\)/);
assert.match(modelsPage, /const readinessRequestFailed = readinessQuery\.isError \|\| readinessQuery\.isRefetchError/);
assert.match(modelsPage, /const hasError = modelQueries\.some\(\(query\) => query\.isError \|\| query\.isRefetchError\)/);
assert.match(modelsPage, /const modelFactsKnown = readinessKnown && \[/);
assert.match(modelsPage, /className="dashboard models-dashboard"/);
assert.doesNotMatch(modelsPage, /models-dashboard \$\{modelFactsKnown \? "" : "fact-unverified"\}/, "单个模型接口不得把整页正常卡片染黄");
assert.match(modelsPage, /部分数据更新中/);
assert.match(modelsPage, /部分数据待确认/);
for (const queryName of ["factorAdvisories", "metaAdvisories", "highLoadAudits"]) {
  assert.match(modelsPage, new RegExp(`${queryName}Query,`), `Models ${queryName} 也必须参与事实与错误边界`);
}
for (const queryName of ["position-quality/audits", "open-quality/audits", "canary-reviews", "inference-audits", "dataset-quality"]) {
  assert.match(modelsPage, new RegExp(`label: "${queryName}"`), `Models ${queryName} 错误必须可见`);
}
for (const contract of [
  "learning.dataset-readiness.v2",
  "learning.model-meta-lightgbm-shadow-report.v2",
  "learning.model-meta-lightgbm-audits.v2",
  "learning.model-position-quality-audits.v2",
  "learning.model-open-quality-audits.v2",
  "learning.factor-governance-lightgbm-audits.v2",
  "learning.factor-governance-lightgbm-advisories.v2",
  "learning.model-shadow-queue.v2",
  "learning.model-canary-reviews.v2",
  "learning.model-inference-audits.v2",
  "learning.model-permission-audits.v2",
  "learning.model-meta-advisories.v2",
  "learning.model-offmarket-high-load-audits.v2",
  "learning.dataset-quality-health.v2",
]) {
  assert.match(modelsPage, new RegExp(`readFact\\([^)]*"${contract.replaceAll(".", "\\.")}"`), `Models ${contract} 必须按端点契约校验`);
}
assert.doesNotMatch(presentationSources, /"--"/, "展示层不得用双横线伪造缺失字段");
assert.match(v16Page, /decodeLiveAutonomyStatus/);
assert.match(v16Page, /<FactBoundary fact=\{liveAutonomyView\.fact\}/);
assert.match(v16Page, /readFact\(readinessQuery\.data, "ops\.backend-readiness\.v2"\)/);
assert.match(v16Page, /const readinessRequestFailed = readinessQuery\.isError \|\| readinessQuery\.isRefetchError/);
assert.match(v16Page, /liveAutonomyQuery\.isError \|\| liveAutonomyQuery\.isRefetchError/);
assert.match(v16Page, /<MetricCard title="实盘自治"[\s\S]*?<FactBoundary fact=\{liveAutonomyView\.fact\}/, "实盘自治控制必须位于 Fact 边界内");
assert.match(v16Page, /className=\{proposalRegistryKnown \? "" : "fact-unverified"\}/);
assert.match(v16Page, /className=\{liveReadyGuardrailKnown \? "" : "fact-unverified"\}/);
assert.doesNotMatch(
  v16Page,
  /Fact\s*=.*:\s*readinessFact/,
  "V16 fallback display data must not inherit the backend-readiness endpoint fact",
);
for (const contract of [
  "ops.v16-brain-state.v2",
  "ops.v16-brain-memory.v2",
  "ops.v16-action-plans.v2",
  "ops.v16-action-plan-evals.v2",
  "ops.v16-low-impact-executions.v2",
  "ops.v16-medium-impact-governance.v2",
  "ops.v16-governance-candidate-reviews.v2",
  "ops.v16-live-ready-guardrails.v2",
  "ops.autonomy-proposals.v2",
]) {
  assert.match(v16Page, new RegExp(`readFact\\([^)]*"${contract.replaceAll(".", "\\.")}"`), `V16 ${contract} 必须按端点契约校验`);
}
assert.doesNotMatch(consoleCss, /\.fact-boundary-stale \.status-ok/, "过期事实不得静默篡改业务状态颜色");
assert.doesNotMatch(consoleCss, /\.fact-unverified \.status-ok/, "待确认事实不得静默把正常状态改成黄色");
assert.match(consoleCss, /\.status-pending/);
assert.match(dashboardBits, /tone === "pending" \? "数据待确认"/);
assert.match(queryErrorList, /query\.isError \|\| query\.isRefetchError/);
assert.match(queryErrorList, /query\.isRefetchError \? "刷新失败，当前显示缓存数据" : "请求失败"/);
assert.match(opsPage, /const logsRequestFailed = logsQuery\.isError \|\| logsQuery\.isRefetchError/);
assert.match(opsPage, /\{ label: "logs", query: logsQuery \}/);
assert.doesNotMatch(v16Page, /tone="ok"/, "V16 固定绿灯必须改为事实绑定");
assert.match(v16Views, /requires_control_gate:\s*"需通过治理审核"/);
assert.match(v16Views, /no_execution_authority:\s*"无执行权限"/);
assert.match(v16Page, /label="一次解锁"[\s\S]*?disabled=\{liveUnlockMutation\.isPending \|\| !unlockAllowed\}/);
assert.match(v16Page, /label="撤销自治"[\s\S]*?disabled=\{liveRevokeMutation\.isPending\}/);
assert.doesNotMatch(v16Page, /pickBoolean\(liveAutonomyEvaluation, \["ok"\]/);
assert.doesNotMatch(v16Page, /readinessLiveAutonomy/);
assert.match(autonomyDecoder, /LIVE_AUTONOMY_STATUS_CONTRACT/);
assert.match(autonomyDecoder, /factIsKnown\(fact, requestFailed\)/);
assert.doesNotMatch(autonomyDecoder, /pick\(|pickValue|WRAPPER_KEYS/);
assert.match(autonomyDecoder, /export function factBoundTone/);
assert.match(tradingPage, /readFact\(strategyStatusQuery\.data, "live\.strategy\.v2"\)/);
assert.match(tradingPage, /readFactComponent\(riskQuery\.data, "risk_inputs", "risk\.inputs\.v1"\)/);
assert.match(tradingPage, /decodeCanonicalRiskSnapshot\(riskQuery\.data\)/);
assert.match(tradingPage, /const loopKnown = factIsKnown\(loopFact, loopRequestFailed\)/);
assert.match(tradingPage, /const positionsKnown = factIsKnown\(positionsViewFact, positionsViewRequestFailed\)/);
assert.match(tradingPage, /tone=\{factBoundTone\(strategyFact, gatePassed \? "ok" : "warn", strategyRequestFailed\)\}/);
assert.doesNotMatch(tradingPage, /tone=\{loopRunning \? "ok" : "warn"\}/, "循环旧值不得绕过 loop Fact 变绿");
assert.doesNotMatch(tradingPage, /tone=\{gatePassed \? "ok" : "warn"\}/, "策略旧值不得绕过 strategy Fact 变绿");
assert.match(tradingPage, /label="停止"[\s\S]*?disabled=\{stopBusy\}/, "Fact 未知不得禁用停止");
assert.match(tradingPage, /label="紧急平仓"[\s\S]*?disabled=\{closeBusy\}/, "Fact 未知不得禁用紧急平仓");
assert.match(
  tradingPage,
  /pickNumber\(b\.item, \["ts"\], 0\) - pickNumber\(a\.item, \["ts"\], 0\)/,
  "最近因子信号必须按业务时间倒序，不能按进程内 tick 排序",
);
assert.match(tradingPage, /`ts:\$\{observedAt\}`/, "最近因子信号必须按业务时间稳定去重");
assert.doesNotMatch(
  tradingPage,
  /\.sort\(\(a, b\) => pickNumber\(b, \["tick"\]/,
  "tick 会在 backend 重启后归零，不能作为跨 generation 的排序键",
);
assert.match(pnlPage, /readFact\(seriesQuery\.data, "live\.realized-pnl\.v2"\)/);
assert.match(pnlPage, /const seriesRequestFailed = seriesQuery\.isError \|\| seriesQuery\.isRefetchError/);
assert.match(pnlPage, /\(\) => seriesDisplayable[\s\S]*?pickArray\(seriesQuery\.data/, "PnL unknown/error payload 不得作为事实值展示");
assert.match(pnlPage, /tone=\{factBoundTone\(seriesFact, numberTone\(realized\), seriesRequestFailed\)\}/);
assert.match(pnlPage, /seriesKnown \? "status-ok" : "status-warn"/, "收益 retained data 不得继续显示绿色行");
for (const endpoint of ["health", "loop", "account", "session", "db", "readiness"]) {
  assert.match(overviewPage, new RegExp(`const ${endpoint}RequestFailed = queries\\.${endpoint}\\.isError \\|\\| queries\\.${endpoint}\\.isRefetchError`));
  assert.match(overviewPage, new RegExp(`const ${endpoint}Known = factIsKnown\\(${endpoint}Fact, ${endpoint}RequestFailed\\)`));
}
assert.match(overviewPage, /readFactComponent\(queries\.risk\.data, "risk_inputs", "risk\.inputs\.v1"\)/);
assert.match(overviewPage, /系统数据流与智能体自治闭环/);
assert.match(overviewPage, /Demo 自动演化协调器/);
assert.match(overviewPage, /后台学习任务定时驱动/);
assert.match(overviewPage, /已登记 7 个智能体与权限/);
for (const flowLabel of [
  "报价 / K线 / 账户 / 持仓",
  "闭合K线 + 实时上下文",
  "方向 / 置信度 / 决策条件",
  "允许 / 拒绝 + 仓位",
  "回放 + 后验 + 反事实",
  "样本 / 记忆 / 质量评分",
  "候选 + 来源链路 + 证据",
  "审查结果 + 单次授权",
]) {
  assert.ok(overviewPage.includes(flowLabel), `系统图必须展示数据流：${flowLabel}`);
}
for (const obsoleteLabel of ["世界模型与 Critic", "刷新大脑", 'label="Critic"', "AWE 置信", "99% Shadow"]) {
  assert.ok(!presentationSources.includes(obsoleteLabel), `前端不得继续展示内部术语：${obsoleteLabel}`);
}
assert.doesNotMatch(overviewPage, /function TopologyNode|runtime-node-track/, "旧卡片式拓扑必须删除");
assert.match(overviewPage, /decodeCanonicalRiskSnapshot\(queries\.risk\.data\)/);
assert.match(overviewPage, /const positionsKnown = factIsKnown\(positionsFact, snapshotRequestFailed\)/);
assert.match(overviewPage, /const priceKnown = factIsKnown\(spotFact, snapshotRequestFailed\)/);
assert.doesNotMatch(overviewPage, /Fact\.state === "known"/, "Overview 绿灯不得绕过 request failure 边界");
assert.doesNotMatch(overviewPage, /tone=\{toneFromStatus\(dbStatus\)\}/, "DB retained data 不得绕过 dbKnown 变绿");
for (const endpoint of ["health", "db", "readiness", "alerts", "recovery", "sync", "token", "external"]) {
  assert.match(opsPage, new RegExp(`const ${endpoint}Known = factIsKnown\\(${endpoint}Fact`), `Ops ${endpoint} 绿灯必须绑定 endpoint Fact`);
}
assert.match(opsPage, /tone=\{factBoundTone\(readinessFact, toneFromStatus\(ctraderStatus\), readinessRequestFailed\)\}/);
assert.match(opsPage, /tone=\{factBoundTone\(dbFact, dbFresh, dbRequestFailed\)\}/);
assert.doesNotMatch(opsPage, /tone=\{loopRunning \? "ok" : "warn"\}/, "Ops 循环旧值不得假绿");
assert.doesNotMatch(opsPage, /tone=\{externalStale \? "warn" : "ok"\}/, "外部数据旧值不得假绿");
assert.match(opsPage, /readFact\(syncQuery\.data, "ops\.sync-status\.v2"\)/);
assert.match(opsPage, /readFact\(tokenQuery\.data, "ops\.ctrader-token-status\.v2"\)/);
assert.match(opsPage, /readFact\(externalQuery\.data, "ops\.external-data-status\.v2"\)/);
for (const endpoint of ["db", "readiness", "trace"]) {
  assert.match(riskPage, new RegExp(`const ${endpoint}Known = factIsKnown\\(${endpoint}Fact`), `Risk ${endpoint} 绿灯必须绑定 endpoint Fact`);
}
assert.match(riskPage, /const riskKnown = factIsKnown\(riskInputsFact, riskRequestFailed\)/);
assert.match(riskPage, /decodeCanonicalRiskSnapshot\(riskQuery\.data\)/);
assert.match(riskPage, /readFactComponent\(riskQuery\.data, "risk_inputs", "risk\.inputs\.v1"\)/);
assert.match(riskPage, /readFactComponent\(riskQuery\.data, "system_health", "system\.runtime-health\.v1"\)/);
assert.doesNotMatch(riskPage, /var_95|value_limit|var_limit|max_single_weight|max_sector_weight|stress_var/, "Risk 页面不得继续消费旧风险字段");
assert.match(riskDecoder, /schemaVersion === "risk_metrics_snapshot\.v2"/);
assert.match(riskDecoder, /components\.var_shadow_99/);
assert.doesNotMatch(riskDecoder, /root\.var|root\.kelly|root\.stress|root\.concentration/, "canonical decoder 不得回退到顶层兼容字段");
assert.match(riskPage, /tone=\{factBoundTone\(policyFact, blocked \? "warn" : "ok", policyRequestFailed\)\}/);
assert.match(riskPage, /tone=\{factBoundTone\(dbFact, dbErrorCount \? "bad" : toneFromStatus\(dbStatus\), dbRequestFailed\)\}/);
assert.doesNotMatch(riskPage, /dbQuery\.isError \|\| dbErrorCount \? "bad" : toneFromStatus\(dbStatus\)/, "DB retained payload 不得在 refetch error 后假绿");
assert.match(riskPage, /readFact\(tradeTracesQuery\.data, "risk\.trade-trace-recent\.v2"\)/);
assert.match(riskPage, /const policy = asRecord\(policyQuery\.data\)/, "策略裁决必须只消费专用端点");
assert.doesNotMatch(riskPage, /pickRecord\(risk, \["policy"\]\)/, "risk.summary 投影不得冒充 policy endpoint 数据");
assert.match(evidencePage, /type EvidenceTab = "replay" \| "incident" \| "release"/);
assert.doesNotMatch(evidencePage, /tone="ok"/, "运行证据页不得绕过端点 Fact 直接渲染绿色");
for (const [queryName, contract] of Object.entries({
  replay: "ops.replay-latest.v2",
  replayChoices: "ops.replay-bar-decisions.v2",
  incident: "ops.incident-control.v2",
  playbook: "ops.incident-playbook-latest.v2",
  enforcement: "ops.autonomy-scope-enforcement-latest.v2",
  release: "ops.release-latest.v2",
  releaseApprovals: "ops.release-approval-trail.v2",
})) {
  assert.match(
    evidencePage,
    new RegExp(`readFact\\(${queryName}Query\\.data, "${contract.replaceAll(".", "\\.")}"\\)`),
    `Evidence ${queryName} 必须消费自己的 endpoint fact`,
  );
}
assert.match(evidencePage, /readFact\(replayPreviewData, "ops\.replay-bar-preview\.v2"\)/);
assert.match(evidencePage, /const hasReplayPreview = Object\.keys\(previewReport\)\.length > 0/);
assert.match(evidencePage, /const replayDisplayFact = hasReplayPreview \? replayPreviewFact : replayFact/);
assert.match(evidencePage, /onSuccess: setReplayPreviewData/);
assert.doesNotMatch(evidencePage, /label="生成回放"/, "只读回放不应经过通用确认弹层");
assert.match(evidencePage, /function KlineWindowPreview/);
assert.match(evidencePage, /pick\(replayMetrics, \["bar_window_preview"\]\)/);
assert.match(evidencePage, /<KlineWindowPreview preview=\{barWindowPreview\}/);
assert.match(evidencePage, /pick\(replayMetrics, \["trade_outcome_learning_preview"\]\)/);
assert.match(evidencePage, /title="交易结果与学习"/);
assert.match(evidencePage, /实际盈亏/);
assert.match(evidencePage, /开仓方向/);
assert.match(evidencePage, /disabled=\{mode === "normal" && !incidentKnown\}/, "事故事实未知时不得解除风险收紧");
for (const tab of ["replay", "incident", "release"]) {
  assert.match(evidencePage, new RegExp(`enabled: activeTab === "${tab}"`), `${tab} 分区只能在打开时请求`);
}
assert.match(evidencePage, /invalidateQueries\(\{ queryKey: \["evidence"\] \}\)/);

console.log("web_frontend architecture test: ok");
