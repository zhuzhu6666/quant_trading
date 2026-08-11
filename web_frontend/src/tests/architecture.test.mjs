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
const surfaceCss = read("src/styles/surface.css");
const domainsCss = read("src/styles/domains.css");
const autonomyCss = read("src/styles/autonomy.css");
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
assert.match(app, /<Route path="\/risk" element=\{<Navigate to="\/trading" replace \/>\}/);
assert.match(workspaces, /if \(section === "risk"\) return <Navigate to="\/trading" replace \/>/);
assert.doesNotMatch(workspaces, /import .*RiskPage/);
assert.match(workspaces, /AutonomyFlowSummary/);
assert.match(workspaces, /运行事实 → 智能体分析 → 模型观察 → 学习形成候选 → 治理决定是否写回/);
assert.match(workspaces, /factStatusLabel\(readinessFact\)/);
assert.match(workspaces, /1\. 运行与裁决/);
assert.match(workspaces, /2\. 学习与候选/);
assert.match(workspaces, /3\. 模型与数据/);
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
assert.match(consoleCss, /\.field-row\s*\{[^}]*min-width:\s*0/, "字段行必须允许收缩");
assert.match(consoleCss, /\.trading-risk-dashboard \.trading-status-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/, "交易总览必须使用可收缩的两列布局");
assert.match(consoleCss, /\.risk-dashboard-embedded \.risk-control-grid\s*\{[^}]*grid-template-columns:\s*repeat\(2,\s*minmax\(0,\s*1fr\)\)/, "风控控制区必须使用可收缩的两列布局");
assert.match(consoleCss, /\.trading-risk-dashboard \.trading-compact-fields \.field-row > \.status-pill[\s\S]*?white-space:\s*normal/, "交易状态徽章必须允许换行");
assert.match(consoleCss, /\.risk-dashboard-embedded \.risk-section-head \.status-pill[\s\S]*?white-space:\s*normal/, "风控状态徽章必须允许换行");
assert.match(consoleCss, /@media \(max-width: 640px\)[\s\S]*?\.trading-risk-dashboard \.trading-status-grid[\s\S]*?grid-template-columns:\s*1fr/, "小屏必须退回单列");
assert.match(surfaceCss, /\.dashboard-grid \{\s*align-items:\s*start;/, "页面网格必须从内容高度开始对齐");
assert.match(surfaceCss, /\.dashboard-grid > \.panel \{\s*height:\s*auto;/, "页面卡片不得用强制等高制造空白");
assert.doesNotMatch(surfaceCss, /\.dashboard-grid > \.panel \{\s*height:\s*100%/, "页面卡片不得恢复强制等高");
assert.match(surfaceCss, /\.risk-control-overview \{ overflow: visible; \}/, "风险事实卡不得裁剪内容");
assert.match(domainsCss, /\.trading-inline-badges \{[\s\S]*?max-height:\s*none;[\s\S]*?overflow:\s*visible;/, "交易状态徽章不得被固定高度裁剪");
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
assert.match(learningPage, /学习结果：证据 → 候选 → 应用/);
assert.doesNotMatch(learningPage, /title="学习控制台"/, "学习控制台摘要必须并入学习结果主卡");
assert.match(learningPage, /learning-quality-disclosure/);
assert.match(learningPage, /className="learning-side-panel"/);
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
assert.match(modelsPage, /模型结论：观察、候选还是参与/);
assert.match(modelsPage, /历史回测与训练样本（按需运行，不改变线上权限）/);
assert.match(modelsPage, /展开模型准入、数据质量和权限边界/);
assert.match(modelsPage, /模型事件流：建议 → 观察 → 审计/);
assert.doesNotMatch(modelsPage, /models-dashboard \$\{modelFactsKnown \? "" : "fact-unverified"\}/, "单个模型接口不得把整页正常卡片染黄");
assert.match(modelsPage, /部分数据更新中/);
assert.match(modelsPage, /部分数据待确认/);
for (const queryName of ["factorAdvisories", "highLoadAudits"]) {
  assert.match(modelsPage, new RegExp(`${queryName}Query,`), `Models ${queryName} 也必须参与事实与错误边界`);
}
for (const queryName of ["position-quality/audits", "open-quality/audits", "canary-reviews", "inference-audits", "dataset-quality"]) {
  assert.match(modelsPage, new RegExp(`label: "${queryName}"`), `Models ${queryName} 错误必须可见`);
}
for (const contract of [
  "learning.dataset-readiness.v2",
  "learning.model-position-quality-audits.v2",
  "learning.model-open-quality-audits.v2",
  "learning.factor-governance-lightgbm-audits.v2",
  "learning.factor-governance-lightgbm-advisories.v2",
  "learning.model-shadow-queue.v2",
  "learning.model-canary-reviews.v2",
  "learning.model-inference-audits.v2",
  "learning.model-permission-audits.v2",
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
assert.match(v16Page, /v16-overview-disclosure/);
assert.doesNotMatch(v16Page, /title="决策信息完整度"/, "上下文覆盖不得在链路总览重复平铺");
assert.match(v16Page, /v16-inline-disclosure[\s\S]*CoveragePanel/, "上下文覆盖应归入待审建议分区并按需展开");
assert.match(v16Page, /运行日志：输入 → 裁决 → 风控 → 执行 → 反馈/);
assert.match(v16Page, /<RuntimeLog rows=\{runtimeRows\}/, "运行分区必须用统一日志行表达治理链");
assert.match(v16Page, /activeTab === "proposals"[\s\S]*GovernancePipeline/, "待治理分区必须把提案、候选和审查合并成一条链");
assert.doesNotMatch(v16Page, /title="治理候选"|title="候选审查"|ProposalRegistryList|GovernanceList|CandidateReviewList/, "待治理分区不得恢复三张重复列表卡");
assert.match(v16Page, /依据与反馈：当前结论 → 证据缺口 → 后验/, "系统态势与证据应归入依据与反馈分区");
assert.match(v16Page, /<RuntimeLog rows=\{evidenceRows\}/, "依据分区必须用反馈日志承载计划、评价和执行结果");
assert.match(v16Page, /展开记忆、计划、评价与执行明细/, "依据明细应按需展开而不是重复平铺");
assert.match(v16Page, /执行边界：能否执行 \/ 当前护栏/, "自治和护栏应合并为一个执行边界结论");
assert.doesNotMatch(v16Views, /v16-blueprint-summary/, "治理链路不得重复渲染顶部指标摘要");
assert.match(autonomyCss, /\.v16-runtime-row[\s\S]*grid-template-columns:/, "自治运行日志必须有稳定的宽屏列约束");
assert.match(autonomyCss, /\.v16-pipeline-row[\s\S]*grid-template-columns:/, "待治理链路必须有稳定的宽屏列约束");
assert.match(autonomyCss, /\.v16-detail-section \.brain-action-plan-compact\s*\{[\s\S]*grid-template-columns:\s*minmax\(0,\s*1fr\)/, "窄明细列中的计划、评价和执行卡必须服从父容器宽度");
assert.match(autonomyCss, /\.v16-detail-section \.brain-hypothesis-head \.status-pill\s*\{[\s\S]*max-width:\s*44%/, "窄明细列中的状态徽章不得撑破卡片");
assert.match(v16Page, /<MetricCard title="执行边界：能否执行 \/ 当前护栏"[\s\S]*?<FactBoundary fact=\{liveAutonomyView\.fact\}/, "执行边界控制必须位于 Fact 边界内");
assert.match(v16Page, /proposalRegistryKnown \? "" : "fact-unverified"/);
assert.match(v16Page, /liveReadyGuardrailKnown \? "" : "fact-unverified"/);
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
assert.match(consoleCss, /\.status-stale/);
assert.match(dashboardBits, /tone === "pending" \? "数据待确认"/);
assert.match(dashboardBits, /tone === "stale" \? "数据已过期"/);
assert.match(queryErrorList, /query\.isError \|\| query\.isRefetchError/);
assert.match(queryErrorList, /query\.isRefetchError \? "刷新失败，当前显示缓存数据" : "请求失败"/);
assert.match(
  riskPage,
  /extractTraceToken\(rawSummary, "largest_contribution_factor"\)/,
  "交易证据链必须识别当前最大贡献因子摘要字段",
);
assert.match(
  riskPage,
  /pickString\(item, \["largest_contribution_factor", "primary_factor"\]/,
  "交易证据链必须优先读取结构化因子字段并兼容旧字段",
);
assert.match(riskPage, /最大贡献 \{translateReasonText\(largestContributionFactor\)\}/);
assert.doesNotMatch(riskPage, /主因 \{primaryFactor\}/, "交易证据链不得继续显示已废弃的空主因字段");
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
assert.match(
  tradingPage,
  /<RiskPanel[\s\S]*?riskData=\{riskQuery\.data\}[\s\S]*?riskRequestFailed=\{riskRequestFailed\}[\s\S]*?embedded[\s\S]*?factorSignals=\{factorTicks\}[\s\S]*?positionsContent=/,
  "交易页必须复用同一份风险事实并把因子信号和仓位数据交给开仓决策链",
);
assert.match(tradingPage, /factorSignalsPending=\{recentTicksQuery\.isPending && !recentTicksQuery\.data\}/);
assert.match(tradingPage, /currentPositionIds=\{positions\.map\(\(item\) => item\.id\)\}/);
assert.match(tradingPage, /className="risk-audit-card risk-position-card"/);
assert.match(tradingPage, /tone=\{factBoundTone\(strategyFact, gatePassed \? "ok" : "warn", strategyRequestFailed\)\}/);
assert.doesNotMatch(tradingPage, /tone=\{loopRunning \? "ok" : "warn"\}/, "循环旧值不得绕过 loop Fact 变绿");
assert.doesNotMatch(tradingPage, /tone=\{gatePassed \? "ok" : "warn"\}/, "策略旧值不得绕过 strategy Fact 变绿");
assert.match(tradingPage, /label=\{loopStopping \? "停止中" : "停止"\}[\s\S]*?disabled=\{stopBusy \|\| stopRequested \|\| loopDraining\}/, "停止只应在请求中或确认 draining 时禁用");
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
assert.match(riskPage, /export function RiskPanel/);
assert.match(riskPage, /factorSignals\?: unknown\[\]/);
assert.match(riskPage, /currentPositionIds\?: readonly string\[\]/);
assert.match(riskPage, /positionsContent\?: ReactNode/);
assert.match(riskPage, /\{positionsContent\}/);
assert.match(riskPage, /risk-audit-columns/);
assert.match(riskPage, /title="开仓决策链"/);
assert.match(riskPage, /executionChainRows/);
assert.match(riskPage, /prePolicySkips/, "决策链必须读取策略前置拦截事实");
assert.match(riskPage, /admissionOwner/, "决策链必须显示前置拦截责任方");
assert.match(riskPage, /风控裁决未调用/);
assert.match(riskPage, /execution-chain-log/);
assert.match(riskPage, /execution-chain-line/);
assert.match(riskPage, /executionChainOutcome/);
assert.match(riskPage, /Math\.round\(timestamp \/ 1000\)/, "因子与策略必须按同一秒级业务时间合并");
assert.match(riskPage, /已开仓 · 持仓确认/);
assert.match(riskPage, /未开仓 · 执行拦截/);
assert.match(riskPage, /未开仓 · 策略拦截/);
assert.match(riskPage, /未进入策略裁决/);
assert.doesNotMatch(riskPage, /无对应裁决/, "未进入策略阶段不得显示含糊的无对应裁决");
assert.doesNotMatch(riskPage, /execution-chain-reasons/, "原因必须并入同一条结果日志");
assert.match(riskPage, /策略接口异常/);
assert.doesNotMatch(riskPage, /等待策略裁决/, "没有对应策略记录不得伪装成等待状态");
assert.doesNotMatch(riskPage, /等待执行结果/, "没有执行事实不得用等待状态掩盖数据语义");
assert.doesNotMatch(riskPage, /factorSignalContent/, "因子信号不得再作为独立 React 卡片插入");
assert.match(riskPage, /tradeTraces\.slice\(0, 6\)/, "交易证据链首页只展示最近 6 条摘要");
assert.match(consoleCss, /execution-chain-card/);
assert.match(consoleCss, /execution-chain-log/);
assert.match(consoleCss, /execution-chain-path/);
assert.doesNotMatch(consoleCss, /execution-chain-flow|execution-chain-step|execution-chain-arrow/, "决策链不得恢复三块大步骤卡片");
assert.doesNotMatch(consoleCss, /risk-policy-factor|risk-factor-signal/, "旧的并排因子/策略样式不得与决策链并存");
assert.match(consoleCss, /risk-position-card/);
assert.match(consoleCss, /risk-audit-columns/);
assert.match(consoleCss, /risk-trace-column/);
assert.match(consoleCss, /risk-trace-card/);
assert.match(riskPage, /decodeCanonicalRiskSnapshot\(riskData\)/);
assert.match(riskPage, /readFactComponent\(riskData, "risk_inputs", "risk\.inputs\.v1"\)/);
assert.match(riskPage, /readFactComponent\(riskData, "system_health", "system\.runtime-health\.v1"\)/);
assert.match(riskPage, /const liveExecutionReadyReported = pickBoolean\(readiness, \["ready_for_live_execution"\], false\)/);
assert.match(riskPage, /const readinessRiskDisplay = readinessRiskKnown/);
assert.match(riskPage, /label="交易执行就绪"/);
assert.match(riskPage, /label="数据库过期"/);
assert.match(riskPage, /label="数据库缺失"/);
assert.match(riskPage, /label="数据库错误"/);
assert.doesNotMatch(riskPage, /label="数据库异常"/);
assert.doesNotMatch(riskPage, /var_95|value_limit|var_limit|max_single_weight|max_sector_weight|stress_var/, "Risk 页面不得继续消费旧风险字段");
assert.match(riskDecoder, /schemaVersion === "risk_metrics_snapshot\.v2"/);
assert.match(riskDecoder, /components\.var_shadow_99/);
assert.doesNotMatch(riskDecoder, /root\.var|root\.kelly|root\.stress|root\.concentration/, "canonical decoder 不得回退到顶层兼容字段");
assert.match(riskPage, /tone=\{factBoundTone\(policyFact, blocked \? "warn" : "ok", policyRequestFailed\)\}/);
assert.match(riskPage, /const dbHealthTone = dbErrorCount \|\| dbMissingCount/);
assert.match(riskPage, /tone=\{factBoundTone\(dbFact, dbHealthTone, dbRequestFailed\)\}/);
assert.doesNotMatch(riskPage, /dbQuery\.isError \|\| dbErrorCount \? "bad" : toneFromStatus\(dbStatus\)/, "DB retained payload 不得在 refetch error 后假绿");
assert.match(riskPage, /readFact\(tradeTracesQuery\.data, "risk\.trade-trace-recent\.v2"\)/);
assert.match(riskPage, /const policy = asRecord\(policyQuery\.data\)/, "策略裁决必须只消费专用端点");
assert.doesNotMatch(riskPage, /pickRecord\(risk, \["policy"\]\)/, "risk.summary 投影不得冒充 policy endpoint 数据");
assert.match(riskPage, /const executionCounts = asRecord\(policy\.execution_counts\)/, "策略裁决必须区分政策许可与执行结果");
assert.match(riskPage, /const varDisplayable = riskDisplayable && knownMetric\(canonicalRisk\.var95\.status\)/, "过期风险快照仍可展示，但不能变成当前已知事实");
assert.match(riskPage, /!executionChainRows\.length && \(policyQuery\.isPending \|\| factorSignalsPending\)/, "决策链初次加载不得伪装成空记录");
assert.match(riskPage, /tradeTracesQuery\.isPending && !tradeTracesQuery\.data/, "交易证据链初次加载不得伪装成空记录");
assert.match(riskPage, /executionCategory === "applied"/, "策略裁决行必须使用后端执行状态");
assert.match(riskPage, /真实执行/);
assert.match(riskPage, /未执行/);
assert.match(tradingPage, /queryKeys\.riskPolicyVerdicts/);
assert.match(tradingPage, /queryKeys\.riskTradeTraces/);
assert.match(tradingPage, /queryKeys\.dbHealth/);
assert.match(tradingPage, /queryKeys\.readiness/);
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
